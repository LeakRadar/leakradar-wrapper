"""
Refresh tests/openapi_ops.json from the published OpenAPI spec.

    python scripts/update_openapi_snapshot.py
    python scripts/update_openapi_snapshot.py --url https://api.leakradar.io/openapi.json

The snapshot keeps, for each operation, the names of its path, query and JSON body
fields. That is all tests/test_contract.py needs to check that every documented
operation has a client method and every documented parameter reaches the request,
without vendoring the whole spec. Run it, then the tests: a failure lists what the
client is missing.
"""

import argparse
import json
import pathlib
import urllib.request

DEFAULT_URL = "https://api.leakradar.io/openapi.json"
SNAPSHOT = pathlib.Path(__file__).resolve().parent.parent / "tests" / "openapi_ops.json"
METHODS = ("get", "post", "put", "patch", "delete")


def fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": "leakradar-wrapper-snapshot"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


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
    operations = {}
    for path, item in sorted(spec["paths"].items()):
        for method in METHODS:
            op = item.get(method)
            if not op:
                continue
            params = op.get("parameters", [])
            content = (op.get("requestBody") or {}).get("content", {})
            schema = (content.get("application/json") or {}).get("schema")
            entry = {
                "path": [p["name"] for p in params if p["in"] == "path"],
                "query": [p["name"] for p in params if p["in"] == "query"],
                "body": body_fields(schema, schemas) if schema else [],
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
