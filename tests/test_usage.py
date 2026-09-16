"""M5 usage source classification tests."""

import pytest

from orc.usage import BudgetSource, ValidatedUsage, measure_usage


def test_structurally_validated_usage_is_measured() -> None:
    """Only the explicit validated model can produce measured usage."""
    # Arrange
    usage = ValidatedUsage(input_tokens=12, output_tokens=8, schema="orc-test-v1")

    # Act
    result = measure_usage(validated_usage=usage, byte_count=1000, invocation_count=1)

    # Assert
    assert result.tokens == 20
    assert result.source is BudgetSource.MEASURED
    assert result.estimator == "orc-test-v1"


def test_unknown_usage_falls_back_to_bytes_proxy() -> None:
    """Unknown CLI JSONL structure is not guessed as measured."""
    # Act
    result = measure_usage(validated_usage=None, byte_count=9, invocation_count=1)

    # Assert
    assert result.tokens == 3
    assert result.source is BudgetSource.BYTES_PROXY
    assert result.estimator == "utf8_bytes_div_4_ceiling"


def test_bytes_unavailable_falls_back_to_count_proxy() -> None:
    """Count proxy is used only for the explicit no-byte case."""
    # Act
    result = measure_usage(validated_usage=None, byte_count=None, invocation_count=2)

    # Assert
    assert result.tokens == 2
    assert result.source is BudgetSource.COUNT_PROXY
    assert result.estimator == "invocation_count"


@pytest.mark.parametrize(
    ("input_tokens", "output_tokens"),
    [(-1, 0), (0, -1), (True, 1)],
)
def test_invalid_explicit_usage_is_rejected(input_tokens: int, output_tokens: int) -> None:
    """Malformed numeric fields cannot become measured usage."""
    with pytest.raises(ValueError, match="non-negative integers"):
        ValidatedUsage(input_tokens, output_tokens, "orc-test-v1")
