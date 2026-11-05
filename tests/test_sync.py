"""
The synchronous client LeakRadar (leakradar/_sync_client.py, generated from LeakRadarClient by
scripts/generate_sync.py): configuration, errors, retries and the gate its threads share, the
helpers, and the generated file being the one the generator gives for leakradar/client.py.
"""

import asyncio
import contextlib
import importlib.util
import inspect
import io
import os
import pathlib
import stat
import sys
import tempfile
import threading
import time
import unittest
import warnings
from unittest import mock

import httpx
from test_client import ENCODED_ID, HOSTILE_ID, ID_ROUTES

import leakradar
import leakradar._sync_client
from leakradar import (
    ConflictError,
    ExportFailedError,
    ForbiddenError,
    LeakRadar,
    LeakRadarAPIError,
    LeakRadarClient,
    NotFoundError,
    ServiceUnavailableError,
    TaskFailedError,
    TooManyRequestsError,
    ValidationError,
    WaitTimeoutError,
    __version__,
)

ROOT = pathlib.Path(__file__).resolve().parent.parent
HERE = os.path.normcase(os.path.abspath(__file__))
ENVIRONMENT = ("LEAKRADAR_API_KEY", "LEAKRADAR_BASE_URL")


def ok(body=None, **kwargs):
    return httpx.Response(200, json={"ok": True} if body is None else body, **kwargs)


def refusal(reason, retry_after=1, status=429):
    """A rate limit refusal in the API's unified format."""
    error = {"code": "rate_limited", "reason": reason, "scope": "credential", "family": "search", "retry_after": retry_after}
    headers = {"Retry-After": str(retry_after), "X-LR-RateLimit-Reason": reason}
    return httpx.Response(status, json={"detail": f"Refused ({reason}).", "error": error}, headers=headers)


def search_unavailable(retry_after="2"):
    return httpx.Response(
        503, json={"detail": "Search temporarily unavailable", "code": "search_unavailable"}, headers={"Retry-After": retry_after}
    )


def without_environment(test):
    """Run `test` with neither LEAKRADAR_API_KEY nor LEAKRADAR_BASE_URL set: LeakRadar reads them."""
    patcher = mock.patch.dict(os.environ)
    patcher.start()
    test.addCleanup(patcher.stop)
    for name in ENVIRONMENT:
        os.environ.pop(name, None)


class FakeTime:
    """Monotonic clock and sleep of the client: sleeping moves the clock, nothing really waits."""

    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += max(0.0, seconds)


class SyncTestCase(unittest.TestCase):
    max_retries = 3

    def setUp(self):
        without_environment(self)
        # Each request takes the next answer (a response, or an exception to raise), then
        # falls back to self.route(request), then to a 200 {"ok": true}.
        self.answers = []
        self.route = None
        self.sent = []
        self.time = FakeTime()
        self.lr = self.make_client(token="tok_ABC123")

    def handle(self, request):
        self.sent.append(request)
        if self.answers:
            answer = self.answers.pop(0)
        elif self.route is not None:
            answer = self.route(request)
        else:
            answer = ok()
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def make_client(self, client_class=LeakRadar, **kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(self.handle))
        kwargs.setdefault("max_retries", self.max_retries)
        client = client_class(**kwargs)
        self.addCleanup(client.close)
        client._monotonic = self.time.monotonic
        client._sleep = self.time.sleep
        client._random = lambda: 0.5
        return client

    def query(self, index=0):
        return dict(self.sent[index].url.params)

    def body(self, index=0):
        content = self.sent[index].content
        return leakradar.client._json.loads(content) if content else None

    def assertSleeps(self, expected):
        self.assertEqual([round(s, 3) for s in self.time.sleeps], expected)

    def assertFromHere(self, warning):
        """The warning points at the caller's code (this file), not at the client."""
        self.assertEqual(os.path.normcase(os.path.abspath(warning.filename)), HERE)

    def spy_clients(self):
        """Record the keyword arguments of every httpx.Client created."""
        created = []
        real_init = httpx.Client.__init__

        def init(client, *args, **kwargs):
            created.append(dict(kwargs))
            real_init(client, *args, **kwargs)

        patcher = mock.patch.object(httpx.Client, "__init__", init)
        patcher.start()
        self.addCleanup(patcher.stop)
        return created


# -------------------------
# The class
# -------------------------


class ClassTest(unittest.TestCase):
    def test_exported_and_synchronous(self):
        self.assertIs(leakradar.LeakRadar, leakradar._sync_client.LeakRadar)
        self.assertIn("LeakRadar", leakradar.__all__)
        self.assertEqual(leakradar.__all__, sorted(leakradar.__all__))
        self.assertTrue(LeakRadar._READ_ENV)
        self.assertFalse(LeakRadarClient._READ_ENV, "LeakRadarClient keeps its 0.x defaults")
        self.assertEqual(LeakRadar.BASE_URL, LeakRadarClient.BASE_URL)
        self.assertFalse(inspect.iscoroutinefunction(LeakRadar.get_profile))
        self.assertTrue(inspect.isgeneratorfunction(LeakRadar.paginate))
        self.assertIn("with LeakRadar() as lr:", LeakRadar.__doc__)
        lr = LeakRadar(token="t")
        self.addCleanup(lr.close)
        self.assertIs(lr._sleep, time.sleep, "the asynchronous client's anyio.sleep becomes time.sleep")

    def test_errors_helpers_and_aliases_are_those_of_leakradar_client(self):
        shared = [
            name
            for name, value in vars(leakradar._sync_client).items()
            if name in vars(leakradar.client) and not inspect.ismodule(value) and not (name.startswith("__") and name != "__version__")
        ]
        self.assertIn("NotFoundError", shared)
        for name in shared:
            with self.subTest(name=name):
                self.assertIs(getattr(leakradar._sync_client, name), getattr(leakradar.client, name))


# -------------------------
# Configuration
# -------------------------


class ConfigurationTest(SyncTestCase):
    ENV = {"LEAKRADAR_API_KEY": "env-key", "LEAKRADAR_BASE_URL": "https://staging.example.test/"}

    def call(self, client):
        self.sent.clear()
        client.get_profile()
        return self.sent[0]

    def test_headers_auth_and_user_agent(self):
        self.assertEqual(self.lr.get_profile(), {"ok": True})
        request = self.sent[0]
        self.assertEqual((request.method, str(request.url)), ("GET", "https://api.leakradar.io/profile"))
        self.assertEqual(request.headers["Authorization"], "Bearer tok_ABC123")
        self.assertEqual(request.headers["User-Agent"], f"LeakRadar-Python-Client/{__version__}")
        self.assertEqual(request.headers["Accept"], "*/*")
        self.lr.token = "tok_NEW"
        self.assertEqual(self.call(self.lr).headers["Authorization"], "Bearer tok_NEW", "a token set later is used")

    def test_the_token_is_sent_without_surrounding_whitespace(self):
        client = self.make_client(token="tok_ABC123\n")
        self.assertEqual(self.call(client).headers["Authorization"], "Bearer tok_ABC123")
        self.assertEqual(client.token, "tok_ABC123\n", "kept as given")
        client.token = " tok_NEW\n"
        self.assertEqual(self.call(client).headers["Authorization"], "Bearer tok_NEW")
        client.token = "  "
        self.assertNotIn("Authorization", self.call(client).headers)

    def test_reads_the_environment(self):
        os.environ.update(self.ENV)
        client = self.make_client()
        self.assertEqual((client.token, client.base_url), ("env-key", "https://staging.example.test"))
        request = self.call(client)
        self.assertEqual(str(request.url), "https://staging.example.test/profile")
        self.assertEqual(request.headers["Authorization"], "Bearer env-key")

    def test_arguments_win_and_an_empty_token_is_anonymous(self):
        os.environ.update(self.ENV)
        client = self.make_client(token="", base_url="https://gateway.example.test/lr/")
        request = self.call(client)
        self.assertEqual(str(request.url), "https://gateway.example.test/lr/profile")
        self.assertNotIn("Authorization", request.headers)

    def test_without_environment(self):
        client = self.make_client()
        self.assertEqual((client.token, client.base_url), (None, "https://api.leakradar.io"))
        self.assertNotIn("Authorization", self.call(client).headers)

    def test_verify_and_proxy_reach_httpx(self):
        created = self.spy_clients()
        client = LeakRadar(token="t", verify=False, proxy="http://proxy.example.test:3128")
        self.addCleanup(client.close)
        self.assertEqual((created[0]["verify"], created[0]["proxy"]), (False, "http://proxy.example.test:3128"))
        self.assertEqual(created[0]["base_url"], "https://api.leakradar.io")
        # httpx routes every request through a proxy mount when it gets one: never with a transport.
        self.make_client(proxy="http://proxy.example.test:3128", verify=False)
        self.assertNotIn("proxy", created[1])
        self.assertNotIn("verify", created[1])

    def test_raw_downloads_use_the_same_settings(self):
        self.answers.append(httpx.Response(302, headers={"Location": "https://files.example.test/raw/5"}))
        created = []
        storage = []
        real_init = httpx.Client.__init__

        def init(client, *args, **kwargs):
            created.append(dict(kwargs))
            if kwargs.get("transport") is None:
                # The anonymous client of the storage downloads: no network in tests.
                kwargs.pop("proxy", None)
                kwargs["transport"] = httpx.MockTransport(lambda r: storage.append(r) or httpx.Response(200, content=b"RAW"))
            real_init(client, *args, **kwargs)

        with mock.patch.object(httpx.Client, "__init__", init):
            client = self.make_client(token="tok_ABC123", verify=False, proxy="http://proxy.example.test:3128")
            self.assertEqual(client.download_raw_file(5), b"RAW")
        self.assertEqual((created[-1]["verify"], created[-1]["proxy"]), (False, "http://proxy.example.test:3128"))
        self.assertNotIn("Authorization", storage[0].headers)
        self.assertEqual(self.sent[0].headers["Authorization"], "Bearer tok_ABC123")

    def test_verify_none_is_the_default_and_other_types_are_refused(self):
        # httpx 0.27 reads any falsy verify as "no verification": None must mean the default.
        self.answers.append(httpx.Response(302, headers={"Location": "https://files.example.test/raw/5"}))
        created = []
        real_init = httpx.Client.__init__

        def init(client, *args, **kwargs):
            created.append(dict(kwargs))
            if kwargs.get("transport") is None and "base_url" not in kwargs:
                # The anonymous client of the storage downloads: no network in tests.
                kwargs["transport"] = httpx.MockTransport(lambda r: httpx.Response(200, content=b"RAW"))
            real_init(client, *args, **kwargs)

        with mock.patch.object(httpx.Client, "__init__", init):
            client = LeakRadar(token="t", verify=None)
            self.addCleanup(client.close)
            client = self.make_client(token="t", verify=None)
            self.assertEqual(client.download_raw_file(5), b"RAW")
            for value in (0, b"ca.pem"):
                with self.subTest(verify=value), self.assertRaises(TypeError):
                    LeakRadar(token="t", verify=value)
        self.assertIs(created[0]["verify"], True, "the API client")
        self.assertIs(created[-1]["verify"], True, "the client of the raw file downloads")
        self.assertEqual(len(created), 3, "nothing is created for a refused value")

    def test_constructor_checks(self):
        for kwargs in ({"max_retries": -1}, {"max_retries": 1.5}, {"max_retries": True}, {"max_retry_wait": -1}):
            with self.subTest(**kwargs), self.assertRaises(ValueError):
                LeakRadar(token="t", **kwargs)


# -------------------------
# Errors
# -------------------------


class ErrorTest(SyncTestCase):
    def test_status_codes_and_str(self):
        self.answers.append(httpx.Response(404, json={"detail": "Not found"}))
        with self.assertRaises(NotFoundError) as caught:
            self.lr.get_domain_report("example.com")
        error = caught.exception
        self.assertEqual((error.status_code, error.detail, str(error)), (404, "Not found", "API Error 404: Not found"))
        self.assertIsInstance(error, LeakRadarAPIError)

    def test_request_id_conflicts_and_message(self):
        self.answers.append(httpx.Response(500, json={"detail": "Internal Server Error", "id": "6f1c2a90"}))
        with self.assertRaises(LeakRadarAPIError) as caught:
            self.lr.get_profile()
        self.assertEqual(caught.exception.request_id, "6f1c2a90")
        self.assertEqual(len(self.sent), 1, "a 500 is not retried")
        conflicts = [{"value": "example.com", "existing": {"id": 4}}]
        detail = {"code": "monitor_overlap", "message": "Already monitored on this channel.", "conflicts": conflicts}
        self.answers.append(httpx.Response(409, json={"detail": detail}))
        with self.assertRaises(ConflictError) as caught:
            self.lr.create_notification({"type": "domain", "value": "example.com", "method_id": 1})
        self.assertEqual((caught.exception.code, caught.exception.conflicts), ("monitor_overlap", conflicts))
        self.answers.append(httpx.Response(422, json={"detail": [{"loc": ["query", "page_size"], "msg": "too big"}]}))
        with self.assertRaises(ValidationError) as caught:
            self.lr.list_exports(page_size=1000)
        self.assertEqual(caught.exception.message, "query.page_size: too big")


# -------------------------
# Retries and the gate
# -------------------------


class RetryTest(SyncTestCase):
    def test_rate_refusal_is_retried_through_the_gate(self):
        self.answers.append(refusal("rate", retry_after=2))
        self.assertEqual(self.lr.search_email("john@example.com"), {"ok": True})
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.sent[0].content, self.sent[1].content, "the same body is sent again")
        self.assertSleeps([2.15])
        self.assertEqual(self.lr._gate_until, 1002.0, "the gate closed for the announced wait")

    def test_concurrency_busy_and_a_503_with_retry_after(self):
        self.answers.extend([refusal("concurrency", retry_after=2), refusal("busy", retry_after=5, status=503)])
        self.assertEqual(self.lr.raw_search(q="admin@example.com"), {"ok": True})
        self.assertEqual(len(self.sent), 3)
        self.sent.clear()
        self.answers.append(search_unavailable("2"))
        self.assertEqual(self.lr.raw_search(q="admin@example.com"), {"ok": True})
        self.assertEqual([r.method for r in self.sent], ["POST", "POST"])

    def test_503_after_a_synchronous_unlock_is_raised(self):
        # The API can answer it once the records are unlocked and the points spent.
        self.answers.append(search_unavailable("2"))
        with self.assertRaises(ServiceUnavailableError):
            self.lr.unlock_email_leaks("john@example.com", max_leaks=100)
        self.assertEqual([(r.method, r.url.path) for r in self.sent], [("POST", "/search/email/unlock")])
        self.assertEqual(self.time.sleeps, [])
        self.sent.clear()
        # A rate limit refusal comes before the unlock runs; a task is queued after the 503.
        self.answers.extend([refusal("busy", retry_after=5, status=503), ok(), search_unavailable("2"), ok()])
        self.assertEqual(self.lr.unlock_email_leaks("john@example.com"), {"ok": True})
        self.lr.queue_email_unlock_task("john@example.com")
        self.assertEqual(
            [r.url.path for r in self.sent], ["/search/email/unlock"] * 2 + ["/search/email/unlock/task"] * 2
        )

    def test_refusals_not_worth_waiting_for(self):
        for answer in (refusal("daily_quota", retry_after=5400), refusal("paused"), refusal("rate", retry_after=61)):
            with self.subTest(answer=answer.headers["x-lr-ratelimit-reason"]):
                self.sent.clear()
                self.answers.append(answer)
                with self.assertRaises(TooManyRequestsError):
                    self.lr.get_profile()
                self.assertEqual(len(self.sent), 1)
        self.answers.append(httpx.Response(503, json={"detail": "Under maintenance"}))
        with self.assertRaises(ServiceUnavailableError):
            self.lr.get_profile()
        self.assertEqual(self.time.sleeps, [])

    def test_retries_stop_after_max_retries(self):
        self.answers.extend(refusal("rate") for _ in range(5))
        with self.assertRaises(TooManyRequestsError):
            self.lr.get_profile()
        self.assertEqual(len(self.sent), 4, "first attempt + 3 retries")

    def test_connection_errors_are_retried_for_every_method(self):
        calls = [
            ("POST", lambda: self.lr.search_email("john@example.com")),
            ("DELETE", lambda: self.lr.delete_notification(4)),
            ("GET", lambda: self.lr.get_profile()),
        ]
        for verb, call in calls:
            for error_class in (httpx.ConnectError, httpx.ConnectTimeout):
                with self.subTest(verb=verb, error=error_class.__name__):
                    self.sent.clear()
                    self.time.sleeps.clear()
                    self.answers.extend([error_class("refused"), ok()])
                    self.assertEqual(call(), {"ok": True})
                    self.assertEqual([r.method for r in self.sent], [verb, verb])
                    self.assertSleeps([0.575])
        self.assertEqual(self.lr._gate_until, 0.0, "a network failure does not close the gate")

    def test_the_original_exception_once_the_budget_is_spent(self):
        errors = [httpx.ConnectError(f"refused {n}") for n in range(4)]
        self.answers.extend(errors)
        with self.assertRaises(httpx.ConnectError) as caught:
            self.lr.get_profile()
        self.assertIs(caught.exception, errors[-1], "raised as is, not wrapped")
        self.assertSleeps([0.575, 1.1, 2.15])

    def test_502_and_504_are_retried_on_get_only(self):
        self.answers.extend([httpx.Response(502, text="Bad gateway"), httpx.Response(504, text="Gateway timeout"), ok()])
        self.assertEqual(self.lr.get_profile(), {"ok": True})
        self.assertEqual(len(self.sent), 3)
        self.assertSleeps([0.575, 1.1])
        self.sent.clear()
        self.answers.append(httpx.Response(502, text="Bad gateway"))
        with self.assertRaises(LeakRadarAPIError) as caught:
            self.lr.search_email("john@example.com")
        self.assertEqual((caught.exception.status_code, len(self.sent)), (502, 1))

    def test_read_errors_are_retried_on_get_but_never_on_post(self):
        for error_class in (httpx.ReadTimeout, httpx.ReadError, httpx.RemoteProtocolError):
            with self.subTest(error=error_class.__name__):
                self.sent.clear()
                self.answers.extend([error_class("lost"), ok()])
                self.assertEqual(self.lr.get_profile(), {"ok": True})
                self.assertEqual(len(self.sent), 2)
                self.sent.clear()
                self.answers.append(error_class("lost"))
                with self.assertRaises(error_class):
                    self.lr.search_email("john@example.com")
                self.assertEqual(len(self.sent), 1, "a POST may have run: never replayed")

    def test_rate_limits_skips_the_gate(self):
        self.lr._close_gate(30)
        self.assertEqual(self.lr.rate_limits(), {"ok": True})
        self.assertEqual(self.time.sleeps, [])


class NoRetryTest(SyncTestCase):
    max_retries = 0

    def test_max_retries_zero_turns_every_retry_off(self):
        answers = [
            httpx.ConnectError("refused"),
            httpx.ReadTimeout("slow"),
            httpx.Response(502, text="Bad gateway"),
            search_unavailable("1"),
            refusal("rate"),
        ]
        for answer in answers:
            with self.subTest(answer=repr(answer)):
                self.sent.clear()
                self.answers.append(answer)
                with self.assertRaises((httpx.HTTPError, LeakRadarAPIError)):
                    self.lr.get_profile()
                self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.time.sleeps, [])
        self.assertEqual(self.lr._gate_until, 0.0)


class ThreadTest(unittest.TestCase):
    """Real threads and the real clock: a short Retry-After keeps the test fast."""

    def setUp(self):
        without_environment(self)

    def test_threads_share_the_client_and_its_gate(self):
        sent = []
        lock = threading.Lock()
        refused = threading.Event()

        def handle(request):
            with lock:
                sent.append((request.url.path, time.monotonic(), request.headers.get("Authorization")))
                first = len(sent) == 1
            if first:
                refused.set()
                return refusal("rate", retry_after=0.3)
            return ok()

        lr = LeakRadar(token="t", transport=httpx.MockTransport(handle))
        self.addCleanup(lr.close)
        lr._random = lambda: 0.0
        results = {}

        def call(name):
            results[name] = getattr(lr, name)()

        first = threading.Thread(target=call, args=("get_profile",))
        first.start()
        self.assertTrue(refused.wait(10))
        deadline = time.monotonic() + 10
        while lr._gate_until == 0.0 and time.monotonic() < deadline:
            time.sleep(0.001)
        gate = lr._gate_until
        self.assertGreater(gate, 0.0, "the refusal closed the gate")
        # Started while the gate is closed: it waits instead of collecting its own 429.
        second = threading.Thread(target=call, args=("get_stats",))
        second.start()
        first.join(10)
        second.join(10)
        self.assertEqual(results, {"get_profile": {"ok": True}, "get_stats": {"ok": True}})
        self.assertEqual(sorted(path for path, _, _ in sent), ["/profile", "/profile", "/stats"])
        for path, sent_at, authorization in sent[1:]:
            with self.subTest(path=path):
                self.assertGreaterEqual(sent_at, gate, "nothing leaves before the gate opens")
                self.assertEqual(authorization, "Bearer t")

    def test_many_threads_one_client(self):
        lr = LeakRadar(token="t", transport=httpx.MockTransport(lambda request: ok({"path": request.url.path})))
        self.addCleanup(lr.close)
        errors = []

        def work(number):
            try:
                for _ in range(20):
                    assert lr.get_domain_report(f"example{number}.com") == {"path": f"/search/domain/example{number}.com"}
            except Exception as exc:  # reported below, a thread cannot fail the test itself
                errors.append(exc)

        threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(errors, [])


class EventLoopTest(SyncTestCase):
    def test_works_inside_a_running_event_loop(self):
        # Jupyter, async frameworks: a sync client that ran its own loop would fail here.
        async def main():
            return self.lr.get_profile()

        self.assertEqual(asyncio.run(main()), {"ok": True})


# -------------------------
# Generic access and domains
# -------------------------


class RequestTest(SyncTestCase):
    def test_same_pipeline_as_the_methods(self):
        self.answers.extend([refusal("rate"), ok({"items": []})])
        self.assertEqual(self.lr.request("get", "/profile/usage", params={"a": 1, "b": None}), {"items": []})
        request = self.sent[-1]
        self.assertEqual((request.method, request.url.path, self.query(1)), ("GET", "/profile/usage", {"a": "1"}))
        self.assertEqual(request.headers["Authorization"], "Bearer tok_ABC123")
        self.answers.append(httpx.Response(200, content=b"a,b\n", headers={"content-type": "text/csv"}))
        self.assertEqual(self.lr.request("GET", "/file"), b"a,b\n")

    def test_only_the_api_host(self):
        self.lr.request("GET", "https://api.leakradar.io/profile")
        self.assertEqual(str(self.sent[0].url), "https://api.leakradar.io/profile")
        self.sent.clear()
        for url in ("https://evil.example/profile", "http://api.leakradar.io/profile", "https://api.leakradar.io:8443/x"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.lr.request("GET", url)
        self.assertEqual(self.sent, [])


class AppError(Exception):
    """An integration's own exception."""


class OverrideTest(SyncTestCase):
    """Subclasses overriding _request or _handle_error keep control, as on the asynchronous client."""

    def test_a_request_override_without_new_keywords(self):
        calls = []

        class Audited(LeakRadar):
            def _request(self, method, endpoint, *, params=None, json=None, headers=None, follow_redirects=False):
                calls.append((method, endpoint))
                return super()._request(
                    method, endpoint, params=params, json=json, headers=headers, follow_redirects=follow_redirects
                )

        client = self.make_client(Audited, token="t")
        client._close_gate(30)
        self.assertEqual(client.rate_limits(), {"ok": True})
        self.assertEqual(self.time.sleeps, [], "GET /rate-limits never waits for the gate")
        self.answers.append(exports(export("COMPLETED")))
        self.assertEqual(client.wait_for_export(7)["id"], 7)
        self.assertEqual(calls, [("GET", "/rate-limits"), ("GET", "/exports")])

    def test_handle_error_overrides(self):
        class Mapped(LeakRadar):
            def _handle_error(self, response):
                raise AppError(response.status_code)

        class NotFoundIsEmpty(LeakRadar):
            def _handle_error(self, response):
                if response.status_code != 404:
                    super()._handle_error(response)

        self.route = lambda request: httpx.Response(404, json={"detail": "Not Found"})
        with self.assertRaises(AppError):
            self.make_client(Mapped, token="t").get_profile()
        client = self.make_client(NotFoundIsEmpty, token="t")
        self.assertEqual(client.get_profile(), {"detail": "Not Found"})
        with self.assertRaises(LeakRadarAPIError) as caught:
            client.get_raw_download_url(55)
        self.assertEqual(caught.exception.detail, "Unexpected response while requesting raw download URL.")


class PathTest(SyncTestCase):
    def test_every_int_id_is_one_path_segment(self):
        self.route = lambda request: httpx.Response(302, headers={"Location": "https://files.example.test/raw/1"})
        for name, extra, route in ID_ROUTES:
            with self.subTest(method=name):
                self.sent.clear()
                getattr(self.lr, name)(HOSTILE_ID, *extra)
                self.assertEqual(len(self.sent), 1)
                self.assertEqual(self.sent[0].url.raw_path.split(b"?", 1)[0].decode("ascii"), route.format(ENCODED_ID))

    def test_dot_segments_are_refused(self):
        for call in (lambda: self.lr.get_task_status(".."), lambda: self.lr.set_unlocked_combolist_status(".", "new")):
            with self.assertRaises(ValueError):
                call()
        self.assertEqual(self.sent, [])
        self.lr.get_task_status("abc.def")
        self.assertEqual(self.sent[0].url.raw_path, b"/tasks/abc.def")


class DomainTest(SyncTestCase):
    def test_a_url_searches_its_domain_with_a_warning_at_the_caller(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            self.lr.get_domain_all("https://www.Example.com/fr/")
        self.assertEqual(self.sent[0].url.path, "/search/domain/example.com/all")
        self.assertEqual(len(caught), 1)
        self.assertIs(caught[0].category, UserWarning)
        self.assertIn("searching the domain 'example.com'", str(caught[0].message))
        self.assertFromHere(caught[0])

    def test_an_invalid_domain_sends_nothing(self):
        with self.assertRaises(ValueError):
            self.lr.get_domain_report("https://")
        self.assertEqual(self.sent, [])

    def test_the_mass_check_sends_its_entries_as_given(self):
        # The API reduces them itself; one junk entry must not fail the batch.
        entries = ["https://www.a-example.com/x", "B-Example.com", "#N/A"]
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            self.lr.domains_locked_exists(list(entries))
        self.assertEqual(self.body()["domains"], entries)


# -------------------------
# Tasks and exports
# -------------------------


def task(status=None, **fields):
    body = {"task_id": "42", "kind": "UNLOCK", "scope": "email", "total": 10, "updated": 0}
    if status is not None:
        body.update(status=status, running=status in ("QUEUED", "RUNNING"), completed=status == "COMPLETED")
    body.update(fields)
    return httpx.Response(200, json=body)


def exports(*items):
    return httpx.Response(200, json={"items": list(items), "total": len(items), "page": 1, "page_size": 20})


def export(status, export_id=7, filename=""):
    return {"id": export_id, "type": "email_export", "status": status, "filename": filename, "params": {}}


class WaitTest(SyncTestCase):
    def test_wait_for_task(self):
        self.answers.extend([task("QUEUED"), task("RUNNING", updated=5), task("COMPLETED", updated=10)])
        seen = []
        result = self.lr.wait_for_task({"task_id": 42, "status": "QUEUED"}, on_progress=seen.append)
        self.assertEqual((result["status"], result["updated"]), ("COMPLETED", 10))
        self.assertEqual([s["status"] for s in seen], ["QUEUED", "RUNNING", "COMPLETED"])
        self.assertEqual([r.url.path for r in self.sent], ["/tasks/42"] * 3)
        self.assertEqual(self.time.sleeps, [2.0, 2.0])

    def test_failed_task_and_timeout(self):
        error = {"code": "insufficient_points", "message": "Need 120 points, have 3"}
        self.answers.append(task("FAILED", error=error))
        with self.assertRaises(TaskFailedError) as caught:
            self.lr.wait_for_task("42")
        self.assertEqual((caught.exception.code, caught.exception.message), ("insufficient_points", "Need 120 points, have 3"))
        self.route = lambda request: task("RUNNING", updated=3)
        with self.assertRaises(WaitTimeoutError) as caught:
            self.lr.wait_for_unlocked_list_task("42", timeout=5)
        self.assertIsInstance(caught.exception, TimeoutError)
        self.assertEqual((caught.exception.waited, caught.exception.last["updated"]), (5.0, 3))
        self.assertEqual(self.time.sleeps, [2.0, 2.0, 1.0])
        self.assertEqual(self.sent[-1].url.path, "/profile/unlocked/list-tasks/42")

    def test_wait_for_export(self):
        url = "https://files.example.test/exports/7.zip"
        self.answers.extend([exports(export("PENDING")), exports(export("IN_PROGRESS")), exports(export("COMPLETED", filename=url))])
        result = self.lr.wait_for_export({"export_id": 7, "status": "PENDING"})
        self.assertEqual((result["status"], result["filename"]), ("COMPLETED", url))
        self.assertEqual(self.query(), {"page": "1", "page_size": "20", "export_id": "7"})
        self.assertEqual(self.time.sleeps, [3.0, 3.0])
        self.answers.append(exports(export("ERROR")))
        with self.assertRaises(ExportFailedError):
            self.lr.wait_for_export(7)
        self.answers.append(exports())
        with self.assertRaises(NotFoundError):
            self.lr.wait_for_export(7)

    def test_a_wrong_argument_is_named_by_its_type_only(self):
        records = [{"email": "john@example.com", "password": "s3cret"}]
        calls = [(self.lr.wait_for_task, records, "list"), (self.lr.wait_for_export, {"items": records}, "dict")]
        for call, argument, kind in calls:
            with self.subTest(method=call.__name__), self.assertRaises(ValueError) as caught:
                call(argument)
            self.assertIn(f"(got {kind})", str(caught.exception))
            self.assertNotIn("s3cret", str(caught.exception))
        self.assertEqual(self.sent, [])


class BrokenStream(httpx.SyncByteStream):
    def __iter__(self):
        yield b"partial"
        raise httpx.ReadError("connection reset")


class DownloadExportTest(SyncTestCase):
    STORAGE = "https://files.example.test/exports/7.zip?X-Amz-Signature=abc"

    def storage(self, request):
        if request.url.host == "files.example.test":
            return httpx.Response(200, content=b"PK\x03\x04zip", headers={"content-type": "application/zip"})
        return ok()

    def test_to_bytes_without_the_api_key_on_another_host(self):
        self.answers.append(exports(export("COMPLETED", filename=self.STORAGE)))
        self.route = self.storage
        self.assertEqual(self.lr.download_export(7), b"PK\x03\x04zip")
        self.assertEqual([r.url.host for r in self.sent], ["api.leakradar.io", "files.example.test"])
        self.assertEqual(self.sent[0].headers["Authorization"], "Bearer tok_ABC123")
        self.assertNotIn("Authorization", self.sent[1].headers)

    def test_to_a_file(self):
        self.route = self.storage
        with tempfile.TemporaryDirectory() as folder:
            target = os.path.join(folder, "export.zip")
            self.assertEqual(self.lr.download_export(export("COMPLETED", filename=self.STORAGE), target, chunk_size=2), target)
            with open(target, "rb") as handle:
                self.assertEqual(handle.read(), b"PK\x03\x04zip")
            self.assertEqual(os.listdir(folder), ["export.zip"])

    def test_a_failed_download_keeps_the_previous_file(self):
        self.route = lambda request: httpx.Response(200, stream=BrokenStream())
        with tempfile.TemporaryDirectory() as folder:
            target = os.path.join(folder, "export.zip")
            with open(target, "wb") as handle:
                handle.write(b"previous")
            with self.assertRaises(httpx.ReadError):
                self.lr.download_export(export("COMPLETED", filename=self.STORAGE), target)
            with open(target, "rb") as handle:
                self.assertEqual(handle.read(), b"previous")
            self.assertEqual(os.listdir(folder), ["export.zip"], "no temporary file left")

    def test_a_planted_part_file_and_a_relative_path(self):
        self.route = self.storage
        previous = os.getcwd()
        with tempfile.TemporaryDirectory() as folder:
            os.chdir(folder)
            try:
                with open("export.zip.part", "wb") as handle:
                    handle.write(b"planted")
                written = self.lr.download_export(export("COMPLETED", filename=self.STORAGE), "export.zip")
                self.assertEqual(written, "export.zip")
                with open("export.zip", "rb") as handle:
                    self.assertEqual(handle.read(), b"PK\x03\x04zip")
                with open("export.zip.part", "rb") as handle:
                    self.assertEqual(handle.read(), b"planted")
                self.assertEqual(sorted(os.listdir(".")), ["export.zip", "export.zip.part"])
                if os.name != "nt":
                    self.assertEqual(stat.S_IMODE(os.stat("export.zip").st_mode), 0o600)
            finally:
                os.chdir(previous)

    def test_nothing_to_export_and_errors(self):
        self.assertIsNone(self.lr.download_export(export("COMPLETED", filename="")))
        self.assertEqual(self.sent, [])
        self.answers.append(httpx.Response(403, text="<Error><Code>AccessDenied</Code></Error>"))
        with self.assertRaises(ForbiddenError):
            self.lr.download_export(export("COMPLETED", filename=self.STORAGE))


# -------------------------
# Pagination
# -------------------------


def listing(count, with_total=True, has_more=None):
    """A page-numbered listing of `count` items {"n": 0..count-1}."""

    def route(request):
        page = int(request.url.params.get("page", "1"))
        size = int(request.url.params.get("page_size", "100"))
        body = {"items": [{"n": n} for n in range((page - 1) * size, min(page * size, count))], "page": page, "page_size": size}
        if with_total:
            body["total"] = count
        if has_more is not None:
            body["has_more"] = has_more(page, size)
        return httpx.Response(200, json=body)

    return route


class PaginateTest(SyncTestCase):
    def pages(self):
        return [int(r.url.params["page"]) for r in self.sent]

    def test_a_plain_lazy_generator(self):
        self.route = listing(30)
        items = self.lr.paginate(self.lr.get_unlocked_leaks, page_size=10)
        self.assertTrue(inspect.isgenerator(items))
        self.assertEqual(self.sent, [], "nothing is sent before iterating")
        self.assertEqual(next(items), {"n": 0})
        self.assertEqual([item["n"] for item in items], list(range(1, 30)))
        self.assertEqual(self.pages(), [1, 2, 3], "stops once page * page_size reaches total")

    def test_stop_rules(self):
        self.route = listing(25, with_total=False)
        self.assertEqual(len(list(self.lr.paginate(self.lr.get_unlocked_leaks, page_size=10))), 25)
        self.assertEqual(self.pages(), [1, 2, 3], "a short page is the last")
        self.sent.clear()
        self.route = listing(40, with_total=False, has_more=lambda page, size: page < 2)
        self.assertEqual(len(list(self.lr.paginate(self.lr.container_subfolders, 5, page_size=10))), 20)
        self.assertEqual(self.pages(), [1, 2], "has_more decides when present")
        self.sent.clear()
        self.route = listing(250)
        self.assertEqual(len(list(self.lr.paginate(self.lr.get_domain_all, "example.com", max_items=120))), 120)
        self.assertEqual(self.pages(), [1, 2])

    def test_monitoring_run_items_are_read_up_to_total(self):
        counts = {1: 99, 2: 0, 3: 50}

        def route(request):
            items = [{"n": n} for n in range(counts[int(request.url.params["page"])])]
            return ok({"items": items, "total": 250, "page_size": 100})

        self.route = route
        self.assertEqual(len(list(self.lr.paginate(self.lr.get_notification_run_items, 12))), 149)
        self.assertEqual(self.pages(), [1, 2, 3], "a short or empty page is not the end before total")

    def test_raw_search_follows_the_cursor(self):
        self.answers.extend([
            ok({"items": [{"n": 1}, {"n": 2}], "total": -1, "page": 1, "page_size": 2, "has_more": True, "next_cursor": "c1"}),
            ok({"items": [], "total": -1, "page": 2, "page_size": 2, "has_more": True, "next_cursor": "c2"}),
            ok({"items": [{"n": 3}], "total": -1, "page": 3, "page_size": 2, "has_more": False, "next_cursor": None}),
        ])
        items = list(self.lr.paginate(self.lr.raw_search, "admin@example.com", page_size=2, q_exact=True))
        self.assertEqual([i["n"] for i in items], [1, 2, 3])
        self.assertEqual([self.query(i).get("cursor") for i in range(3)], [None, "c1", "c2"])
        self.assertEqual({self.sent[i].content for i in range(3)}, {self.sent[0].content})

    def test_deep_paging_stops_with_a_warning_at_the_caller(self):
        self.route = listing(5_000_000)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            items = list(self.lr.paginate(self.lr.search_advanced, page=1000, page_size=1000, username="john"))
        self.assertEqual((len(items), self.pages()), (1000, [1000]))
        self.assertEqual(len(caught), 1)
        self.assertIn("export_advanced()", str(caught[0].message))
        self.assertFromHere(caught[0])

    def test_dark_web_stops_at_its_window(self):
        self.route = listing(25_000)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            items = list(self.lr.paginate(self.lr.search_dark_web, "acme", page_size=100))
        self.assertEqual((len(items), self.pages()[-1]), (10_000, 100))
        self.assertEqual(len(caught), 1)
        self.assertIn("at most 10,000 results", str(caught[0].message))
        self.assertFromHere(caught[0])
        self.sent.clear()
        with self.assertWarns(UserWarning):
            self.assertEqual(len(list(self.lr.paginate(self.lr.search_dark_web, "acme", page_size=30))), 9_990)
        self.assertEqual(self.pages()[-1], 333, "page 334 would end past 10,000")

    def test_wrong_methods(self):
        with self.assertRaises(TypeError):
            list(self.lr.paginate(self.lr.get_profile))


# -------------------------
# Context manager
# -------------------------


class ContextManagerTest(SyncTestCase):
    def test_the_block_closes_both_httpx_clients(self):
        self.answers.append(httpx.Response(302, headers={"Location": "https://files.example.test/raw/5"}))
        real_init = httpx.Client.__init__

        def init(client, *args, **kwargs):
            if kwargs.get("transport") is None:
                kwargs.pop("proxy", None)
                kwargs["transport"] = httpx.MockTransport(lambda r: httpx.Response(200, content=b"RAW"))
            real_init(client, *args, **kwargs)

        with mock.patch.object(httpx.Client, "__init__", init):
            with self.make_client(token="t") as lr:
                self.assertIsInstance(lr, LeakRadar)
                self.assertEqual(lr.download_raw_file(5), b"RAW")
                self.assertIsInstance(lr._anon_client, httpx.Client)
                self.assertFalse(lr._client.is_closed)
        self.assertTrue(lr._client.is_closed)
        self.assertTrue(lr._anon_client.is_closed)

    def test_close(self):
        lr = self.make_client(token="t")
        lr.close()
        self.assertTrue(lr._client.is_closed)
        self.assertIsNone(lr._anon_client)


# -------------------------
# The generated file
# -------------------------


def load_generator():
    """scripts/generate_sync.py as a module, without writing bytecode next to it."""
    spec = importlib.util.spec_from_file_location("leakradar_generate_sync", ROOT / "scripts" / "generate_sync.py")
    module = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


MINIMAL_CLIENT = '''\
import asyncio
import inspect
from typing import AsyncIterator, Dict, Optional

import httpx

from ._version import __version__


def _helper(value):
    return value


class LeakRadarClient:
    """Asynchronous client."""

    # Off here; on in AsyncLeakRadar.
    _READ_ENV = False

    def __init__(self, transport: httpx.AsyncBaseTransport = None):
        self._sleep = asyncio.sleep
        self._client = httpx.AsyncClient(transport=transport)  # an httpx.AsyncClient
        self.version = __version__

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.aclose()

    async def aclose(self) -> None:
        """Close the clients: await client.aclose()."""
        await self._client.aclose()

    async def fetch(self, url: str) -> bytes:
        """Fetch a URL. on_progress: called with each chunk; a coroutine function is awaited."""
        async with self._client.stream("GET", url) as response:
            chunks = [_helper(chunk) async for chunk in response.aiter_bytes()]
            await response.aread()
        return b"".join(chunks) + f"{url!r} {self.version}".encode()

    async def version_text(self) -> str:
        return f"{self.version}"

    async def same(self, other: "Optional[LeakRadarClient]" = None) -> "LeakRadarClient":
        return other or self

    async def items(self) -> AsyncIterator[Dict[str, int]]:
        """Iterate: async for item in client.items()."""
        for n in range(3):
            await self._sleep(0)
            yield {"n": n}

    @staticmethod
    async def _progress(callback, state):
        result = callback(state)
        if inspect.isawaitable(result):
            await result


class AsyncLeakRadar(LeakRadarClient):
    _READ_ENV = True
'''


class GeneratedFileTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.generator = load_generator()

    def test_the_generated_file_is_up_to_date(self):
        source = (ROOT / "leakradar" / "client.py").read_bytes().decode("utf-8")
        current = (ROOT / "leakradar" / "_sync_client.py").read_bytes().decode("utf-8").replace("\r\n", "\n")
        self.assertTrue(
            current == self.generator.generate(source),
            "leakradar/_sync_client.py is out of date: run python scripts/generate_sync.py",
        )

    def test_the_rewrite(self):
        out = self.generator.generate(MINIMAL_CLIENT)
        self.assertNotIn("\r", out)
        expected = [
            "# Generated by scripts/generate_sync.py from leakradar/client.py: do not edit.",
            "import time\nfrom typing import Dict, Iterator, Optional\n\nimport httpx\n\nfrom .client import __version__, _helper\n\n\nclass LeakRadar:",
            "    # A missing token / base_url is read from LEAKRADAR_API_KEY / LEAKRADAR_BASE_URL, as AsyncLeakRadar does.\n"
            "    _READ_ENV = True\n",
            "def __init__(self, transport: httpx.BaseTransport = None):",
            "self._sleep = time.sleep",
            "self._client = httpx.Client(transport=transport)  # an httpx.Client",
            "    def __enter__(self):\n",
            "    def __exit__(self, *exc):\n        self.close()\n",
            '    def close(self) -> None:\n        """Close the clients: client.close()."""\n        self._client.close()\n',
            '"""Fetch a URL. on_progress: called with each chunk."""',
            'with self._client.stream("GET", url) as response:',
            "chunks = [_helper(chunk) for chunk in response.iter_bytes()]",
            "            response.read()\n",
            'return b"".join(chunks) + f"{url!r} {self.version}".encode()',
            '    def items(self) -> Iterator[Dict[str, int]]:\n        """Iterate: for item in client.items()."""',
            "            self._sleep(0)\n            yield",
            "    def _progress(callback, state):\n        callback(state)\n",
            '    def version_text(self) -> str:\n        return f"{self.version}"\n',
            '    def same(self, other: "Optional[LeakRadar]" = None) -> "LeakRadar":\n',
        ]
        for text in expected:
            with self.subTest(text=text):
                self.assertIn(text, out)
        header, body = out.split("\nclass LeakRadar:\n", 1)
        imports = header.split('"""', 2)[2]
        rest = body.split('"""', 2)[2]  # after the class docstring, which names AsyncLeakRadar on purpose
        for absent in ("async", "await", "asyncio", "inspect", "aclose", "aread", "aiter", "Asynchronous"):
            with self.subTest(absent=absent):
                self.assertNotIn(absent, rest)
        for absent in ("asyncio", "inspect", "AsyncIterator"):
            with self.subTest(absent=absent):
                self.assertNotIn(absent, imports)
        compile(out, "_sync_client.py", "exec")

    def test_anyio_sleep_becomes_time_sleep(self):
        source = MINIMAL_CLIENT.replace("import asyncio\n", "import anyio\n", 1)
        source = source.replace("asyncio.sleep", "anyio.sleep", 1)
        out = self.generator.generate(source)
        self.assertIn("self._sleep = time.sleep", out)
        self.assertIn("import time\nfrom typing import Dict, Iterator, Optional\n", out)
        self.assertNotIn("anyio", out)
        with self.assertRaisesRegex(self.generator.GenerateError, "anyio is only rewritten"):
            self.generator.generate(source.replace("anyio.sleep", "anyio.Event", 1))
        with self.assertRaisesRegex(self.generator.GenerateError, "'anyio' left in a docstring"):
            self.generator.generate(source.replace('"""Fetch a URL.', '"""Fetch a URL with anyio.', 1))

    def test_what_the_rules_do_not_cover_stops_the_generator(self):
        cases = [
            # (what client.py has, what it gets, what the error says)
            ('return f"{self.version}"', 'return f"{await self.aclose()}"', "in an f-string"),
            ("self._sleep = asyncio.sleep", "self._sleep = asyncio.gather", "asyncio is only rewritten"),
            ('"""Iterate: async for item', '"""Iterate: an awaitable for item', "'awaitable' left in a docstring"),
            ("        return self\n", '        return getattr(self, "aclose")\n', "a string names aclose"),
            ("self.version = __version__", "self.version = missing(__version__)", "reads `missing`"),
            ("    _READ_ENV = False\n", "", "no `_READ_ENV"),
        ]
        for old, new, error in cases:
            with self.subTest(error=error):
                self.assertIn(old, MINIMAL_CLIENT)
                with self.assertRaisesRegex(self.generator.GenerateError, error):
                    self.generator.generate(MINIMAL_CLIENT.replace(old, new, 1))

    def test_check_mode(self):
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stdout(io.StringIO()) as output:
            target = pathlib.Path(folder) / "_sync_client.py"
            self.assertEqual(self.generator.main(["--out", str(target)]), 0)
            self.assertNotIn(b"\r", target.read_bytes())
            self.assertEqual(self.generator.main(["--check", "--out", str(target)]), 0)
            # A CRLF checkout of an up to date file passes.
            target.write_bytes(target.read_bytes().replace(b"\n", b"\r\n"))
            self.assertEqual(self.generator.main(["--check", "--out", str(target)]), 0)
            target.write_bytes(target.read_bytes() + b"# edited\n")
            self.assertEqual(self.generator.main(["--check", "--out", str(target)]), 1)
        self.assertIn("is out of date: run python scripts/generate_sync.py", output.getvalue())


if __name__ == "__main__":
    unittest.main()
