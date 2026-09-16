"""Path-safe DLP scan, atomic publication, and quarantine policy."""

from __future__ import annotations

import hashlib
import tempfile
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from orc.artifact_files import ArtifactSourceReader, publish_artifact, write_private
from orc.dlp_matcher import DlpMatcher
from orc.dlp_models import (
    CategoryCounts,
    DlpResult,
    DlpStatus,
    ManagerPayloadAssessment,
    ScannerResult,
    ScannerStatus,
    normalize_category_counts,
)
from orc.dlp_reporting import build_dlp_result
from orc.errors import DlpBoundaryError
from orc.gitleaks_adapter import GitleaksScanner
from orc.manager_dlp import assess_manager_payload
from orc.paths import validate_identifier
from orc.quarantine import QuarantineStore
from orc.store import RunStateStore

ALLOWED_ARTIFACT_KINDS = {
    "patch",
    "stdout",
    "stderr",
    "transcript",
    "findings",
    "summary",
    "result",
    "plan",
    "verify",
    "review",
    "review_bundle",
    "events",
}


class SecretScanner(Protocol):
    """External scanner boundary used by production and fake adapters."""

    def scan(self, path: Path) -> ScannerResult: ...


class QuarantineWriter(Protocol):
    """Injectable quarantine filesystem boundary."""

    def save(
        self,
        artifact_id: str,
        artifact_kind: str,
        payload: bytes,
        safe_metadata: dict[str, Any],
    ) -> Path: ...

    def remove(self, artifact_id: str) -> None: ...


class ArtifactIngestor:
    """Publish only artifacts proven clean by both local and external scanners."""

    def __init__(
        self,
        store: RunStateStore,
        scanner: SecretScanner | None = None,
        *,
        matcher: DlpMatcher | None = None,
        quarantine: QuarantineWriter | None = None,
        max_artifact_bytes: int = 10 * 1024 * 1024,
        allowed_source_roots: tuple[Path, ...] | None = None,
    ) -> None:
        if max_artifact_bytes < 1:
            raise ValueError("artifact size limit must be positive")
        self.store = store
        self.scanner = scanner or GitleaksScanner.discover()
        self.matcher = matcher or DlpMatcher()
        self.quarantine = quarantine or QuarantineStore(store.run_dir)
        self.max_artifact_bytes = max_artifact_bytes
        configured_roots = allowed_source_roots or (store.paths.worktree_root,)
        self.source_reader = ArtifactSourceReader(configured_roots, max_artifact_bytes)

    def ingest(
        self,
        source_root: Path,
        relative_source: Path,
        artifact_kind: str,
        artifact_id: str,
        *,
        pre_detected: dict[str, int] | None = None,
        force_reason: str | None = None,
        publish_clean: bool = True,
    ) -> DlpResult:
        """Scan one staging source, then atomically publish or quarantine it."""
        source = self.source_reader.read(source_root, relative_source)
        try:
            return self.ingest_payload(
                source.payload,
                artifact_kind,
                artifact_id,
                pre_detected=pre_detected,
                force_reason=force_reason,
                publish_clean=publish_clean,
                source_unlink=source.unlink,
            )
        finally:
            source.close()

    def ingest_payload(
        self,
        payload: bytes,
        artifact_kind: str,
        artifact_id: str,
        *,
        pre_detected: dict[str, int] | None = None,
        force_reason: str | None = None,
        publish_clean: bool = True,
        source_unlink: Callable[[], None] | None = None,
    ) -> DlpResult:
        """Scan exact bytes already read through a Manager-pinned descriptor."""
        safe_kind, safe_id = self._validate_labels(artifact_kind, artifact_id)
        if len(payload) > self.max_artifact_bytes:
            raise DlpBoundaryError("DLP_SOURCE_SIZE_LIMIT")
        scanner = self._scan_payload(payload)
        try:
            local_counts = Counter(dict(self._local_counts(payload)))
        except DlpBoundaryError:
            scanner = ScannerResult(
                ScannerStatus.FAILED,
                scanner.scanner_name,
                scanner.scanner_version,
                "decode",
                scanner.category_counts,
                "CONTENT_DECODE_FAILED",
            )
            local_counts = Counter()
        if pre_detected:
            local_counts.update(dict(normalize_category_counts(pre_detected)))
        counts = local_counts + Counter(dict(scanner.category_counts))
        if scanner.status is ScannerStatus.FAILED:
            return self._commit_quarantine_result(
                safe_kind,
                safe_id,
                payload,
                counts,
                scanner,
                DlpStatus.SCAN_FAILED,
                scanner.reason_code,
                source_unlink,
            )
        elif force_reason is not None:
            return self._commit_quarantine_result(
                safe_kind,
                safe_id,
                payload,
                counts,
                scanner,
                DlpStatus.QUARANTINED,
                force_reason,
                source_unlink,
            )
        elif counts:
            return self._commit_quarantine_result(
                safe_kind,
                safe_id,
                payload,
                counts,
                scanner,
                DlpStatus.QUARANTINED,
                "DLP_DETECTED",
                source_unlink,
            )
        elif publish_clean:
            return self._commit_clean(safe_kind, safe_id, payload, scanner)
        result = build_dlp_result(
            safe_kind,
            safe_id,
            DlpStatus.CLEAN,
            (),
            scanner,
            False,
            "CLEAN",
            len(payload),
            hashlib.sha256(payload).hexdigest(),
        )
        self._record_result(result)
        return result

    def assess_manager_payload(
        self,
        artifact_kind: str,
        artifact_id: str,
        payload: dict[str, Any],
    ) -> ManagerPayloadAssessment:
        """Scan canonical JSON in an ephemeral private directory for Manager clearance."""
        safe_kind, safe_id = self._validate_labels(artifact_kind, artifact_id)
        result = assess_manager_payload(
            self.scanner,
            self.matcher,
            self.max_artifact_bytes,
            safe_kind,
            safe_id,
            payload,
        )
        with self.store._fenced():
            self._append_result_and_checkpoint(result)
            clearance = self.store._issue_dlp_clearance(result) if result.clean else None
        return ManagerPayloadAssessment(result, clearance)

    def assess_raw_payload(
        self,
        artifact_kind: str,
        artifact_id: str,
        payload: bytes,
    ) -> ManagerPayloadAssessment:
        """Scan exact bytes and issue a one-store clearance without normal publication."""
        result = self.ingest_payload(
            payload,
            artifact_kind,
            artifact_id,
            publish_clean=False,
        )
        clearance = None
        if result.clean:
            with self.store._fenced():
                clearance = self.store._issue_dlp_clearance(result)
        return ManagerPayloadAssessment(result, clearance)

    def _scan_payload(self, payload: bytes) -> ScannerResult:
        with tempfile.TemporaryDirectory(prefix="orc-artifact-dlp-") as temporary_name:
            temporary = Path(temporary_name)
            temporary.chmod(0o700)
            path = temporary / "payload"
            write_private(path, payload)
            return self.scanner.scan(path)

    def _local_counts(self, payload: bytes) -> CategoryCounts:
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as error:
            raise DlpBoundaryError("DLP_SOURCE_ENCODING") from error
        return self.matcher.scan(text)

    def _commit_clean(
        self,
        kind: str,
        artifact_id: str,
        payload: bytes,
        scanner: ScannerResult,
    ) -> DlpResult:
        result = build_dlp_result(
            kind,
            artifact_id,
            DlpStatus.CLEAN,
            (),
            scanner,
            False,
            "CLEAN",
            len(payload),
            hashlib.sha256(payload).hexdigest(),
        )
        with self.store._fenced():
            target = publish_artifact(self.store.run_dir, kind, artifact_id, payload)
            self._append_result_and_checkpoint(
                result,
                rollback=lambda: self._remove_normal_artifact(target),
            )
        return result

    def _commit_quarantine_result(
        self,
        kind: str,
        artifact_id: str,
        payload: bytes,
        counts: Counter[str],
        scanner: ScannerResult,
        status: DlpStatus,
        reason: str,
        source_unlink: Callable[[], None] | None,
    ) -> DlpResult:
        safe_counts = normalize_category_counts(dict(counts)) if counts else ()
        metadata = {
            "status": status.value,
            "categories": dict(safe_counts),
            "scanner": scanner.scanner_name,
            "scanner_version": scanner.scanner_version,
            "scanner_result_code": scanner.result_code,
            "reason_code": reason,
            "size_bytes": len(payload),
        }
        result = build_dlp_result(
            kind,
            artifact_id,
            status,
            safe_counts,
            scanner,
            True,
            reason,
            len(payload),
        )
        saved = False
        with self.store._fenced():
            try:
                self.quarantine.save(artifact_id, kind, payload, metadata)
                saved = True
            except (OSError, ValueError, TypeError):
                result = build_dlp_result(
                    kind,
                    artifact_id,
                    DlpStatus.SCAN_FAILED,
                    safe_counts,
                    scanner,
                    False,
                    "QUARANTINE_FAILED",
                    len(payload),
                )
            rollback = (lambda: self.quarantine.remove(artifact_id)) if saved else None
            self._append_result_and_checkpoint(result, rollback=rollback)
            if saved and source_unlink is not None:
                source_unlink()
        return result

    def _record_result(self, result: DlpResult) -> None:
        with self.store._fenced():
            self._append_result_and_checkpoint(result)

    def _append_result_and_checkpoint(
        self,
        result: DlpResult,
        *,
        rollback: Callable[[], None] | None = None,
    ) -> None:
        event_recorded = False
        try:
            self.store.events.append(
                "dlp_artifact_processed", "manager", result.to_event_data()
            )
            event_recorded = True
            manifest = self.store._read_manifest()
            self.store._write_checkpoint(run_state=manifest["state"])
        except Exception as primary_error:
            if rollback is not None:
                try:
                    rollback()
                except Exception as rollback_error:
                    raise DlpBoundaryError("DLP_TRANSACTION_ROLLBACK_FAILED") from rollback_error
            if event_recorded:
                event_type = (
                    "dlp_artifact_rolled_back"
                    if rollback is not None
                    else "dlp_artifact_checkpoint_failed"
                )
                try:
                    self.store.events.append(
                        event_type,
                        "manager",
                        {
                            "artifact_kind": result.artifact_kind,
                            "artifact_id": result.artifact_id,
                            "reason_code": "CHECKPOINT_FAILED",
                        },
                    )
                except Exception as audit_error:
                    raise DlpBoundaryError("DLP_AUDIT_COMPENSATION_FAILED") from audit_error
            raise primary_error

    @staticmethod
    def _remove_normal_artifact(target: Path) -> None:
        target.unlink()
        target.parent.rmdir()

    def _validate_labels(self, kind: str, artifact_id: str) -> tuple[str, str]:
        if kind not in ALLOWED_ARTIFACT_KINDS:
            raise ValueError("unsupported DLP artifact kind")
        if self.matcher.scan(artifact_id):
            raise ValueError("artifact id must be opaque and non-sensitive")
        return kind, validate_identifier(artifact_id, label="artifact_id")
