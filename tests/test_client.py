"""Behaviour of the client itself: request shapes, client-side checks, deprecations, errors."""

import json
import unittest
import warnings
from datetime import datetime, timezone

import httpx

from leakradar import (
    BadRequestError,
    GoneError,
    LeakRadarAPIError,
    LeakRadarClient,
    NotFoundError,
    ServiceUnavailableError,
    TooManyRequestsError,
)


class ClientTestCase(unittest.IsolatedAsyncioTestCase):
    response = httpx.Response(200, json={})

    async def asyncSetUp(self):
        self.requests = []

        def handle(request):
            self.requests.append(request)
            return self.response

        self.client = LeakRadarClient(token="secret", transport=httpx.MockTransport(handle))

    async def asyncTearDown(self):
        await self.client.aclose()

    @property
    def request(self):
        self.assertEqual(len(self.requests), 1)
        return self.requests[0]

    @property
    def body(self):
        return json.loads(self.request.content) if self.request.content else None

    @property
    def query(self):
        return dict(self.request.url.params)


class TransportAndHeadersTest(ClientTestCase):
    async def test_bearer_token_and_user_agent(self):
        await self.client.get_profile()
        self.assertEqual(self.request.headers["Authorization"], "Bearer secret")
        self.assertTrue(self.request.headers["User-Agent"].startswith("LeakRadar-Python-Client/"))
        self.assertEqual(str(self.request.url), "https://api.leakradar.io/profile")

    async def test_path_segments_are_encoded(self):
        await self.client.get_domain_all("evil.com/../../profile")
        path = self.request.url.raw_path.split(b"?", 1)[0]
        self.assertEqual(path, b"/search/domain/evil.com%2F..%2F..%2Fprofile/all")

    async def test_dates_are_sent_as_iso_8601(self):
        moment = datetime(2026, 9, 1, tzinfo=timezone.utc)
        await self.client.search_advanced(username="john", added_from=moment)
        self.assertEqual(self.body["added_from"], "2026-09-01T00:00:00+00:00")

    async def test_dates_in_query_and_nested_filters(self):
        moment = datetime(2026, 9, 1, tzinfo=timezone.utc)
        await self.client.export_notification_runs(filters={"sent_after": moment})
        self.assertEqual(self.body["filters"]["sent_after"], "2026-09-01T00:00:00+00:00")


class AdvancedSearchTest(ClientTestCase):
    async def test_scalars_become_lists_and_match_types_go_in_the_body(self):
        await self.client.search_advanced(
            username="john",
            url_port=443,
            username_match_type="starts_with",
            force_and=True,
            auto_unlock=True,
            page_size=50,
        )
        self.assertEqual(
            self.body,
            {"username": ["john"], "url_port": [443], "username_match_type": "starts_with", "force_and": True},
        )
        self.assertEqual(self.query, {"page": "1", "page_size": "50", "auto_unlock": "true"})

    async def test_unset_filters_are_not_sent(self):
        await self.client.search_advanced(email_domain=("a.com", "b.com"))
        self.assertEqual(self.body, {"email_domain": ["a.com", "b.com"]})
        self.assertNotIn("auto_unlock", self.query)


class RawSearchTest(ClientTestCase):
    async def test_new_filters_cursor_and_dedup(self):
        await self.client.raw_search(
            q="admin@example.com",
            exts="txt",
            folder_name="Passwords",
            folder_name_match_type="exact",
            include_terms=["paypal"],
            q_exact=True,
            cursor="abc",
            dedup=True,
        )
        self.assertEqual(
            self.body,
            {
                "q": "admin@example.com",
                "q_exact": True,
                "exts": ["txt"],
                "folder_name": ["Passwords"],
                "folder_name_match_type": "exact",
                "include_terms": ["paypal"],
            },
        )
        self.assertEqual(self.query, {"page": "1", "page_size": "10", "cursor": "abc", "dedup": "true"})

    async def test_selector_required_without_q(self):
        with self.assertRaises(ValueError):
            await self.client.raw_search()
        # Exclusions only narrow a selection, like on the API.
        with self.assertRaises(ValueError):
            await self.client.raw_count(folder_name_not=["Cookies"], exclude_terms=["test"])
        self.assertEqual(self.requests, [])

    async def test_include_terms_and_dates_select_on_their_own(self):
        await self.client.raw_count(include_terms=["paypal"])
        self.assertEqual(self.request.url.path, "/search/raw/count")
        self.requests.clear()
        await self.client.queue_raw_unlock_task(ingested_at_min="2026-10-01T00:00:00Z", max_leaks=100)
        self.assertEqual(self.query, {"max": "100"})

    async def test_export_mode_is_checked(self):
        with self.assertRaises(ValueError):
            await self.client.raw_export("lines", q="admin@example.com")
        await self.client.raw_export("parts", q="admin@example.com", dedup=True)
        self.assertEqual(self.query, {"export": "parts", "dedup": "true"})


class CombolistTest(ClientTestCase):
    async def test_email_search(self):
        await self.client.search_combolist_email("john@example.com", search="2024", auto_unlock=True)
        self.assertEqual(self.request.url.path, "/search/combolist/email")
        self.assertEqual(self.body, {"email": "john@example.com", "search": "2024"})
        self.assertEqual(self.query["auto_unlock"], "true")

    async def test_advanced_search_only_sends_combolist_fields(self):
        await self.client.search_combolist_advanced(password="hunter2", email_domain_match_type="ends_with")
        self.assertEqual(self.body, {"password": ["hunter2"], "email_domain_match_type": "ends_with"})

    async def test_scope_body_for_value_scopes(self):
        await self.client.queue_combolist_unlock_task("domain", value="example.com", search="admin", max_leaks=10)
        self.assertEqual(self.body, {"scope": "domain", "value": "example.com", "search": "admin"})
        self.assertEqual(self.query, {"max": "10"})

    async def test_scope_body_for_advanced(self):
        await self.client.export_combolist("advanced", filters={"username": "john"}, format="json")
        self.assertEqual(self.body, {"scope": "advanced", "filters": {"username": ["john"]}})
        self.assertEqual(self.query, {"format": "json"})

    async def test_scope_rules_match_the_api(self):
        invalid = [
            {"scope": "url", "value": "x"},
            {"scope": "advanced"},
            {"scope": "advanced", "filters": {"username": "john"}, "value": "john"},
            {"scope": "advanced", "filters": {"username": "john"}, "search": "x"},
            {"scope": "email", "value": "john@example.com", "filters": {}},
            {"scope": "email"},
        ]
        for kwargs in invalid:
            with self.subTest(**{k: str(v) for k, v in kwargs.items()}), self.assertRaises(ValueError):
                await self.client.queue_combolist_unlock_task(**kwargs)
        self.assertEqual(self.requests, [])

    async def test_domain_report_cache_bypass(self):
        await self.client.get_combolist_domain_report("example.com", no_cache=True)
        self.assertEqual(self.request.headers["x-no-cache"], "1")

    async def test_unlocked_combolists_advanced_filters(self):
        await self.client.get_unlocked_combolists_advanced(filters={"is_email": True}, status="new", page_size=500)
        self.assertEqual(self.body, {"status": "new", "filters": {"is_email": True}})
        self.assertEqual(self.query, {"page": "1", "page_size": "500"})


class RemediationAndListsTest(ClientTestCase):
    async def test_bulk_status(self):
        await self.client.bulk_update_unlocked_status(
            "fixed", filters={"email_domain": "example.com"}, dataset="combolist", status_filter="new"
        )
        self.assertEqual(
            self.body,
            {
                "target_status": "fixed",
                "dataset": "combolist",
                "status_filter": "new",
                "filters": {"email_domain": ["example.com"]},
            },
        )

    async def test_bulk_assign_keeps_explicit_unassign(self):
        await self.client.bulk_assign_unlocked_list(None, dataset="combolist")
        self.assertEqual(self.body, {"target_list_id": None, "dataset": "combolist"})


class MonitoringTest(ClientTestCase):
    async def test_runs_export_needs_run_ids_or_filters(self):
        with self.assertRaises(ValueError):
            await self.client.export_notification_runs()
        with self.assertRaises(ValueError):
            await self.client.export_notification_runs(run_ids=[1], filters={"dataset": "combolist"})
        with self.assertRaises(ValueError):
            await self.client.export_notification_runs(run_ids=[1], exclude_run_ids=[2])
        self.assertEqual(self.requests, [])
        await self.client.export_notification_runs(run_ids=[1, 2], format="txt")
        self.assertEqual(self.body, {"run_ids": [1, 2], "format": "txt"})

    async def test_run_filters(self):
        await self.client.list_notification_runs(dataset="combolist", q="#12", notification_id=4)
        self.assertEqual(
            self.query, {"page": "1", "page_size": "20", "dataset": "combolist", "q": "#12", "notification_id": "4"}
        )

    async def test_test_with_data_sends_the_type_in_the_query(self):
        await self.client.test_notification_method_with_data(3, "dark_web")
        self.assertEqual(self.query, {"notification_type": "dark_web"})
        self.assertIsNone(self.body)


class DeprecationTest(ClientTestCase):
    async def test_notification_run_leaks_uses_items(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            await self.client.notification_run_leaks(7, page_size=10, search="x")
        self.assertEqual(self.request.url.path, "/notification_runs/7/items")
        self.assertEqual(self.query, {"page": "1", "page_size": "10"})
        self.assertTrue(any(issubclass(w.category, DeprecationWarning) and "ignored" in str(w.message) for w in caught))

    async def test_raw_export_preview_uses_count(self):
        with self.assertWarns(DeprecationWarning):
            await self.client.raw_export_preview("rows", q="admin@example.com", exts=["txt"])
        self.assertEqual(self.request.url.path, "/search/raw/count")
        self.assertEqual(self.body, {"q": "admin@example.com", "exts": ["txt"]})

    async def test_test_with_data_keeps_the_0_1_behaviour(self):
        # 0.1.x sent the dict as a body the API never read, so the test was always "leak".
        with self.assertWarns(DeprecationWarning):
            await self.client.test_notification_method_with_data(3, {"type": "email", "value": "a@b.c"})
        self.assertEqual(self.query, {"notification_type": "leak"})
        self.assertIsNone(self.body)

    async def test_test_with_data_honours_an_explicit_type_in_the_dict(self):
        with self.assertWarns(DeprecationWarning):
            await self.client.test_notification_method_with_data(3, data={"notification_type": "raw"})
        self.assertEqual(self.query, {"notification_type": "raw"})


class DarkWebTest(ClientTestCase):
    async def test_sources_list_is_comma_joined(self):
        await self.client.search_dark_web(title="acme", logic="OR", sources=["breachforums", "xss"])
        self.assertEqual(self.query["sources"], "breachforums,xss")
        self.assertEqual(self.body, {"title": "acme", "logic": "OR"})


class ResponseTest(ClientTestCase):
    response = httpx.Response(200, content=b"%PDF-1.7", headers={"content-type": "application/pdf"})

    async def test_binary_content_is_returned_as_bytes(self):
        self.assertEqual(await self.client.get_domain_report_pdf("example.com"), b"%PDF-1.7")


class ErrorTest(ClientTestCase):
    async def _raises(self, response, error_class):
        self.response = response
        with self.assertRaises(error_class) as caught:
            await self.client.get_profile()
        return caught.exception

    async def test_status_codes_map_to_exceptions(self):
        cases = [
            (400, BadRequestError),
            (404, NotFoundError),
            (410, GoneError),
            (429, TooManyRequestsError),
            (503, ServiceUnavailableError),
            (500, LeakRadarAPIError),
        ]
        for status, error_class in cases:
            with self.subTest(status=status):
                error = await self._raises(httpx.Response(status, json={"detail": "nope"}), error_class)
                self.assertEqual((error.status_code, error.detail), (status, "nope"))

    async def test_object_detail_exposes_its_code(self):
        detail = {"code": "invalid_cursor", "message": "Cursor expired"}
        error = await self._raises(httpx.Response(400, json={"detail": detail}), BadRequestError)
        self.assertEqual(error.detail, detail)
        self.assertEqual(error.code, "invalid_cursor")

    async def test_non_json_and_list_bodies(self):
        error = await self._raises(httpx.Response(502, text="Bad gateway"), LeakRadarAPIError)
        self.assertEqual(error.detail, "Bad gateway")
        self.assertIsNone(error.code)
        error = await self._raises(httpx.Response(422, json=[{"loc": ["body"]}]), LeakRadarAPIError)
        self.assertEqual(error.status_code, 422)


if __name__ == "__main__":
    unittest.main()
