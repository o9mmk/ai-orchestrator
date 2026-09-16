"""Concurrent process pipe draining through streaming redaction."""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from orc.dlp_matcher import DlpMatcher
from orc.dlp_models import StreamRedactionResult
from orc.errors import ChildExecutionError
from orc.stream_redaction import StreamingRedactor


@dataclass(frozen=True)
class CapturedStreams:
    """Safe metadata for both child output channels."""

    stdout: StreamRedactionResult
    stderr: StreamRedactionResult


class ProcessStreamCapture:
    """Drain stdout and stderr in parallel without retaining raw bytes."""

    def __init__(
        self,
        stdout: BinaryIO,
        stderr: BinaryIO,
        stdout_path: Path,
        stderr_path: Path,
        *,
        output_dir_fd: int,
        matcher: DlpMatcher,
        overlap_chars: int,
        output_limit_bytes: int,
    ) -> None:
        self._channels = (
            ("stdout", stdout, stdout_path),
            ("stderr", stderr, stderr_path),
        )
        self._matcher = matcher
        self._output_dir_fd = output_dir_fd
        self._overlap_chars = overlap_chars
        self._output_limit_bytes = output_limit_bytes
        self._results: dict[str, StreamRedactionResult] = {}
        self._failed = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        """Start both drainers before the child can fill either pipe."""
        if self._threads:
            raise ValueError("process stream capture already started")
        for name, pipe, path in self._channels:
            thread = threading.Thread(
                target=self._drain,
                args=(name, pipe, path),
                daemon=True,
            )
            self._threads.append(thread)
            thread.start()

    def finish(self) -> CapturedStreams:
        """Join both EOF drainers and expose only structured redaction state."""
        for thread in self._threads:
            thread.join(timeout=5)
        if any(thread.is_alive() for thread in self._threads) or self._failed.is_set():
            raise ChildExecutionError("STREAM_CAPTURE_FAILED")
        if set(self._results) != {"stdout", "stderr"}:
            raise ChildExecutionError("STREAM_CAPTURE_INCOMPLETE")
        return CapturedStreams(self._results["stdout"], self._results["stderr"])

    def _drain(self, name: str, pipe: BinaryIO, path: Path) -> None:
        redactor = StreamingRedactor(
            self._matcher,
            overlap_chars=self._overlap_chars,
            output_limit_bytes=self._output_limit_bytes,
        )
        try:
            descriptor = os.open(
                path.name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=self._output_dir_fd,
            )
            with os.fdopen(descriptor, "wb", closefd=True) as output:
                while True:
                    chunk = pipe.read(64 * 1024)
                    if not chunk:
                        break
                    safe = redactor.feed(chunk)
                    if safe:
                        output.write(safe)
                final = redactor.finish()
                if final:
                    output.write(final)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(path.name, 0o600, dir_fd=self._output_dir_fd, follow_symlinks=False)
            self._results[name] = redactor.result()
        except (OSError, ValueError):
            self._failed.set()
        finally:
            pipe.close()
