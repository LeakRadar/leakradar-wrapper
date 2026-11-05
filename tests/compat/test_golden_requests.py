"""
Every call written against a published version still sends the same requests.

tests/compat/fixtures/requests-<version>.json holds, for each release on PyPI, realistic calls
of every public method (and of the internals an integration calls directly) with the requests
that release sent for them (scripts/build_compat_fixtures.py); together they pass every
argument of every method (test_argument_coverage.py). Each call is replayed on the
current LeakRadarClient against a mock transport, and the requests must be the same for the
server: same verb and host, same path once each segment is decoded, same query, same JSON body
once None is dropped from optional fields and a scalar is read as a one-element list where the
API takes an array (fixtures/spec-fields.json), key order ignored; same Authorization,
Content-Type, request-specific Accept and extra headers; same User-Agent prefix.

KNOWN_DIFFERENCES lists the requests a published version already changed on purpose, with the
version that shipped the change and a check of that exact change: a fixture older than that
version must show the documented change, that version and every later one must match exactly.
A method the current client lost is reported by test_surface.py, not here.
"""

import asyncio
import contextlib
import copy
import json
import pathlib
import re
import unittest
import warnings
from unittest import mock
from urllib.parse import unquote, urlsplit

import httpx

from leakradar.client import LeakRadarClient

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"
API_ORIGIN = "https://api.leakradar.io"
DOWNLOAD_FILE = re.compile(r"^/profile/raw/downloads/[^/]+/file$")
# Headers recorded on their own, or set by httpx itself.
STANDARD_HEADERS = frozenset(
    {"host", "user-agent", "accept", "accept-encoding", "connection", "content-length", "authorization", "content-type"}
)
# Accept values a client sends on every request; any other Accept belongs to the request.
CLIENT_ACCEPT = ("*/*", "application/json")
TEST_TYPES = ("leak", "raw", "dark_web")
SPEC = json.loads((FIXTURES / "spec-fields.json").read_text(encoding="utf-8"))["operations"]


def version_key(version):
    return tuple(int(part) for part in version.split("."))


def load_fixtures():
    paths = sorted(FIXTURES.glob("requests-*.json"), key=lambda p: version_key(p.stem.split("-", 1)[1]))
    return [json.loads(p.read_text(encoding="utf-8")) for p in paths]


# -------------------------
# Canonical form
# -------------------------

def record(request):
    """What the API sees of one request, in the form of the fixtures."""
    user_agent = request.headers.get("user-agent", "")
    entry = {
        "method": request.method,
        "origin": f"{request.url.scheme}://{request.url.host}" + (f":{request.url.port}" if request.url.port else ""),
        "raw_path": request.url.raw_path.split(b"?", 1)[0].decode("ascii"),
        "query": [list(item) for item in request.url.params.multi_items()],
        "json": None,
        "authorization": request.headers.get("authorization"),
        "content_type": request.headers.get("content-type"),
        "accept": request.headers.get("accept"),
        "user_agent_prefix": user_agent.split("/", 1)[0] + "/" if "/" in user_agent else user_agent,
        "headers": {k: v for k, v in request.headers.items() if k not in STANDARD_HEADERS},
    }
    if request.content:
        try:
            entry["json"] = json.loads(request.content.decode("utf-8"))
        except ValueError:
            entry["body"] = request.content.decode("utf-8", "replace")
    return entry


def match_route(method, raw_path):
    """The spec operation ("METHOD /template") a request hits, else None; ties go to literal segments."""
    segments = [unquote(s) for s in raw_path.split("/")]
    best, best_score = None, -1
    for key in SPEC:
        op_method, template = key.split(" ", 1)
        parts = template.split("/")
        if op_method != method or len(parts) != len(segments):
            continue
        if all(p == s or (p.startswith("{") and p.endswith("}") and s) for p, s in zip(parts, segments)):
            score = sum(1 for p in parts if not p.startswith("{"))
            if score > best_score:
                best, best_score = key, score
    return best


def normalized(value, arrays, required, prefix=""):
    """A JSON body as the API reads it: None of optional fields dropped, scalars of array fields listed."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            path = prefix + key
            if item is None and path not in required:
                continue
            item = normalized(item, arrays, required, path + ".")
            if path in arrays and item is not None and not isinstance(item, list):
                item = [item]
            out[key] = item
        return out
    if isinstance(value, list):
        return [normalized(item, arrays, required, prefix) for item in value]
    return value


def canonical(request, arrays, required):
    query = {}
    for key, value in request["query"]:
        query.setdefault(key, []).append(value)
    entry = {
        "method": request["method"],
        "origin": request["origin"],
        "path": [unquote(segment) for segment in request["raw_path"].split("/")],
        "query": query,
        "json": normalized(request["json"], arrays, required),
        "body": request.get("body"),
        "authorization": request["authorization"],
        "content_type": request["content_type"],
        "user_agent_prefix": request["user_agent_prefix"],
        "headers": request["headers"],
    }
    if request["accept"] not in CLIENT_ACCEPT:
        entry["accept"] = request["accept"]
    return entry


def canonical_pair(old_requests, new_requests):
    """Both lists in canonical form, each pair read with the fields of the routes it hits."""
    old, new = [], []
    for index in range(max(len(old_requests), len(new_requests))):
        pair = [r[index] if index < len(r) else None for r in (old_requests, new_requests)]
        arrays, required = set(), set()
        for request in pair:
            route = match_route(request["method"], request["raw_path"]) if request and request["origin"] == API_ORIGIN else None
            if route:
                arrays.update(SPEC[route]["array"])
                required.update(SPEC[route]["required"])
        for request, out in zip(pair, (old, new)):
            if request is not None:
                out.append(canonical(request, arrays, required))
    return old, new


def query_values(value):
    """How httpx writes a value in a query string (a list repeats the key)."""
    items = value if isinstance(value, list) else [value]
    return ["true" if v is True else "false" if v is False else "" if v is None else str(v) for v in items]


def differences(expected, actual):
    """Readable differences between two lists of canonical requests."""
    if len(expected) != len(actual):
        return [f"{len(expected)} request(s) expected, {len(actual)} sent: {expected} != {actual}"]
    found = []
    for index, (old, new) in enumerate(zip(expected, actual), 1):
        for key in sorted(set(old) | set(new)):
            if old.get(key) != new.get(key):
                found.append(f"request {index} {key}: {json.dumps(old.get(key))} -> {json.dumps(new.get(key))}")
    return found


# -------------------------
# Known differences: each check gets the old and new canonical requests, returns the problems.
# -------------------------

def _moved_to_body(old, new, fixture):
    if len(old) != len(new):
        return differences(old, new)
    problems = []
    for index, (o, n) in enumerate(zip(old, new), 1):
        sent = {key: list(values) for key, values in n["query"].items()}
        for key, value in (n["json"] or {}).items():
            sent.setdefault(key, []).extend(query_values(value))
        if (o["method"], n["method"]) != ("GET", "POST"):
            problems.append(f"request {index}: {o['method']} -> {n['method']}, expected GET -> POST")
        if sent != o["query"]:
            problems.append(f"request {index}: arguments {o['query']} became query {n['query']} + body {n['json']}")
        if n["content_type"] != "application/json":
            problems.append(f"request {index}: Content-Type {n['content_type']}, expected application/json")
        rest = [dict(r, method=None, query=None, json=None, content_type=None) for r in (o, n)]
        problems += differences([rest[0]], [rest[1]])
    return problems


def _page_size_sent(old, new, fixture):
    expected = copy.deepcopy(old)
    for request in expected:
        request["query"].setdefault("page_size", ["100"])
    return differences(expected, new)


def _token_per_request(old, new, fixture):
    expected = copy.deepcopy(old)
    for request in expected:
        request["authorization"] = None
    return differences(expected, new)


def _download_redirect(old, new, fixture):
    expected = copy.deepcopy(old)
    for request in expected:
        request["method"] = "POST"
    problems = differences(expected, new[:1])
    storage_url = fixture["storage_url"]
    storage = urlsplit(storage_url)
    followed = new[1:]
    if (
        len(followed) != 1
        or followed[0]["method"] != "GET"
        or followed[0]["origin"] != f"{storage.scheme}://{storage.netloc}"
        or followed[0]["path"] != [unquote(s) for s in storage.path.split("/")]
        or followed[0]["authorization"] is not None
    ):
        problems.append(f"expected one GET of {storage_url} without Authorization, got {followed}")
    return problems


def _run_items(old, new, fixture):
    expected = copy.deepcopy(old)
    for request in expected:
        request["path"][-1] = "items"
        request["query"].pop("search", None)
        request["query"].pop("is_email", None)
    return differences(expected, new)


def _preview_to_count(old, new, fixture):
    expected = copy.deepcopy(old)
    for request in expected:
        request["path"] = ["", "search", "raw", "count"]
        request["query"].pop("export", None)
    return differences(expected, new)


def _type_to_query(old, new, fixture):
    expected = copy.deepcopy(old)
    for request in expected:
        body = request["json"] if isinstance(request["json"], dict) else {}
        kind = body.get("notification_type")
        request["query"] = {"notification_type": [kind if kind in TEST_TYPES else "leak"]}
        request["json"] = None
        request["content_type"] = None
    return differences(expected, new)


MOVED_TO_BODY = (
    "0.1.5 followed API v1.0.0, which moved the endpoint from GET (every argument in the query) to "
    "POST (filters in a JSON body, format and paging left in the query); the API still answers the "
    "GET form on a hidden deprecated route."
)

# Method -> (version that shipped the change, why, check of the exact change).
KNOWN_DIFFERENCES = {
    "_client.request": (
        "0.1.5",
        "Since 0.1.5 _request adds the token to each request instead of the httpx client's default "
        "headers (a token changed after construction is honoured): a request sent straight through "
        "_client carries no Authorization.",
        _token_per_request,
    ),
    "download_raw_file": (
        "0.1.5",
        "The API serves the file with POST and a 302 to a presigned storage URL, which 0.1.5 follows "
        "without the API token (0.1.4 sent a GET and returned the empty redirect).",
        _download_redirect,
    ),
    "export_advanced": ("0.1.5", MOVED_TO_BODY, _moved_to_body),
    "export_advanced_urls": ("0.1.5", MOVED_TO_BODY, _moved_to_body),
    "export_domain_leaks": ("0.1.5", MOVED_TO_BODY, _moved_to_body),
    "export_domain_subdomains": ("0.1.5", MOVED_TO_BODY, _moved_to_body),
    "export_domain_urls": ("0.1.5", MOVED_TO_BODY, _moved_to_body),
    "export_unlocked_advanced": ("0.1.5", MOVED_TO_BODY, _moved_to_body),
    "export_unlocked_leaks": ("0.1.5", MOVED_TO_BODY, _moved_to_body),
    "raw_download_source": ("0.1.5", MOVED_TO_BODY, _moved_to_body),
    "search_advanced": ("0.1.5", MOVED_TO_BODY, _moved_to_body),
    "search_email": (
        "0.1.4",
        "0.1.4 added page_size: a call that leaves it out sends the API default, 100.",
        _page_size_sent,
    ),
    "notification_run_leaks": (
        "0.2.0",
        "The API replaced GET /notification_runs/{id}/leaks with /items, without search and is_email; "
        "0.2.0 calls get_notification_run_items().",
        _run_items,
    ),
    "raw_export_preview": (
        "0.2.0",
        "The API removed POST /search/raw/export/preview; 0.2.0 answers with raw_count() for the same filters.",
        _preview_to_count,
    ),
    "test_notification_method_with_data": (
        "0.2.0",
        "The API reads the test type from the query string and reads no body; a 0.1.x dict still sends "
        "the leak test, unless it names a valid notification_type.",
        _type_to_query,
    ),
}


def check(fixture, target, case, sent):
    """The problems of one replayed call (empty when the server sees the same requests)."""
    old, new = canonical_pair(case["requests"], sent)
    known = KNOWN_DIFFERENCES.get(target)
    if known and version_key(fixture["version"]) < version_key(known[0]):
        return known[2](old, new, fixture)
    return differences(old, new)


# -------------------------
# Replay
# -------------------------

@contextlib.contextmanager
def every_client_on(transport):
    """Route every httpx.AsyncClient built meanwhile (a client a method creates lazily) to `transport`."""
    original = httpx.AsyncClient

    class OnTransport(original):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    with mock.patch.object(httpx, "AsyncClient", OnTransport):
        yield


def resolve(value, client):
    """A recorded argument, {"$client": name} turned into the client's bound method."""
    if isinstance(value, dict):
        if list(value) == ["$client"]:
            return getattr(client, value["$client"])
        return {k: resolve(v, client) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, client) for v in value]
    return value


async def replay(fixture, target, case):
    """(requests sent, error raised or None) when the current client runs the recorded call."""
    sent = []

    def handle(request):
        sent.append(record(request))
        if request.url.host == "api.leakradar.io" and DOWNLOAD_FILE.match(request.url.path):
            return httpx.Response(302, headers={"Location": fixture["storage_url"]})
        return httpx.Response(200, json={})

    transport = httpx.MockTransport(handle)
    with every_client_on(transport):
        client = LeakRadarClient(token=fixture["token"], transport=transport)
        try:
            func = client._client.request if target == "_client.request" else getattr(client, target)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                result = func(*resolve(copy.deepcopy(case["args"]), client), **resolve(copy.deepcopy(case["kwargs"]), client))
                if hasattr(result, "__aiter__"):
                    async for _ in result:
                        pass
                else:
                    await result
            error = None
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        finally:
            await client.aclose()
    return sent, error


async def replay_all(fixtures):
    """(version, target, case id) -> (requests, error), for every target the current client has."""
    results = {}
    for fixture in fixtures:
        for target, cases in fixture["methods"].items():
            if target != "_client.request" and not callable(getattr(LeakRadarClient, target, None)):
                continue
            for case in cases:
                results[(fixture["version"], target, case["case"])] = await replay(fixture, target, case)
    return results


class GoldenRequestsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixtures = load_fixtures()
        cls.replays = asyncio.run(replay_all(cls.fixtures))

    def test_fixtures_cover_the_published_versions(self):
        versions = [f["version"] for f in self.fixtures]
        for version in ("0.1.4", "0.1.6", "0.2.0"):
            self.assertIn(version, versions)
        # The versions most integrations run: every one of their calls is replayed.
        for fixture in self.fixtures:
            if fixture["version"] in ("0.1.6", "0.2.0"):
                for target, cases in fixture["methods"].items():
                    for case in cases:
                        self.assertIn((fixture["version"], target, case["case"]), self.replays)

    def test_every_published_call_sends_the_same_requests(self):
        for fixture in self.fixtures:
            for target, cases in fixture["methods"].items():
                for case in cases:
                    key = (fixture["version"], target, case["case"])
                    if key not in self.replays:
                        continue  # a method the current client lost: test_surface.py names it
                    sent, error = self.replays[key]
                    with self.subTest(version=fixture["version"], method=target, case=case["case"]):
                        self.assertEqual(
                            (error or "").split(":", 1)[0],
                            (case.get("error") or "").split(":", 1)[0],
                            f"the call raised {error!r}, the published version {case.get('error')!r}",
                        )
                        problems = check(fixture, target, case, sent)
                        if problems:
                            self.fail("\n".join([f"{fixture['version']} {target}({case['args']}, {case['kwargs']}):"] + problems))

    def test_every_known_difference_matches_a_published_call(self):
        for target, (since, reason, _) in KNOWN_DIFFERENCES.items():
            with self.subTest(method=target):
                self.assertTrue(reason)
                older = [f for f in self.fixtures if version_key(f["version"]) < version_key(since) and target in f["methods"]]
                self.assertTrue(older, f"no published version before {since} calls {target}: remove the entry")

    def test_known_difference_checks_catch_a_wrong_request(self):
        # The checks of KNOWN_DIFFERENCES must not pass whatever the client sends.
        for target, (since, _, rule) in KNOWN_DIFFERENCES.items():
            fixture = next(f for f in self.fixtures if version_key(f["version"]) < version_key(since) and target in f["methods"])
            case = fixture["methods"][target][0]
            sent, _ = self.replays[(fixture["version"], target, case["case"])]
            old, new = canonical_pair(case["requests"], sent)
            with self.subTest(method=target):
                self.assertEqual(check(fixture, target, case, sent), [])
                wrong = copy.deepcopy(new)
                wrong[0]["path"] = wrong[0]["path"] + ["elsewhere"]
                self.assertTrue(rule(old, wrong, fixture))


if __name__ == "__main__":
    unittest.main()
