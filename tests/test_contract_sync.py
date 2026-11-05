"""
The synchronous client LeakRadar keeps the contract of LeakRadarClient, the class it is generated
from (scripts/generate_sync.py):

- the same methods, with the same parameters (names, kinds, defaults, order) and the same return
  types (AsyncIterator[X] becomes Iterator[X]), and no public method of its own;
- the same requests for every operation of tests/test_contract.py (OPERATIONS, SAMPLES);
- the same requests for every call recorded from a published version (tests/compat/fixtures,
  read at test time: the compat fixtures may gain cases).
"""

import asyncio
import contextlib
import copy
import inspect
import os
import unittest
import warnings
from unittest import mock

import httpx
import test_contract
from compat import test_golden_requests as golden

from leakradar import LeakRadar, LeakRadarClient

# Async name -> sync name, for what the generator renames.
RENAMED = {"__aenter__": "__enter__", "__aexit__": "__exit__", "aclose": "close"}
# How the annotations of the async class read in the sync one.
ANNOTATIONS = (("AsyncIterator", "Iterator"), ("AsyncBaseTransport", "BaseTransport"), ("AsyncClient", "Client"))
# Class attributes that differ on purpose (dunder attributes are not compared: line numbers...).
OWN_ATTRIBUTES = {"_READ_ENV"}


def setUpModule():
    # LeakRadar reads these, LeakRadarClient does not: neither may differ because of the machine.
    patcher = mock.patch.dict(os.environ)
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)
    for name in ("LEAKRADAR_API_KEY", "LEAKRADAR_BASE_URL"):
        os.environ.pop(name, None)


def methods(cls):
    """Name -> (kind, function) of every method the class defines itself."""
    found = {}
    for name, value in vars(cls).items():
        if isinstance(value, (staticmethod, classmethod)):
            found[name] = (type(value).__name__, value.__func__)
        elif inspect.isfunction(value):
            found[name] = ("method", value)
    return found


def as_sync(annotation):
    """The annotation of the async class, as the sync class must write it."""
    text = annotation if isinstance(annotation, str) else repr(annotation)
    for old, new in ANNOTATIONS:
        text = text.replace(old, new)
    return text


def describe(function):
    """What a caller relies on: each parameter (name, kind, default, annotation) and the return type."""
    signature = inspect.signature(function)
    parameters = [
        (p.name, p.kind.name, repr(p.default), as_sync(p.annotation)) for p in signature.parameters.values()
    ]
    return parameters, as_sync(signature.return_annotation)


class ParityTest(unittest.TestCase):
    def test_every_method_with_the_same_signature(self):
        async_methods = methods(LeakRadarClient)
        sync_methods = methods(LeakRadar)
        self.assertEqual(
            sorted(RENAMED.get(name, name) for name in async_methods), sorted(sync_methods), "methods of one class only"
        )
        for name, (kind, function) in sorted(async_methods.items()):
            sync_name = RENAMED.get(name, name)
            sync_kind, sync_function = sync_methods[sync_name]
            with self.subTest(method=name):
                self.assertEqual(sync_kind, kind)
                self.assertEqual(describe(sync_function), describe(function))
                self.assertFalse(inspect.iscoroutinefunction(sync_function) or inspect.isasyncgenfunction(sync_function))
                self.assertEqual(inspect.isgeneratorfunction(sync_function), inspect.isasyncgenfunction(function))
                if function.__doc__:
                    self.assertTrue(sync_function.__doc__, "the docstring is kept")

    def test_public_surface(self):
        def public(cls, renamed=None):
            names = {name for name in dir(cls) if not name.startswith("_") and callable(getattr(cls, name))}
            return {(renamed or {}).get(name, name) for name in names}

        self.assertEqual(public(LeakRadar), public(LeakRadarClient, RENAMED))
        self.assertIn("request", public(LeakRadar))
        self.assertIn("close", public(LeakRadar))

    def test_same_class_attributes(self):
        def attributes(cls):
            return {
                name: value
                for name, value in vars(cls).items()
                if name not in OWN_ATTRIBUTES
                and not (name.startswith("__") and name.endswith("__"))
                and not inspect.isfunction(value)
                and not isinstance(value, (staticmethod, classmethod))
            }

        self.assertEqual(attributes(LeakRadar), attributes(LeakRadarClient))
        self.assertIs(LeakRadar._READ_ENV, True)


# -------------------------
# The operations of tests/test_contract.py
# -------------------------


def operation_calls():
    """(operation, variant, method, kwargs): the smallest call of each operation, and the call with
    every spec field filled as tests/test_contract.py builds it."""
    calls = []
    for operation, (method_name, base_kwargs) in sorted(test_contract.OPERATIONS.items()):
        calls.append((operation, "smallest", method_name, copy.deepcopy(base_kwargs)))
        if operation not in test_contract.SPEC_OPERATIONS:
            continue
        arguments, var_keyword = test_contract._arguments(method_name)
        _path, query, body = test_contract._spec_fields(operation)
        skipped = test_contract.PLACEMENT_SKIP.get(operation, set())
        body_dict = next((n for n in test_contract.BODY_DICT_ARGS if body and n in arguments and n not in body), None)
        kwargs = copy.deepcopy(base_kwargs)
        if body_dict:
            kwargs[body_dict] = dict(kwargs.get(body_dict) or {})
        for field in query + body:
            if field in skipped:
                continue
            argument = test_contract.RENAMED.get(field, field)
            if argument in kwargs:
                continue
            sample = copy.deepcopy(test_contract.SAMPLES.get(argument, "sample"))
            if argument in arguments:
                kwargs[argument] = sample
            elif body_dict and field in body:
                kwargs[body_dict].setdefault(field, sample)
            elif var_keyword:
                kwargs[argument] = sample
        calls.append((operation, "every field", method_name, kwargs))
    return calls


def records(requests):
    return [golden.record(request) for request in requests]


async def async_requests(calls):
    seen = []
    client = LeakRadarClient(token="t", transport=httpx.MockTransport(test_contract._handler(seen)))
    sent = []
    try:
        for _operation, _variant, method_name, kwargs in calls:
            del seen[:]
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                await getattr(client, method_name)(**copy.deepcopy(kwargs))
            sent.append(records(seen))
    finally:
        await client.aclose()
    return sent


def sync_requests(calls):
    seen = []
    sent = []
    with LeakRadar(token="t", transport=httpx.MockTransport(test_contract._handler(seen))) as client:
        for _operation, _variant, method_name, kwargs in calls:
            del seen[:]
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                getattr(client, method_name)(**copy.deepcopy(kwargs))
            sent.append(records(seen))
    return sent


class OperationsTest(unittest.TestCase):
    def test_every_operation_sends_the_same_request(self):
        calls = operation_calls()
        self.assertGreaterEqual(len(calls), len(test_contract.OPERATIONS))
        expected = asyncio.run(async_requests(calls))
        actual = sync_requests(calls)
        for (operation, variant, method_name, _kwargs), old, new in zip(calls, expected, actual):
            with self.subTest(operation=operation, variant=variant, method=method_name):
                self.assertEqual(len(new), 1)
                self.assertEqual(new, old)


# -------------------------
# The calls recorded from the published versions
# -------------------------


@contextlib.contextmanager
def every_sync_client_on(transport):
    """Route every httpx.Client built meanwhile (a client a method creates lazily) to `transport`."""
    original = httpx.Client

    class OnTransport(original):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    with mock.patch.object(httpx, "Client", OnTransport):
        yield


def replay_sync(fixture, target, case):
    """(requests sent, error raised or None) when LeakRadar runs the recorded call."""
    sent = []

    def handle(request):
        sent.append(golden.record(request))
        if request.url.host == "api.leakradar.io" and golden.DOWNLOAD_FILE.match(request.url.path):
            return httpx.Response(302, headers={"Location": fixture["storage_url"]})
        return httpx.Response(200, json={})

    transport = httpx.MockTransport(handle)
    with every_sync_client_on(transport):
        client = LeakRadar(token=fixture["token"], transport=transport)
        try:
            func = client._client.request if target == "_client.request" else getattr(client, RENAMED.get(target, target))
            args = golden.resolve(copy.deepcopy(case["args"]), client)
            kwargs = golden.resolve(copy.deepcopy(case["kwargs"]), client)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                result = func(*args, **kwargs)
                if inspect.isgenerator(result):
                    for _ in result:
                        pass
            error = None
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        finally:
            client.close()
    return sent, error


async def replay_async(cases):
    return [await golden.replay(fixture, target, case) for fixture, target, case in cases]


class GoldenRequestsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = []
        for fixture in golden.load_fixtures():
            for target, recorded in fixture["methods"].items():
                if target != "_client.request" and not callable(getattr(LeakRadarClient, target, None)):
                    continue  # a method the async client lost: tests/compat/test_surface.py names it
                cls.cases += [(fixture, target, case) for case in recorded]
        cls.expected = asyncio.run(replay_async(cls.cases))
        cls.actual = [replay_sync(fixture, target, case) for fixture, target, case in cls.cases]

    def test_fixtures_are_replayed(self):
        versions = {fixture["version"] for fixture, _, _ in self.cases}
        self.assertLessEqual({"0.1.4", "0.1.6", "0.2.0"}, versions)
        self.assertTrue(any(target == "_request" for _, target, _ in self.cases), "internals called in production")

    def test_every_published_call_sends_the_same_requests(self):
        self.assertEqual(len(self.actual), len(self.cases))
        self.assertGreater(sum(len(sent) for sent, _ in self.actual), len(self.cases) // 2, "the replays send requests")
        for (fixture, target, case), (old_sent, old_error), (new_sent, new_error) in zip(self.cases, self.expected, self.actual):
            with self.subTest(version=fixture["version"], method=target, case=case["case"]):
                self.assertEqual(new_error, old_error)
                self.assertEqual(new_sent, old_sent)


if __name__ == "__main__":
    unittest.main()
