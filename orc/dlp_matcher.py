"""Versioned high-confidence secret and PII matching for M6."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from orc.dlp_models import CategoryCounts, DlpStatus, normalize_category_counts

MATCHER_VERSION = "m6-high-confidence-v1"
MIN_STREAM_OVERLAP_CHARS = 8192
PRIVATE_KEY_STREAM_BUFFER_CHARS = 64 * 1024
PRIVATE_KEY_MARKER_OVERLAP_CHARS = 128

_PRIVATE_KEY_HEADER = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"
)
_PRIVATE_KEY_FOOTER = re.compile(
    r"-----END (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"
)

_PATTERNS = (
    (
        "PRIVATE_KEY",
        r"(?:-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----[\s\S]{0,49152}?"
        r"-----END (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----|"
        r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----[\s\S]*\Z)",
    ),
    (
        "AUTHORIZATION",
        r"(?i:\bAuthorization\s*:\s*(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{8,4096})",
    ),
    (
        "CREDENTIAL",
        r"(?i:\b(?:api[_-]?key|secret|token|password|passwd)\s*[:=]\s*[\"']?"
        r"[A-Za-z0-9._~+/=-]{8,4096}[\"']?)",
    ),
    (
        "API_KEY",
        r"(?:AKIA[0-9A-Z]{16}|(?:sk|ghp)[-_][A-Za-z0-9_-]{16,4096}|"
        r"xox[baprs]-[A-Za-z0-9-]{16,4096})",
    ),
    (
        "EMAIL",
        r"(?<![A-Za-z0-9.!#$%&'*+/=?^_`{|}~-])[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+(?![A-Za-z0-9-])",
    ),
    (
        "PHONE",
        r"(?<!\w)(?:\+?\d{1,3}[- ]?)?\d{2,4}[- ]\d{2,4}[- ]\d{3,4}(?!\w)",
    ),
)
_COMBINED = re.compile("|".join(f"(?P<{name}>{pattern})" for name, pattern in _PATTERNS))


@dataclass(frozen=True)
class MatcherResult:
    """In-memory redacted text plus safe counts."""

    text: str
    status: DlpStatus
    category_counts: CategoryCounts
    matcher_version: str = MATCHER_VERSION


class DlpMatcher:
    """Apply bounded high-confidence patterns without claiming complete PII coverage."""

    version = MATCHER_VERSION
    minimum_stream_overlap_chars = MIN_STREAM_OVERLAP_CHARS
    private_key_stream_buffer_chars = PRIVATE_KEY_STREAM_BUFFER_CHARS
    private_key_marker_overlap_chars = PRIVATE_KEY_MARKER_OVERLAP_CHARS

    def matches(self, text: str) -> tuple[re.Match[str], ...]:
        """Return matches for streaming boundary calculation."""
        return tuple(_COMBINED.finditer(text))

    def redact(self, text: str) -> MatcherResult:
        """Replace every match with a category-only fixed token."""
        counts: Counter[str] = Counter()

        def replacement(match: re.Match[str]) -> str:
            category = match.lastgroup
            if category is None:
                raise RuntimeError("DLP matcher produced an unclassified match")
            counts[category] += 1
            return f"[REDACTED:{category}]"

        redacted = _COMBINED.sub(replacement, text)
        normalized = normalize_category_counts(dict(counts)) if counts else ()
        status = DlpStatus.REDACTED if counts else DlpStatus.CLEAN
        return MatcherResult(redacted, status, normalized)

    def scan(self, text: str) -> CategoryCounts:
        """Return category counts without exposing match text or locations."""
        return self.redact(text).category_counts

    @staticmethod
    def private_key_header_start(text: str) -> int | None:
        """Return only the start offset needed for bounded stream state."""
        match = _PRIVATE_KEY_HEADER.search(text)
        return None if match is None else match.start()

    @staticmethod
    def private_key_footer_end(text: str) -> int | None:
        """Return only the end offset needed to resume after a discarded key."""
        match = _PRIVATE_KEY_FOOTER.search(text)
        return None if match is None else match.end()
