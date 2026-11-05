"""
Return types and docs links of the client methods, checked against tests/openapi_ops.json.

For each operation the snapshot records its operationId, its tag and the Python type of its
answer, written as scripts/generate_types.py writes it ("EmailSearchResponse",
"List[NotificationOut]", "bytes" for a file, "Any" without a body). Here:

- the method calling an operation (OPERATIONS of tests/test_contract.py) is annotated with that
  type, unless it returns something else at runtime: RUNTIME_RETURNS says what and why;
- the other public methods (helpers, deprecated aliases) return what HELPERS says;
- every public method that calls the API ends its docstring with one line per operation:
  "API: <operationId>, https://docs.leakradar.io/#tag/<tag slug>/<operationId>", the anchor
  of that operation on the docs page;
- typing.get_type_hints() resolves every public method of LeakRadarClient, AsyncLeakRadar and
  LeakRadar, and the synchronous LeakRadar (generated from LeakRadarClient) has the same types and
  API lines, except that paginate returns an Iterator instead of an AsyncIterator.

Refresh the snapshot with `python scripts/update_openapi_snapshot.py` and the types with
`python scripts/generate_types.py`, then run `python -m unittest discover -s tests`.
"""

import inspect
import re
import typing
import unittest
from typing import Any, AsyncIterator, Dict, Iterator, List, Union

from test_contract import AHEAD_OF_SPEC, OPERATIONS, SPEC_OPERATIONS

from leakradar import AsyncLeakRadar, LeakRadar, LeakRadarClient
from leakradar import types as api_types

DOCS = "https://docs.leakradar.io/"
API_LINE = re.compile(r"API: (?P<operation_id>\S+), (?P<url>\S+)")
TYPING_NAMES = ("Any", "Dict", "List", "Literal", "Optional", "Union")

# Methods that do not return the documented answer of their operation, with the reason.
RUNTIME_RETURNS = {
    "get_raw_download_url": (
        str,
        "the operation answers a 307 redirect without a body: the method returns its Location URL",
    ),
    "export_domain_subdomains": (
        Union[bytes, List[Dict[str, str]]],
        "the spec documents a file; csv and txt come back as bytes, but format='json' is served as "
        "application/json, which the client decodes into a list of {'Subdomain': ...}",
    ),
    "export_domain_urls": (
        Union[bytes, List[Dict[str, str]]],
        "the spec documents a file; csv and txt come back as bytes, but format='json' is served as "
        "application/json, which the client decodes into a list of {'URL': ...}",
    ),
}

# Public methods that are no operation of OPERATIONS: (what they return, operations they call).
# The return is a type, the key of an operation whose answer they return, or (operation, key)
# for the type of the items listed under `key` in that answer.
HELPERS = {
    # Closes the HTTP clients: no API call.
    "aclose": (type(None), ()),
    # Calls the route it is given: no operation of its own.
    "request": (Any, ()),
    # Calls the listing method it is given (see that method's API line), yields its items.
    "paginate": (AsyncIterator[Dict[str, Any]], ()),
    "wait_for_task": ("GET /tasks/{task_id}", ("GET /tasks/{task_id}",)),
    "wait_for_unlocked_list_task": (
        "GET /profile/unlocked/list-tasks/{task_id}",
        ("GET /profile/unlocked/list-tasks/{task_id}",),
    ),
    "wait_for_export": (("GET /exports", "items"), ("GET /exports",)),
    # The file's bytes, the path it was written to, or None when there was nothing to export.
    "download_export": (Union[bytes, str, None], ("GET /exports",)),
    # Reads the redirect of the operation, then downloads the file from the storage it points to.
    "download_raw_file": (bytes, ("POST /profile/raw/downloads/{download_id}/file",)),
    # Deprecated aliases: the answer of their target.
    "notification_run_leaks": (
        "GET /notification_runs/{run_id}/items",
        ("GET /notification_runs/{run_id}/items",),
    ),
    "raw_export_preview": ("POST /search/raw/count", ("POST /search/raw/count",)),
}


def tag_slug(tag: str) -> str:
    """The anchor of a tag on the docs page: lowercase, each run of other characters than a-z and
    0-9 turned into one hyphen, no hyphen at either end ("Usage and Limits" -> "usage-and-limits")."""
    return re.sub(r"[^a-z0-9]+", "-", tag.lower()).strip("-")


def docs_url(operation: str) -> str:
    entry = SPEC_OPERATIONS[operation]
    return f"{DOCS}#tag/{tag_slug(entry['tag'])}/{entry['operation_id']}"


def evaluate(expression: str) -> Any:
    """The type a snapshot "response" names, read the way typing.get_type_hints() reads an annotation."""
    namespace: Dict[str, Any] = {name: getattr(typing, name) for name in TYPING_NAMES}
    namespace.update((name, getattr(api_types, name)) for name in api_types.__all__)

    def probe() -> None:
        pass

    probe.__annotations__ = {"return": expression}
    return typing.get_type_hints(probe, namespace)["return"]


def answer(operation: str) -> Any:
    return evaluate(SPEC_OPERATIONS[operation]["response"])


def expected_helper_return(returns: Any) -> Any:
    if isinstance(returns, str):
        return answer(returns)
    if isinstance(returns, tuple):
        operation, key = returns
        (item,) = typing.get_args(typing.get_type_hints(answer(operation))[key])
        return item
    return returns


def public_methods(cls: type) -> List[str]:
    return sorted(name for name in dir(cls) if not name.startswith("_") and inspect.isfunction(getattr(cls, name)))


def return_hint(name: str) -> Any:
    return typing.get_type_hints(getattr(LeakRadarClient, name))["return"]


def contains(hint: Any, part: Any) -> bool:
    """Whether a type is `part` or holds it (List[part], Union[..., part]...)."""
    return hint == part or any(contains(arg, part) for arg in typing.get_args(hint))


def calls(name: str) -> List[str]:
    """The snapshot operations a public method calls."""
    if name in HELPERS:
        return list(HELPERS[name][1])
    return sorted(op for op, (method, _) in OPERATIONS.items() if method == name and op not in AHEAD_OF_SPEC)


class SnapshotTest(unittest.TestCase):
    def test_every_operation_records_its_answer_and_its_anchor(self):
        for operation, entry in sorted(SPEC_OPERATIONS.items()):
            with self.subTest(operation=operation):
                self.assertRegex(entry.get("operation_id") or "", r"^[a-z][a-z0-9_]*$")
                self.assertTrue(tag_slug(entry.get("tag") or ""), "no tag")
                self.assertTrue(entry.get("response"), "no response type")
                evaluate(entry["response"])  # every name exists in leakradar.types

    def test_operation_ids_are_unique(self):
        ids = [entry["operation_id"] for entry in SPEC_OPERATIONS.values()]
        self.assertEqual(sorted(name for name in set(ids) if ids.count(name) > 1), [])


class AnnotationTest(unittest.TestCase):
    def test_every_public_method_is_classified(self):
        mapped = {method for method, _ in OPERATIONS.values()}
        self.assertEqual(sorted(set(public_methods(LeakRadarClient)) - mapped - set(HELPERS)), [])
        self.assertEqual(sorted(mapped & set(HELPERS)), [], "a helper is also mapped to an operation")
        self.assertEqual(sorted(set(HELPERS) - set(public_methods(LeakRadarClient))), [], "stale HELPERS entry")
        self.assertLessEqual(set(RUNTIME_RETURNS), mapped)

    def test_type_hints_resolve(self):
        for cls in (LeakRadarClient, AsyncLeakRadar, LeakRadar):
            for name in public_methods(cls):
                with self.subTest(cls=cls.__name__, method=name):
                    hints = typing.get_type_hints(getattr(cls, name))
                    self.assertIn("return", hints)

    def test_each_method_returns_the_answer_of_its_operation(self):
        for operation, (name, _) in sorted(OPERATIONS.items()):
            if operation not in SPEC_OPERATIONS:
                continue
            with self.subTest(operation=operation, method=name):
                if name in RUNTIME_RETURNS:
                    expected = RUNTIME_RETURNS[name][0]
                else:
                    expected = answer(operation)
                self.assertEqual(return_hint(name), expected)

    def test_runtime_exceptions_excuse_a_real_difference(self):
        for name, (runtime, reason) in sorted(RUNTIME_RETURNS.items()):
            with self.subTest(method=name):
                self.assertTrue(reason)
                documented = [answer(op) for op in calls(name)]
                self.assertTrue(documented, "not mapped to an operation of the snapshot")
                self.assertNotIn(runtime, documented, "the spec documents this type now: drop the exception")

    def test_helpers(self):
        for name, (returns, _calls) in sorted(HELPERS.items()):
            with self.subTest(method=name):
                self.assertEqual(return_hint(name), expected_helper_return(returns))

    def test_async_with_gives_back_the_client_class(self):
        # `async with AsyncLeakRadar() as client` types `client` as AsyncLeakRadar, not Any.
        hints = typing.get_type_hints(LeakRadarClient.__aenter__)
        self.assertIsInstance(hints["return"], typing.TypeVar)
        self.assertIs(hints["self"], hints["return"])
        # Unbound: the generated sync client, which is no LeakRadarClient, reuses it.
        self.assertIsNone(hints["return"].__bound__)

    def test_no_generic_dict_where_the_spec_names_a_type(self):
        generated = set(api_types.__all__)
        for operation, (name, _) in sorted(OPERATIONS.items()):
            if operation not in SPEC_OPERATIONS:
                continue
            names = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", SPEC_OPERATIONS[operation]["response"]))
            if not names & generated:
                continue
            with self.subTest(operation=operation, method=name):
                self.assertFalse(contains(return_hint(name), Dict[str, Any]), return_hint(name))


class SyncClientTest(unittest.TestCase):
    """LeakRadar, generated from LeakRadarClient by scripts/generate_sync.py, as mypy and IDEs read it."""

    # Public methods the generator renames: async name -> sync name.
    RENAMED = {"aclose": "close"}

    def sync_method(self, name: str) -> Any:
        return getattr(LeakRadar, self.RENAMED.get(name, name))

    def test_same_types_as_the_async_client(self):
        self.assertEqual(public_methods(LeakRadar), sorted(self.RENAMED.get(n, n) for n in public_methods(LeakRadarClient)))
        for name in public_methods(LeakRadarClient):
            with self.subTest(method=name):
                expected = typing.get_type_hints(getattr(LeakRadarClient, name))
                if name == "paginate":
                    # The async generator becomes a plain one, yielding the same items.
                    (item,) = typing.get_args(expected["return"])
                    self.assertEqual(expected["return"], AsyncIterator[item])
                    expected["return"] = Iterator[item]
                self.assertEqual(typing.get_type_hints(self.sync_method(name)), expected)

    def test_same_api_lines(self):
        def api_lines(method: Any) -> List[str]:
            return [line for line in (inspect.getdoc(method) or "").splitlines() if line.startswith("API:")]

        for name in public_methods(LeakRadarClient):
            with self.subTest(method=name):
                self.assertEqual(api_lines(self.sync_method(name)), api_lines(getattr(LeakRadarClient, name)))

    def test_with_gives_back_the_client_class(self):
        # `with LeakRadar() as lr` types `lr` as LeakRadar, not Any.
        hints = typing.get_type_hints(LeakRadar.__enter__)
        self.assertIsInstance(hints["return"], typing.TypeVar)
        self.assertIs(hints["self"], hints["return"])


class DocsLinkTest(unittest.TestCase):
    def test_every_public_method_names_the_operations_it_calls(self):
        by_id = {entry["operation_id"]: operation for operation, entry in SPEC_OPERATIONS.items()}
        for name in public_methods(LeakRadarClient):
            expected = calls(name)
            with self.subTest(method=name):
                lines = (inspect.getdoc(getattr(LeakRadarClient, name)) or "").splitlines()
                api = [line for line in lines if line.startswith("API:")]
                if not expected:
                    self.assertEqual(api, [], "this method calls no operation of its own")
                    continue
                # One line per operation, closing the docstring after a blank line.
                self.assertTrue(api, "no API line")
                self.assertEqual(lines[-len(api):], api, "the API lines must end the docstring")
                self.assertEqual(lines[-len(api) - 1], "", "a blank line before the API lines")
                named = []
                for line in api:
                    match = API_LINE.fullmatch(line)
                    self.assertIsNotNone(match, line)
                    operation = by_id.get(match.group("operation_id"))
                    self.assertIsNotNone(operation, f"{match.group('operation_id')} is not in the snapshot")
                    self.assertEqual(match.group("url"), docs_url(operation))
                    named.append(operation)
                self.assertEqual(sorted(named), sorted(expected))

    def test_slug_rule(self):
        self.assertEqual(tag_slug("Search Email"), "search-email")
        self.assertEqual(tag_slug("Usage and Limits"), "usage-and-limits")
        self.assertEqual(tag_slug("Dark Web Search"), "dark-web-search")
        self.assertEqual(docs_url("GET /tasks/{task_id}"), "https://docs.leakradar.io/#tag/tasks/get_task_status")


if __name__ == "__main__":
    unittest.main()
