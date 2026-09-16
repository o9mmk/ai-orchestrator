"""Conservative usage classification for M5 budget accounting."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum


class BudgetSource(StrEnum):
    """Confidence source for token-like budget units."""

    MEASURED = "measured"
    BYTES_PROXY = "bytes_proxy"
    COUNT_PROXY = "count_proxy"


@dataclass(frozen=True)
class ValidatedUsage:
    """Usage fields already validated against an explicit upstream schema."""

    input_tokens: int
    output_tokens: int
    schema: str

    def __post_init__(self) -> None:
        values = (self.input_tokens, self.output_tokens)
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values):
            raise ValueError("usage tokens must be non-negative integers")
        if not self.schema:
            raise ValueError("usage schema identifier must be non-empty")


@dataclass(frozen=True)
class UsageValue:
    """Budget units plus a truthful estimator/source label."""

    tokens: int
    source: BudgetSource
    estimator: str

    def __post_init__(self) -> None:
        if isinstance(self.tokens, bool) or not isinstance(self.tokens, int) or self.tokens < 0:
            raise ValueError("usage value must be a non-negative integer")
        if not self.estimator:
            raise ValueError("usage estimator must be non-empty")


def measure_usage(
    *,
    validated_usage: ValidatedUsage | None,
    byte_count: int | None,
    invocation_count: int,
) -> UsageValue:
    """Use measured values only with proof, else degrade bytes then count."""
    if invocation_count < 0:
        raise ValueError("invocation_count must be non-negative")
    if validated_usage is not None:
        return UsageValue(
            validated_usage.input_tokens + validated_usage.output_tokens,
            BudgetSource.MEASURED,
            validated_usage.schema,
        )
    if byte_count is not None:
        if byte_count < 0:
            raise ValueError("byte_count must be non-negative")
        return UsageValue(
            math.ceil(byte_count / 4),
            BudgetSource.BYTES_PROXY,
            "utf8_bytes_div_4_ceiling",
        )
    return UsageValue(invocation_count, BudgetSource.COUNT_PROXY, "invocation_count")
