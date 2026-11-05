"""
0.3.0 additions: errors (pickle, message, request_id, conflicts, code and error_code),
configuration (environment, base_url, verify, proxy, token), asyncio and trio, transient retries
(never a 503 after a synchronous unlock), Deprecation headers, request(), overrides written for
earlier releases (_request, _handle_error, list_exports), domain arguments, docstrings, waiting
for tasks and exports, export downloads and pagination.
"""

import asyncio
import copy
import inspect
import os
import pickle
import ssl
import stat
import tempfile
import unittest
import warnings
from unittest import mock

import anyio
import certifi
import httpx

import leakradar
import leakradar._sync_client
import leakradar.client
import leakradar.webhooks
from leakradar import (
    AsyncLeakRadar,
    BadRequestError,
    ConflictError,
    ExportFailedError,
    ForbiddenError,
    GoneError,
    LeakRadarAPIError,
    LeakRadarClient,
    LeakRadarError,
    NotFoundError,
    PaymentRequiredError,
    ServiceUnavailableError,
    TaskFailedError,
    TooManyRequestsError,
    UnauthorizedError,
    ValidationError,
    WaitTimeoutError,
    __version__,
)
from leakradar.client import _domain

try:
    import trio
except ImportError:  # optional: one CI leg installs it
    trio = None

HERE = os.path.normcase(os.path.abspath(__file__))


def ok(body=None, **kwargs):
    return httpx.Response(200, json={"ok": True} if body is None else body, **kwargs)


def refusal(reason, retry_after=1, status=429):
    """A rate limit refusal in the API's unified format."""
    error = {"code": "rate_limited", "reason": reason, "scope": "credential", "family": "search", "retry_after": retry_after}
    headers = {"Retry-After": str(retry_after), "X-LR-RateLimit-Reason": reason}
    return httpx.Response(status, json={"detail": f"Refused ({reason}).", "error": error}, headers=headers)


def search_unavailable(retry_after="2"):
    headers = {"Retry-After": retry_after} if retry_after is not None else {}
    return httpx.Response(
        503, json={"detail": "Search temporarily unavailable", "code": "search_unavailable"}, headers=headers
    )


class FakeTime:
    """Monotonic clock and sleep of the client: sleeping moves the clock, nothing really waits."""

    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        target = self.now + seconds
        await asyncio.sleep(0)
        self.now = max(self.now, target)


class CustomError(LeakRadarAPIError):
    """A user subclass written for 0.2: its own constructor (pickle must not call it), its own message."""

    def __init__(self, text):
        super().__init__(418, text)
        self.message = f"Custom: {text}"


class HelperTestCase(unittest.IsolatedAsyncioTestCase):
    max_retries = 3

    async def asyncSetUp(self):
        # Each request takes the next answer (a response, or an exception to raise), then
        # falls back to self.route(request), then to a 200 {"ok": true}.
        self.answers = []
        self.route = None
        self.sent = []
        self.time = FakeTime()
        self.client = self.make_client(token="tok_ABC123")

    async def asyncTearDown(self):
        await self.client.aclose()

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

    def make_client(self, client_class=LeakRadarClient, **kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(self.handle))
        kwargs.setdefault("max_retries", self.max_retries)
        client = client_class(**kwargs)
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


# -------------------------
# Errors
# -------------------------


class ErrorClassTest(unittest.TestCase):
    def test_hierarchy(self):
        api_errors = (
            BadRequestError,
            UnauthorizedError,
            PaymentRequiredError,
            ForbiddenError,
            NotFoundError,
            ConflictError,
            GoneError,
            ValidationError,
            TooManyRequestsError,
            ServiceUnavailableError,
        )
        for error_class in api_errors:
            self.assertTrue(issubclass(error_class, LeakRadarAPIError), error_class)
        for error_class in (LeakRadarAPIError, TaskFailedError, ExportFailedError, WaitTimeoutError):
            self.assertTrue(issubclass(error_class, LeakRadarError), error_class)
        self.assertTrue(issubclass(WaitTimeoutError, TimeoutError))
        self.assertFalse(issubclass(TaskFailedError, LeakRadarAPIError))
        self.assertFalse(issubclass(ExportFailedError, LeakRadarAPIError))

    def test_public_names(self):
        self.assertEqual(leakradar.__all__, sorted(leakradar.__all__))
        old = {
            "LeakRadarClient", "LeakRadarAPIError", "UnauthorizedError", "ForbiddenError", "BadRequestError",
            "TooManyRequestsError", "NotFoundError", "ValidationError", "ConflictError", "PaymentRequiredError",
            "GoneError", "ServiceUnavailableError", "__version__",
        }
        new = {"AsyncLeakRadar", "LeakRadar", "LeakRadarError", "TaskFailedError", "ExportFailedError", "WaitTimeoutError"}
        self.assertLessEqual(old | new, set(leakradar.__all__))
        for name in leakradar.__all__:
            if name != "__version__":
                # LeakRadar is generated into leakradar._sync_client, every other name lives in leakradar.client.
                home = leakradar._sync_client if name == "LeakRadar" else leakradar.client
                self.assertIs(getattr(leakradar, name), getattr(home, name))

    def test_str_is_unchanged(self):
        self.assertEqual(str(LeakRadarAPIError(404, "Not found")), "API Error 404: Not found")
        self.assertEqual(str(NotFoundError(404, {"code": "x"})), "API Error 404: {'code': 'x'}")

    def test_every_error_survives_pickle_with_its_state(self):
        refused = TooManyRequestsError(
            429,
            "Refused (rate).",
            headers={"Retry-After": "3", "X-LR-RateLimit-Reason": "rate", "X-Request-ID": "req-9"},
            error={"code": "rate_limited", "reason": "rate", "scope": "credential", "family": "search",
                   "retry_at": "2026-10-21T14:32:10Z", "limit": {"per_second": 30, "burst": 30}},
        )
        busy = ServiceUnavailableError(503, "Busy", headers={"Retry-After": "5"}, error={"reason": "busy"})
        errors = [
            LeakRadarAPIError(500, "Internal Server Error", request_id="6f1c2a"),
            BadRequestError(400, {"code": "invalid_cursor", "message": "Invalid or expired search cursor"}),
            UnauthorizedError(401, "Invalid API key"),
            PaymentRequiredError(402, "Raw quota used up"),
            ForbiddenError(403, "email_not_confirmed"),
            NotFoundError(404, "Not found", headers={"X-Request-ID": "req-1"}),
            ConflictError(409, {"code": "monitor_overlap", "message": "Already monitored.", "conflicts": [{"value": "a.com"}]}),
            GoneError(410, "Expired"),
            ValidationError(422, [{"loc": ["query", "page_size"], "msg": "too big", "type": "less_than_equal"}]),
            refused,
            busy,
            CustomError("teapot"),
            TaskFailedError("42", "FAILED", "insufficient_points", "Need 120 points, have 3", {"task_id": "42"}),
            ExportFailedError(7, "ERROR", {"id": 7, "status": "ERROR"}),
            WaitTimeoutError("Stopped waiting for task 42", waited=30.0, last={"status": "RUNNING"}),
        ]
        # Kept as is, not read again from the headers: the state wins over the constructor.
        refused.reason = "concurrency"
        for error in errors:
            for protocol in range(pickle.HIGHEST_PROTOCOL + 1):
                with self.subTest(error=type(error).__name__, protocol=protocol):
                    restored = pickle.loads(pickle.dumps(error, protocol))
                    self.assertIs(type(restored), type(error))
                    self.assertEqual(str(restored), str(error))
                    self.assertEqual(restored.args, error.args)
                    self.assertEqual(vars(restored), vars(error))
        restored = pickle.loads(pickle.dumps(refused))
        self.assertEqual(
            (restored.reason, restored.retry_after, restored.retry_at, restored.limit, restored.request_id),
            ("concurrency", 3.0, refused.retry_at, {"per_second": 30, "burst": 30}, "req-9"),
        )
        self.assertEqual(restored.headers["x-lr-ratelimit-reason"], "rate")
        self.assertEqual((restored.error_code, restored.message), ("rate_limited", "Refused (rate)."))
        self.assertEqual(copy.deepcopy(busy).reason, "busy")
        timeout = pickle.loads(pickle.dumps(errors[-1]))
        self.assertIsInstance(timeout, TimeoutError)
        self.assertEqual((timeout.waited, timeout.last), (30.0, {"status": "RUNNING"}))
        self.assertEqual(pickle.loads(pickle.dumps(CustomError("teapot"))).message, "Custom: teapot")

    def test_message_and_conflicts_can_be_set(self):
        # An exception subclass written before 0.3.0 commonly sets self.message.
        error = CustomError("teapot")
        self.assertEqual((error.message, error.detail, str(error)), ("Custom: teapot", "teapot", "API Error 418: teapot"))
        error.conflicts = [{"value": "a.com"}]
        self.assertEqual(error.conflicts, [{"value": "a.com"}])

    def test_message(self):
        cases = [
            (LeakRadarAPIError(404, "Not found"), "Not found"),
            (BadRequestError(400, {"code": "invalid_cursor", "message": "Invalid or expired search cursor"}),
             "Invalid or expired search cursor"),
            (LeakRadarAPIError(400, {"detail": "Inner text"}), "Inner text"),
            (
                ValidationError(422, [
                    {"loc": ["body", "emails", 0], "msg": "Value error, Invalid input: 'x'", "type": "value_error"},
                    {"loc": ["query", "page_size"], "msg": "Input should be less than or equal to 100"},
                ]),
                "body.emails.0: Value error, Invalid input: 'x'; query.page_size: Input should be less than or equal to 100",
            ),
            (LeakRadarAPIError(400, {"code": "x"}), "{'code': 'x'}"),
            (LeakRadarAPIError(422, [1, 2]), "[1, 2]"),
        ]
        for error, message in cases:
            with self.subTest(message=message):
                self.assertEqual(error.message, message)

    def test_conflicts_and_request_id_by_hand(self):
        detail = {"code": "monitor_overlap", "message": "Already monitored.", "conflicts": [{"value": "a.com"}]}
        self.assertEqual(ConflictError(409, detail).conflicts, [{"value": "a.com"}])
        self.assertIsNone(ConflictError(409, "An identical notification already exists.").conflicts)
        self.assertEqual(LeakRadarAPIError(500, "x", request_id="r-1").request_id, "r-1")
        self.assertEqual(LeakRadarAPIError(502, "x", headers={"X-Request-ID": "r-2"}).request_id, "r-2")
        self.assertIsNone(LeakRadarAPIError(500, "x").request_id)

    def test_task_failed_error_text(self):
        error = TaskFailedError("42", "FAILED", "insufficient_points", "Need 120 points, have 3")
        self.assertEqual(str(error), "Task 42 FAILED (insufficient_points): Need 120 points, have 3")
        self.assertEqual(str(TaskFailedError("43", "CANCELED")), "Task 43 CANCELED: Task 43 was canceled.")
        self.assertEqual(TaskFailedError("43", "CANCELED").task, {})


class ErrorFromAnswerTest(HelperTestCase):
    async def test_500_id_is_the_request_id(self):
        self.answers.append(httpx.Response(500, json={"detail": "Internal Server Error", "id": "6f1c2a90"}))
        with self.assertRaises(LeakRadarAPIError) as caught:
            await self.client.get_profile()
        self.assertEqual((caught.exception.request_id, caught.exception.message), ("6f1c2a90", "Internal Server Error"))
        self.assertEqual(len(self.sent), 1, "a 500 is not retried")

    async def test_request_id_header_and_resource_ids(self):
        self.answers.append(httpx.Response(404, json={"detail": "Not found", "id": "abc"}, headers={"X-Request-ID": "req-7"}))
        with self.assertRaises(NotFoundError) as caught:
            await self.client.get_profile()
        self.assertEqual(caught.exception.request_id, "req-7")
        self.answers.append(httpx.Response(404, json={"detail": "Not found", "id": "abc"}))
        with self.assertRaises(NotFoundError) as caught:
            await self.client.get_profile()
        self.assertIsNone(caught.exception.request_id, "only a 5xx body carries a request id")

    async def test_monitor_overlap_conflicts(self):
        conflicts = [{"value": "example.com", "existing": {"id": 4}, "datasets": ["combolist"], "absorbable": True}]
        detail = {"code": "monitor_overlap", "message": "This value is already monitored on this channel.", "conflicts": conflicts}
        self.answers.append(httpx.Response(409, json={"detail": detail}))
        with self.assertRaises(ConflictError) as caught:
            await self.client.create_notification({"type": "domain", "value": "example.com", "method_id": 1})
        error = caught.exception
        self.assertEqual((error.code, error.conflicts), ("monitor_overlap", conflicts))
        self.assertEqual(error.message, "This value is already monitored on this channel.")
        restored = pickle.loads(pickle.dumps(error))
        self.assertEqual((restored.code, restored.conflicts, str(restored)), (error.code, conflicts, str(error)))

    async def test_top_level_code(self):
        self.answers.append(httpx.Response(422, json={"detail": "Too many clauses", "code": "search_query_too_complex"}))
        with self.assertRaises(ValidationError) as caught:
            await self.client.search_advanced(username="a*")
        self.assertEqual(caught.exception.error_code, "search_query_too_complex")
        self.assertEqual(pickle.loads(pickle.dumps(caught.exception)).error_code, "search_query_too_complex")

    async def test_code_is_the_one_of_an_object_detail_only(self):
        # As in 0.2.0, `code` set means "detail is an object"; error_code reads every shape of answer.
        answers = [
            (refusal("daily_quota", retry_after=5400), TooManyRequestsError, "rate_limited"),
            (
                httpx.Response(422, json={"detail": "Too many clauses", "code": "search_query_too_complex"}),
                ValidationError,
                "search_query_too_complex",
            ),
            (search_unavailable(None), ServiceUnavailableError, "search_unavailable"),
            (httpx.Response(404, json={"detail": "Not Found"}), NotFoundError, None),
        ]
        for answer, error_class, error_code in answers:
            with self.subTest(status=answer.status_code):
                self.answers.append(answer)
                with self.assertRaises(error_class) as caught:
                    await self.client.get_profile()
                for error in (caught.exception, pickle.loads(pickle.dumps(caught.exception))):
                    self.assertIsNone(error.code)
                    self.assertEqual(error.error_code, error_code)
        detail = {"code": "monitor_overlap", "message": "Already monitored.", "conflicts": []}
        self.answers.append(httpx.Response(409, json={"detail": detail}))
        with self.assertRaises(ConflictError) as caught:
            await self.client.get_profile()
        for error in (caught.exception, pickle.loads(pickle.dumps(caught.exception))):
            self.assertEqual((error.code, error.error_code), ("monitor_overlap", "monitor_overlap"))


# -------------------------
# Configuration
# -------------------------


class ConfigurationTest(HelperTestCase):
    ENV = {"LEAKRADAR_API_KEY": "env-key", "LEAKRADAR_BASE_URL": "https://staging.example.test/"}

    def env(self, **values):
        patcher = mock.patch.dict(os.environ, values)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("LEAKRADAR_API_KEY", "LEAKRADAR_BASE_URL"):
            if name not in values:
                os.environ.pop(name, None)

    async def call(self, client):
        self.addAsyncCleanup(client.aclose)
        self.sent.clear()
        await client.get_profile()
        return self.sent[0]

    async def test_leakradar_client_never_reads_the_environment(self):
        self.env(**self.ENV)
        client = self.make_client()
        self.assertIsNone(client.token)
        self.assertEqual(client.base_url, "https://api.leakradar.io")
        request = await self.call(client)
        self.assertEqual(str(request.url), "https://api.leakradar.io/profile")
        self.assertNotIn("Authorization", request.headers)

    async def test_async_leakradar_reads_the_environment(self):
        self.env(**self.ENV)
        client = self.make_client(AsyncLeakRadar)
        self.assertIsInstance(client, LeakRadarClient)
        self.assertEqual((client.token, client.base_url), ("env-key", "https://staging.example.test"))
        request = await self.call(client)
        self.assertEqual(str(request.url), "https://staging.example.test/profile")
        self.assertEqual(request.headers["Authorization"], "Bearer env-key")
        self.assertEqual(request.headers["User-Agent"], f"LeakRadar-Python-Client/{__version__}")

    async def test_arguments_win_and_an_empty_token_is_anonymous(self):
        self.env(**self.ENV)
        client = self.make_client(AsyncLeakRadar, token="", base_url="https://gateway.example.test/lr/")
        request = await self.call(client)
        self.assertEqual(str(request.url), "https://gateway.example.test/lr/profile")
        self.assertNotIn("Authorization", request.headers)

    async def test_the_token_is_sent_without_surrounding_whitespace(self):
        # A key read from a file ends with a newline: httpx would refuse it and repeat it in its error.
        client = self.make_client(token="tok_ABC123\n")
        self.assertEqual((await self.call(client)).headers["Authorization"], "Bearer tok_ABC123")
        self.assertEqual(client.token, "tok_ABC123\n", "kept as given")
        client.token = " tok_NEW\r\n"
        self.assertEqual((await self.call(client)).headers["Authorization"], "Bearer tok_NEW")
        client.token = "  "
        self.assertNotIn("Authorization", (await self.call(client)).headers)

    async def test_without_environment(self):
        self.env()
        client = self.make_client(AsyncLeakRadar)
        self.assertEqual((client.token, client.base_url), (None, "https://api.leakradar.io"))
        request = await self.call(client)
        self.assertNotIn("Authorization", request.headers)

    async def test_a_subclass_overriding_base_url(self):
        self.env()

        class Staging(LeakRadarClient):
            BASE_URL = "https://staging.example.test"

        class AsyncStaging(AsyncLeakRadar):
            BASE_URL = "https://staging2.example.test/"

        self.assertEqual(str((await self.call(self.make_client(Staging, token="t"))).url), "https://staging.example.test/profile")
        self.assertEqual(str((await self.call(self.make_client(AsyncStaging))).url), "https://staging2.example.test/profile")
        client = self.make_client(Staging, base_url="https://other.example.test")
        self.assertEqual(str((await self.call(client)).url), "https://other.example.test/profile")

    def spy_clients(self):
        """Record the keyword arguments of every httpx.AsyncClient created."""
        created = []
        real_init = httpx.AsyncClient.__init__

        def init(client, *args, **kwargs):
            created.append(dict(kwargs))
            real_init(client, *args, **kwargs)

        patcher = mock.patch.object(httpx.AsyncClient, "__init__", init)
        patcher.start()
        self.addCleanup(patcher.stop)
        return created

    async def test_verify_and_proxy_reach_httpx(self):
        created = self.spy_clients()
        client = LeakRadarClient(verify=False, proxy="http://proxy.example.test:3128")
        self.addAsyncCleanup(client.aclose)
        self.assertEqual((created[0]["verify"], created[0]["proxy"]), (False, "http://proxy.example.test:3128"))
        context = ssl.create_default_context()
        client = LeakRadarClient(verify=context)
        self.addAsyncCleanup(client.aclose)
        self.assertIs(created[1]["verify"], context)

    async def test_a_transport_keeps_its_requests(self):
        # httpx routes every request through a proxy mount when it gets one: never with a transport.
        created = self.spy_clients()
        client = self.make_client(proxy="http://proxy.example.test:3128", verify=False)
        self.assertNotIn("proxy", created[0])
        self.assertNotIn("verify", created[0])
        request = await self.call(client)
        self.assertEqual(str(request.url), "https://api.leakradar.io/profile")

    async def test_raw_downloads_use_the_same_settings(self):
        self.answers.append(httpx.Response(302, headers={"Location": "https://files.example.test/raw/5"}))
        created = []
        storage = []
        real_init = httpx.AsyncClient.__init__

        def init(client, *args, **kwargs):
            created.append(dict(kwargs))
            if kwargs.get("transport") is None:
                # The anonymous client of the storage downloads: no network in tests.
                kwargs.pop("proxy", None)
                kwargs["transport"] = httpx.MockTransport(
                    lambda r: storage.append(r) or httpx.Response(200, content=b"RAW")
                )
            real_init(client, *args, **kwargs)

        with mock.patch.object(httpx.AsyncClient, "__init__", init):
            client = self.make_client(verify=False, proxy="http://proxy.example.test:3128")
            self.addAsyncCleanup(client.aclose)
            self.assertEqual(await client.download_raw_file(5), b"RAW")
            # verify=None is the default there too: verification on.
            self.answers.append(httpx.Response(302, headers={"Location": "https://files.example.test/raw/6"}))
            client = self.make_client(verify=None)
            self.addAsyncCleanup(client.aclose)
            self.assertEqual(await client.download_raw_file(6), b"RAW")
        self.assertEqual((created[1]["verify"], created[1]["proxy"]), (False, "http://proxy.example.test:3128"))
        self.assertIs(created[-1]["verify"], True)
        self.assertNotIn("Authorization", storage[0].headers)


class CertificateAuthorityTest(unittest.TestCase):
    # Not an async test: loading a CA bundle is slow enough for asyncio's debug mode to complain.
    def test_a_ca_path_becomes_an_ssl_context(self):
        created = []
        real_init = httpx.AsyncClient.__init__

        def init(client, *args, **kwargs):
            created.append(dict(kwargs))
            real_init(client, *args, **kwargs)

        with mock.patch.object(httpx.AsyncClient, "__init__", init), warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with tempfile.TemporaryDirectory() as folder:
                for path in (certifi.where(), folder):
                    asyncio.run(LeakRadarClient(verify=path).aclose())
        self.assertEqual([type(c["verify"]) for c in created], [ssl.SSLContext, ssl.SSLContext])
        self.assertFalse([w for w in caught if issubclass(w.category, DeprecationWarning)], "httpx 0.28 deprecates paths")
        with self.assertRaises(OSError):
            LeakRadarClient(verify=os.path.join(tempfile.gettempdir(), "missing-leakradar-ca.pem"))

    def test_verify_none_is_the_default_and_other_types_are_refused(self):
        # httpx 0.27 reads any falsy verify as "no verification": None must mean the default.
        created = []
        real_init = httpx.AsyncClient.__init__

        def init(client, *args, **kwargs):
            created.append(dict(kwargs))
            real_init(client, *args, **kwargs)

        with mock.patch.object(httpx.AsyncClient, "__init__", init):
            asyncio.run(LeakRadarClient(verify=None).aclose())
            asyncio.run(LeakRadarClient(verify=False).aclose())
            for value in (0, 1, b"/etc/ssl/certs/ca.pem", ["ca.pem"]):
                with self.subTest(verify=value), self.assertRaises(TypeError) as caught:
                    LeakRadarClient(verify=value)
                self.assertEqual(
                    str(caught.exception), "verify must be True, False, the path of a CA bundle or an ssl.SSLContext"
                )
        self.assertEqual([c["verify"] for c in created], [True, False], "nothing is created for a refused value")
        self.assertIs(created[0]["verify"], True)


# -------------------------
# Async libraries
# -------------------------


class AsyncLibraryTest(unittest.TestCase):
    """The client waits with anyio (installed by httpx), so its retries run on asyncio and on trio."""

    def retry_once(self, backend):
        answers = [refusal("rate", retry_after=0.01), ok({"id": 1})]
        sent = []

        def handle(request):
            sent.append(request.url.path)
            return answers.pop(0)

        async def main():
            client = LeakRadarClient(token="t", transport=httpx.MockTransport(handle))
            client._random = lambda: 0.0
            try:
                self.assertIs(client._sleep, anyio.sleep)
                return await client.get_profile()
            finally:
                await client.aclose()

        self.assertEqual(anyio.run(main, backend=backend), {"id": 1})
        self.assertEqual(sent, ["/profile", "/profile"])

    def test_retries_on_asyncio(self):
        self.retry_once("asyncio")

    @unittest.skipIf(trio is None, "trio is not installed")
    def test_retries_on_trio(self):
        self.retry_once("trio")


# -------------------------
# Transient failures
# -------------------------


class TransientRetryTest(HelperTestCase):
    async def test_connection_errors_are_retried_for_every_method(self):
        calls = [
            ("POST", lambda: self.client.search_email("john@example.com")),
            ("DELETE", lambda: self.client.delete_notification(4)),
            ("GET", lambda: self.client.get_profile()),
        ]
        for verb, call in calls:
            for error_class in (httpx.ConnectError, httpx.ConnectTimeout):
                with self.subTest(verb=verb, error=error_class.__name__):
                    self.sent.clear()
                    self.time.sleeps.clear()
                    self.answers.extend([error_class("refused"), ok()])
                    self.assertEqual(await call(), {"ok": True})
                    self.assertEqual([r.method for r in self.sent], [verb, verb])
                    self.assertSleeps([0.575])
        self.assertEqual(self.client._gate_until, 0.0, "a network failure does not close the gate")

    async def test_read_failures_are_retried_on_get_only(self):
        for error_class in (httpx.ReadTimeout, httpx.ReadError, httpx.RemoteProtocolError):
            with self.subTest(error=error_class.__name__):
                self.sent.clear()
                self.answers.extend([error_class("lost"), ok()])
                self.assertEqual(await self.client.get_profile(), {"ok": True})
                self.assertEqual(len(self.sent), 2)
                self.sent.clear()
                self.answers.append(error_class("lost"))
                with self.assertRaises(error_class):
                    await self.client.search_email("john@example.com")
                self.assertEqual(len(self.sent), 1, "a POST may have run: never replayed")

    async def test_head_is_retried_like_get(self):
        self.answers.extend([httpx.ReadTimeout("slow"), ok()])
        await self.client.request("HEAD", "/profile")
        self.assertEqual([r.method for r in self.sent], ["HEAD", "HEAD"])

    async def test_backoff_and_the_original_exception_once_spent(self):
        errors = [httpx.ConnectError(f"refused {n}") for n in range(4)]
        self.answers.extend(errors)
        with self.assertRaises(httpx.ConnectError) as caught:
            await self.client.get_profile()
        self.assertIs(caught.exception, errors[-1], "raised as is, not wrapped")
        self.assertEqual(len(self.sent), 4)
        self.assertSleeps([0.575, 1.1, 2.15])

    async def test_backoff_is_capped_by_max_retry_wait(self):
        self.client.max_retry_wait = 0.8
        self.answers.extend([httpx.ConnectError("x"), httpx.ConnectError("x"), ok()])
        await self.client.get_profile()
        self.assertSleeps([0.575, 0.89])

    async def test_other_transport_errors_are_raised_at_once(self):
        for error in (httpx.ProxyError("proxy refused"), httpx.WriteError("broken pipe"), httpx.UnsupportedProtocol("x")):
            with self.subTest(error=type(error).__name__):
                self.sent.clear()
                self.answers.append(error)
                with self.assertRaises(type(error)):
                    await self.client.get_profile()
                self.assertEqual(len(self.sent), 1)

    async def test_502_and_504_are_retried_on_get(self):
        self.answers.extend([httpx.Response(502, text="Bad gateway"), httpx.Response(504, text="Gateway timeout"), ok()])
        self.assertEqual(await self.client.get_profile(), {"ok": True})
        self.assertEqual(len(self.sent), 3)
        self.assertSleeps([0.575, 1.1])
        self.assertEqual(self.client._gate_until, 0.0)

    async def test_502_on_post_is_raised_at_once(self):
        self.answers.append(httpx.Response(502, text="Bad gateway"))
        with self.assertRaises(LeakRadarAPIError) as caught:
            await self.client.search_email("john@example.com")
        self.assertEqual((caught.exception.status_code, caught.exception.detail), (502, "Bad gateway"))
        self.assertEqual(len(self.sent), 1)

    async def test_502_once_spent_raises_the_api_error(self):
        self.answers.extend(httpx.Response(504, text="Gateway timeout") for _ in range(4))
        with self.assertRaises(LeakRadarAPIError) as caught:
            await self.client.get_profile()
        self.assertEqual(caught.exception.status_code, 504)
        self.assertEqual(len(self.sent), 4)

    async def test_one_budget_for_every_kind_of_retry(self):
        self.answers.extend([refusal("rate"), httpx.ConnectError("x"), httpx.Response(502), ok()])
        self.assertEqual(await self.client.get_profile(), {"ok": True})
        self.assertSleeps([1.1, 1.1, 2.15])
        self.sent.clear()
        self.client.max_retries = 2
        self.answers.extend([refusal("rate"), httpx.ConnectError("x"), httpx.Response(502), ok()])
        with self.assertRaises(LeakRadarAPIError) as caught:
            await self.client.get_profile()
        self.assertEqual(caught.exception.status_code, 502)
        self.assertEqual(len(self.sent), 3)


class ServiceUnavailableRetryTest(HelperTestCase):
    async def test_503_with_retry_after_is_retried_for_any_method(self):
        self.answers.extend([search_unavailable("2"), ok()])
        self.assertEqual(await self.client.raw_search(q="admin@example.com"), {"ok": True})
        self.assertEqual([r.method for r in self.sent], ["POST", "POST"])
        self.assertSleeps([2.15])
        self.assertGreater(self.client._gate_until, 0.0, "the gate is closed, like for a busy refusal")
        self.assertEqual(self.sent[0].content, self.sent[1].content)

    async def test_503_without_retry_after_is_raised(self):
        detail = {"message": "This search type is currently under maintenance.", "search_type": "raw", "maintenance": True}
        self.answers.append(httpx.Response(503, json={"detail": detail}))
        with self.assertRaises(ServiceUnavailableError) as caught:
            await self.client.raw_search(q="admin@example.com")
        self.assertEqual(caught.exception.message, "This search type is currently under maintenance.")
        self.assertIsNone(caught.exception.retry_after)
        self.assertEqual(len(self.sent), 1)

    async def test_long_or_repeated_503_is_raised(self):
        self.answers.append(search_unavailable("120"))
        with self.assertRaises(ServiceUnavailableError):
            await self.client.get_profile()
        self.assertEqual(len(self.sent), 1, "longer than max_retry_wait")
        self.sent.clear()
        self.answers.extend(search_unavailable("1") for _ in range(4))
        with self.assertRaises(ServiceUnavailableError) as caught:
            await self.client.get_profile()
        self.assertEqual(len(self.sent), 4)
        self.assertEqual((caught.exception.error_code, caught.exception.retry_after), ("search_unavailable", 1.0))

    async def test_503_after_a_synchronous_unlock_is_raised(self):
        # The API can answer it once the records are unlocked and the points spent: a replay
        # would unlock (and charge) the next records.
        calls = [
            ("/search/email/unlock", lambda: self.client.unlock_email_leaks("john@example.com", max_leaks=100)),
            (
                "/search/domain/example.com/employees/unlock",
                lambda: self.client.unlock_domain_leaks("example.com", "employees"),
            ),
            ("/search/advanced/unlock", lambda: self.client.unlock_all_advanced({"username": "john"})),
            ("/search/applications/com.app/unlock", lambda: self.client.unlock_application_leaks("com.app")),
            ("/notification_runs/12/leaks/unlock", lambda: self.client.unlock_notification_run_leaks(12)),
            ("/unlock", lambda: self.client.unlock_specific_leaks(["a1b2"])),
            ("/search/combolist/unlock", lambda: self.client.unlock_combolist_leaks(["cl1_a1b2"])),
            ("/search/email/unlock", lambda: self.client.request("POST", "/search/email/unlock?max=5")),
        ]
        for path, call in calls:
            with self.subTest(path=path):
                self.sent.clear()
                self.answers.append(search_unavailable("2"))
                with self.assertRaises(ServiceUnavailableError) as caught:
                    await call()
                self.assertEqual([(r.method, r.url.path) for r in self.sent], [("POST", path)])
                self.assertEqual(caught.exception.retry_after, 2.0)
        self.assertEqual(self.time.sleeps, [])
        self.assertEqual(self.client._gate_until, 0.0)

    async def test_unlock_refusals_and_unlock_tasks_are_still_retried(self):
        # A rate limit refusal comes before the unlock runs: replaying it is safe.
        self.answers.extend([refusal("busy", retry_after=5, status=503), refusal("rate"), ok()])
        self.assertEqual(await self.client.unlock_email_leaks("john@example.com"), {"ok": True})
        self.assertEqual([r.url.path for r in self.sent], ["/search/email/unlock"] * 3)
        self.sent.clear()
        # The /unlock/task routes answer a 503 before queuing anything, and searches before running.
        self.answers.extend([search_unavailable("2"), ok(), search_unavailable("2"), ok()])
        await self.client.queue_email_unlock_task("john@example.com")
        await self.client.raw_search(q="admin@example.com")
        self.assertEqual(
            [r.url.path for r in self.sent], ["/search/email/unlock/task"] * 2 + ["/search/raw"] * 2
        )


class NoRetryTest(HelperTestCase):
    max_retries = 0

    async def test_max_retries_zero_turns_every_retry_off(self):
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
                    await self.client.get_profile()
                self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.time.sleeps, [])
        self.assertEqual(self.client._gate_until, 0.0)

    async def test_search_unavailable_code(self):
        self.answers.append(search_unavailable("2"))
        with self.assertRaises(ServiceUnavailableError) as caught:
            await self.client.raw_search(q="admin@example.com")
        self.assertEqual(caught.exception.error_code, "search_unavailable")


# -------------------------
# Deprecations and logging
# -------------------------


class DeprecationHeaderTest(HelperTestCase):
    def deprecated(self):
        headers = {
            "Deprecation": "@1798761600",
            "Sunset": "Wed, 01 Sep 2027 00:00:00 GMT",
            "Link": '<https://docs.leakradar.io/changelog>; rel="deprecation"',
        }
        return httpx.Response(200, json={"ok": True}, headers=headers)

    async def test_warned_once_per_route_and_client(self):
        self.route = lambda request: self.deprecated()
        with warnings.catch_warnings(record=True) as caught, self.assertLogs("leakradar", "WARNING") as logs:
            warnings.simplefilter("always")
            await self.client.get_profile()
            await self.client.get_profile()
            await self.client.get_stats()
            other = self.make_client(token="t")
            self.addAsyncCleanup(other.aclose)
            await other.get_profile()
        deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        self.assertEqual(len(deprecations), 3)
        message = str(deprecations[0].message)
        self.assertIn("GET /profile", message)
        self.assertIn("Wed, 01 Sep 2027 00:00:00 GMT", message)
        self.assertIn("https://docs.leakradar.io/changelog", message)
        self.assertIn("GET /stats", str(deprecations[1].message))
        self.assertFromHere(deprecations[0])
        self.assertEqual(len(logs.records), 3)

    async def test_no_header_no_warning(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            await self.client.get_profile()
        self.assertEqual(caught, [])


class LoggingTest(HelperTestCase):
    async def test_debug_log_has_no_secret_nor_query_value(self):
        self.answers.extend([ok(), httpx.ConnectError("x"), ok()])
        with self.assertLogs("leakradar", "DEBUG") as logs:
            await self.client.get_domain_all("example.com", search="john@example.com")
            await self.client.search_email("jane@example.com", search="hunter2")
        text = "\n".join(logs.output)
        self.assertIn("GET /search/domain/example.com/all -> 200", text)
        self.assertIn("POST /search/email", text)
        self.assertIn("ConnectError", text)
        for secret in ("tok_ABC123", "john@example.com", "jane@example.com", "hunter2", "page_size"):
            self.assertNotIn(secret, text)


# -------------------------
# Generic access
# -------------------------


class RequestTest(HelperTestCase):
    async def test_same_pipeline_as_the_methods(self):
        self.answers.append(ok({"items": []}))
        result = await self.client.request("get", "/profile/usage", params={"a": 1, "b": None})
        self.assertEqual(result, {"items": []})
        request = self.sent[0]
        self.assertEqual((request.method, request.url.path, self.query()), ("GET", "/profile/usage", {"a": "1"}))
        self.assertEqual(request.headers["Authorization"], "Bearer tok_ABC123")
        self.assertTrue(request.headers["User-Agent"].startswith("LeakRadar-Python-Client/"))

    async def test_json_body_errors_and_retries(self):
        await self.client.request("POST", "/search/email", json={"email": "john@example.com"})
        self.assertEqual(self.body(), {"email": "john@example.com"})
        self.assertEqual(self.sent[0].headers["Content-Type"], "application/json")
        self.answers.extend([refusal("rate"), ok()])
        self.assertEqual(await self.client.request("GET", "/profile"), {"ok": True})
        self.answers.append(httpx.Response(404, json={"detail": "Not Found"}))
        with self.assertRaises(NotFoundError):
            await self.client.request("GET", "/nope")
        self.answers.append(httpx.Response(200, content=b"a,b\n", headers={"content-type": "text/csv"}))
        self.assertEqual(await self.client.request("GET", "/file"), b"a,b\n")

    async def test_only_the_api_host(self):
        await self.client.request("GET", "https://api.leakradar.io/profile")
        self.assertEqual(str(self.sent[0].url), "https://api.leakradar.io/profile")
        self.sent.clear()
        for url in ("https://evil.example/profile", "http://api.leakradar.io/profile", "https://api.leakradar.io:8443/x"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                await self.client.request("GET", url)
        self.assertEqual(self.sent, [])


# -------------------------
# Subclasses written for earlier releases
# -------------------------


class AppError(Exception):
    """An integration's own exception."""


class PublishedOverridesTest(HelperTestCase):
    """Overrides written to the signatures of 0.1.x and 0.2.0 keep control, new helpers included."""

    def subclass(self, client_class):
        client = self.make_client(client_class, token="t")
        self.addAsyncCleanup(client.aclose)
        return client

    def test_request_keeps_its_published_signature(self):
        # The 0.1.5-0.2.0 signature: an override written for it must get every call of the client.
        parameters = inspect.signature(LeakRadarClient._request).parameters
        keywords = ["params", "json", "headers", "follow_redirects"]
        self.assertEqual(list(parameters), ["self", "method", "endpoint"] + keywords)
        self.assertEqual([name for name, p in parameters.items() if p.kind is p.KEYWORD_ONLY], keywords)

    async def test_a_published_request_override_runs_the_new_helpers(self):
        calls = []

        class Audited(LeakRadarClient):
            async def _request(self, method, endpoint, *, params=None, json=None, headers=None, follow_redirects=False):
                calls.append((method, endpoint))
                return await super()._request(
                    method, endpoint, params=params, json=json, headers=headers, follow_redirects=follow_redirects
                )

        client = self.subclass(Audited)
        client._close_gate(30)
        self.assertEqual(await client.rate_limits(), {"ok": True})
        self.assertEqual(self.time.sleeps, [], "GET /rate-limits never waits for the gate")
        self.answers.append(exports(export("COMPLETED")))
        self.assertEqual((await client.wait_for_export(7))["id"], 7)
        self.assertEqual(calls, [("GET", "/rate-limits"), ("GET", "/exports")])

    async def test_a_published_list_exports_override(self):
        class Published(LeakRadarClient):
            async def list_exports(self, page=1, page_size=20):
                return await super().list_exports(page=page, page_size=page_size)

        client = self.subclass(Published)
        self.answers.extend([exports(export("COMPLETED")), exports(export("COMPLETED"))])
        self.assertEqual((await client.wait_for_export(7))["id"], 7)
        self.assertIsNone(await client.download_export(7))
        self.assertEqual([self.query(i) for i in range(2)], [{"page": "1", "page_size": "20", "export_id": "7"}] * 2)

    async def test_a_handle_error_override_raises_its_own_exception(self):
        class Mapped(LeakRadarClient):
            async def _handle_error(self, response):
                raise AppError(response.status_code)

        client = self.subclass(Mapped)
        self.route = lambda request: httpx.Response(404, json={"detail": "Not Found"})
        with self.assertRaises(AppError):
            await client.get_profile()
        with self.assertRaises(AppError):
            await client.get_raw_download_url(55)

    async def test_a_handle_error_override_that_returns_gets_the_answer(self):
        class NotFoundIsEmpty(LeakRadarClient):
            async def _handle_error(self, response):
                if response.status_code != 404:
                    await super()._handle_error(response)

        client = self.subclass(NotFoundIsEmpty)
        self.route = lambda request: httpx.Response(404, json={"detail": "Run not found"})
        self.assertEqual(await client.get_notification_run(1), {"detail": "Run not found"})
        with self.assertRaises(LeakRadarAPIError) as caught:
            await client.get_raw_download_url(55)
        self.assertEqual(
            (caught.exception.status_code, caught.exception.detail),
            (404, "Unexpected response while requesting raw download URL."),
        )
        self.route = lambda request: httpx.Response(400, json={"detail": "Bad request"})
        with self.assertRaises(BadRequestError):
            await client.get_profile()

    async def test_handle_error_sees_the_final_answer_only(self):
        seen = []

        class Logs(LeakRadarClient):
            async def _handle_error(self, response):
                seen.append(response.status_code)
                await super()._handle_error(response)

        client = self.make_client(Logs, token="t", max_retries=2)
        self.addAsyncCleanup(client.aclose)
        self.answers.extend([refusal("rate"), refusal("rate"), refusal("rate")])
        with self.assertRaises(TooManyRequestsError):
            await client.get_profile()
        self.assertEqual((len(self.sent), seen), (3, [429]))
        self.answers.extend([refusal("rate"), ok()])
        self.assertEqual(await client.get_profile(), {"ok": True})
        self.assertEqual(seen, [429], "a refusal retried with success never reaches the hook")


# -------------------------
# Domains
# -------------------------


class DomainTest(HelperTestCase):
    CASES = [
        # (argument, searched domain, warned)
        ("example.com", "example.com", False),
        ("  Example.COM. ", "example.com", False),
        ("www.example.com", "www.example.com", False),
        ("john@example.com", "john@example.com", False),
        ("*.example.com", "*.example.com", False),
        ("https://www.example.com/pt-br", "example.com", True),
        ("http://Example.com:8080/path?q=1#top", "example.com", True),
        ("example.com/login", "example.com", True),
        ("example.com:443", "example.com", True),
        ("example.com?ref=x", "example.com", True),
        ("https://www.com/", "www.com", True),
        ("https://www.sub.example.co.uk", "sub.example.co.uk", True),
        ("https://user:pass@shop.example.com/", "shop.example.com", True),
        ("https://example.com./", "example.com", True),
    ]
    INVALID = ["", "   ", ".", "/", "?q=1", "https://", "https:///path", "http://[::1"]

    def test_table(self):
        for value, expected, warned in self.CASES:
            with self.subTest(value=value), warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                self.assertEqual(_domain(value), expected)
                if warned:
                    self.assertEqual(len(caught), 1)
                    self.assertIs(caught[0].category, UserWarning)
                    self.assertEqual(
                        str(caught[0].message), f"'{value.strip()}' is a URL: searching the domain '{expected}'."
                    )
                else:
                    self.assertEqual(caught, [])
        for value in self.INVALID:
            with self.subTest(value=value), self.assertRaises(ValueError) as caught:
                _domain(value)
            self.assertEqual(str(caught.exception), "Pass a domain such as example.com")

    async def test_every_domain_argument_is_normalized(self):
        calls = [
            ("get_domain_report", (), "/search/domain/example.com"),
            ("get_domain_report_pdf", (), "/search/domain/example.com/report/pdf"),
            ("get_domain_all", (), "/search/domain/example.com/all"),
            ("get_domain_customers", (), "/search/domain/example.com/customers"),
            ("get_domain_employees", (), "/search/domain/example.com/employees"),
            ("get_domain_third_parties", (), "/search/domain/example.com/third_parties"),
            ("get_domain_subdomains", (), "/search/domain/example.com/subdomains"),
            ("export_domain_subdomains", (), "/search/domain/example.com/subdomains/export"),
            ("get_domain_urls", (), "/search/domain/example.com/urls"),
            ("export_domain_urls", (), "/search/domain/example.com/urls/export"),
            ("export_domain_leaks", ("employees",), "/search/domain/example.com/employees/export"),
            ("unlock_domain_leaks", ("employees",), "/search/domain/example.com/employees/unlock"),
            ("queue_domain_unlock_task", ("employees",), "/search/domain/example.com/employees/unlock/task"),
            ("search_combolist_domain", (), "/search/combolist/domain/example.com"),
            ("get_combolist_domain_report", (), "/search/combolist/domain/example.com/report"),
        ]
        for name, extra, path in calls:
            with self.subTest(method=name), warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                self.sent.clear()
                await getattr(self.client, name)("https://www.Example.com/fr/", *extra)
                self.assertEqual(self.sent[0].url.path, path)
                self.assertEqual(len(caught), 1)
                self.assertFromHere(caught[0])

    async def test_mass_check_and_combolist_scopes(self):
        # The API reduces the entries of the mass check itself, and answers junk as not found:
        # sent as given, as before 0.3.0 (one junk entry must not fail a batch of 100).
        entries = ["https://www.a-example.com/x", "B-Example.com", "#N/A", "N/A", "/"]
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            await self.client.domains_locked_exists(list(entries))
            await self.client.domains_locked_exists(tuple(entries[:2]))
        self.assertEqual(self.body(0)["domains"], entries)
        self.assertEqual(self.body(1)["domains"], entries[:2])
        self.sent.clear()
        await self.client.domains_locked_exists("example.com")
        self.assertEqual(self.body()["domains"], ["example.com"])
        self.sent.clear()
        with self.assertWarns(UserWarning):
            await self.client.queue_combolist_unlock_task("domain", value="http://www.example.com/")
        self.assertEqual(self.body(), {"scope": "domain", "value": "example.com"})
        self.sent.clear()
        with self.assertWarns(UserWarning):
            await self.client.export_combolist("domain", value="example.com/")
        self.assertEqual(self.body()["value"], "example.com")
        self.sent.clear()
        await self.client.export_combolist("email", value="John@Example.com")
        self.assertEqual(self.body()["value"], "John@Example.com", "only the domain scope is a domain")

    async def test_an_invalid_domain_sends_nothing(self):
        for call in (
            lambda: self.client.get_domain_all(""),
            lambda: self.client.get_domain_report("https://"),
            lambda: self.client.queue_combolist_unlock_task("domain", value="?"),
        ):
            with self.assertRaises(ValueError):
                await call()
        self.assertEqual(self.sent, [])


# -------------------------
# New operations and parameters
# -------------------------


class NewOperationsTest(HelperTestCase):
    async def test_extend_monitor_to_all_datasets(self):
        await self.client.extend_monitor_to_all_datasets(5)
        self.assertEqual((self.sent[0].method, self.sent[0].url.path), ("POST", "/notifications/5/scope_all"))
        self.assertEqual(self.body(), {}, "the API requires a body, even empty")
        self.sent.clear()
        await self.client.extend_monitor_to_all_datasets(5, absorb_ids=(6, 7), reactivate=True)
        self.assertEqual(self.body(), {"absorb_ids": [6, 7], "reactivate": True})

    async def test_extend_monitors_to_all_datasets(self):
        await self.client.extend_monitors_to_all_datasets([1, 2], merge=False)
        self.assertEqual((self.sent[0].method, self.sent[0].url.path), ("POST", "/notifications/scope_all/bulk"))
        self.assertEqual(self.body(), {"ids": [1, 2], "merge": False})
        self.sent.clear()
        await self.client.extend_monitors_to_all_datasets([3])
        self.assertEqual(self.body(), {"ids": [3]})

    async def test_new_parameters(self):
        await self.client.list_exports(export_id=9)
        self.assertEqual(self.query(), {"page": "1", "page_size": "20", "export_id": "9"})
        self.sent.clear()
        await self.client.list_exports()
        self.assertEqual(self.query(), {"page": "1", "page_size": "20"})
        self.sent.clear()
        await self.client.list_raw_downloads(file_id=3)
        self.assertEqual(self.query()["file_id"], "3")
        self.sent.clear()
        await self.client.emails_locked_exists(["john@example.com"], datasets="stealer_logs, combolist")
        self.assertEqual(self.body(), {"emails": ["john@example.com"], "datasets": ["stealer_logs", "combolist"]})
        self.sent.clear()
        await self.client.domains_locked_exists(["example.com"], include_counts=True, datasets=("combolist",))
        self.assertEqual(self.body(), {"domains": ["example.com"], "include_counts": True, "datasets": ["combolist"]})
        self.sent.clear()
        await self.client.emails_locked_exists(["john@example.com"])
        self.assertNotIn("datasets", self.body())

    async def test_all_test_type(self):
        await self.client.test_notification_method_with_data(3, "all")
        self.assertEqual(self.query(), {"notification_type": "all"})
        self.sent.clear()
        with self.assertWarns(DeprecationWarning):
            await self.client.test_notification_method_with_data(3, {"notification_type": "all"})
        self.assertEqual(self.query(), {"notification_type": "all"})


class DocstringTest(unittest.TestCase):
    """What the docstrings must say, on both clients (the synchronous one is generated)."""

    def assertDocSays(self, method, *texts):
        """Each text is in the docstring of `method`, line breaks and indentation aside."""
        for client_class in (LeakRadarClient, leakradar.LeakRadar):
            doc = " ".join((inspect.getdoc(getattr(client_class, method)) or "").split())
            for text in texts:
                with self.subTest(client=client_class.__name__, method=method, text=text):
                    self.assertIn(" ".join(text.split()), doc)

    def test_raw_download_id_is_the_number_in_download_url(self):
        # The item `id` of list_raw_downloads() is another number: the obvious call fails.
        self.assertDocSays("get_raw_download_url", "`download_url`", "not the item's `id`", "`download_token`")
        self.assertDocSays("download_raw_file", "`download_url`", "not its `id`", "`download_token`")

    def test_synchronous_unlocks_say_when_max_leaks_is_needed(self):
        # Without max_leaks the API caps at the whole point balance: above 10,000 points, a 400.
        for method, queue in (
            ("unlock_email_leaks", "queue_email_unlock_task()"),
            ("unlock_domain_leaks", "queue_domain_unlock_task()"),
            ("unlock_all_advanced", "queue_advanced_unlock_task()"),
            ("unlock_application_leaks", "queue_application_unlock_task()"),
        ):
            self.assertDocSays(method, "at most 10,000 records", "max_leaks", "BadRequestError", queue)

    def test_raw_search_pages_by_cursor_and_names_its_selectors(self):
        self.assertDocSays("raw_search", "page: echoed back, not used by the API", "the only way to the next page")
        self.assertDocSays("paginate", "except for raw_search(), which pages by cursor only")
        # The selectors the client accepts without q (_RAW_SELECTOR_FIELDS), and only those.
        doc = " ".join((inspect.getdoc(LeakRadarClient.raw_search) or "").split())
        selectors = doc.split("give at least one selector: ", 1)[1].split(" (", 1)[0]
        named = {name.strip() for name in selectors.replace(" or ", ", ").split(",")}
        expected = {name.replace("ingested_at_min", "ingested_at_*") for name in LeakRadarClient._RAW_SELECTOR_FIELDS}
        self.assertEqual(named, expected - {"ingested_at_max"})
        self.assertIn("(folder_name_not and exclude_terms only narrow a selection)", doc)

    def test_maintenance_is_not_given_as_a_retried_503(self):
        # A maintenance 503 carries no Retry-After: the client raises it at once.
        for client_class in (LeakRadarClient, leakradar.LeakRadar):
            for doc in (inspect.getdoc(ServiceUnavailableError), inspect.getdoc(client_class.__init__)):
                with self.subTest(client=client_class.__name__):
                    self.assertNotIn("unavailable, maintenance", " ".join((doc or "").split()))
        self.assertIn("A maintenance 503 carries no Retry-After", " ".join(ServiceUnavailableError.__doc__.split()))

    def test_api_facts(self):
        self.assertDocSays("queue_combolist_unlock_task", "At most 2 combolist unlock tasks queued or running")
        self.assertDocSays("test_notification_method_with_data", "one POST per dataset, four in this test")
        self.assertDocSays("export_domain_subdomains", 'Returns the file as bytes for format "csv"')
        self.assertNotIn("server configuration", inspect.getdoc(LeakRadarClient.export_domain_subdomains))
        self.assertIn("same alert can arrive up to three times", " ".join(leakradar.webhooks.__doc__.split()))
        self.assertDocSays("search_dark_web", "Paging stops at page * page_size = 10,000")


# -------------------------
# Waiting for tasks and exports
# -------------------------


def task(status=None, **fields):
    body = {"task_id": "42", "kind": "UNLOCK", "scope": "email", "total": 10, "updated": 0}
    if status is not None:
        body["status"] = status
        body["running"] = status in ("QUEUED", "RUNNING")
        body["completed"] = status == "COMPLETED"
    body.update(fields)
    return httpx.Response(200, json=body)


def exports(*items):
    return httpx.Response(200, json={"items": list(items), "total": len(items), "page": 1, "page_size": 20})


def export(status, export_id=7, filename=""):
    return {"id": export_id, "user_id": 1, "type": "email_export", "status": status, "filename": filename,
            "params": {}, "timestamp": "2026-10-29T10:00:00Z"}


class WaitForTaskTest(HelperTestCase):
    async def test_until_completed(self):
        self.answers.extend([task("QUEUED"), task("RUNNING", updated=5), task("COMPLETED", updated=10)])
        seen = []
        result = await self.client.wait_for_task("42", on_progress=seen.append)
        self.assertEqual((result["status"], result["updated"]), ("COMPLETED", 10))
        self.assertEqual([s["status"] for s in seen], ["QUEUED", "RUNNING", "COMPLETED"])
        self.assertEqual([r.url.path for r in self.sent], ["/tasks/42"] * 3)
        self.assertEqual(self.time.sleeps, [2.0, 2.0])

    async def test_task_id_from_the_queue_answer_and_async_progress(self):
        self.answers.extend([task("RUNNING"), task("COMPLETED")])
        seen = []

        async def progress(state):
            await asyncio.sleep(0)
            seen.append(state["status"])

        await self.client.wait_for_task({"task_id": 42, "status": "QUEUED"}, interval=0.5, on_progress=progress)
        self.assertEqual(seen, ["RUNNING", "COMPLETED"])
        self.assertEqual(self.time.sleeps, [0.5])

    async def test_failed_and_canceled(self):
        error = {"code": "insufficient_points", "message": "Need 120 points, have 3"}
        self.answers.extend([task("RUNNING"), task("FAILED", error=error)])
        with self.assertRaises(TaskFailedError) as caught:
            await self.client.wait_for_task(42)
        failure = caught.exception
        self.assertEqual(
            (failure.task_id, failure.status, failure.code, failure.message),
            ("42", "FAILED", "insufficient_points", "Need 120 points, have 3"),
        )
        self.assertEqual(failure.task["error"], error)
        self.answers.append(task("CANCELED", error=None))
        with self.assertRaises(TaskFailedError) as caught:
            await self.client.wait_for_task("42")
        self.assertEqual((caught.exception.status, caught.exception.code), ("CANCELED", None))

    async def test_legacy_flags_without_status(self):
        self.answers.extend([task(running=True, completed=False), task(running=False, completed=True)])
        self.assertTrue((await self.client.wait_for_unlocked_list_task("node:9"))["completed"])
        self.assertEqual(self.sent[0].url.raw_path, b"/profile/unlocked/list-tasks/node%3A9")
        self.answers.append(task(running=False, completed=False))
        with self.assertRaises(TaskFailedError) as caught:
            await self.client.wait_for_unlocked_list_task("node:9")
        self.assertEqual((caught.exception.status, caught.exception.code), ("FAILED", None))

    async def test_timeout(self):
        self.route = lambda request: task("RUNNING", updated=3)
        with self.assertRaises(WaitTimeoutError) as caught:
            await self.client.wait_for_task("42", timeout=5)
        error = caught.exception
        self.assertIsInstance(error, TimeoutError)
        self.assertEqual(error.waited, 5.0)
        self.assertEqual(error.last["updated"], 3)
        self.assertIn("RUNNING", str(error))
        self.assertEqual(self.time.sleeps, [2.0, 2.0, 1.0])
        self.assertEqual(len(self.sent), 4)
        self.sent.clear()
        with self.assertRaises(WaitTimeoutError):
            await self.client.wait_for_task("42", timeout=0)
        self.assertEqual(len(self.sent), 1, "timeout=0 reads once")

    async def test_errors_and_arguments(self):
        self.answers.append(httpx.Response(404, json={"detail": "Task not found."}))
        with self.assertRaises(NotFoundError):
            await self.client.wait_for_task("999")
        for kwargs in ({"interval": 0}, {"interval": -1}, {"timeout": -1}, {"interval": True}):
            with self.subTest(**kwargs), self.assertRaises(ValueError):
                await self.client.wait_for_task("42", **kwargs)
        for task_id in ("", None, True, {"status": "QUEUED"}):
            with self.subTest(task_id=task_id), self.assertRaises(ValueError):
                await self.client.wait_for_task(task_id)

    async def test_a_wrong_argument_is_named_by_its_type_only(self):
        # The records of a synchronous unlock, passed instead of the queued task: never repeated.
        records = [{"email": "john@example.com", "password": "s3cret"}]
        for wait in (self.client.wait_for_task, self.client.wait_for_unlocked_list_task):
            with self.subTest(method=wait.__name__), self.assertRaises(ValueError) as caught:
                await wait(records)
            self.assertIn("(got list)", str(caught.exception))
            self.assertNotIn("s3cret", str(caught.exception))
        self.assertEqual(self.sent, [])


class WaitForExportTest(HelperTestCase):
    async def test_until_completed(self):
        url = "https://files.example.test/exports/7.zip"
        self.answers.extend([exports(export("PENDING")), exports(export("IN_PROGRESS")), exports(export("COMPLETED", filename=url))])
        seen = []
        result = await self.client.wait_for_export({"export_id": 7, "status": "PENDING"}, on_progress=seen.append)
        self.assertEqual((result["status"], result["filename"]), ("COMPLETED", url))
        self.assertEqual([s["status"] for s in seen], ["PENDING", "IN_PROGRESS", "COMPLETED"])
        self.assertEqual(self.query(), {"page": "1", "page_size": "20", "export_id": "7"})
        self.assertEqual(self.time.sleeps, [3.0, 3.0])

    async def test_error_unknown_and_timeout(self):
        self.answers.append(exports(export("ERROR")))
        with self.assertRaises(ExportFailedError) as caught:
            await self.client.wait_for_export(7)
        self.assertEqual((caught.exception.export_id, caught.exception.status), (7, "ERROR"))
        self.assertEqual(caught.exception.export["id"], 7)
        self.answers.append(exports())
        with self.assertRaises(NotFoundError):
            await self.client.wait_for_export("7")
        # An API older than the export_id filter lists the latest exports instead.
        self.answers.append(exports(export("COMPLETED", export_id=8)))
        with self.assertRaises(NotFoundError):
            await self.client.wait_for_export(7)
        self.route = lambda request: exports(export("IN_PROGRESS"))
        with self.assertRaises(WaitTimeoutError):
            await self.client.wait_for_export(7, timeout=10)
        for value in (0, "x", None, {"status": "PENDING"}, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                await self.client.wait_for_export(value)

    async def test_a_wrong_argument_is_named_by_its_type_only(self):
        # A search answer passed instead of the export: its records are never repeated.
        answer = {"items": [{"email": "john@example.com", "password": "s3cret"}], "total": 1}
        for call in (self.client.wait_for_export, self.client.download_export):
            with self.subTest(method=call.__name__), self.assertRaises(ValueError) as caught:
                await call(answer)
            self.assertIn("(got dict)", str(caught.exception))
            self.assertNotIn("s3cret", str(caught.exception))
        self.assertEqual(self.sent, [])


# -------------------------
# Export downloads
# -------------------------


class BrokenStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"partial"
        raise httpx.ReadError("connection reset")


class DownloadExportTest(HelperTestCase):
    STORAGE = "https://files.example.test/exports/7.zip?X-Amz-Signature=abc"

    def storage(self, request):
        if request.url.host == "files.example.test":
            return httpx.Response(200, content=b"PK\x03\x04zip", headers={"content-type": "application/zip"})
        return ok()

    async def test_to_bytes_without_the_api_key_on_another_host(self):
        self.route = self.storage
        data = await self.client.download_export(export("COMPLETED", filename=self.STORAGE))
        self.assertEqual(data, b"PK\x03\x04zip")
        request = self.sent[0]
        self.assertEqual(str(request.url), self.STORAGE)
        self.assertNotIn("Authorization", request.headers)
        self.assertTrue(request.headers["User-Agent"].startswith("LeakRadar-Python-Client/"))

    async def test_from_an_id_reads_the_export_first(self):
        self.answers.append(exports(export("COMPLETED", filename=self.STORAGE)))
        self.route = self.storage
        self.assertEqual(await self.client.download_export(7), b"PK\x03\x04zip")
        self.assertEqual([r.url.host for r in self.sent], ["api.leakradar.io", "files.example.test"])
        self.assertEqual(self.sent[0].headers["Authorization"], "Bearer tok_ABC123")
        self.sent.clear()
        self.answers.append(exports(export("COMPLETED", filename=self.STORAGE)))
        await self.client.download_export({"export_id": 7, "status": "PENDING", "message": "Export queued."})
        self.assertEqual(len(self.sent), 2)

    async def test_the_api_key_only_goes_to_the_api(self):
        for filename in ("/exports/7/file", "https://api.leakradar.io/exports/7/file"):
            with self.subTest(filename=filename):
                self.sent.clear()
                self.answers.append(httpx.Response(200, content=b"zip"))
                self.assertEqual(await self.client.download_export(export("COMPLETED", filename=filename)), b"zip")
                self.assertEqual(str(self.sent[0].url), "https://api.leakradar.io/exports/7/file")
                self.assertEqual(self.sent[0].headers["Authorization"], "Bearer tok_ABC123")
        # A redirect away from the API drops the key.
        self.sent.clear()
        self.answers.append(httpx.Response(302, headers={"Location": self.STORAGE}))
        self.route = self.storage
        await self.client.download_export(export("COMPLETED", filename="/exports/7/file"))
        self.assertEqual([r.url.host for r in self.sent], ["api.leakradar.io", "files.example.test"])
        self.assertNotIn("Authorization", self.sent[1].headers)

    async def test_nothing_to_export_and_not_ready(self):
        self.assertIsNone(await self.client.download_export(export("COMPLETED", filename="")))
        self.assertEqual(self.sent, [])
        self.answers.append(exports(export("IN_PROGRESS")))
        with self.assertRaises(ValueError) as caught:
            await self.client.download_export(export("IN_PROGRESS"))
        self.assertIn("IN_PROGRESS", str(caught.exception))
        self.assertEqual(len(self.sent), 1, "read again before giving up")

    async def test_storage_errors(self):
        self.answers.append(httpx.Response(403, text="<Error><Code>AccessDenied</Code></Error>"))
        with self.assertRaises(ForbiddenError) as caught:
            await self.client.download_export(export("COMPLETED", filename=self.STORAGE))
        self.assertIn("AccessDenied", caught.exception.detail)

    async def test_to_a_file(self):
        self.route = self.storage
        with tempfile.TemporaryDirectory() as folder:
            target = os.path.join(folder, "export.zip")
            result = await self.client.download_export(export("COMPLETED", filename=self.STORAGE), target, chunk_size=2)
            self.assertEqual(result, target)
            with open(target, "rb") as handle:
                self.assertEqual(handle.read(), b"PK\x03\x04zip")
            self.assertEqual(os.listdir(folder), ["export.zip"])

    async def test_a_failed_download_keeps_the_previous_file(self):
        self.route = lambda request: httpx.Response(200, stream=BrokenStream())
        with tempfile.TemporaryDirectory() as folder:
            target = os.path.join(folder, "export.zip")
            with open(target, "wb") as handle:
                handle.write(b"previous")
            with self.assertRaises(httpx.ReadError):
                await self.client.download_export(export("COMPLETED", filename=self.STORAGE), target)
            with open(target, "rb") as handle:
                self.assertEqual(handle.read(), b"previous")
            self.assertEqual(os.listdir(folder), ["export.zip"], "no temporary file left")
        with self.assertRaises(ValueError):
            await self.client.download_export(export("COMPLETED", filename=self.STORAGE), chunk_size=0)

    async def test_a_planted_part_file_is_left_alone(self):
        # "<path>.part" was the temporary name before: another local user could create or link it first.
        self.route = self.storage
        with tempfile.TemporaryDirectory() as folder:
            target = os.path.join(folder, "export.zip")
            with open(target + ".part", "wb") as handle:
                handle.write(b"planted")
            written = await self.client.download_export(export("COMPLETED", filename=self.STORAGE), target)
            self.assertEqual(written, target)
            with open(target, "rb") as handle:
                self.assertEqual(handle.read(), b"PK\x03\x04zip")
            with open(target + ".part", "rb") as handle:
                self.assertEqual(handle.read(), b"planted")
            self.assertEqual(sorted(os.listdir(folder)), ["export.zip", "export.zip.part"])

    @unittest.skipIf(os.name == "nt", "POSIX file modes")
    async def test_the_archive_is_readable_by_its_owner_only(self):
        self.route = self.storage
        with tempfile.TemporaryDirectory() as folder:
            target = os.path.join(folder, "export.zip")
            await self.client.download_export(export("COMPLETED", filename=self.STORAGE), target)
            self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o600)


# -------------------------
# Pagination
# -------------------------


def listing(count, with_total=True, has_more=None):
    """A page-numbered listing of `count` items {"n": 0..count-1}."""

    def route(request):
        page = int(request.url.params.get("page", "1"))
        size = int(request.url.params.get("page_size", "100"))
        if (page - 1) * size >= count and page > 1 and with_total:
            return httpx.Response(404, json={"detail": "Page out of range"})
        body = {"items": [{"n": n} for n in range((page - 1) * size, min(page * size, count))], "page": page, "page_size": size}
        if with_total:
            body["total"] = count
        if has_more is not None:
            body["has_more"] = has_more(page, size)
        return httpx.Response(200, json=body)

    return route


class PaginateTest(HelperTestCase):
    async def collect(self, *args, **kwargs):
        return [item async for item in self.client.paginate(*args, **kwargs)]

    def pages(self):
        return [int(r.url.params["page"]) for r in self.sent]

    async def test_stops_once_page_times_size_reaches_total(self):
        self.route = listing(30)
        items = await self.collect(self.client.get_unlocked_leaks, page_size=10)
        self.assertEqual([i["n"] for i in items], list(range(30)))
        self.assertEqual(self.pages(), [1, 2, 3])
        self.assertEqual({r.url.params["page_size"] for r in self.sent}, {"10"})

    async def test_stops_on_a_short_or_empty_page(self):
        self.route = listing(25, with_total=False)
        self.assertEqual(len(await self.collect(self.client.get_unlocked_leaks, page_size=10)), 25)
        self.assertEqual(self.pages(), [1, 2, 3])
        self.sent.clear()
        self.route = listing(20, with_total=False)
        self.assertEqual(len(await self.collect(self.client.get_unlocked_leaks, page_size=10)), 20)
        self.assertEqual(self.pages(), [1, 2, 3], "the third page is empty")

    async def test_monitoring_run_items_are_read_up_to_total(self):
        # Records removed from the index after the run leave short, even empty, pages before the end.
        counts = {1: 99, 2: 0, 3: 50}

        def route(request):
            page = int(request.url.params["page"])
            items = [{"n": page * 1000 + n} for n in range(counts[page])]
            return ok({"items": items, "total": 250, "page": page, "page_size": 100})

        self.route = route
        items = await self.collect(self.client.get_notification_run_items, 12, page_size=100)
        self.assertEqual(len(items), 149)
        self.assertEqual(self.pages(), [1, 2, 3])
        self.assertEqual({r.url.path for r in self.sent}, {"/notification_runs/12/items"})
        self.sent.clear()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            self.assertEqual(len(await self.collect(self.client.notification_run_leaks, 12)), 149)
        self.assertEqual(self.pages(), [1, 2, 3])
        # Every other listing still ends on a short page.
        self.sent.clear()
        self.assertEqual(len(await self.collect(self.client.get_unlocked_leaks, page_size=100)), 99)
        self.assertEqual(self.pages(), [1])

    async def test_has_more_decides_when_present(self):
        self.route = listing(40, with_total=False, has_more=lambda page, size: page < 2)
        self.assertEqual(len(await self.collect(self.client.container_subfolders, 5, page_size=10)), 20)
        self.assertEqual(self.pages(), [1, 2])

    async def test_max_items_and_default_size(self):
        self.route = listing(250)
        items = await self.collect(self.client.get_domain_all, "example.com", max_items=120)
        self.assertEqual(len(items), 120)
        self.assertEqual(self.pages(), [1, 2])
        self.assertEqual(self.sent[0].url.params["page_size"], "100", "the method's default")
        self.sent.clear()
        self.assertEqual(await self.collect(self.client.get_domain_all, "example.com", max_items=0), [])
        self.assertEqual(self.sent, [])

    async def test_arguments_reach_the_method(self):
        self.route = listing(30)
        items = await self.collect(self.client.get_domain_all, "example.com", 2, 10, search="admin")
        self.assertEqual(items[0], {"n": 10})
        self.assertEqual(self.pages(), [2, 3])
        self.assertEqual(self.sent[0].url.path, "/search/domain/example.com/all")
        self.assertEqual(self.sent[0].url.params["search"], "admin")
        self.sent.clear()
        await self.collect(self.client.get_unlocked_leaks, page=3, page_size=10)
        self.assertEqual(self.pages(), [3])

    async def test_a_listing_that_shrinks_ends_quietly(self):
        first_page = {"items": [{"n": 1}, {"n": 2}], "total": 4, "page": 1, "page_size": 2}
        self.answers.extend([ok(first_page), httpx.Response(404, json={"detail": "Page out of range"})])
        self.assertEqual(len(await self.collect(self.client.list_exports, page_size=2)), 2)
        # Any other 404, or one on the first page, is raised.
        self.answers.extend([ok(first_page), httpx.Response(404, json={"detail": "Combolist search is not available."})])
        with self.assertRaises(NotFoundError):
            await self.collect(self.client.get_unlocked_combolists, page_size=2)
        self.answers.append(httpx.Response(404, json={"detail": "Page out of range"}))
        with self.assertRaises(NotFoundError):
            await self.collect(self.client.list_exports)

    async def test_deep_paging_stops_with_a_warning(self):
        self.route = listing(5_000_000)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            items = await self.collect(self.client.search_advanced, page=1000, page_size=1000, username="john")
        self.assertEqual(len(items), 1000)
        self.assertEqual(self.pages(), [1000], "page 1001 would be past 1,000,000 results")
        self.assertEqual(len(caught), 1)
        self.assertIs(caught[0].category, UserWarning)
        self.assertIn("export_advanced()", str(caught[0].message))
        self.assertFromHere(caught[0])
        self.sent.clear()
        with self.assertWarns(UserWarning) as warned:
            self.assertEqual(await self.collect(self.client.get_domain_employees, "example.com", page=1001, page_size=1000), [])
        self.assertIn('export_domain_leaks(domain, "employees")', str(warned.warning))
        self.assertEqual(self.sent, [])

    async def test_dark_web_stops_at_its_window_with_a_warning(self):
        total = 25_000

        def route(request):
            page, size = int(request.url.params["page"]), int(request.url.params["page_size"])
            if page * size > 10_000:
                detail = "Pagination limit exceeded. Maximum offset + page_size is 10000."
                return httpx.Response(400, json={"detail": detail})
            items = [{"n": n} for n in range((page - 1) * size, min(page * size, total))]
            return ok({"items": items, "total": total, "page": page, "page_size": size})

        self.route = route
        # 100 pages from a mock transport that never pauses: not a slow callback for debug mode.
        asyncio.get_running_loop().slow_callback_duration = 10.0
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            items = await self.collect(self.client.search_dark_web, "acme", page_size=100)
        self.assertEqual(len(items), 10_000)
        self.assertEqual(self.pages(), list(range(1, 101)), "page 101 would be past the window")
        self.assertEqual(len(caught), 1)
        self.assertIs(caught[0].category, UserWarning)
        self.assertIn("at most 10,000 results", str(caught[0].message))
        self.assertIn("date_from / date_to", str(caught[0].message))
        self.assertFromHere(caught[0])

    async def test_other_listings_have_no_deep_cap(self):
        self.route = listing(1_001_500)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            items = await self.collect(self.client.get_unlocked_leaks, page=1001, page_size=1000)
        self.assertEqual((len(items), self.pages(), caught), (1500, [1001, 1002], []))

    async def test_raw_search_follows_the_cursor(self):
        self.answers.extend([
            ok({"items": [{"n": 1}, {"n": 2}], "total": -1, "page": 1, "page_size": 2, "has_more": True, "next_cursor": "c1"}),
            ok({"items": [], "total": -1, "page": 2, "page_size": 2, "has_more": True, "next_cursor": "c2"}),
            ok({"items": [{"n": 3}], "total": -1, "page": 3, "page_size": 2, "has_more": False, "next_cursor": None}),
        ])
        items = await self.collect(self.client.raw_search, "admin@example.com", page_size=2, q_exact=True)
        self.assertEqual([i["n"] for i in items], [1, 2, 3], "a short or empty page is not the end while has_more")
        self.assertEqual([self.query(i).get("cursor") for i in range(3)], [None, "c1", "c2"])
        self.assertEqual({self.sent[i].content for i in range(3)}, {self.sent[0].content})
        self.assertEqual({self.query(i)["page_size"] for i in range(3)}, {"2"})

    async def test_raw_search_without_a_new_cursor_stops(self):
        self.answers.append(ok({"items": [{"n": 1}], "total": -1, "page": 1, "page_size": 1, "has_more": True, "next_cursor": None}))
        self.assertEqual(len(await self.collect(self.client.raw_search, "admin@example.com", page_size=1)), 1)
        self.assertEqual(len(self.sent), 1)
        self.sent.clear()
        self.answers.extend([
            ok({"items": [{"n": 1}], "total": -1, "page": 1, "page_size": 1, "has_more": True, "next_cursor": "same"}),
            ok({"items": [{"n": 2}], "total": -1, "page": 2, "page_size": 1, "has_more": True, "next_cursor": "same"}),
        ])
        self.assertEqual(len(await self.collect(self.client.raw_search, "admin@example.com", page_size=1)), 2)
        self.assertEqual(len(self.sent), 2)

    async def test_wrong_methods_and_arguments(self):
        with self.assertRaises(TypeError):
            await self.collect(self.client.get_profile)
        self.answers.append(ok())
        with self.assertRaises(TypeError):
            await self.collect(self.client.get_unlocked_leaks)
        for kwargs in ({"max_items": -1}, {"page_size": 0}, {"max_items": 1.5}):
            with self.subTest(**kwargs), self.assertRaises(ValueError):
                await self.collect(self.client.get_unlocked_leaks, **kwargs)


if __name__ == "__main__":
    unittest.main()
