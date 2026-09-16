"""Fail-loud worktrees.json PID/PGID ledger and orphan recovery."""

from __future__ import annotations

import json
from contextlib import AbstractContextManager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast

from jsonschema import ValidationError, validate

from orc.errors import ChildExecutionError, ProcessLedgerError
from orc.io_utils import atomic_write_json
from orc.process_control import terminate_orphan_group, validate_process_identity
from orc.process_schema import (
    LEDGER_SCHEMA,
    STATUS_VALUES,
    RecoveryResult,
    TerminationEvidence,
)


class LedgerStore(Protocol):
    """Store operations required for one fenced ledger transaction."""

    run_dir: Path
    paths: Any
    events: Any

    def _fenced(self) -> AbstractContextManager[None]: ...

    def _read_manifest(self, *, tamper: bool = False) -> dict[str, Any]: ...

    def _write_checkpoint(
        self,
        *,
        run_state: str,
        tasks: dict[str, Any] | None = None,
        budget: dict[str, Any] | None = None,
    ) -> Any: ...


class ProcessLedger:
    """Persist child identity and recover only validated live process groups."""

    def __init__(
        self,
        store: LedgerStore,
        *,
        term_grace_seconds: float = 5.0,
        poll_interval: float = 0.05,
    ) -> None:
        if term_grace_seconds < 0 or poll_interval <= 0:
            raise ValueError("process termination intervals must be positive")
        self.store = store
        self.path = store.run_dir / "worktrees.json"
        self.worktree_root = store.paths.worktree_root.resolve(strict=False)
        self.term_grace_seconds = term_grace_seconds
        self.poll_interval = poll_interval

    def register(
        self,
        *,
        task_id: str,
        attempt: int,
        pid: int,
        pgid: int,
        worktree: Path,
    ) -> None:
        """Append one RUNNING process after validating its owned worktree."""
        self._validate_worktree(task_id, worktree)
        record = {
            "task_id": task_id,
            "attempt": attempt,
            "pid": pid,
            "pgid": pgid,
            "started_at": datetime.now(UTC).isoformat(),
            "worktree": str(worktree.resolve(strict=True)),
            "status": "RUNNING",
        }
        with self.store._fenced():
            data = self._load()
            if any(
                item["status"] == "RUNNING"
                and item["task_id"] == task_id
                and item["attempt"] == attempt
                for item in data["processes"]
            ):
                raise ProcessLedgerError("duplicate RUNNING process ledger entry")
            data["processes"].append(record)
            self._commit(data, "child_started", record, task_id=task_id)

    def complete(
        self,
        *,
        pid: int,
        pgid: int,
        status: str,
        exit_code: int | None,
        timed_out: bool,
        termination_signal: str | None,
    ) -> None:
        """Finalize exactly one matching RUNNING record."""
        if status not in STATUS_VALUES or status == "RUNNING":
            raise ValueError(f"invalid terminal process status: {status}")
        with self.store._fenced():
            data = self._load()
            record = self._matching_running(data, pid, pgid)
            record.update(
                {
                    "status": status,
                    "exit_code": exit_code,
                    "timed_out": timed_out,
                    "signal": termination_signal,
                }
            )
            event_type = "child_timeout" if timed_out else "child_finished"
            self._commit(data, event_type, record, task_id=record["task_id"])

    def recover_orphans(self) -> tuple[RecoveryResult, ...]:
        """Recover validated RUNNING PGIDs and record one event per decision."""
        results: list[RecoveryResult] = []
        with self.store._fenced():
            data = self._load()
            for record in data["processes"]:
                if record["status"] != "RUNNING":
                    continue
                self._validate_record_worktree(record)
                try:
                    alive = validate_process_identity(record["pid"], record["pgid"])
                except ChildExecutionError as error:
                    raise ProcessLedgerError(str(error)) from error
                if not alive:
                    record.update(
                        {
                            "status": "EXITED_UNRECORDED",
                            "exit_code": None,
                            "timed_out": False,
                            "signal": None,
                        }
                    )
                    result = RecoveryResult(
                        record["task_id"],
                        record["attempt"],
                        record["pid"],
                        record["pgid"],
                        None,
                        record["status"],
                    )
                    self._commit(data, "orphan_absent", asdict(result), task_id=record["task_id"])
                    results.append(result)
                    continue
                termination = terminate_orphan_group(
                    record["pid"],
                    record["pgid"],
                    grace_seconds=self.term_grace_seconds,
                    poll_interval=self.poll_interval,
                )
                status = "RECOVERED_KILL" if termination.forced else "RECOVERED_TERM"
                record.update(
                    {"status": status, "exit_code": None, "timed_out": False, "signal": termination.signal}
                )
                result = RecoveryResult(
                    record["task_id"],
                    record["attempt"],
                    record["pid"],
                    record["pgid"],
                    termination.signal,
                    status,
                )
                self._commit(data, "orphan_recovered", asdict(result), task_id=record["task_id"])
                results.append(result)
        return tuple(results)

    def terminate_running(self, *, reason: str) -> tuple[TerminationEvidence, ...]:
        """Terminate every safely validated RUNNING group for a hard halt."""
        return self._terminate_running(
            reason=reason,
            status_prefix="BUDGET",
            event_type="budget_child_terminated",
        )

    def cancel_running(self) -> tuple[TerminationEvidence, ...]:
        """Terminate every safely validated RUNNING group for human cancel."""
        return self._terminate_running(
            reason="cancel_requested",
            status_prefix="CANCEL",
            event_type="cancel_child_terminated",
        )

    def _terminate_running(
        self,
        *,
        reason: str,
        status_prefix: str,
        event_type: str,
    ) -> tuple[TerminationEvidence, ...]:
        """Shared identity-checked all-group termination transaction."""
        results: list[TerminationEvidence] = []
        with self.store._fenced():
            data = self._load()
            for record in data["processes"]:
                if record["status"] != "RUNNING":
                    continue
                self._validate_record_worktree(record)
                try:
                    alive = validate_process_identity(record["pid"], record["pgid"])
                except ChildExecutionError as error:
                    raise ProcessLedgerError(str(error)) from error
                if alive:
                    termination = terminate_orphan_group(
                        record["pid"],
                        record["pgid"],
                        grace_seconds=self.term_grace_seconds,
                        poll_interval=self.poll_interval,
                    )
                    status = (
                        f"{status_prefix}_KILL" if termination.forced else f"{status_prefix}_TERM"
                    )
                    signal_name = termination.signal
                    exit_code = termination.exit_code
                else:
                    status = "EXITED_UNRECORDED"
                    signal_name = None
                    exit_code = None
                record.update(
                    {
                        "status": status,
                        "exit_code": exit_code,
                        "timed_out": False,
                        "signal": signal_name,
                    }
                )
                result = TerminationEvidence(
                    record["task_id"],
                    record["attempt"],
                    record["pid"],
                    record["pgid"],
                    signal_name,
                    status,
                    exit_code,
                )
                event_data = asdict(result)
                event_data["reason"] = reason
                self._commit(
                    data,
                    event_type,
                    event_data,
                    task_id=record["task_id"],
                )
                results.append(result)
        return tuple(results)

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"version": 1, "processes": []}
        except json.JSONDecodeError as error:
            raise ProcessLedgerError("invalid worktrees ledger JSON") from error
        try:
            validate(instance=data, schema=LEDGER_SCHEMA)
        except ValidationError as error:
            raise ProcessLedgerError("invalid worktrees ledger schema") from error
        return cast(dict[str, Any], data)

    def _commit(
        self,
        data: dict[str, Any],
        event_type: str,
        event_data: dict[str, Any],
        *,
        task_id: str,
    ) -> None:
        validate(instance=data, schema=LEDGER_SCHEMA)
        atomic_write_json(self.path, data)
        self.store.events.append(event_type, "manager", event_data, task_id=task_id)
        manifest = self.store._read_manifest()
        self.store._write_checkpoint(run_state=manifest["state"])

    @staticmethod
    def _matching_running(data: dict[str, Any], pid: int, pgid: int) -> dict[str, Any]:
        matches = [
            item
            for item in data["processes"]
            if item["status"] == "RUNNING" and item["pid"] == pid and item["pgid"] == pgid
        ]
        if len(matches) != 1:
            raise ProcessLedgerError("RUNNING PID/PGID ledger entry mismatch")
        return cast(dict[str, Any], matches[0])

    def _validate_record_worktree(self, record: dict[str, Any]) -> None:
        self._validate_worktree(record["task_id"], Path(record["worktree"]))

    def _validate_worktree(self, task_id: str, worktree: Path) -> None:
        if worktree.is_symlink():
            raise ProcessLedgerError("ledger worktree root must not be a symlink")
        try:
            resolved = worktree.resolve(strict=True)
        except FileNotFoundError as error:
            raise ProcessLedgerError("ledger worktree does not exist") from error
        expected = (self.worktree_root / task_id).resolve(strict=False)
        if resolved != expected or not resolved.is_relative_to(self.worktree_root):
            raise ProcessLedgerError("ledger worktree does not match owned task path")
