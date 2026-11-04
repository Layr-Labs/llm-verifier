"""Bounded, closed output schemas for organizer-selected score derivations.

This is deliberately separate from experiment input schemas. Local acyclic
references are expanded before review; no schema can fetch or execute anything.
"""

import re

from .errors import VerifierError
from .jsonutil import canonical, integer, keys, number, text

MAX_SCHEMA_BYTES = 131072
MAX_VALUE_BYTES = 65536
ANNOTATIONS = {"$schema", "$comment", "title", "description"}
# These linear-time forms cover exact rational and digest representations.
# Arbitrary organizer regexes are not run in the credentialed reviewer process.
PATTERNS = {
    "^(0|-?[1-9][0-9]*)$", "^[1-9][0-9]*$", "^[a-f0-9]{64}$",
    "^[0-9a-f]{64}$",
}


def compile_schema(raw):
    """Validate and expand the supported dialect without altering ordinary schemas."""
    if type(raw) is not dict or len(canonical(raw)) > MAX_SCHEMA_BYTES:
        raise VerifierError("derivation_schema_limit")
    definitions = raw.get("$defs", {})
    if type(definitions) is not dict or len(definitions) > 64:
        raise VerifierError("derivation_schema_definitions")
    for name in definitions:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", name):
            raise VerifierError("derivation_schema_definition_name")
    nodes = 0

    def expand(value, depth=0, trail=(), root=False):
        nonlocal nodes
        nodes += 1
        if depth > 16 or nodes > 4096 or type(value) is not dict:
            raise VerifierError("derivation_schema_limit")
        for key in ANNOTATIONS & value.keys():
            text(value[key], 4000)
        value = {k: v for k, v in value.items() if k not in ANNOTATIONS}
        if root:
            value = {k: v for k, v in value.items() if k != "$defs"}
        if "$ref" in value:
            keys(value, {"$ref"})
            ref = value["$ref"]
            if type(ref) is not str or not ref.startswith("#/$defs/"):
                raise VerifierError("derivation_schema_local_reference_required")
            name = ref[len("#/$defs/"):]
            if name not in definitions or name in trail:
                raise VerifierError("derivation_schema_reference_cycle_or_missing")
            return expand(definitions[name], depth + 1, (*trail, name))
        union = next((k for k in ("anyOf", "oneOf") if k in value), None)
        if union:
            keys(value, {union})
            choices = value[union]
            if type(choices) is not list or not 1 <= len(choices) <= 16:
                raise VerifierError("derivation_schema_union")
            return {union: [expand(v, depth + 1, trail) for v in choices]}
        if "const" in value:
            keys(value, {"const"}, {"type"})
            item = value["const"]
            if item is not None and type(item) not in (str, bool, int):
                raise VerifierError("derivation_schema_constant")
            inferred = "null" if item is None else {
                str: "string", bool: "boolean", int: "integer",
            }[type(item)]
            if "type" in value and value["type"] != inferred:
                raise VerifierError("derivation_schema_constant_type")
            if type(item) is str:
                text(item, 16384)
            elif type(item) is int:
                integer(item, -(2**53 - 1), 2**53 - 1)
            return {"type": inferred, "const": item}
        kind = value.get("type")
        if kind == "object":
            keys(value, {"type", "properties", "required", "additionalProperties"})
            props, required = value["properties"], value["required"]
            if (type(props) is not dict or len(props) > 64 or value["additionalProperties"] is not False
                    or type(required) is not list or any(type(k) is not str for k in required)
                    or len(set(required)) != len(required) or set(required) != set(props)):
                raise VerifierError("derivation_schema_closed_required_object")
            for name in props:
                text(name, 128)
            return {**value, "properties": {k: expand(v, depth + 1, trail) for k, v in props.items()}}
        if kind == "array":
            keys(value, {"type", "items", "maxItems"}, {"minItems"})
            integer(value["maxItems"], 0, 128)
            integer(value.get("minItems", 0), 0, value["maxItems"])
            return {**value, "items": expand(value["items"], depth + 1, trail)}
        if kind == "string":
            keys(value, {"type", "maxLength"}, {"minLength", "enum", "pattern"})
            integer(value["maxLength"], 0, 16384)
            integer(value.get("minLength", 0), 0, value["maxLength"])
            if "pattern" in value and value["pattern"] not in PATTERNS:
                raise VerifierError("derivation_schema_unsupported_pattern")
        elif kind in ("integer", "number"):
            keys(value, {"type", "minimum", "maximum"}, {"enum"})
            number(value["minimum"], -1e100, 1e100)
            number(value["maximum"], value["minimum"], 1e100)
            if kind == "integer" and any(type(value[k]) is not int for k in ("minimum", "maximum")):
                raise VerifierError("derivation_schema_integer_bounds")
        elif kind in ("null", "boolean"):
            keys(value, {"type"})
        else:
            raise VerifierError("derivation_schema_type")
        if "enum" in value:
            items = value["enum"]
            if (type(items) is not list or not 1 <= len(items) <= 64
                    or len({canonical(v) for v in items}) != len(items)):
                raise VerifierError("derivation_schema_enum")
            for item in items:
                validate_value({k: v for k, v in value.items() if k != "enum"}, item)
        return value

    compiled = expand(raw, root=True)
    if compiled.get("type") != "object" or len(canonical(compiled)) > MAX_SCHEMA_BYTES:
        raise VerifierError("derivation_schema_root_object")
    return compiled


def validate_value(schema, value):
    if len(canonical(value)) > MAX_VALUE_BYTES:
        raise VerifierError("derivation_payload_limit")
    for union in ("anyOf", "oneOf"):
        if union in schema:
            matches = 0
            for choice in schema[union]:
                try:
                    validate_value(choice, value)
                    matches += 1
                except VerifierError:
                    pass
            if not matches or union == "oneOf" and matches != 1:
                raise VerifierError("derivation_payload_union")
            return value
    kind = schema["type"]
    if kind == "object":
        keys(value, schema["properties"])
        for name, child in schema["properties"].items():
            validate_value(child, value[name])
    elif kind == "array":
        if type(value) is not list or not schema.get("minItems", 0) <= len(value) <= schema["maxItems"]:
            raise VerifierError("derivation_payload_array")
        for item in value:
            validate_value(schema["items"], item)
    elif kind == "string":
        if type(value) is not str or not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", 16384):
            raise VerifierError("derivation_payload_string")
        if "pattern" in schema and not re.fullmatch(schema["pattern"], value):
            raise VerifierError("derivation_payload_pattern")
    elif kind in ("integer", "number"):
        if kind == "integer" and type(value) is not int:
            raise VerifierError("derivation_payload_integer")
        number(value, schema.get("minimum", -1e100), schema.get("maximum", 1e100))
    elif kind == "boolean":
        if type(value) is not bool:
            raise VerifierError("derivation_payload_boolean")
    elif kind == "null" and value is not None:
        raise VerifierError("derivation_payload_null")
    if "const" in schema and canonical(value) != canonical(schema["const"]):
        raise VerifierError("derivation_payload_constant")
    if "enum" in schema and canonical(value) not in {canonical(v) for v in schema["enum"]}:
        raise VerifierError("derivation_payload_enum")
    return value


def provider_schema(schema):
    """OpenAI strict schemas use anyOf; exclusive-union checking remains local."""
    if type(schema) is list:
        return [provider_schema(v) for v in schema]
    if type(schema) is not dict:
        return schema
    # Convert only schema positions; property names may themselves be "oneOf".
    result = {k: v for k, v in schema.items() if k not in {"oneOf", "const"}}
    if "const" in schema:
        result["enum"] = [schema["const"]]
    if "properties" in result:
        result["properties"] = {k: provider_schema(v) for k, v in result["properties"].items()}
    if "items" in result:
        result["items"] = provider_schema(result["items"])
    if "oneOf" in schema:
        result["anyOf"] = [provider_schema(v) for v in schema["oneOf"]]
    elif "anyOf" in schema:
        result["anyOf"] = [provider_schema(v) for v in schema["anyOf"]]
    return result
