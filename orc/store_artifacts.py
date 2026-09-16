"""RunStateStoreのplan/baseline artifact責務。"""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Protocol

from jsonschema import ValidationError

from orc.dlp_models import DlpClearance
from orc.errors import TamperDetected
from orc.events import EventLog
from orc.io_utils import atomic_write_json, canonical_json
from orc.paths import validate_identifier
from orc.plan_schema import validate_plan
from orc.review_schema import validate_review
from orc.schemas import validate_manifest
from orc.state_machine import RunState
from orc.verify_schema import validate_verify_report


class ArtifactStoreHost(Protocol):
    """mixinが必要とするfenced store interface。"""

    run_dir: Path
    plan_path: Path
    events: EventLog

    def _fenced(self) -> AbstractContextManager[None]: ...

    def _read_manifest(self, *, tamper: bool = False) -> dict[str, Any]: ...

    def require_dlp_clearance(
        self,
        clearance: DlpClearance,
        *,
        artifact_kind: str,
        digest: str,
    ) -> None: ...

    def _write_checkpoint(
        self,
        *,
        run_state: str,
        tasks: dict[str, Any] | None = None,
        budget: dict[str, Any] | None = None,
    ) -> Any: ...


class StoreArtifactMixin:
    """plan固定とbaseline cacheをfencing付きで永続化する。"""

    def record_plan(
        self: ArtifactStoreHost,
        plan: dict[str, Any],
        final_size: str,
        *,
        clearance: DlpClearance,
    ) -> None:
        """schema済みplanとManager確定sizeを固定する。"""
        with self._fenced():
            validate_plan(plan)
            self.require_dlp_clearance(
                clearance,
                artifact_kind="plan",
                digest=hashlib.sha256(canonical_json(plan)).hexdigest(),
            )
            if plan["final_size"] != final_size:
                raise ValueError("plan final_size does not match decision")
            manifest = self._read_manifest()
            if manifest["state"] != RunState.PLANNING.value:
                raise ValueError("plan can only be recorded while PLANNING")
            if self.plan_path.exists():
                raise FileExistsError("recorded plan is immutable")
            atomic_write_json(self.plan_path, plan)
            manifest["size"] = final_size
            validate_manifest(manifest)
            atomic_write_json(self.run_dir / "manifest.json", manifest)
            digest = hashlib.sha256(self.plan_path.read_bytes()).hexdigest()
            self.events.append(
                "plan_recorded",
                "manager",
                {
                    "planner_size": plan["planner_size"],
                    "deterministic_size": plan["deterministic_size"],
                    "final_size": plan["final_size"],
                    "plan_digest": digest,
                },
            )
            self._write_checkpoint(run_state=manifest["state"])

    def read_plan(self: ArtifactStoreHost) -> dict[str, Any] | None:
        """Read an immutable schema-valid plan under the fencing boundary."""
        with self._fenced():
            try:
                data = json.loads(self.plan_path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    raise TamperDetected("tamper_detected: recorded plan must be an object")
                validate_plan(data)
            except FileNotFoundError:
                return None
            except (json.JSONDecodeError, ValidationError) as error:
                raise TamperDetected("tamper_detected: invalid recorded plan") from error
            return data

    def read_baseline_cache(
        self: ArtifactStoreHost,
        base_commit: str,
        gate_name: str,
    ) -> dict[str, Any] | None:
        """fencing一致時だけbaseline cacheを読み込む。"""
        path = _baseline_cache_path(self.run_dir, base_commit, gate_name)
        with self._fenced():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return None
            except json.JSONDecodeError as error:
                raise TamperDetected("tamper_detected: invalid baseline cache") from error
            if not isinstance(data, dict):
                raise TamperDetected("tamper_detected: baseline cache must be an object")
            return data

    def write_baseline_cache(
        self: ArtifactStoreHost,
        base_commit: str,
        gate_name: str,
        data: dict[str, Any],
    ) -> Path:
        """baseline cacheを保存しcheckpoint digestへ含める。"""
        path = _baseline_cache_path(self.run_dir, base_commit, gate_name)
        with self._fenced():
            atomic_write_json(path, data)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            self.events.append(
                "baseline_cached",
                "verifier",
                {"base_commit": base_commit, "gate": gate_name, "digest": digest},
            )
            manifest = self._read_manifest()
            self._write_checkpoint(run_state=manifest["state"])
        return path

    def write_verify_report(
        self: ArtifactStoreHost,
        task_id: str,
        attempt: int,
        report: dict[str, Any],
        *,
        clearance: DlpClearance,
    ) -> Path:
        """schema済みverify.jsonをtask attempt領域へ保存する。"""
        safe_task = validate_identifier(task_id, label="task_id")
        if attempt < 1:
            raise ValueError("attempt must be positive")
        validate_verify_report(report)
        self.require_dlp_clearance(
            clearance,
            artifact_kind="verify",
            digest=hashlib.sha256(canonical_json(report)).hexdigest(),
        )
        path = self.run_dir / "tasks" / safe_task / f"attempt-{attempt}" / "verify.json"
        with self._fenced():
            atomic_write_json(path, report)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            self.events.append(
                "verify_recorded",
                "verifier",
                {"attempt": attempt, "digest": digest},
                task_id=safe_task,
            )
            manifest = self._read_manifest()
            self._write_checkpoint(run_state=manifest["state"])
        return path

    def write_review_report(
        self: ArtifactStoreHost,
        task_id: str,
        attempt: int,
        report: dict[str, Any],
        *,
        clearance: DlpClearance,
    ) -> Path:
        """Persist one identity-bound review after exact canonical DLP clearance."""
        safe_task = validate_identifier(task_id, label="task_id")
        if attempt < 1:
            raise ValueError("attempt must be positive")
        validate_review(report)
        digest = hashlib.sha256(canonical_json(report)).hexdigest()
        self.require_dlp_clearance(
            clearance,
            artifact_kind="review",
            digest=digest,
        )
        path = self.run_dir / "tasks" / safe_task / f"attempt-{attempt}" / "review.json"
        with self._fenced():
            atomic_write_json(path, report)
            stored_digest = hashlib.sha256(path.read_bytes()).hexdigest()
            self.events.append(
                "review_recorded",
                "manager",
                {
                    "attempt": attempt,
                    "digest": stored_digest,
                    "reviewed_by": report["reviewed_by"],
                    "verdict": report["verdict"],
                    "findings_count": len(report["findings"]),
                },
                task_id=safe_task,
            )
            manifest = self._read_manifest()
            self._write_checkpoint(run_state=manifest["state"])
        return path


def _baseline_cache_path(run_dir: Path, base_commit: str, gate_name: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{40}", base_commit):
        raise ValueError("base_commit must be a full lowercase SHA-1")
    safe_gate = gate_name
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", safe_gate):
        raise ValueError(f"invalid gate_name: {gate_name}")
    return run_dir / "baseline" / base_commit / f"gate-{safe_gate}.json"
