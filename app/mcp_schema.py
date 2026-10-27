"""Server-side validation of MCP tool arguments against the flat JSON Schema
subset the tools publish (docs/mcp/01-tool-design.md §0: primitives, enums,
arrays, nested objects; no $ref/oneOf/anyOf/allOf).

Clients do not all enforce a tool's inputSchema (Gemini strips
``additionalProperties``), so every call is checked here before a handler
runs, and a violation is reported as a tool error the model can act on,
listing each failing field. No library: the subset is small and the message
format is ours.
"""
import math

TYPES = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "boolean": lambda v: isinstance(v, bool),
    "integer": lambda v: (isinstance(v, int) and not isinstance(v, bool))
    or (isinstance(v, float) and math.isfinite(v) and v == int(v)),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)
    and math.isfinite(v),
    "null": lambda v: v is None,
}


def validate(schema, value, path=""):
    """Every violation of ``schema`` by ``value`` as ``{field, problem}``
    rows; an empty list means valid. Checks: type (a list of types is a
    union), enum, minimum/maximum, minLength/maxLength, pattern-free string
    bounds, minItems/maxItems, required, properties, additionalProperties,
    items. Unknown keywords are ignored."""
    problems = []
    label = path or "(arguments)"
    types = schema.get("type")
    if types is not None:
        allowed = types if isinstance(types, list) else [types]
        if not any(TYPES[t](value) for t in allowed if t in TYPES):
            problems.append({"field": label, "problem": "must be " + _name(allowed)})
            return problems
    if value is None:
        return problems
    if "enum" in schema and value not in schema["enum"]:
        problems.append({"field": label, "problem": "must be one of "
                         + ", ".join(str(e) for e in schema["enum"])})
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        lo, hi = schema.get("minimum"), schema.get("maximum")
        if lo is not None and value < lo:
            problems.append({"field": label, "problem": "must be at least %s" % lo})
        if hi is not None and value > hi:
            problems.append({"field": label, "problem": "must be at most %s" % hi})
    if isinstance(value, str):
        lo, hi = schema.get("minLength"), schema.get("maxLength")
        if lo is not None and len(value) < lo:
            problems.append({"field": label, "problem": "must be at least %d characters" % lo})
        if hi is not None and len(value) > hi:
            problems.append({"field": label, "problem": "must be at most %d characters" % hi})
    if isinstance(value, list):
        lo, hi = schema.get("minItems"), schema.get("maxItems")
        if lo is not None and len(value) < lo:
            problems.append({"field": label, "problem": "must have at least %d items" % lo})
        if hi is not None and len(value) > hi:
            problems.append({"field": label, "problem": "must have at most %d items" % hi})
        items = schema.get("items")
        if items:
            for i, item in enumerate(value):
                problems += validate(items, item, "%s[%d]" % (path, i))
    if isinstance(value, dict):
        props = schema.get("properties") or {}
        for key in schema.get("required") or []:
            if value.get(key) is None:
                problems.append({"field": _join(path, key), "problem": "is required"})
        for key, sub in props.items():
            if key in value and value[key] is not None:
                problems += validate(sub, value[key], _join(path, key))
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in props:
                    problems.append({"field": _join(path, key),
                                     "problem": "is not a known field"})
    return problems


def _join(path, key):
    return "%s.%s" % (path, key) if path else key


def _name(types):
    words = {"object": "an object", "array": "a list", "string": "text",
             "boolean": "true or false", "integer": "a whole number",
             "number": "a number", "null": "null"}
    return " or ".join(words.get(t, t) for t in types)


def apply_defaults(schema, value):
    """``value`` with every absent property that declares a ``default``
    filled in, recursively for nested objects that are present. Only for
    dicts; the caller has already validated."""
    if not isinstance(value, dict):
        return value
    out = dict(value)
    for key, sub in (schema.get("properties") or {}).items():
        if key not in out and "default" in sub:
            out[key] = sub["default"]
        elif isinstance(out.get(key), dict) and sub.get("type") == "object":
            out[key] = apply_defaults(sub, out[key])
    return out
