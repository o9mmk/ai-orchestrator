"""leaseでfenceされたrun state store。"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

from jsonschema import ValidationError

from orc.artifact_files import write_private
from orc.checkpoint import (
    QUARANTINE_HMAC_KEY_NAME,
    QUARANTINE_KEY_COMMITMENT_EVENT,
    CheckpointView,
    collect_artifact_digests,
    write_checkpoint,
)
from orc.dlp_models import DlpClearance, DlpResult
from orc.errors import DuplicateRunId, LeaseLost, TamperDetected
from orc.events import EventLog
from orc.integrity import load_or_replay, verify_integrity_snapshot
from orc.io_utils import atomic_write_json
from orc.lease import Lease, LeaseManager
from orc.paths import StatePaths, ensure_private_dir, validate_identifier
from orc.result_artifacts import ResultArtifactMixin
from orc.schemas import validate_budget, validate_checkpoint, validate_manifest
from orc.state_machine import RunState, RunStateMachine
from orc.store_artifacts import StoreArtifactMixin


class RunStateStore(ResultArtifactMixin, StoreArtifactMixin):
    """manifest/events/checkpointを1つのfencing境界で管理する。"""

    def __init__(
        self,
        repo_path: Path,
        run_id: str,
        lease_manager: LeaseManager,
        lease: Lease,
    ) -> None:
        self.repo_path = repo_path.resolve(strict=True)
        self.run_id = run_id
        self.lease_manager = lease_manager
        self.lease = lease
        self.paths = StatePaths.for_run(self.repo_path, run_id)
        self.run_dir = self.paths.run_dir
        self.manifest_path = self.run_dir / "manifest.json"
        self.events_path = self.run_dir / "events.jsonl"
        self.checkpoint_path = self.run_dir / "checkpoint.json"
        self.plan_path = self.run_dir / "plan.json"
        self.runtime_state = RunState.INIT.value
        self.halt_reason: str | None = None
        self.runtime_budget: dict[str, Any] | None = None
        self.runtime_tasks: dict[str, Any] | None = None
        self.events = EventLog(self.events_path, run_id)
        self._dlp_clearances: dict[str, tuple[str, str, str]] = {}

    @property
    def fencing_token(self) -> int:
        """このstore generationのfencing tokenを返す。"""
        return self.lease.fencing_token

    def maintain_lease(self, *, interval_seconds: float = 30.0) -> None:
        """Active Managerが60秒未満の間隔でleaseを更新できるようにする。"""
        if interval_seconds <= 0 or interval_seconds > 60:
            raise ValueError("lease renewal interval must be in (0, 60]")
        if self.lease_manager.clock() - self.lease.renewed_at < interval_seconds:
            try:
                self.lease_manager.assert_fencing(self.run_id, self.fencing_token)
            except LeaseLost:
                self.runtime_state = RunState.HALTED.value
                self.halt_reason = "lease_lost"
                raise
            return
        try:
            self.lease = self.lease_manager.renew(self.lease)
        except LeaseLost:
            self.runtime_state = RunState.HALTED.value
            self.halt_reason = "lease_lost"
            raise

    def initialize(self, manifest: dict[str, Any]) -> None:
        """schema済みmanifest、run_created event、初期checkpointを作る。"""
        with self._fenced():
            validate_manifest(manifest)
            if manifest["run_id"] != self.run_id:
                raise ValueError("manifest run_id does not match store")
            if manifest["state"] != RunState.INIT.value:
                raise ValueError("new run manifest must start in INIT")
            if manifest["fencing_token"] != self.fencing_token:
                raise LeaseLost("lease_lost: manifest fencing token mismatch")
            ensure_private_dir(self.paths.root)
            ensure_private_dir(self.run_dir.parent)
            try:
                self.run_dir.mkdir(mode=0o700, exist_ok=False)
            except FileExistsError as error:
                raise DuplicateRunId(f"duplicate_run_id: {self.run_id}") from error
            key_commitment = self._ensure_quarantine_integrity_key()
            atomic_write_json(self.manifest_path, manifest)
            self.events.append("run_created", "manager", {"state": manifest["state"]})
            self.events.append(
                QUARANTINE_KEY_COMMITMENT_EVENT,
                "manager",
                {"algorithm": "sha256", "commitment": key_commitment},
            )
            self.runtime_state = manifest["state"]
            self._write_checkpoint(run_state=self.runtime_state)

    def append_event(
        self,
        event_type: str,
        actor: str,
        data: dict[str, Any],
        *,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        """fencing一致時だけhash chainへ追記する。"""
        with self._fenced():
            return self.events.append(event_type, actor, data, task_id=task_id)

    def transition(self, event: str, *, reason: str | None = None) -> RunState:
        """run状態機械を適用し、event/manifest/checkpointを同期する。"""
        with self._fenced():
            manifest = self._read_manifest()
            current = RunState(manifest["state"])
            target = RunStateMachine(current).transition(event)
            self.events.append(
                "state_transition",
                "manager",
                {"from": current.value, "to": target.value, "reason": reason or event},
            )
            manifest["state"] = target.value
            atomic_write_json(self.manifest_path, manifest)
            self.runtime_state = target.value
            self._write_checkpoint(run_state=target.value)
            return target

    def write_checkpoint(
        self,
        *,
        run_state: str,
        tasks: dict[str, Any] | None = None,
        budget: dict[str, Any] | None = None,
    ) -> CheckpointView:
        """最新event headと全artifact digestをatomic checkpointへ固定する。"""
        with self._fenced():
            return self._write_checkpoint(run_state=run_state, tasks=tasks, budget=budget)

    def record_budget(self, budget: dict[str, Any]) -> CheckpointView:
        """単調更新済みbudgetをeventとcheckpointへ同一fencing境界で保存する。"""
        validate_budget(budget)
        if budget["invocations"] != budget["child_invocations"]:
            raise ValueError("budget invocations alias must match child_invocations")
        with self._fenced():
            previous = self._budget_snapshot(self._read_manifest())
            for field in (
                "tokens_used",
                "child_invocations",
                "manager_calls",
                "active_seconds",
                "calendar_seconds",
            ):
                if budget[field] < previous[field]:
                    raise ValueError(f"budget counter must be monotonic: {field}")
            self.runtime_budget = budget.copy()
            self.events.append("budget_updated", "manager", budget.copy())
            manifest = self._read_manifest()
            return self._write_checkpoint(run_state=manifest["state"], budget=budget)

    def read_budget(self) -> dict[str, Any]:
        """現在の永続budget snapshotをfencing一致時だけ返す。"""
        with self._fenced():
            return self._budget_snapshot(self._read_manifest()).copy()

    def _write_checkpoint(
        self,
        *,
        run_state: str,
        tasks: dict[str, Any] | None = None,
        budget: dict[str, Any] | None = None,
    ) -> CheckpointView:
        manifest = self._read_manifest()
        events = self.events.verify()
        selected_budget = budget.copy() if budget is not None else self._budget_snapshot(manifest)
        validate_budget(selected_budget)
        self.runtime_budget = selected_budget.copy()
        selected_tasks = tasks.copy() if tasks is not None else self._task_snapshot()
        self.runtime_tasks = selected_tasks.copy()
        data = {
            "seq": len(events),
            "events_head_hash": events[-1]["hash"] if events else "0" * 64,
            "run_state": run_state,
            "tasks": selected_tasks,
            "budget": selected_budget,
            "base_commit": manifest["base_commit"],
            "artifact_digests": collect_artifact_digests(self.run_dir),
            "fencing_token": self.fencing_token,
        }
        return write_checkpoint(self.checkpoint_path, data)

    def record_task_state(self, task_id: str, data: dict[str, Any]) -> CheckpointView:
        """Persist one Manager-owned task state without dropping sibling snapshots."""
        safe_task = validate_identifier(task_id, label="task_id")
        _validate_task_state(data)
        with self._fenced():
            tasks = self._task_snapshot()
            tasks[safe_task] = data.copy()
            self.runtime_tasks = tasks.copy()
            self.events.append("task_updated", "manager", data.copy(), task_id=safe_task)
            manifest = self._read_manifest()
            return self._write_checkpoint(run_state=manifest["state"], tasks=tasks)

    def write_task_patch(
        self,
        task_id: str,
        attempt: int,
        payload: bytes,
        *,
        clearance: DlpClearance,
    ) -> Path:
        """Publish one exact DLP-cleared Manager-generated patch for integration."""
        safe_task = validate_identifier(task_id, label="task_id")
        if attempt < 1:
            raise ValueError("attempt must be positive")
        digest = hashlib.sha256(payload).hexdigest()
        self.require_dlp_clearance(clearance, artifact_kind="patch", digest=digest)
        target = self.run_dir / "tasks" / safe_task / f"attempt-{attempt}" / "patch.diff"
        with self._fenced():
            if target.exists():
                raise FileExistsError("task patch is immutable")
            ensure_private_dir(target.parent)
            write_private(target, payload)
            self.events.append(
                "patch_recorded",
                "manager",
                {"attempt": attempt, "digest": digest, "size_bytes": len(payload)},
                task_id=safe_task,
            )
            manifest = self._read_manifest()
            self._write_checkpoint(run_state=manifest["state"])
        return target

    def write_summary(
        self,
        payload: bytes,
        *,
        clearance: DlpClearance,
    ) -> Path:
        """Atomically publish the exact DLP-cleared user summary at a stable path."""
        digest = hashlib.sha256(payload).hexdigest()
        self.require_dlp_clearance(clearance, artifact_kind="summary", digest=digest)
        target = self.run_dir / "summary.md"
        with self._fenced():
            write_private(target, payload)
            self.events.append(
                "summary_recorded",
                "manager",
                {"digest": digest, "size_bytes": len(payload)},
            )
            manifest = self._read_manifest()
            self._write_checkpoint(run_state=manifest["state"])
        return target

    def read_tasks(self) -> dict[str, Any]:
        """Return the current checkpointed task snapshot under fencing."""
        with self._fenced():
            return self._task_snapshot().copy()

    def _task_snapshot(self) -> dict[str, Any]:
        if self.runtime_tasks is not None:
            return {key: value.copy() for key, value in self.runtime_tasks.items()}
        if self.checkpoint_path.exists():
            try:
                checkpoint = json.loads(self.checkpoint_path.read_text(encoding="utf-8"))
                validate_checkpoint(checkpoint)
            except (json.JSONDecodeError, ValidationError) as error:
                raise TamperDetected("tamper_detected: invalid checkpoint tasks") from error
            tasks = checkpoint["tasks"]
            if not isinstance(tasks, dict):
                raise TamperDetected("tamper_detected: checkpoint tasks must be an object")
            for task_id, task_data in tasks.items():
                try:
                    validate_identifier(task_id, label="task_id")
                    _validate_task_state(task_data)
                except (TypeError, ValueError) as error:
                    raise TamperDetected("tamper_detected: invalid checkpoint task") from error
            self.runtime_tasks = {key: value.copy() for key, value in tasks.items()}
            return {key: value.copy() for key, value in tasks.items()}
        self.runtime_tasks = {}
        return {}

    def _budget_snapshot(self, manifest: dict[str, Any]) -> dict[str, Any]:
        if self.runtime_budget is not None:
            return self.runtime_budget.copy()
        if self.checkpoint_path.exists():
            try:
                checkpoint = json.loads(self.checkpoint_path.read_text(encoding="utf-8"))
                validate_checkpoint(checkpoint)
            except (json.JSONDecodeError, ValidationError) as error:
                raise TamperDetected("tamper_detected: invalid checkpoint budget") from error
            self.runtime_budget = cast(dict[str, Any], checkpoint["budget"]).copy()
            return self.runtime_budget.copy()
        self.runtime_budget = {
            "tokens_used": 0,
            "budget_source": manifest["budget_source"],
            "invocations": 0,
            "child_invocations": 0,
            "manager_calls": 0,
            "active_seconds": 0,
            "calendar_seconds": 0,
            "soft_reached": False,
            "hard_reached": False,
            "halt_reason": None,
        }
        return self.runtime_budget.copy()

    def verify_events(self) -> list[dict[str, Any]]:
        """events chainを検証して返す。"""
        return self.events.verify()

    def read_manifest(self) -> dict[str, Any]:
        """fencing一致時だけmanifest snapshotを返す。"""
        with self._fenced():
            return self._read_manifest().copy()

    def _issue_dlp_clearance(self, result: DlpResult) -> DlpClearance:
        """Register an opaque capability for one exact clean canonical payload."""
        if not result.clean or result.digest is None:
            raise ValueError("clean DLP result is required")
        capability_id = secrets.token_hex(32)
        record = (result.artifact_kind, result.artifact_id, result.digest)
        self._dlp_clearances[capability_id] = record
        return DlpClearance._issue(capability_id, *record)

    def require_dlp_clearance(
        self,
        clearance: DlpClearance,
        *,
        artifact_kind: str,
        digest: str,
    ) -> None:
        """Reject forged, cross-store, wrong-kind, or wrong-payload capabilities."""
        record = self._dlp_clearances.get(clearance.capability_id)
        expected = (clearance.artifact_kind, clearance.artifact_id, clearance.digest)
        if record != expected:
            raise ValueError("DLP clearance capability is not registered by this Manager")
        if clearance.artifact_kind != artifact_kind or clearance.digest != digest:
            raise ValueError("DLP clearance does not match Manager payload")

    def _ensure_quarantine_integrity_key(self) -> str:
        path = self.run_dir / QUARANTINE_HMAC_KEY_NAME
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as error:
            info = path.lstat()
            key = path.read_bytes()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600
                or len(key) != 32
            ):
                raise TamperDetected(
                    "tamper_detected: invalid quarantine integrity key"
                ) from error
            return hashlib.sha256(key).hexdigest()
        key = secrets.token_bytes(32)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(key)
            handle.flush()
            os.fsync(handle.fileno())
        return hashlib.sha256(key).hexdigest()

    def load_checkpoint_or_replay(self) -> CheckpointView:
        """checkpoint破損/遅延時は検証済みeventsから非破壊replayする。"""
        with self._fenced():
            events = self.events.verify()
            manifest = self._read_manifest(tamper=True)
            return load_or_replay(
                events,
                manifest,
                self.checkpoint_path,
                self.run_dir,
                self.fencing_token,
            )

    def verify_integrity(self) -> CheckpointView:
        """resume前提のchain/digest/base/fence照合をfail-loudに実行する。"""
        with self._fenced():
            events = self.events.verify()
            manifest = self._read_manifest(tamper=True)
            return verify_integrity_snapshot(
                events,
                manifest,
                self.checkpoint_path,
                self.run_dir,
                self.fencing_token,
            )

    def _read_manifest(self, *, tamper: bool = False) -> dict[str, Any]:
        try:
            data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            validate_manifest(data)
        except (FileNotFoundError, json.JSONDecodeError, ValidationError) as error:
            if tamper:
                raise TamperDetected("tamper_detected: invalid manifest") from error
            raise
        return cast(dict[str, Any], data)

    @contextmanager
    def _fenced(self) -> Iterator[None]:
        try:
            with self.lease_manager.fenced(self.run_id, self.fencing_token):
                yield
        except LeaseLost:
            self.runtime_state = RunState.HALTED.value
            self.halt_reason = "lease_lost"
            raise


def _validate_task_state(data: Any) -> None:
    if not isinstance(data, dict):
        raise TypeError("task state must be an object")
    state = data.get("state")
    allowed = {
        "QUEUED",
        "RUNNING",
        "VERIFYING",
        "REVIEWING",
        "FIXING",
        "DONE",
        "ESCALATED",
        "ABORTED",
    }
    if state not in allowed:
        raise ValueError("invalid task state")
    for field in ("attempt", "review_cycles"):
        value = data.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"invalid task counter: {field}")
