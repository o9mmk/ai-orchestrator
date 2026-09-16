"""Derive provider-compatible schemas without weakening canonical validation."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


class CodexTransportSchemaError(ValueError):
    """The canonical schema contains a keyword outside the reviewed subset."""


CODEX_SCHEMA_KEYWORDS = frozenset(
    {
        "$defs",
        "$ref",
        "additionalProperties",
        "anyOf",
        "const",
        "description",
        "enum",
        "exclusiveMaximum",
        "exclusiveMinimum",
        "format",
        "items",
        "maximum",
        "maxItems",
        "minimum",
        "minItems",
        "multipleOf",
        "pattern",
        "properties",
        "required",
        "type",
    }
)

# These remain authoritative in the canonical jsonschema pass after generation.
_CANONICAL_ONLY_KEYWORDS = frozenset({"minLength", "maxLength", "uniqueItems"})


def to_codex_transport_schema(canonical: dict[str, Any]) -> dict[str, Any]:
    """Return an allowlisted deep copy suitable for Codex Structured Outputs."""
    return _convert_schema_node(canonical, path="$")


def _convert_schema_node(node: dict[str, Any], *, path: str) -> dict[str, Any]:
    converted: dict[str, Any] = {}
    for keyword, value in node.items():
        if keyword in _CANONICAL_ONLY_KEYWORDS:
            continue
        if keyword not in CODEX_SCHEMA_KEYWORDS:
            raise CodexTransportSchemaError(
                f"unsupported Codex transport schema keyword at {path}: {keyword}"
            )
        if keyword in {"properties", "$defs"}:
            if not isinstance(value, dict):
                raise CodexTransportSchemaError(f"{path}.{keyword} must be an object")
            converted[keyword] = {
                name: _convert_schema_node(child, path=f"{path}.{keyword}.{name}")
                for name, child in value.items()
            }
        elif keyword == "items":
            if not isinstance(value, dict):
                raise CodexTransportSchemaError(f"{path}.items must be an object")
            converted[keyword] = _convert_schema_node(value, path=f"{path}.items")
        elif keyword == "anyOf":
            if not isinstance(value, list) or not all(isinstance(child, dict) for child in value):
                raise CodexTransportSchemaError(f"{path}.anyOf must contain schemas")
            converted[keyword] = [
                _convert_schema_node(child, path=f"{path}.anyOf[{index}]")
                for index, child in enumerate(value)
            ]
        else:
            converted[keyword] = deepcopy(value)

    properties = converted.get("properties")
    if properties is not None:
        if converted.get("type") != "object":
            raise CodexTransportSchemaError(f"{path}.properties requires object type")
        if converted.get("additionalProperties") is not False:
            raise CodexTransportSchemaError(
                f"{path} object must set additionalProperties to false"
            )
        # Structured Outputs requires every generated property to be present.
        converted["required"] = list(properties)
    return converted
