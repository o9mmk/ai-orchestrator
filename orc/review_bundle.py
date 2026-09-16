"""Allowlisted, digest-bound reviewer input bundle."""

from __future__ import annotations

import hashlib
import math
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from orc.io_utils import canonical_json
from orc.paths import validate_identifier

MAX_REVIEW_INPUT_TOKENS = 30_000


@dataclass(frozen=True)
class ReviewBundle:
    """Exact payload sent to one reviewer, plus truthful pre-spend evidence."""

    payload: dict[str, Any]
    prompt: str
    input_digest: str
    estimated_tokens: int
    estimator: str = "utf8_bytes_div_4_ceiling"

    @property
    def within_limit(self) -> bool:
        """Return whether the bundle is eligible for the external review cap."""
        return self.estimated_tokens <= MAX_REVIEW_INPUT_TOKENS


def build_review_bundle(
    *,
    task_id: str,
    objective: str,
    acceptance: tuple[str, ...],
    patch: str,
    related_context: tuple[str, ...],
    verify: dict[str, Any],
) -> ReviewBundle:
    """Build the only fields a reviewer may receive without silent truncation."""
    safe_task = validate_identifier(task_id, label="task_id")
    if not isinstance(objective, str) or not isinstance(patch, str):
        raise TypeError("objective and patch must be strings")
    if any(not isinstance(item, str) for item in (*acceptance, *related_context)):
        raise TypeError("acceptance and related context must contain strings")
    if not isinstance(verify, dict):
        raise TypeError("verify must be an object")
    payload = {
        "task_id": safe_task,
        "objective": objective,
        "acceptance": list(acceptance),
        "patch": patch,
        "related_context": list(related_context),
        "verify": deepcopy(verify),
    }
    raw = canonical_json(payload)
    return ReviewBundle(
        payload,
        raw.decode("utf-8"),
        hashlib.sha256(raw).hexdigest(),
        math.ceil(len(raw) / 4),
    )
