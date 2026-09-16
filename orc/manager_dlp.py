"""DLP assessment for canonical Manager-facing JSON payloads."""

from __future__ import annotations

import hashlib
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Protocol

from orc.artifact_files import write_private
from orc.dlp_matcher import DlpMatcher
from orc.dlp_models import CategoryCounts, DlpResult, DlpStatus, ScannerResult, ScannerStatus
from orc.dlp_reporting import build_dlp_result
from orc.errors import DlpBoundaryError
from orc.io_utils import canonical_json


class PayloadScanner(Protocol):
    """External scanner contract for exact ephemeral payload files."""

    def scan(self, path: Path) -> ScannerResult: ...


def assess_manager_payload(
    scanner: PayloadScanner,
    matcher: DlpMatcher,
    max_bytes: int,
    artifact_kind: str,
    artifact_id: str,
    payload: dict[str, Any],
) -> DlpResult:
    """Return clearance metadata without publishing or quarantining raw JSON."""
    raw = canonical_json(payload)
    if len(raw) > max_bytes:
        not_run = ScannerResult(
            ScannerStatus.FAILED,
            "not-run",
            "not-run",
            "size",
            (),
            "ARTIFACT_SIZE_LIMIT",
        )
        return build_dlp_result(
            artifact_kind,
            artifact_id,
            DlpStatus.SCAN_FAILED,
            (),
            not_run,
            False,
            "ARTIFACT_SIZE_LIMIT",
            len(raw),
        )
    scanner_result = _scan_exact(scanner, raw)
    counts = Counter(dict(_local_counts(matcher, raw)))
    counts.update(dict(scanner_result.category_counts))
    if scanner_result.status is ScannerStatus.FAILED:
        status, reason = DlpStatus.SCAN_FAILED, scanner_result.reason_code
    elif counts:
        status, reason = DlpStatus.QUARANTINED, "DLP_DETECTED"
    else:
        return build_dlp_result(
            artifact_kind,
            artifact_id,
            DlpStatus.CLEAN,
            (),
            scanner_result,
            False,
            "CLEAN",
            len(raw),
            hashlib.sha256(raw).hexdigest(),
        )
    return build_dlp_result(
        artifact_kind,
        artifact_id,
        status,
        counts,
        scanner_result,
        False,
        reason,
        len(raw),
    )


def _scan_exact(scanner: PayloadScanner, payload: bytes) -> ScannerResult:
    with tempfile.TemporaryDirectory(prefix="orc-manager-dlp-") as temporary_name:
        temporary = Path(temporary_name)
        temporary.chmod(0o700)
        path = temporary / "payload.json"
        write_private(path, payload)
        return scanner.scan(path)


def _local_counts(matcher: DlpMatcher, payload: bytes) -> CategoryCounts:
    try:
        return matcher.scan(payload.decode("utf-8"))
    except UnicodeDecodeError as error:
        raise DlpBoundaryError("DLP_SOURCE_ENCODING") from error
