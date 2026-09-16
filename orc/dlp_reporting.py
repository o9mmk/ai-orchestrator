"""Safe DLP result construction and event/checkpoint recording."""

from __future__ import annotations

from collections.abc import Mapping

from orc.dlp_models import (
    CategoryCounts,
    DlpResult,
    DlpStatus,
    ScannerResult,
    normalize_category_counts,
)


def build_dlp_result(
    kind: str,
    artifact_id: str,
    status: DlpStatus,
    counts: Mapping[str, int] | CategoryCounts,
    scanner: ScannerResult,
    quarantined: bool,
    reason: str,
    size: int,
    digest: str | None = None,
) -> DlpResult:
    """Build one normalized result without accepting raw scanner fields."""
    safe_counts = normalize_category_counts(dict(counts)) if counts else ()
    return DlpResult(
        kind,
        artifact_id,
        status,
        safe_counts,
        scanner.scanner_name,
        scanner.scanner_version,
        scanner.result_code,
        quarantined,
        reason,
        size,
        digest,
    )
