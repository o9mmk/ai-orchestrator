"""Strict fake-provider checks for the Codex Structured Outputs subset."""

STRICT_SCHEMA_CHECK_SOURCE = r'''
SUPPORTED_SCHEMA_KEYWORDS = {
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

def reject_unsupported_schema(schema):
    for keyword, value in schema.items():
        if keyword not in SUPPORTED_SCHEMA_KEYWORDS:
            raise SystemExit(12)
        if keyword == "properties":
            for child in value.values():
                reject_unsupported_schema(child)
        elif keyword == "$defs":
            for child in value.values():
                reject_unsupported_schema(child)
        elif keyword == "items":
            reject_unsupported_schema(value)
        elif keyword == "anyOf":
            for child in value:
                reject_unsupported_schema(child)
'''
