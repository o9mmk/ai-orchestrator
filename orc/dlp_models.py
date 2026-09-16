"""Safe, bounded public models for M6 DLP processing."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class DlpStatus(StrEnum):
    """Artifact status exposed to Manager-facing code."""

    CLEAN = "CLEAN"
    REDACTED = "REDACTED"
    QUARANTINED = "QUARANTINED"
    SCAN_FAILED = "SCAN_FAILED"


class ScannerStatus(StrEnum):
    """External scanner outcome before artifact policy is applied."""

    CLEAN = "CLEAN"
    FINDINGS = "FINDINGS"
    FAILED = "FAILED"


CategoryCounts = tuple[tuple[str, int], ...]


def normalize_category_counts(items: dict[str, int] | CategoryCounts) -> CategoryCounts:
    """Return sorted positive counts with safe category identifiers only."""
    values = items.items() if isinstance(items, dict) else items
    normalized: list[tuple[str, int]] = []
    for category, count in values:
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", category):
            raise ValueError("invalid DLP category")
        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            raise ValueError("DLP category count must be a positive integer")
        normalized.append((category, count))
    return tuple(sorted(normalized))


@dataclass(frozen=True)
class ScannerResult:
    """Scanner metadata with all raw output and matches removed."""

    status: ScannerStatus
    scanner_name: str
    scanner_version: str
    result_code: str
    category_counts: CategoryCounts
    reason_code: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "category_counts", normalize_category_counts(self.category_counts))
        for value in (self.scanner_name, self.scanner_version, self.result_code, self.reason_code):
            if not value or len(value) > 128 or any(char in value for char in "\r\n\x00"):
                raise ValueError("unsafe scanner metadata")
        if self.status is ScannerStatus.CLEAN and self.category_counts:
            raise ValueError("clean scanner result cannot contain findings")
        if self.status is ScannerStatus.FINDINGS and not self.category_counts:
            raise ValueError("scanner findings require category counts")


@dataclass(frozen=True)
class DlpResult:
    """Manager-facing DLP result; raw values and quarantine paths are excluded."""

    artifact_kind: str
    artifact_id: str
    status: DlpStatus
    category_counts: CategoryCounts
    scanner_name: str
    scanner_version: str
    scanner_result_code: str
    quarantined: bool
    reason_code: str
    size_bytes: int
    digest: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "category_counts", normalize_category_counts(self.category_counts))
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", self.artifact_kind):
            raise ValueError("invalid artifact kind metadata")
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,127}", self.artifact_id):
            raise ValueError("invalid artifact id metadata")
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", self.reason_code):
            raise ValueError("invalid DLP reason code")
        for value in (self.scanner_name, self.scanner_version, self.scanner_result_code):
            if not value or len(value) > 128 or any(char in value for char in "\r\n\x00"):
                raise ValueError("unsafe DLP scanner metadata")
        if isinstance(self.size_bytes, bool) or self.size_bytes < 0:
            raise ValueError("artifact size must be a non-negative integer")
        if self.digest is not None and not re.fullmatch(r"[0-9a-f]{64}", self.digest):
            raise ValueError("invalid clean artifact digest")
        if self.status is DlpStatus.CLEAN and self.digest is None:
            raise ValueError("clean artifact requires a whole-artifact digest")
        if self.status is DlpStatus.CLEAN and self.category_counts:
            raise ValueError("clean artifact cannot contain DLP findings")
        if self.status is not DlpStatus.CLEAN and self.digest is not None:
            raise ValueError("non-clean artifact must not expose a digest")

    @property
    def clean(self) -> bool:
        """Return whether this artifact may reach normal Manager surfaces."""
        return self.status is DlpStatus.CLEAN

    def to_event_data(self) -> dict[str, object]:
        """Serialize only fields allowed by the M6 event trust boundary."""
        data: dict[str, object] = {
            "artifact_kind": self.artifact_kind,
            "artifact_id": self.artifact_id,
            "status": self.status.value,
            "categories": dict(self.category_counts),
            "scanner": self.scanner_name,
            "scanner_version": self.scanner_version,
            "scanner_result_code": self.scanner_result_code,
            "quarantined": self.quarantined,
            "reason_code": self.reason_code,
            "size_bytes": self.size_bytes,
        }
        if self.digest is not None:
            data["artifact_digest"] = self.digest
        return data


@dataclass(frozen=True, init=False)
class DlpClearance:
    """Opaque capability issued only by one Manager-owned clearance registry."""

    capability_id: str
    artifact_kind: str
    artifact_id: str
    digest: str

    @classmethod
    def _issue(
        cls,
        capability_id: str,
        artifact_kind: str,
        artifact_id: str,
        digest: str,
    ) -> DlpClearance:
        instance = object.__new__(cls)
        object.__setattr__(instance, "capability_id", capability_id)
        object.__setattr__(instance, "artifact_kind", artifact_kind)
        object.__setattr__(instance, "artifact_id", artifact_id)
        object.__setattr__(instance, "digest", digest)
        instance._validate()
        return instance

    def _validate(self) -> None:
        if not re.fullmatch(r"[0-9a-f]{64}", self.capability_id):
            raise ValueError("invalid DLP clearance capability")
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", self.artifact_kind):
            raise ValueError("invalid DLP clearance kind")
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,127}", self.artifact_id):
            raise ValueError("invalid DLP clearance id")
        if not re.fullmatch(r"[0-9a-f]{64}", self.digest):
            raise ValueError("invalid DLP clearance digest")


@dataclass(frozen=True)
class ManagerPayloadAssessment:
    """Canonical payload scan plus an unforgeable clearance when clean."""

    result: DlpResult
    clearance: DlpClearance | None

    def __post_init__(self) -> None:
        if self.result.clean != (self.clearance is not None):
            raise ValueError("Manager payload clearance does not match DLP status")

    @property
    def clean(self) -> bool:
        return self.result.clean

    @property
    def reason_code(self) -> str:
        return self.result.reason_code

    def to_event_data(self) -> dict[str, object]:
        return self.result.to_event_data()


@dataclass(frozen=True)
class StreamRedactionResult:
    """Safe metadata for one drained stdout or stderr stream."""

    status: DlpStatus
    category_counts: CategoryCounts
    bytes_written: int
    limit_exceeded: bool
    reason_code: str
    matcher_version: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "category_counts", normalize_category_counts(self.category_counts))
        if self.bytes_written < 0:
            raise ValueError("stream byte count must be non-negative")
