"""Deliberately small JSON Schema subset; every value has a finite size bound.

No references, regexes, unions, conditionals, coercion or ignored keywords.
This is the experiment-input dialect, not a general JSON Schema implementation.
"""

from .errors import VerifierError, error_context
from .jsonutil import canonical, integer, keys, number, text


def validate_schema(schema, depth=0):
    if depth > 8 or len(canonical(schema)) > 16384 or type(schema) is not dict:
        raise VerifierError("input_schema_limit")
    kind = schema.get("type")
    if type(kind) is not str:
        raise VerifierError("unsupported_input_schema")
    common = {"type"}
    if kind == "object":
        keys(schema, common | {"properties", "required", "additionalProperties"})
        props, required = schema["properties"], schema["required"]
        if type(props) is not dict or len(props) > 32 or schema["additionalProperties"] is not False:
            raise VerifierError("closed_input_object_required")
        if type(required) is not list or any(type(k) is not str for k in required) or len(set(required)) != len(required) or not set(required) <= props.keys():
            raise VerifierError("input_required_keys")
        for key, value in props.items():
            text(key, 64)
            with error_context("properties", key):
                validate_schema(value, depth + 1)
    elif kind == "array":
        keys(schema, common | {"items", "maxItems"}, {"minItems"})
        integer(schema["maxItems"], 0, 128)
        integer(schema.get("minItems", 0), 0, schema["maxItems"])
        with error_context("items"):
            validate_schema(schema["items"], depth + 1)
    elif kind in {"integer", "number"}:
        keys(schema, common | {"minimum", "maximum"}, {"enum"} if kind == "integer" else ())
        number(schema["minimum"], -1e100, 1e100)
        number(schema["maximum"], schema["minimum"], 1e100)
        if kind == "integer" and any(type(schema[k]) is not int for k in ("minimum", "maximum")):
            raise VerifierError("integer_schema_bounds")
        if "enum" in schema:
            values = schema["enum"]
            if (type(values) is not list or not 1 <= len(values) <= 64
                    or any(type(v) is not int for v in values) or len(set(values)) != len(values)):
                raise VerifierError("input_enum")
            for value in values:
                validate_value({k: v for k, v in schema.items() if k != "enum"}, value)
    elif kind == "string":
        keys(schema, common | {"maxLength"}, {"minLength", "enum"})
        integer(schema["maxLength"], 0, 4096)
        integer(schema.get("minLength", 0), 0, schema["maxLength"])
        if "enum" in schema:
            values = schema["enum"]
            if type(values) is not list or not 1 <= len(values) <= 64 or any(type(v) is not str for v in values) or len(set(values)) != len(values):
                raise VerifierError("input_enum")
            for value in values:
                validate_value({k: v for k, v in schema.items() if k != "enum"}, value)
    elif kind in {"boolean", "null"}:
        keys(schema, common)
    else:
        raise VerifierError("unsupported_input_schema")


def validate_value(schema, value):
    kind = schema["type"]
    if kind == "object":
        if type(value) is not dict or value.keys() - schema["properties"].keys() or not set(schema["required"]) <= value.keys():
            raise VerifierError("input_object_fields")
        for k, v in value.items():
            with error_context(k):
                validate_value(schema["properties"][k], v)
    elif kind == "array":
        if type(value) is not list or not schema.get("minItems", 0) <= len(value) <= schema["maxItems"]:
            raise VerifierError("input_array")
        for index, v in enumerate(value):
            with error_context(index):
                validate_value(schema["items"], v)
    elif kind in {"integer", "number"}:
        if kind == "integer" and type(value) is not int:
            raise VerifierError("input_integer")
        number(value, schema["minimum"], schema["maximum"])
        if "enum" in schema and value not in schema["enum"]:
            raise VerifierError("input_enum")
    elif kind == "string":
        if type(value) is not str or not schema.get("minLength", 0) <= len(value) <= schema["maxLength"] or ("enum" in schema and value not in schema["enum"]):
            raise VerifierError("input_string")
    elif kind == "boolean" and type(value) is not bool:
        raise VerifierError("input_boolean")
    elif kind == "null" and value is not None:
        raise VerifierError("input_null")
    if len(canonical(value)) > 16384:
        raise VerifierError("input_byte_limit")
