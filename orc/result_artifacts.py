"""Fenced result.json ingestion without invalid-body propagation."""

from __future__ import annotations

import hashlib
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Protocol

from orc.dlp_models import DlpClearance
from orc.io_utils import atomic_write_json, canonical_json
from orc.paths import validate_identifier
from orc.result_schema import validate_result


class ResultStoreHost(Protocol):
    """Store operations required for result artifact transactions."""

    run_dir: Path
    events: Any

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


class ResultArtifactMixin:
    """Persist only schema-validated results; invalid content becomes a digest."""

    def write_result_report(
        self: ResultStoreHost,
        task_id: str,
        role: str,
        attempt: int,
        result: dict[str, Any],
        *,
        clearance: DlpClearance,
    ) -> Path:
        """Revalidate and checkpoint one valid result.json."""
        safe_task = validate_identifier(task_id, label="task_id")
        validate_result(
            result,
            expected_task_id=safe_task,
            expected_role=role,
            expected_attempt=attempt,
        )
        canonical_digest = hashlib.sha256(canonical_json(result)).hexdigest()
        self.require_dlp_clearance(
            clearance,
            artifact_kind="result",
            digest=canonical_digest,
        )
        path = self.run_dir / "tasks" / safe_task / f"attempt-{attempt}" / "result.json"
        with self._fenced():
            atomic_write_json(path, result)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            self.events.append(
                "result_recorded",
                "manager",
                {"attempt": attempt, "role": role, "digest": digest},
                task_id=safe_task,
            )
            manifest = self._read_manifest()
            self._write_checkpoint(run_state=manifest["state"])
        return path

    def record_schema_invalid(
        self: ResultStoreHost,
        task_id: str,
        attempt: int,
        *,
        reason: str,
        content_digest: str,
        task_state: str,
    ) -> None:
        """Record bounded invalid-result evidence without retaining its body."""
        safe_task = validate_identifier(task_id, label="task_id")
        with self._fenced():
            self.events.append(
                "schema_invalid",
                "manager",
                {
                    "attempt": attempt,
                    "reason": reason,
                    "content_digest": content_digest,
                    "task_state": task_state,
                },
                task_id=safe_task,
            )
            manifest = self._read_manifest()
            self._write_checkpoint(run_state=manifest["state"])
