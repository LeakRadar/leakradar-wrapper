"""Rate limit refusals: what TooManyRequestsError exposes, the retries and the gate shared by the tasks of a client."""

import asyncio
import unittest
from datetime import datetime, timezone

import httpx

from leakradar import LeakRadarAPIError, LeakRadarClient, ServiceUnavailableError, TooManyRequestsError


def refusal(reason, family="search", scope="credential", retry_after=1, status=429, **extra):
    """An answer in the API's unified refusal format."""
    error = {
        "code": "rate_limited",
        "reason": reason,
        "scope": scope,
        "family": family,
        "retry_after": retry_after,
        "retry_at": "2026-10-21T14:32:10Z",
        "docs": "https://docs.leakradar.io/#section/Rate-limits",
    }
    error.update(extra)
    headers = {
        "Retry-After": str(retry_after),
        "X-LR-RateLimit-Reason": reason,
        "X-LR-RateLimit-Scope": scope,
        "X-RateLimit-Limit": "5",
        "X-RateLimit-Remaining": "0",
        "X-RateLimit-Reset": "1",
    }
    return httpx.Response(status, json={"detail": f"Refused ({reason}).", "error": error}, headers=headers)


OK = httpx.Response(200, json={"ok": True})


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


class RateLimitTestCase(unittest.IsolatedAsyncioTestCase):
    max_retries = 3

    async def asyncSetUp(self):
        self.answers = []
        self.sent = []
        self.time = FakeTime()

        def handle(request):
            self.sent.append((request.method, request.url.path, self.time.now, request.content))
            return self.answers.pop(0) if self.answers else OK

        self.client = LeakRadarClient(token="secret", transport=httpx.MockTransport(handle), max_retries=self.max_retries)
        self.client._monotonic = self.time.monotonic
        self.client._sleep = self.time.sleep
        self.client._random = lambda: 0.5

    async def asyncTearDown(self):
        await self.client.aclose()


class ErrorAttributesTest(RateLimitTestCase):
    max_retries = 0

    async def test_unified_refusal_is_exposed(self):
        self.answers.append(refusal("rate", retry_after=3, limit={"per_second": 30, "burst": 30}))
        with self.assertRaises(TooManyRequestsError) as caught:
            await self.client.get_profile()
        error = caught.exception
        self.assertIsInstance(error, LeakRadarAPIError)
        self.assertEqual((error.status_code, error.detail), (429, "Refused (rate)."))
        self.assertEqual((error.reason, error.scope, error.family), ("rate", "credential", "search"))
        self.assertEqual(error.retry_after, 3.0)
        self.assertEqual(error.retry_at, datetime(2026, 10, 21, 14, 32, 10, tzinfo=timezone.utc))
        self.assertEqual(error.limit, {"per_second": 30, "burst": 30})
        self.assertIsNone(error.daily)
        self.assertEqual(error.headers["x-lr-ratelimit-reason"], "rate")
        self.assertEqual((error.code, error.error_code), (None, "rate_limited"), "detail is a string")
        self.assertEqual(error.error["docs"], "https://docs.leakradar.io/#section/Rate-limits")

    async def test_daily_quota_details(self):
        daily = {"limit": 500, "used": 500, "retry_at": "2026-10-21T16:00:00Z"}
        self.answers.append(refusal("daily_quota", scope="account", retry_after=5400, daily=daily))
        with self.assertRaises(TooManyRequestsError) as caught:
            await self.client.search_email("john@example.com")
        self.assertEqual(caught.exception.reason, "daily_quota")
        self.assertEqual(caught.exception.daily, daily)

    async def test_raw_part_object_detail_keeps_its_code(self):
        detail = {"code": "raw_unlock_concurrency_limit", "message": "2 raw unlocks already running"}
        body = {"detail": detail, "error": {"code": "rate_limited", "reason": "concurrency", "scope": "account", "retry_after": 2}}
        self.answers.append(httpx.Response(429, json=body, headers={"Retry-After": "2"}))
        with self.assertRaises(TooManyRequestsError) as caught:
            await self.client.get_profile()
        self.assertEqual(caught.exception.detail, detail)
        self.assertEqual(caught.exception.code, "raw_unlock_concurrency_limit")
        self.assertEqual(caught.exception.reason, "concurrency")

    async def test_older_answers_and_hand_built_errors(self):
        # A 429 from an API older than the error object: the reason comes from its headers.
        self.answers.append(httpx.Response(429, json={"detail": "Rate limit exceeded"}, headers={"Retry-After": "1"}))
        with self.assertRaises(TooManyRequestsError) as caught:
            await self.client.get_profile()
        self.assertIsNone(caught.exception.reason)
        self.assertEqual(caught.exception.retry_after, 1.0)
        self.assertIsNotNone(caught.exception.retry_at)
        legacy = [
            ({"X-RateLimit-Inflight-Kind": "raw", "Retry-After": "5"}, "concurrency"),
            ({"X-RateLimit-Daily-Limit": "500", "X-RateLimit-Daily-Remaining": "0", "Retry-After": "600"}, "daily_quota"),
            ({"X-RateLimit-Limit": "5", "X-RateLimit-Remaining": "0", "Retry-After": "1"}, "rate"),
        ]
        for headers, reason in legacy:
            with self.subTest(reason=reason):
                error = TooManyRequestsError(429, "x", headers=headers)
                self.assertEqual(error.reason, reason)
        # 0.2 call shape still works.
        error = TooManyRequestsError(429, "slow down")
        self.assertEqual((error.reason, error.retry_after, error.retry_at, error.scope, error.family), (None,) * 5)
        self.assertEqual(len(error.headers), 0)
        self.assertEqual(str(error), "API Error 429: slow down")

    async def test_html_answer(self):
        self.answers.append(httpx.Response(503, text="<html>maintenance</html>", headers={"Retry-After": "30"}))
        with self.assertRaises(ServiceUnavailableError) as caught:
            await self.client.get_profile()
        self.assertIsNone(caught.exception.reason)
        self.assertEqual(caught.exception.retry_after, 30.0)
        self.assertEqual(caught.exception.detail, "<html>maintenance</html>")

    async def test_http_date_retry_after(self):
        error = TooManyRequestsError(429, "x", headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"})
        self.assertEqual(error.retry_after, 0.0)

    async def test_max_retries_zero_is_the_0_2_behaviour(self):
        self.answers.append(refusal("rate"))
        with self.assertRaises(TooManyRequestsError):
            await self.client.get_profile()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.time.sleeps, [])
        self.assertEqual(self.client._gate_until, 0.0, "no gate without retries")


class RetryTest(RateLimitTestCase):
    async def test_rate_refusal_is_retried_after_retry_after(self):
        self.answers.append(refusal("rate", retry_after=2))
        self.assertEqual(await self.client.search_email("john@example.com"), {"ok": True})
        self.assertEqual(len(self.sent), 2)
        # 2 s plus the jitter (0.5 of min(1, 0.1 + 0.1 * 2) here).
        self.assertEqual(self.time.sleeps, [2.15])
        self.assertGreaterEqual(self.sent[1][2] - self.sent[0][2], 2.0)
        self.assertEqual(self.sent[0][3], self.sent[1][3], "the same body is sent again")

    async def test_concurrency_and_busy_are_retried(self):
        self.answers.append(refusal("concurrency", family="heavy", scope="account", retry_after=2))
        self.answers.append(refusal("busy", scope="global", retry_after=5, status=503))
        self.assertEqual(await self.client.raw_search(q="admin@example.com"), {"ok": True})
        self.assertEqual(len(self.sent), 3)

    async def test_daily_quota_and_paused_are_never_retried(self):
        for reason, wait in (("daily_quota", 5400), ("paused", 1)):
            with self.subTest(reason=reason):
                self.sent.clear()
                self.answers.append(refusal(reason, retry_after=wait))
                with self.assertRaises(TooManyRequestsError) as caught:
                    await self.client.get_profile()
                self.assertEqual(caught.exception.reason, reason)
                self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.time.sleeps, [])

    async def test_retries_stop_after_max_retries(self):
        self.answers.extend(refusal("rate") for _ in range(5))
        with self.assertRaises(TooManyRequestsError):
            await self.client.get_profile()
        self.assertEqual(len(self.sent), 4, "first attempt + 3 retries")

    async def test_long_wait_is_raised_at_once(self):
        self.answers.append(refusal("concurrency", retry_after=61))
        with self.assertRaises(TooManyRequestsError) as caught:
            await self.client.get_profile()
        self.assertEqual(caught.exception.retry_after, 61.0)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.client._gate_until, 0.0)

    async def test_alert_channel_test_refusal_is_raised_at_once(self):
        # The channel tests have an hourly budget: a wait of up to an hour, beyond max_retry_wait.
        limit = {"per_second": 30 / 3600, "burst": 30, "window_seconds": 3600}
        self.answers.append(refusal("rate", family="channel_test", scope="account", retry_after=1800, limit=limit))
        with self.assertRaises(TooManyRequestsError) as caught:
            await self.client.test_notification_method_with_data(3, "all")
        error = caught.exception
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(
            (error.reason, error.family, error.scope, error.retry_after), ("rate", "channel_test", "account", 1800.0)
        )
        self.assertEqual(error.limit["window_seconds"], 3600)
        self.assertEqual(self.time.sleeps, [])
        self.assertEqual(self.client._gate_until, 0.0, "a refusal that is not retried leaves the gate open")

    async def test_unknown_reason_is_not_retried(self):
        self.answers.append(httpx.Response(429, json={"detail": "Please wait before resending this invitation"}))
        with self.assertRaises(TooManyRequestsError):
            await self.client.resend_team_invitation(4)
        self.assertEqual(len(self.sent), 1)

    async def test_legacy_rate_headers_are_retried(self):
        self.answers.append(httpx.Response(429, json={"detail": "Rate limit exceeded"},
                                           headers={"Retry-After": "1", "X-RateLimit-Remaining": "0"}))
        self.assertEqual(await self.client.get_profile(), {"ok": True})
        self.assertEqual(len(self.sent), 2)

    async def test_raw_download_url_is_retried_too(self):
        self.answers.append(refusal("rate"))
        self.answers.append(httpx.Response(302, headers={"Location": "https://files.example.invalid/x"}))
        self.assertEqual(await self.client.get_raw_download_url(5), "https://files.example.invalid/x")
        self.assertEqual(len(self.sent), 2)

    async def test_constructor_checks(self):
        for kwargs in ({"max_retries": -1}, {"max_retries": 1.5}, {"max_retries": True}, {"max_retry_wait": -1}):
            with self.subTest(**kwargs), self.assertRaises(ValueError):
                LeakRadarClient(token="t", **kwargs)


class GateTest(RateLimitTestCase):
    async def test_other_tasks_wait_for_the_gate(self):
        refused = asyncio.Event()

        async def handle(request):
            self.sent.append((request.url.path, self.time.now))
            if len(self.sent) == 1:
                refused.set()
                return refusal("rate", retry_after=2)
            return OK

        await self.client.aclose()
        self.client._client = httpx.AsyncClient(base_url=LeakRadarClient.BASE_URL, transport=httpx.MockTransport(handle))
        first = asyncio.ensure_future(self.client.get_profile())
        await refused.wait()
        while self.client._gate_until <= self.time.now:
            await asyncio.sleep(0)
        start = self.time.now
        # Started while the gate is closed: it waits instead of collecting its own 429.
        self.assertEqual(await self.client.get_stats(), {"ok": True})
        self.assertEqual(await first, {"ok": True})
        self.assertEqual(sorted(path for path, _ in self.sent), ["/profile", "/profile", "/stats"])
        for _path, sent_at in self.sent[1:]:
            self.assertGreaterEqual(sent_at, start + 2.0)

    async def test_rate_limits_skips_the_gate(self):
        self.client._close_gate(30)
        self.assertEqual(await self.client.rate_limits(), {"ok": True})
        self.assertEqual(self.sent[0][:2], ("GET", "/rate-limits"))
        self.assertEqual(self.time.sleeps, [])

    async def test_a_refusal_after_the_last_retry_still_closes_the_gate(self):
        self.client.max_retries = 1
        self.answers.extend([refusal("rate", retry_after=4), refusal("rate", retry_after=4)])
        with self.assertRaises(TooManyRequestsError):
            await self.client.get_profile()
        self.assertGreater(self.client._gate_until, self.time.now)


class RateLimitsMethodTest(RateLimitTestCase):
    async def test_rate_limits_shape(self):
        body = {
            "lane": "auto",
            "credential_kind": "api_key",
            "families": [{"family": "search", "per_second": 30, "burst": 30, "scope": "credential"}],
            "daily": {"limit": None, "used": None, "retry_at": None, "legacy": False},
            "pauses": [],
        }
        self.answers.append(httpx.Response(200, json=body))
        self.assertEqual(await self.client.rate_limits(), body)
        method, path, _at, content = self.sent[0]
        self.assertEqual((method, path, content), ("GET", "/rate-limits", b""))


if __name__ == "__main__":
    unittest.main()
