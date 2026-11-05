"""
Refresh tests/openapi_ops.json from the published OpenAPI spec.

    python scripts/update_openapi_snapshot.py
    python scripts/update_openapi_snapshot.py --url https://api.leakradar.io/openapi.json

The snapshot keeps, for each operation:
- the names of its path, query and JSON body fields;
- its operationId and its tag, which make its anchor in the docs
  (https://docs.leakradar.io/#tag/<tag slug>/<operationId>);
- the Python type of its answer (the JSON body of its first 2xx status), written as
  scripts/generate_types.py writes it: "EmailSearchResponse", "List[NotificationOut]",
  "bytes" for a file, "Any" without a body.

That is all tests/test_contract.py and tests/test_annotations.py need to check that every
documented operation has a client method, every documented parameter reaches the request and
every method is annotated with the answer it returns, without vendoring the whole spec. Run it,
then the tests: a failure lists what the client is missing.
"""

import argparse
import importlib.util
import json
import pathlib
import urllib.request

DEFAULT_URL = "https://api.leakradar.io/openapi.json"
SNAPSHOT = pathlib.Path(__file__).resolve().parent.parent / "tests" / "openapi_ops.json"
TYPES_GENERATOR = pathlib.Path(__file__).resolve().parent / "generate_types.py"
METHODS = ("get", "post", "put", "patch", "delete")


def fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": "leakradar-wrapper-snapshot"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def load_types_generator():
    """scripts/generate_types.py, which names the types of leakradar/types.py (scripts/ is no package)."""
    spec = importlib.util.spec_from_file_location("generate_types", TYPES_GENERATOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def body_fields(schema, schemas):
    """Property names of a request body schema, following $ref and Optional (anyOf with null)."""
    while schema:
        if "$ref" in schema:
            schema = schemas[schema["$ref"].rsplit("/", 1)[-1]]
        elif "anyOf" in schema:
            schema = next((s for s in schema["anyOf"] if s.get("type") != "null"), None)
        else:
            return sorted(schema.get("properties") or {})
    return []


def trim(spec, url):
    schemas = spec.get("components", {}).get("schemas", {})
    # Built on this very spec dict: the generator finds the schemas of the answers by identity.
    types = load_types_generator().TypeGenerator(spec)
    operations = {}
    for path, item in sorted(spec["paths"].items()):
        for method in METHODS:
            op = item.get(method)
            if not op:
                continue
            params = op.get("parameters", [])
            content = (op.get("requestBody") or {}).get("content", {})
            schema = (content.get("application/json") or {}).get("schema")
            tags = op.get("tags") or [None]
            entry = {
                "path": [p["name"] for p in params if p["in"] == "path"],
                "query": [p["name"] for p in params if p["in"] == "query"],
                "body": body_fields(schema, schemas) if schema else [],
                "operation_id": op.get("operationId"),
                "tag": tags[0],
                "response": types.response_type(method, path),
            }
            if op.get("deprecated"):
                entry["deprecated"] = True
            operations[f"{method.upper()} {path}"] = entry
    return {"source": url, "api_version": spec["info"]["version"], "operations": operations}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default=DEFAULT_URL)
    args = parser.parse_args()

    snapshot = trim(fetch(args.url), args.url)
    SNAPSHOT.write_text(json.dumps(snapshot, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"{len(snapshot['operations'])} operations written to {SNAPSHOT}")


if __name__ == "__main__":
    main()
