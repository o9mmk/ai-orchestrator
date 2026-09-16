"""Shared one-shot Codex process, timeout, and PGID accounting."""

from __future__ import annotations

import os
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Protocol, cast

from orc.cancel_intent import CancelIntent
from orc.child_request import validate_owned_worktree
from orc.dlp_matcher import DlpMatcher
from orc.dlp_models import StreamRedactionResult
from orc.errors import ChildExecutionError
from orc.process_control import (
    TerminationResult,
    process_group_alive,
    terminate_known_group,
    terminate_popen_group,
)
from orc.process_ledger import ProcessLedger
from orc.store import RunStateStore
from orc.stream_capture import ProcessStreamCapture
from orc.worktree import Worktree


@dataclass(frozen=True)
class StructuredProcessOutcome:
    """Bounded OS outcome; stdout/stderr and structured body are excluded."""

    status: str
    exit_code: int | None
    timed_out: bool
    cancelled: bool
    termination_signal: str | None
    forced_kill: bool
    pid: int
    pgid: int
    stdout_stream: StreamRedactionResult
    stderr_stream: StreamRedactionResult


class StructuredProcessAdapter(Protocol):
    """Capability-probed adapter contract shared by Codex and Claude reviewers."""

    def probe(self) -> Any: ...

    def spawn(
        self,
        *,
        worktree: Path,
        role: str,
        schema_path: Path,
        result_path: Path,
    ) -> subprocess.Popen[bytes]: ...


class StructuredProcessRunner:
    """Reuse M4 capability probe, timeout, and ledger for structured children."""

    def __init__(
        self,
        store: RunStateStore,
        adapter: StructuredProcessAdapter,
        *,
        term_grace_seconds: float = 5.0,
        poll_interval: float = 0.05,
        matcher: DlpMatcher | None = None,
        stream_overlap_chars: int = 64 * 1024,
        stream_output_limit_bytes: int = 4 * 1024 * 1024,
    ) -> None:
        self.store = store
        self.adapter = adapter
        self.ledger = ProcessLedger(
            store,
            term_grace_seconds=term_grace_seconds,
            poll_interval=poll_interval,
        )
        self.term_grace_seconds = term_grace_seconds
        self.poll_interval = poll_interval
        self.matcher = matcher or DlpMatcher()
        self.stream_overlap_chars = stream_overlap_chars
        self.stream_output_limit_bytes = stream_output_limit_bytes
        self._startup_complete = False

    def run(
        self,
        worktree: Worktree,
        *,
        task_id: str,
        role: str,
        attempt: int,
        prompt: str,
        schema_path: Path,
        result_path: Path,
        stdout_path: Path,
        stderr_path: Path,
        staging_dir_fd: int,
        timeout: float,
    ) -> StructuredProcessOutcome:
        """Run one owned process group and persist every terminal boundary."""
        root = validate_owned_worktree(self.store, worktree, task_id=task_id)
        if attempt < 1 or not prompt or timeout <= 0:
            raise ChildExecutionError("attempt, prompt, and timeout must be positive")
        for artifact in (schema_path, result_path, stdout_path, stderr_path):
            if not artifact.resolve(strict=False).is_relative_to(root):
                raise ChildExecutionError("structured process artifact escaped worktree")
        self._startup()
        process: subprocess.Popen[bytes] | None = None
        pgid: int | None = None
        capture: ProcessStreamCapture | None = None
        streams = None
        capture_attempted = False
        registered = False
        finalized = False
        try:
            process = self.adapter.spawn(
                worktree=root,
                role=role,
                schema_path=schema_path,
                result_path=result_path,
            )
            if process.stdout is None or process.stderr is None:
                raise ChildExecutionError("child pipes were not created")
            capture = ProcessStreamCapture(
                cast(BinaryIO, process.stdout),
                cast(BinaryIO, process.stderr),
                stdout_path,
                stderr_path,
                output_dir_fd=staging_dir_fd,
                matcher=self.matcher,
                overlap_chars=self.stream_overlap_chars,
                output_limit_bytes=self.stream_output_limit_bytes,
            )
            capture.start()
            pgid = os.getpgid(process.pid)
            try:
                self.ledger.register(
                    task_id=task_id,
                    attempt=attempt,
                    pid=process.pid,
                    pgid=pgid,
                    worktree=root,
                )
                registered = True
            except Exception:
                terminate_popen_group(
                    process,
                    grace_seconds=self.term_grace_seconds,
                    poll_interval=self.poll_interval,
                )
                capture_attempted = True
                capture.finish()
                raise
            termination, cancelled = self._wait(process, prompt, timeout)
            if termination is None and process_group_alive(pgid):
                terminate_known_group(
                    process,
                    pgid,
                    grace_seconds=self.term_grace_seconds,
                    poll_interval=self.poll_interval,
                )
            capture_attempted = True
            streams = capture.finish()
            if termination is not None:
                prefix = "CANCEL" if cancelled else "TIMED_OUT"
                status = f"{prefix}_{'KILL' if termination.forced else 'TERM'}"
                self.ledger.complete(
                    pid=process.pid,
                    pgid=pgid,
                    status=status,
                    exit_code=termination.exit_code,
                    timed_out=not cancelled,
                    termination_signal=termination.signal,
                )
                finalized = True
                return StructuredProcessOutcome(
                    "CANCELLED" if cancelled else "TIMED_OUT",
                    termination.exit_code,
                    not cancelled,
                    cancelled,
                    termination.signal,
                    termination.forced,
                    process.pid,
                    pgid,
                    streams.stdout,
                    streams.stderr,
                )
            self.ledger.complete(
                pid=process.pid,
                pgid=pgid,
                status="EXITED",
                exit_code=process.returncode,
                timed_out=False,
                termination_signal=None,
            )
            finalized = True
            return StructuredProcessOutcome(
                "EXITED",
                process.returncode,
                False,
                False,
                None,
                False,
                process.pid,
                pgid,
                streams.stdout,
                streams.stderr,
            )
        except BaseException as primary:
            termination = None
            try:
                if process is not None:
                    if pgid is not None and (
                        process.poll() is None or process_group_alive(pgid)
                    ):
                        termination = terminate_known_group(
                            process,
                            pgid,
                            grace_seconds=self.term_grace_seconds,
                            poll_interval=self.poll_interval,
                        )
                    elif process.poll() is None:
                        termination = terminate_popen_group(
                            process,
                            grace_seconds=self.term_grace_seconds,
                            poll_interval=self.poll_interval,
                        )
            except Exception as termination_error:
                primary.add_note(
                    f"process termination failed: {type(termination_error).__name__}"
                )
            capture_cleanup_error: Exception | None = None
            if capture is not None and streams is None and not capture_attempted:
                capture_attempted = True
                try:
                    capture.finish()
                except Exception as error:
                    capture_cleanup_error = error
            if registered and not finalized and process is not None and pgid is not None:
                status = "ABORTED"
                if termination is not None:
                    status = "ABORTED_KILL" if termination.forced else "ABORTED_TERM"
                try:
                    self.ledger.complete(
                        pid=process.pid,
                        pgid=pgid,
                        status=status,
                        exit_code=(termination.exit_code if termination else process.returncode),
                        timed_out=False,
                        termination_signal=(termination.signal if termination else None),
                    )
                except Exception as ledger_error:
                    primary.add_note(
                        f"ledger finalization failed: {type(ledger_error).__name__}"
                    )
            if capture_cleanup_error is not None:
                primary.add_note(
                    f"stream capture cleanup failed: {type(capture_cleanup_error).__name__}"
                )
            raise

    def _startup(self) -> None:
        if self._startup_complete:
            return
        self.ledger.recover_orphans()
        self.adapter.probe()
        self._startup_complete = True

    def _wait(
        self,
        process: subprocess.Popen[bytes],
        prompt: str,
        timeout: float,
    ) -> tuple[TerminationResult | None, bool]:
        if process.stdin is None:
            raise ChildExecutionError("child stdin pipe was not created")
        stdin = process.stdin
        write_failed = threading.Event()

        def write_prompt() -> None:
            try:
                stdin.write(prompt.encode("utf-8"))
                stdin.flush()
            except BrokenPipeError:
                return
            except OSError:
                write_failed.set()
            finally:
                try:
                    stdin.close()
                except BrokenPipeError:
                    pass
                except OSError:
                    write_failed.set()

        writer = threading.Thread(target=write_prompt, daemon=True)
        writer.start()
        termination = None
        cancelled = False
        deadline = time.monotonic() + timeout
        intent = CancelIntent(self.store.repo_path, self.store.run_id)
        while process.poll() is None:
            self.store.maintain_lease()
            if intent.pending(self.store.fencing_token):
                cancelled = True
                termination = terminate_popen_group(
                    process,
                    grace_seconds=self.term_grace_seconds,
                    poll_interval=self.poll_interval,
                )
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                termination = terminate_popen_group(
                    process,
                    grace_seconds=self.term_grace_seconds,
                    poll_interval=self.poll_interval,
                )
                break
            try:
                process.wait(timeout=min(self.poll_interval, remaining))
            except subprocess.TimeoutExpired:
                continue
        writer.join(timeout=2)
        if writer.is_alive() or write_failed.is_set():
            raise ChildExecutionError("CHILD_STDIN_WRITE_FAILED")
        return termination, cancelled
