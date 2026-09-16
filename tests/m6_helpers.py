"""Safe M6 fixtures; dummy sensitive values are assembled only at runtime."""

import hashlib
from pathlib import Path
from typing import Any

from orc.artifact_ingest import ArtifactIngestor
from orc.dlp_models import DlpClearance, ScannerResult, ScannerStatus
from orc.manager_context_models import canonical_json


def dummy_api_key() -> str:
    """Return a matcher-visible dummy without storing its completed form in git."""
    return "sk" + "_" + ("m6safe" * 6)


def dummy_email() -> str:
    """Return a reserved-domain email assembled at runtime."""
    return "m6.person" + "@" + "example.invalid"


def dummy_phone() -> str:
    """Return a synthetic phone-like value assembled at runtime."""
    return "090" + "-1234" + "-5678"


class FakeScanner:
    """Injectable scanner returning only predeclared safe metadata."""

    def __init__(self, result: ScannerResult | None = None) -> None:
        self.result = result or ScannerResult(
            ScannerStatus.CLEAN,
            "fake-gitleaks",
            "8.test",
            "0",
            (),
            "CLEAN",
        )
        self.paths: list[Path] = []

    def scan(self, path: Path) -> ScannerResult:
        self.paths.append(path)
        return self.result


def make_clean_ingestor(store) -> ArtifactIngestor:  # type: ignore[no-untyped-def]
    """Create a production-path ingestor with a deterministic clean scanner."""
    return ArtifactIngestor(store, FakeScanner())


class FixtureClearanceRegistry:
    """Test-only registry that exercises the production capability check contract."""

    def __init__(self) -> None:
        self._records: dict[str, tuple[str, str]] = {}

    def issue(self, kind: str, payload: dict[str, Any]) -> DlpClearance:
        digest = hashlib.sha256(canonical_json(payload)).hexdigest()
        capability_id = hashlib.sha256(
            ("fixture:" + kind + ":" + digest).encode()
        ).hexdigest()
        clearance = DlpClearance._issue(capability_id, kind, "test-clearance", digest)
        self._records[capability_id] = (kind, digest)
        return clearance

    def require_dlp_clearance(
        self,
        clearance: DlpClearance,
        *,
        artifact_kind: str,
        digest: str,
    ) -> None:
        if self._records.get(clearance.capability_id) != (artifact_kind, digest):
            raise ValueError("unregistered test clearance")
        if clearance.artifact_kind != artifact_kind or clearance.digest != digest:
            raise ValueError("test clearance mismatch")


def fixture_clearance(
    kind: str,
    payload: dict[str, Any],
    registry: FixtureClearanceRegistry | None = None,
) -> tuple[DlpClearance, FixtureClearanceRegistry]:
    """Issue one explicit test-only capability for a bounded benign fixture."""
    selected = registry or FixtureClearanceRegistry()
    return selected.issue(kind, payload), selected
