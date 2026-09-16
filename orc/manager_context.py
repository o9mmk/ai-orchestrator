"""Deterministic pinned/variable Manager prompt construction."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from typing import Any

from orc.manager_context_models import (
    ClearanceRegistry,
    ContextLimitError,
    ManagerContextBundle,
    PinnedContext,
    PinnedContextTooLarge,
    ValidatedVariable,
    canonical_json,
)
from orc.usage import UsageValue, measure_usage

__all__ = [
    "ContextLimitError",
    "ManagerContextBuilder",
    "ManagerContextBundle",
    "PinnedContext",
    "PinnedContextTooLarge",
    "ValidatedVariable",
    "utf8_bytes_estimator",
]


def utf8_bytes_estimator(value: str) -> UsageValue:
    """Default estimator, explicitly labelled as a byte proxy."""
    return measure_usage(
        validated_usage=None,
        byte_count=len(value.encode("utf-8")),
        invocation_count=1,
    )


class ManagerContextBuilder:
    """Keep pinned bytes exact and retain newest validated variables first."""

    def __init__(
        self,
        clearance_registry: ClearanceRegistry,
        *,
        estimator: Callable[[str], UsageValue] = utf8_bytes_estimator,
    ) -> None:
        self.clearance_registry = clearance_registry
        self.estimator = estimator

    def serialize_pinned(self, pinned: PinnedContext) -> bytes:
        """Serialize pinned authority fields in one fixed order."""
        payload = {
            "role_contract": pinned.role_contract,
            "goal": pinned.goal,
            "acceptance_criteria": list(pinned.acceptance_criteria),
            "forbidden": list(pinned.forbidden),
            "authority_sources": list(pinned.authority_sources),
            "base_commit": pinned.base_commit,
            "path_scope": list(pinned.path_scope),
            "safety_policy_version": pinned.safety_policy_version,
            "unresolved_blockers": list(pinned.unresolved_blockers),
        }
        return b"AUTHORITY_PINNED_V1\n" + _ordered_json(payload) + b"\nEND_AUTHORITY_PINNED_V1"

    def build(
        self,
        pinned: PinnedContext,
        variables: Sequence[ValidatedVariable],
        manager_input_tokens_hard: int,
        model_window_tokens: int | None,
    ) -> ManagerContextBundle:
        """Build a deterministic prompt within both Manager and 20% child caps."""
        if model_window_tokens is None:
            raise ContextLimitError("model_window_unknown")
        if manager_input_tokens_hard <= 0 or model_window_tokens <= 0:
            raise ContextLimitError("context caps must be positive")
        if any(not isinstance(item, ValidatedVariable) for item in variables):
            raise TypeError("Manager context requires validated variable artifacts")
        for item in variables:
            self.clearance_registry.require_dlp_clearance(
                item.clearance,
                artifact_kind=item.kind,
                digest=item.digest,
            )
        pinned_bytes = self.serialize_pinned(pinned)
        pinned_estimate = self.estimator(pinned_bytes.decode("utf-8"))
        if pinned_estimate.tokens > manager_input_tokens_hard:
            raise PinnedContextTooLarge("goal_too_large")
        initial_hard = model_window_tokens // 5
        if pinned_estimate.tokens > initial_hard:
            raise ContextLimitError("initial_bundle_too_large")
        effective_hard = min(manager_input_tokens_hard, initial_hard)
        ordered = sorted(
            enumerate(variables),
            key=lambda entry: (entry[1].sequence, entry[1].digest, entry[0]),
        )
        kept: list[tuple[int, ValidatedVariable]] = []
        for entry in reversed(ordered):
            candidate = sorted(
                (*kept, entry),
                key=lambda value: (value[1].sequence, value[1].digest, value[0]),
            )
            prompt = self._serialize_prompt(
                pinned_bytes,
                [item for _, item in candidate],
            )
            if self.estimator(prompt.decode("utf-8")).tokens <= effective_hard:
                kept = candidate
        kept_indexes = {index for index, _ in kept}
        dropped = [item for index, item in ordered if index not in kept_indexes]
        kept_items = [item for _, item in kept]
        prompt_bytes = self._serialize_prompt(pinned_bytes, kept_items)
        estimate = self.estimator(prompt_bytes.decode("utf-8"))
        return ManagerContextBundle(
            prompt_bytes=prompt_bytes,
            prompt_digest=hashlib.sha256(prompt_bytes).hexdigest(),
            pinned_bytes=pinned_bytes,
            estimate=estimate,
            kept_sequences=tuple(item.sequence for item in kept_items),
            dropped_count=len(dropped),
            dropped_digests=tuple(item.digest for item in dropped),
        )

    @staticmethod
    def _serialize_prompt(pinned_bytes: bytes, variables: Sequence[ValidatedVariable]) -> bytes:
        data = [
            {
                "kind": item.kind,
                "sequence": item.sequence,
                "payload": json.loads(item.payload_bytes),
            }
            for item in variables
        ]
        variable_bytes = canonical_json(data)
        return (
            pinned_bytes
            + b"\nUNTRUSTED_SCHEMA_VALIDATED_VARIABLE_V1\n"
            + variable_bytes
            + b"\nEND_UNTRUSTED_SCHEMA_VALIDATED_VARIABLE_V1"
        )

def _ordered_json(value: dict[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=False,
        separators=(",", ":"),
    ).encode("utf-8")
