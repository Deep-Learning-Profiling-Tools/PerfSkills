#!/usr/bin/env python3
"""Validate a ProfileWeaver document against one of the shared schemas.

    validate.py shared/schema/profile.schema.json runs/R/profile.baseline.json

Uses the `jsonschema` package when it is installed; otherwise falls back to a small
built-in checker covering the subset of draft-07 these schemas use (type, required,
properties, items, enum, minimum, minItems, local $ref). The fallback is deliberately
strict about the things that matter here - missing required fields, wrong types, and
values outside an enum - and silent about the rest.

Exit code 0 = valid, 1 = invalid, 2 = usage/IO error.
"""

import json
import sys

TYPE_MAP = {
    "object": dict, "array": list, "string": str, "boolean": bool,
    "number": (int, float), "integer": int, "null": type(None),
}


def type_ok(value, expected):
    if isinstance(expected, str):
        expected = [expected]
    for t in expected:
        py = TYPE_MAP.get(t)
        if py is None:
            continue
        # bool is a subclass of int; keep them distinct
        if t in ("number", "integer") and isinstance(value, bool):
            continue
        if isinstance(value, py):
            return True
    return False


def resolve(schema, root):
    ref = schema.get("$ref")
    if not ref:
        return schema
    if not ref.startswith("#/"):
        return schema
    node = root
    for part in ref[2:].split("/"):
        node = node[part]
    return node


def check(value, schema, root, path, errors):
    schema = resolve(schema, root)

    if "type" in schema and not type_ok(value, schema["type"]):
        errors.append(f"{path or '<root>'}: expected type {schema['type']}, "
                      f"got {type(value).__name__}")
        return

    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} not in {schema['enum']}")

    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path or '<root>'}: missing required field '{key}'")
        props = schema.get("properties", {})
        for key, sub in props.items():
            if key in value:
                check(value[key], sub, root, f"{path}.{key}" if path else key, errors)

    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path}: needs at least {schema['minItems']} items, has {len(value)}")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for i, item in enumerate(value):
                check(item, item_schema, root, f"{path}[{i}]", errors)

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: {value} is below minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: {value} is above maximum {schema['maximum']}")


def main():
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    schema_path, doc_path = sys.argv[1], sys.argv[2]
    try:
        with open(schema_path) as f:
            schema = json.load(f)
        with open(doc_path) as f:
            doc = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    try:
        import jsonschema
        validator = jsonschema.Draft7Validator(schema)
        errors = [f"{'.'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}"
                  for e in validator.iter_errors(doc)]
        engine = "jsonschema"
    except ImportError:
        errors = []
        check(doc, schema, schema, "", errors)
        engine = "builtin subset checker"

    if errors:
        print(f"INVALID ({engine}) - {len(errors)} problem(s) in {doc_path}:")
        for e in errors:
            print(f"  - {e}")
        return 1

    print(f"valid ({engine}): {doc_path} conforms to {schema_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
