"""
Contract between the client and the published OpenAPI spec (tests/openapi_ops.json).

Refresh the snapshot with `python scripts/update_openapi_snapshot.py`, then run
`python -m unittest discover -s tests`. A failure names the operation or the
parameter the client is missing, so the wrapper cannot silently drift from the API.
"""

import inspect
import json
import pathlib
import re
import unittest

import httpx

from leakradar import LeakRadarClient

SNAPSHOT = json.loads((pathlib.Path(__file__).parent / "openapi_ops.json").read_text(encoding="utf-8"))
SPEC_OPERATIONS = SNAPSHOT["operations"]

CL_ID = "cl1_" + "a" * 60

# Spec operation -> (client method, the smallest arguments that pass the client-side checks).
OPERATIONS = {
    "GET /password-range": ("password_range", {"prefix": "5BAA6"}),
    "POST /search/advanced": ("search_advanced", {"username": "john"}),
    "POST /search/advanced/unlock": ("unlock_all_advanced", {"filters": {"username": "john"}}),
    "POST /search/advanced/unlock/task": ("queue_advanced_unlock_task", {"filters": {"username": "john"}}),
    "POST /search/advanced/export": ("export_advanced", {"username": "john"}),
    "POST /search/advanced/export_urls": ("export_advanced_urls", {"username": "john"}),
    "GET /search/dark-web/sources": ("get_dark_web_sources", {}),
    "GET /search/dark-web/stats": ("get_dark_web_stats", {}),
    "GET /search/dark-web/post/{post_id}": ("get_dark_web_post", {"post_id": "abc"}),
    "POST /search/dark-web": ("search_dark_web", {"query": "acme"}),
    "GET /search/domain/{domain}/all": ("get_domain_all", {"domain": "example.com"}),
    "GET /search/domain/{domain}/customers": ("get_domain_customers", {"domain": "example.com"}),
    "GET /search/domain/{domain}/employees": ("get_domain_employees", {"domain": "example.com"}),
    "GET /search/domain/{domain}/third_parties": ("get_domain_third_parties", {"domain": "example.com"}),
    "GET /search/domain/{domain}": ("get_domain_report", {"domain": "example.com"}),
    "GET /search/domain/{domain}/report/pdf": ("get_domain_report_pdf", {"domain": "example.com"}),
    "GET /search/domain/{domain}/subdomains": ("get_domain_subdomains", {"domain": "example.com"}),
    "POST /search/domain/{domain}/subdomains/export": ("export_domain_subdomains", {"domain": "example.com"}),
    "GET /search/domain/{domain}/urls": ("get_domain_urls", {"domain": "example.com"}),
    "POST /search/domain/{domain}/urls/export": ("export_domain_urls", {"domain": "example.com"}),
    "POST /search/domain/{domain}/{leak_type}/export": (
        "export_domain_leaks",
        {"domain": "example.com", "leak_type": "employees"},
    ),
    "POST /search/domain/{domain}/{leak_type}/unlock": (
        "unlock_domain_leaks",
        {"domain": "example.com", "leak_type": "employees"},
    ),
    "POST /search/domain/{domain}/{leak_type}/unlock/task": (
        "queue_domain_unlock_task",
        {"domain": "example.com", "leak_type": "employees"},
    ),
    "POST /search/domains/locked-exists": ("domains_locked_exists", {"domains": ["example.com"]}),
    "POST /search/email": ("search_email", {"email": "john@example.com"}),
    "POST /search/email/unlock": ("unlock_email_leaks", {"email": "john@example.com"}),
    "POST /search/email/unlock/task": ("queue_email_unlock_task", {"email": "john@example.com"}),
    "POST /search/email/export": ("export_email_leaks", {"email": "john@example.com"}),
    "POST /search/emails/locked-exists": ("emails_locked_exists", {"emails": ["john@example.com"]}),
    "GET /search/applications": ("suggest_applications", {}),
    "GET /search/applications/{package}/leaks": ("get_application_leaks", {"package": "com.example.app"}),
    "POST /search/applications/{package}/unlock": ("unlock_application_leaks", {"package": "com.example.app"}),
    "POST /search/applications/{package}/unlock/task": (
        "queue_application_unlock_task",
        {"package": "com.example.app"},
    ),
    "POST /search/applications/{package}/export": ("export_application_leaks", {"package": "com.example.app"}),
    "POST /search/combolist/email": ("search_combolist_email", {"email": "john@example.com"}),
    "POST /search/combolist/username": ("search_combolist_username", {"username": "john"}),
    "GET /search/combolist/domain/{domain}": ("search_combolist_domain", {"domain": "example.com"}),
    "GET /search/combolist/domain/{domain}/report": ("get_combolist_domain_report", {"domain": "example.com"}),
    "POST /search/combolist/advanced": ("search_combolist_advanced", {"username": "john"}),
    "GET /search/combolist/{credential_id}/source": ("locate_combolist_source", {"credential_id": CL_ID}),
    "POST /search/combolist/unlock": ("unlock_combolist_leaks", {"leak_ids": [CL_ID]}),
    "POST /search/combolist/unlock/task": (
        "queue_combolist_unlock_task",
        {"scope": "advanced", "filters": {"username": "john"}},
    ),
    "POST /search/combolist/export": ("export_combolist", {"scope": "advanced", "filters": {"username": "john"}}),
    "POST /search/raw": ("raw_search", {"q": "admin@example.com"}),
    "POST /search/raw/count": ("raw_count", {"q": "admin@example.com"}),
    "GET /search/raw/parts": ("raw_list_parts", {"container_id": 1, "entry_path": "a.txt"}),
    "GET /search/raw/part": ("raw_get_part", {"container_id": 1, "entry_path": "a.txt", "seq": 0}),
    "GET /container/tree": ("container_tree", {"container_id": 1}),
    "GET /container/subfolders": ("container_subfolders", {"container_id": 1}),
    "POST /container/tree/resolve_path": ("container_resolve_path", {"container_id": 1}),
    "GET /container/file_info": ("container_file_info", {"container_id": 1, "entry_path": "a.txt"}),
    "POST /raw/download": ("raw_download_source", {"container_id": 1, "entry_path": "a.txt"}),
    "GET /raw/download/preview": ("raw_download_preview", {"container_id": 1, "entry_path": "a.txt"}),
    "POST /search/raw/export": ("raw_export", {"export": "rows", "q": "admin@example.com"}),
    "GET /raw/files": ("list_raw_files", {}),
    "POST /search/raw/unlock/task": ("queue_raw_unlock_task", {"q": "admin@example.com"}),
    "GET /profile": ("get_profile", {}),
    "PATCH /profile": ("update_profile", {"first_name": "John"}),
    "GET /profile/unlocked": ("get_unlocked_leaks", {}),
    "GET /profile/unlocked/advanced": ("get_unlocked_advanced", {}),
    "POST /profile/unlocked/export": ("export_unlocked_leaks", {}),
    "POST /profile/unlocked/advanced/export": ("export_unlocked_advanced", {}),
    "GET /profile/unlocked/combolists": ("get_unlocked_combolists", {}),
    "POST /profile/unlocked/combolists/advanced": ("get_unlocked_combolists_advanced", {}),
    "PUT /profile/unlocked/combolists/{leak_id}/list": ("set_unlocked_combolist_list", {"leak_id": CL_ID, "list_id": 3}),
    "PUT /profile/unlocked/combolists/{leak_id}/comment": (
        "upsert_unlocked_combolist_comment",
        {"leak_id": CL_ID, "comment": "rotated"},
    ),
    "DELETE /profile/unlocked/combolists/{leak_id}/comment": ("delete_unlocked_combolist_comment", {"leak_id": CL_ID}),
    "PUT /profile/unlocked/combolists/{leak_id}/status": (
        "set_unlocked_combolist_status",
        {"leak_id": CL_ID, "status": "fixed"},
    ),
    "POST /profile/unlocked/combolists/export": ("export_unlocked_combolists", {}),
    "GET /profile/unlocked/lists": ("list_unlocked_lists", {}),
    "POST /profile/unlocked/lists": ("create_unlocked_list", {"name": "VIP"}),
    "PATCH /profile/unlocked/lists/{list_id}": ("update_unlocked_list", {"list_id": 3, "name": "VIP"}),
    "DELETE /profile/unlocked/lists/{list_id}": ("delete_unlocked_list", {"list_id": 3}),
    "POST /profile/unlocked/lists/{list_id}/clear": ("clear_unlocked_list", {"list_id": 3}),
    "GET /profile/unlocked/list-tasks/{task_id}": ("get_unlocked_list_task", {"task_id": "12"}),
    "PUT /profile/unlocked/{leak_id}/list": ("set_unlocked_leak_list", {"leak_id": "abc", "list_id": 3}),
    "PUT /profile/unlocked/{leak_id}/comment": ("upsert_unlocked_leak_comment", {"leak_id": "abc", "comment": "x"}),
    "DELETE /profile/unlocked/{leak_id}/comment": ("delete_unlocked_leak_comment", {"leak_id": "abc"}),
    "PUT /profile/unlocked/{leak_id}/status": ("set_unlocked_leak_status", {"leak_id": "abc", "status": "fixed"}),
    "POST /profile/unlocked/bulk-status": ("bulk_update_unlocked_status", {"target_status": "fixed"}),
    "POST /profile/unlocked/lists/bulk-assign": ("bulk_assign_unlocked_list", {"target_list_id": 3}),
    "GET /profile/raw/downloads": ("list_raw_downloads", {}),
    "GET /profile/raw/unlocked-parts": ("list_unlocked_raw_parts", {}),
    "POST /profile/raw/downloads/{download_id}/file": ("get_raw_download_url", {"download_id": 5}),
    "GET /profile/team": ("get_team", {}),
    "POST /profile/team/invitations": ("create_team_invitation", {"email": "jane@example.com"}),
    "POST /profile/team/invitations/{invitation_id}/resend": ("resend_team_invitation", {"invitation_id": 4}),
    "POST /profile/team/invitations/{invitation_id}/revoke": ("revoke_team_invitation", {"invitation_id": 4}),
    "DELETE /profile/team/members/{member_id}": ("delete_team_member", {"member_id": 4}),
    "PATCH /profile/team/members/{member_id}": ("update_team_member_role", {"member_id": 4, "team_role": "admin"}),
    "POST /profile/team/leave": ("leave_team", {}),
    "GET /profile/activities/balance-logs": ("list_balance_logs", {}),
    "GET /profile/activities/tasks": ("list_task_history", {}),
    "GET /profile/preferences": ("get_preferences", {}),
    "PATCH /profile/preferences": ("update_preferences", {"hide_passwords": True}),
    "GET /profile/usage": ("get_usage", {}),
    "GET /stats": ("get_stats", {}),
    "GET /live-stats": ("get_live_stats", {}),
    "GET /transparency-stats": ("get_transparency_stats", {}),
    "POST /unlock": ("unlock_specific_leaks", {"leak_ids": ["abc"]}),
    "GET /exports": ("list_exports", {}),
    "GET /notification_methods": ("list_notification_methods", {}),
    "POST /notification_methods": ("create_notification_method", {"data": {"type": "email"}}),
    "PATCH /notification_methods/{method_id}": ("update_notification_method", {"method_id": 2, "data": {}}),
    "DELETE /notification_methods/{method_id}": ("delete_notification_method", {"method_id": 2}),
    "PATCH /notification_methods/{method_id}/active": (
        "set_notification_method_active",
        {"method_id": 2, "is_active": True},
    ),
    "GET /notifications": ("list_notifications", {}),
    "POST /notifications": ("create_notification", {"data": {"type": "email"}}),
    "PATCH /notifications/{notification_id}": ("update_notification", {"notification_id": 2, "data": {}}),
    "DELETE /notifications/{notification_id}": ("delete_notification", {"notification_id": 2}),
    "DELETE /notifications/bulk": ("delete_notifications_bulk", {"ids": [2]}),
    "PATCH /notifications/{notification_id}/active": ("set_notification_active", {"notification_id": 2, "is_active": True}),
    "POST /notifications/{notification_id}/scope_all": ("extend_monitor_to_all_datasets", {"notification_id": 2}),
    "POST /notifications/scope_all/bulk": ("extend_monitors_to_all_datasets", {"ids": [2]}),
    "POST /notification_methods/test": ("test_notification_method", {"data": {"type": "email"}}),
    "POST /notification_methods/{method_id}/test-with-data": ("test_notification_method_with_data", {"method_id": 2}),
    "GET /notifications/stats": ("get_notifications_stats", {}),
    "GET /notification_runs": ("list_notification_runs", {}),
    "GET /notification_runs/{run_id}": ("get_notification_run", {"run_id": 9}),
    "POST /notification_runs/export": ("export_notification_runs", {"filters": {"dataset": "combolist"}}),
    "PUT /notification_runs/{run_id}/status": ("set_notification_run_status", {"run_id": 9, "status": "fixed"}),
    "GET /notification_runs/{run_id}/items": ("get_notification_run_items", {"run_id": 9}),
    "POST /notification_runs/{run_id}/leaks/unlock": ("unlock_notification_run_leaks", {"run_id": 9}),
    "GET /notification_runs/{run_id}/export": ("export_notification_run", {"run_id": 9}),
    "GET /tasks/{task_id}": ("get_task_status", {"task_id": "12"}),
    "GET /rate-limits": ("rate_limits", {}),
}

# Operations the client covers before the published spec lists them. Drop an entry once a
# snapshot refresh brings its operation in (GET /rate-limits came in with API 1.4.9.8).
AHEAD_OF_SPEC = set()

# Client arguments named differently from the spec parameter they fill.
RENAMED = {"max": "max_leaks"}

# Arguments taking a dict that becomes the whole JSON body (unless the spec body has a field of that name).
BODY_DICT_ARGS = ("filters", "data")

# Spec fields the client leaves out on purpose.
NOT_EXPOSED = {
    # Deprecated by the API in favour of file_name_match_type / folder_name_match_type.
    "POST /search/raw": {"file_name_exact", "folder_name_exact"},
    "POST /search/raw/count": {"file_name_exact", "folder_name_exact"},
    "POST /search/raw/export": {"file_name_exact", "folder_name_exact"},
    "POST /search/raw/unlock/task": {"file_name_exact", "folder_name_exact"},
}

# Fields that cannot be sent together with the arguments of OPERATIONS (the API rejects the
# combination too): the client tests cover them with their own call.
PLACEMENT_SKIP = {
    "POST /search/combolist/unlock/task": {"value", "search"},
    "POST /search/combolist/export": {"value", "search"},
    "POST /notification_runs/export": {"run_ids", "exclude_run_ids"},
}

# Operations whose server reads these query parameters from the JSON body as well.
QUERY_IN_BODY_OK = {
    # The API reads these from the JSON body first and falls back to the query string.
    "POST /raw/download",
}

# Typed sample values for arguments the client inspects (everything else gets "sample").
SAMPLES = {
    "filters": {"username": ["john"]},
    "data": {},
    "leak_ids": ["abc"],
    "ids": [1],
    "domains": ["example.com"],
    "emails": ["john@example.com"],
    "absorb_ids": [3],
    "datasets": ["stealer_logs", "combolist"],
    "export_id": 5,
    "file_id": 7,
}


def _route_regex(template):
    return re.compile("^" + re.sub(r"\{[^}]+\}", "[^/]+", template) + "$")


def _handler(seen):
    def handle(request):
        seen.append(request)
        if request.url.path.endswith("/file"):
            return httpx.Response(302, headers={"Location": "https://files.example.invalid/x"})
        return httpx.Response(200, json={})

    return handle


def _body(request):
    return json.loads(request.content) if request.content else {}


def _arguments(method_name):
    """Named arguments of a client method, and whether it also takes **kwargs."""
    parameters = inspect.signature(getattr(LeakRadarClient, method_name)).parameters.values()
    named = {p.name for p in parameters if p.kind not in (p.VAR_KEYWORD, p.VAR_POSITIONAL)}
    return named, any(p.kind is p.VAR_KEYWORD for p in parameters)


def _spec_fields(operation):
    spec = SPEC_OPERATIONS[operation]
    skipped = NOT_EXPOSED.get(operation, set())
    query = [f for f in spec["query"] if f not in skipped]
    body = [f for f in spec["body"] if f not in skipped]
    return spec["path"], query, body


class ContractTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.seen = []
        self.client = LeakRadarClient(token="t", transport=httpx.MockTransport(_handler(self.seen)))

    async def asyncTearDown(self):
        await self.client.aclose()

    async def _call(self, method_name, kwargs):
        del self.seen[:]
        await getattr(self.client, method_name)(**kwargs)
        return list(self.seen)

    def test_every_spec_operation_has_a_method(self):
        missing = sorted(set(SPEC_OPERATIONS) - set(OPERATIONS))
        self.assertEqual(missing, [], "spec operations without a client method")

    def test_every_mapped_operation_is_in_the_spec(self):
        stale = sorted(set(OPERATIONS) - set(SPEC_OPERATIONS) - AHEAD_OF_SPEC)
        self.assertEqual(stale, [], "client methods mapped to operations the API no longer publishes")
        self.assertLessEqual(AHEAD_OF_SPEC, set(OPERATIONS))

    async def test_each_method_calls_its_operation(self):
        for operation, (method_name, kwargs) in sorted(OPERATIONS.items()):
            if operation not in SPEC_OPERATIONS:
                continue
            with self.subTest(operation=operation, method=method_name):
                verb, template = operation.split(" ", 1)
                seen = await self._call(method_name, kwargs)
                self.assertEqual(len(seen), 1)
                self.assertEqual(seen[0].method, verb)
                self.assertRegex(seen[0].url.path, _route_regex(template))

    def test_every_spec_parameter_has_an_argument(self):
        for operation, (method_name, _) in sorted(OPERATIONS.items()):
            if operation not in SPEC_OPERATIONS:
                continue
            with self.subTest(operation=operation, method=method_name):
                arguments, var_keyword = _arguments(method_name)
                path, query, body = _spec_fields(operation)
                body_dict = bool(body) and any(name in arguments and name not in body for name in BODY_DICT_ARGS)
                missing = []
                for field in path + query + body:
                    if field in arguments or RENAMED.get(field) in arguments or var_keyword:
                        continue
                    if body_dict and field in body:
                        continue
                    missing.append(field)
                self.assertEqual(missing, [], "spec parameters the method cannot send")

    async def test_every_spec_parameter_reaches_the_request(self):
        """Fill every argument and check each field lands in the query string or the body, as documented."""
        for operation, (method_name, base_kwargs) in sorted(OPERATIONS.items()):
            if operation not in SPEC_OPERATIONS:
                continue
            with self.subTest(operation=operation, method=method_name):
                arguments, var_keyword = _arguments(method_name)
                _path, query, body = _spec_fields(operation)
                skipped = PLACEMENT_SKIP.get(operation, set())
                body_dict = next((n for n in BODY_DICT_ARGS if body and n in arguments and n not in body), None)

                kwargs = dict(base_kwargs)
                if body_dict:
                    kwargs[body_dict] = dict(kwargs.get(body_dict) or {})
                for field in query + body:
                    if field in skipped:
                        continue
                    argument = RENAMED.get(field, field)
                    if argument in kwargs:
                        continue
                    sample = SAMPLES.get(argument, "sample")
                    if argument in arguments:
                        kwargs[argument] = sample
                    elif body_dict and field in body:
                        kwargs[body_dict].setdefault(field, sample)
                    elif var_keyword:
                        kwargs[argument] = sample

                seen = await self._call(method_name, kwargs)
                self.assertEqual(len(seen), 1)
                sent_query = set(seen[0].url.params.keys())
                sent_body = set(_body(seen[0]))
                misplaced = []
                for field in query:
                    if field in skipped or field in sent_query:
                        continue
                    if field in sent_body and (field in body or operation in QUERY_IN_BODY_OK):
                        continue
                    misplaced.append(f"query:{field}")
                for field in body:
                    if field in skipped or field in sent_body:
                        continue
                    if field in query and field in sent_query:
                        continue
                    misplaced.append(f"body:{field}")
                self.assertEqual(misplaced, [], "spec fields missing from the request")


if __name__ == "__main__":
    unittest.main()
