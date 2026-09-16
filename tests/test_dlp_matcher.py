"""M6 high-confidence matcher and streaming redaction tests."""

import pytest

from orc.dlp_matcher import DlpMatcher
from orc.dlp_models import DlpStatus
from orc.stream_redaction import StreamingRedactor
from tests.m6_helpers import dummy_api_key, dummy_email, dummy_phone


def test_matcher_redacts_secret_and_pii_with_fixed_tokens() -> None:
    """API key, email, and phone values never survive a redaction pass."""
    matcher = DlpMatcher()
    values = (dummy_api_key(), dummy_email(), dummy_phone())

    redacted = matcher.redact(" | ".join(values))

    assert redacted.status is DlpStatus.REDACTED
    assert dict(redacted.category_counts) == {"API_KEY": 1, "EMAIL": 1, "PHONE": 1}
    assert "[REDACTED:API_KEY]" in redacted.text
    assert "[REDACTED:EMAIL]" in redacted.text
    assert "[REDACTED:PHONE]" in redacted.text
    assert all(value not in redacted.text for value in values)


def test_streaming_redactor_detects_match_across_chunk_boundary() -> None:
    """The overlap retains a token split at an arbitrary read boundary."""
    secret = dummy_api_key()
    split = len(secret) // 2
    redactor = StreamingRedactor(DlpMatcher(), overlap_chars=8192, output_limit_bytes=4096)

    output = b"".join(
        (
            redactor.feed(("prefix " + secret[:split]).encode()),
            redactor.feed((secret[split:] + " suffix").encode()),
            redactor.finish(),
        )
    ).decode()

    assert secret not in output
    assert "[REDACTED:API_KEY]" in output
    assert dict(redactor.result().category_counts) == {"API_KEY": 1}


def test_streaming_redactor_is_bounded_and_reports_output_limit() -> None:
    """Limit overflow drains input but is never reported as a clean full capture."""
    redactor = StreamingRedactor(DlpMatcher(), overlap_chars=8192, output_limit_bytes=64)

    chunks = [redactor.feed(b"x" * 80), redactor.feed(b"y" * 80), redactor.finish()]
    result = redactor.result()

    assert len(b"".join(chunks)) <= 64
    assert result.limit_exceeded is True
    assert result.reason_code == "OUTPUT_LIMIT_EXCEEDED"
    assert result.status is DlpStatus.QUARANTINED


def test_private_key_block_is_replaced_without_leaking_body() -> None:
    """A bounded PEM block is replaced as one unit, not header-only."""
    header = "-----BEGIN " + "PRIVATE KEY-----"
    footer = "-----END " + "PRIVATE KEY-----"
    body = "QUJDREVGR0hJSktMTU5PUA=="
    payload = "\n".join((header, body, footer))

    result = DlpMatcher().redact(payload)

    assert result.text == "[REDACTED:PRIVATE_KEY]"
    assert body not in result.text


def test_incomplete_private_key_stream_never_emits_body() -> None:
    """A missing footer keeps the whole PEM tail private through EOF."""
    header = "-----BEGIN " + "PRIVATE KEY-----\n"
    body = "Q" * (70 * 1024)
    redactor = StreamingRedactor(
        DlpMatcher(), overlap_chars=8192, output_limit_bytes=128 * 1024
    )

    output = b"".join(
        (
            redactor.feed((header + body[:40_000]).encode()),
            redactor.feed(body[40_000:].encode()),
            redactor.finish(),
        )
    ).decode()

    assert output == "[REDACTED:PRIVATE_KEY]"
    assert redactor.buffered_chars <= DlpMatcher().private_key_marker_overlap_chars
    assert body[:100] not in output
    assert dict(redactor.result().category_counts) == {"PRIVATE_KEY": 1}


def test_stream_overlap_below_matcher_minimum_is_rejected() -> None:
    """Callers cannot weaken cross-chunk matching below the declared bound."""
    with pytest.raises(ValueError, match="safety minimum"):
        StreamingRedactor(DlpMatcher(), overlap_chars=128, output_limit_bytes=4096)
