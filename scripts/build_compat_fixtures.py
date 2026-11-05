"""
Freeze what every published version of the SDK exposes and sends, for tests/compat.

    python scripts/build_compat_fixtures.py
    python scripts/build_compat_fixtures.py --spec path/to/openapi.json --check
    python scripts/build_compat_fixtures.py --tree leakradar --report report.md --routes routes.json

Each published version (VERSIONS: PyPI release -> commit of this repository) is read with
`git show <commit>:leakradar/...` (nothing is checked out), copied to a work directory and
imported under its own package name. The script then writes, in tests/compat/fixtures/:

- surface-<version>.json: the public names of `leakradar` and `leakradar.client`, every
  public class (MRO, constructor, str() of a sample exception) and every public method of
  LeakRadarClient plus `_request` (coroutine or not, parameters with kind, default, position);
- requests-<version>.json: for every public method, the calls made (ARGUMENTS below, one
  table shared by every version, plus EXTRA_CASES and INTERNAL_CASES) and the HTTP requests
  that version sends for them, against an httpx.MockTransport answering 200 {} (302 to a
  storage URL for the raw download endpoint). Together the calls pass every parameter of every
  published method with a value other than its default (tests/compat/test_argument_coverage.py);
- spec-fields.json: the array and required body fields of every operation of the spec, so
  that tests/compat/test_golden_requests.py compares bodies the way the API reads them.

Once a release is on PyPI, add it to VERSIONS with its commit and run the script again: from
then on the tests hold every later version to what it exposes and sends. --check rebuilds
everything in memory and fails if a fixture would change. --report and --routes describe the
differences between the versions (and a working tree given with --tree) and the union of the
routes they call, for the API's legacy routes test.
"""

import argparse
import ast
import asyncio
import copy
import importlib
import inspect
import json
import pathlib
import re
import subprocess
import sys
import tempfile
import urllib.request
import warnings
from collections import OrderedDict
from urllib.parse import unquote

import httpx

ROOT = pathlib.Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "compat" / "fixtures"
DEFAULT_SPEC = "https://api.leakradar.io/openapi.json"
METHODS = ("get", "post", "put", "patch", "delete")

# PyPI release -> (commit of this repository, PyPI upload time UTC). Every commit carries the
# version string of its release (pyproject.toml, leakradar/__init__.py or _version.py), which
# the script checks. 0.1.0 (uploaded 2025-06-04 11:27) predates the first commit (11:34, already
# 0.1.1): its source is not in the history and it cannot be rebuilt. 0.1.3's commit came 30
# minutes after its upload; its only change from 0.1.2 is the version bump.
VERSIONS = (
    ("0.1.1", "81c7f5712767bd4f15e044296a6ce7a13165e9ac", "2025-06-04 11:33"),
    ("0.1.2", "f5f7e71c1e3d5782612272b2103767c2ae67953e", "2025-07-18 01:34"),
    ("0.1.3", "e4c86c293f98d5d71411b957ba8f4ac380a77066", "2025-07-18 01:48"),
    ("0.1.4", "979c1750fe7afb7a38f91e192d4b0a881e0a44bd", "2025-11-12 15:59"),
    ("0.1.5", "24d9af8b266828393a85146611f5bde6f3f17a01", "2025-12-12 09:25"),
    ("0.1.6", "c2c610feb6dd9a429717e47ebb60bcea7306fc32", "2026-01-07 11:40"),
    ("0.2.0", "c6f6882f15c0fb26358a42985434ce60e89f3b63", "2026-10-15 16:04"),
)
# Published releases the history cannot rebuild: (version, upload time UTC, why).
UNMAPPED = (("0.1.0", "2025-06-04 11:27", "uploaded before the first commit, which already says 0.1.1"),)

TOKEN = "lr_test_compat_0123456789abcdef"
API_HOST = "api.leakradar.io"
# Where the API redirects a raw file download (a presigned storage URL, never the API).
STORAGE_URL = "https://leakradar-raw.s3.storage.example/downloads/55/Passwords.zip?X-Amz-Expires=900&X-Amz-Signature=5c0ffee"
DOWNLOAD_FILE = re.compile(r"^/profile/raw/downloads/[^/]+/file$")
# Headers recorded on their own, or set by httpx itself.
STANDARD_HEADERS = frozenset(
    {"host", "user-agent", "accept", "accept-encoding", "connection", "content-length", "authorization", "content-type"}
)

EMAIL = "john.doe@example.com"
DOMAIN = "example.com"
PACKAGE = "com.example.app"
LEAK_ID = "3f1a9c2b7e"
CL_ID = "cl1_" + "9f2c" * 15
ENTRY = "US[8F2A]/Browsers/Chrome/Passwords.txt"
SHA256 = "9b74c9897bac770ffc029102a200c5de2b5b7d2c4e3d1f6b8a0c5e7f9a1b3c5d"
META_ID = "m_5d41402abc4b2a76"
SINCE = "2025-01-01T00:00:00Z"
UNTIL = "2025-12-31T23:59:59Z"
UNLOCKED_SINCE = "2025-10-01T00:00:00Z"

# The stealer-log filters of LeakSearchFilters, every field set: lists, scalars a client wraps
# itself, and every match type, so that a version that drops or moves one is caught.
LEAK_FILTERS = OrderedDict(
    [
        ("username", ["john.doe", "jdoe"]),
        ("username_not", "test.user"),
        ("password", "Summer2024!"),
        ("password_not", "123456"),
        ("url", "login.example.com/auth"),
        ("url_not", "logout"),
        ("url_domain", DOMAIN),
        ("url_domain_not", "example.org"),
        ("url_host", "vpn.example.com"),
        ("url_host_not", "www.example.com"),
        ("username_hash", "5f4dcc3b5aa765d61d8327deb882cf99"),
        ("password_hash", "e38ad214943daad1d64c102faec29de4afe9da3d"),
        ("url_scheme", "https"),
        ("url_scheme_not", "http"),
        ("url_port", [443, 8443]),
        ("url_port_not", 80),
        ("url_tld", "com"),
        ("url_tld_not", "ru"),
        ("is_email", True),
        ("email_domain", DOMAIN),
        ("email_domain_not", "gmail.com"),
        ("email_host", "mail.example.com"),
        ("email_host_not", "smtp.example.com"),
        ("email_tld", "com"),
        ("email_tld_not", "cn"),
        ("password_strength", "weak"),
        ("added_from", SINCE),
        ("added_to", UNTIL),
        ("force_and", True),
        ("username_match_type", "starts_with"),
        ("username_not_match_type", "contains"),
        ("password_match_type", "contains"),
        ("password_not_match_type", "ends_with"),
        ("url_match_type", "contains"),
        ("url_not_match_type", "contains"),
        ("url_domain_match_type", "ends_with"),
        ("url_domain_not_match_type", "ends_with"),
        ("url_host_match_type", "starts_with"),
        ("url_host_not_match_type", "contains"),
        ("email_domain_match_type", "ends_with"),
        ("email_domain_not_match_type", "contains"),
        ("email_host_match_type", "contains"),
        ("email_host_not_match_type", "contains"),
    ]
)
COMBOLIST_FILTERS = OrderedDict(
    [
        ("username", "john"),
        ("username_not", ["test", "demo"]),
        ("username_match_type", "starts_with"),
        ("username_not_match_type", "contains"),
        ("password", "Summer2024!"),
        ("password_not", "123456"),
        ("password_match_type", "contains"),
        ("password_not_match_type", "contains"),
        ("email_domain", DOMAIN),
        ("email_domain_not", "gmail.com"),
        ("email_domain_match_type", "ends_with"),
        ("email_domain_not_match_type", "contains"),
        ("username_hash", "5f4dcc3b5aa765d61d8327deb882cf99"),
        ("password_hash", "e38ad214943daad1d64c102faec29de4afe9da3d"),
        ("is_email", True),
        ("password_strength", "weak"),
        ("added_from", SINCE),
        ("added_to", UNTIL),
        ("force_and", False),
    ]
)
RAW_FILTERS = OrderedDict(
    [
        ("q", "admin@example.com"),
        ("container_id", 101),
        ("exts", ["txt", "csv"]),
        ("categories", "credentials"),
        ("file_name", "Passwords.txt"),
        ("dedup", True),
        ("q_exact", True),
        ("exts_not", "log"),
        ("categories_not", ["cookies"]),
        ("file_name_not", "History.txt"),
        ("file_name_match_type", "exact"),
        ("file_name_not_match_type", "contains"),
        ("folder_name", "Browsers"),
        ("folder_name_not", ["Telegram"]),
        ("folder_name_match_type", "starts_with"),
        ("folder_name_not_match_type", "contains"),
        ("force_and", False),
        ("include_terms", ["password"]),
        ("exclude_terms", "test"),
        ("ingested_at_min", SINCE),
        ("ingested_at_max", UNTIL),
    ]
)


def _with(base, **extra):
    """A copy of `base` (an ordered dict) with more entries."""
    merged = OrderedDict(base)
    merged.update(extra)
    return merged


DOMAIN_LIST = {"domain": DOMAIN, "page": 2, "page_size": 250, "search": "vpn", "is_email": True, "auto_unlock": False}
DOMAIN_UNLOCK = {
    "domain": DOMAIN, "leak_type": "employees", "search": "vpn", "is_email": True, "max_leaks": 1000, "list_id": 3,
}
APP_UNLOCK = {"package": PACKAGE, "search": "john", "is_email": True, "max_leaks": 100, "list_id": 3}
METADATA_UNLOCK = {"meta_id": META_ID, "max_leaks": 100, "search": "john", "is_email": True, "list_id": 3}

# One realistic call per public method name, shared by every version. A version receives the
# arguments its signature names; the entries under "**" are free-form filters, passed to the
# versions that name them or take **kwargs. A case whose required argument a version does not
# name is skipped for that version (a method left without any case stops the script). The
# fixtures keep the arguments actually passed, so the tests replay exactly what an integration
# of that version wrote. {"$client": name} stands for the bound method `name` of the client.
#
# Every parameter of every published method must be passed by at least one call (this table,
# EXTRA_CASES or INTERNAL_CASES), at least once with a value other than its default (non-None
# when it has none): a parameter left out, or only given its default, is never compared, so the
# current client could drop it unseen. tests/compat/test_argument_coverage.py enforces it.
ARGUMENTS = {
    "aclose": {},
    "bulk_assign_unlocked_list": {
        "target_list_id": None,
        "filters": {"url_domain": DOMAIN, "username": "john.doe"},
        "search": "vpn",
        "is_email": True,
        "list_id_filter": 3,
        "list_none": False,
        "status_filter": "new",
        "dataset": "stealer_logs",
    },
    "bulk_update_unlocked_status": {
        "target_status": "accepted_risk",
        "filters": {"url_domain": DOMAIN},
        "search": "vpn",
        "is_email": True,
        "list_id_filter": 3,
        "list_none": False,
        "status_filter": "new",
        "dataset": "stealer_logs",
    },
    "clear_unlocked_list": {"list_id": 3},
    "container_file_info": {"container_id": 101, "entry_path": ENTRY},
    "container_resolve_path": {
        "container_id": 101,
        "entry_path": "US[8F2A]/Browsers/Chrome",
        "options": {"prefetch_files": True, "max_subfolders": 500, "file_page_size": 100},
    },
    "container_subfolders": {"container_id": 101, "prefix": "US[8F2A]/Browsers/", "page": 2, "page_size": 500},
    "container_tree": {"container_id": 101, "prefix": "US[8F2A]/", "page": 2, "page_size": 100},
    "create_notification": {
        "data": {
            "type": "domain",
            "value": DOMAIN,
            "method_id": 7,
            "dataset": "stealer_logs",
            "second_type": "employees",
            "auto_unlock": True,
            "max_points": 5000,
        }
    },
    "create_notification_method": {
        "data": {"type": "webhook", "value": "https://hooks.example.com/leakradar", "name": "SOC webhook"}
    },
    "create_team_invitation": {"email": "jane.roe@example.com", "name": "Jane Roe", "role": "admin"},
    "create_unlocked_list": {"name": "VIP accounts", "color": "#ff9800"},
    "delete_notification": {"notification_id": 42},
    "delete_notification_method": {"method_id": 7},
    "delete_notifications_bulk": {"ids": [42, 43]},
    "delete_team_member": {"member_id": 12},
    "delete_unlocked_combolist_comment": {"leak_id": CL_ID},
    "delete_unlocked_leak_comment": {"leak_id": LEAK_ID},
    "delete_unlocked_list": {"list_id": 3},
    "domains_locked_exists": {
        "domains": [DOMAIN, "example.org"],
        "categories": ["employees", "third_parties"],
        "include_counts": True,
        "datasets": "stealer_logs,combolist",
    },
    "download_export": {
        "export": {"id": 77, "status": "COMPLETED", "filename": "https://exports.storage.example/77/leakradar-export.zip"},
        "chunk_size": 65536,
    },
    "download_raw_file": {"download_id": 55, "token": "dlt_8c1f0e"},
    "emails_locked_exists": {
        "emails": [EMAIL, "jane.roe@example.com"], "include_counts": True, "datasets": ["stealer_logs", "combolist"],
    },
    # 0.1.1 to 0.1.3 name each filter (each one is passed), 0.1.4 and later take **filters.
    "export_advanced": {"format": "csv", "**": LEAK_FILTERS},
    "export_advanced_urls": {"format": "txt", "**": {"url_domain": DOMAIN, "url_host_not": "www.example.com"}},
    "export_application_leaks": {"package": PACKAGE, "search": "john", "is_email": True, "format": "csv"},
    "export_combolist": {
        "scope": "advanced",
        "filters": {"email_domain": DOMAIN, "password_strength": "weak"},
        "format": "json",
    },
    "export_domain_leaks": {
        "domain": DOMAIN, "leak_type": "employees", "search": "vpn", "is_email": True, "format": "json",
    },
    "export_domain_subdomains": {"domain": DOMAIN, "search": "dev", "format": "txt"},
    "export_domain_urls": {"domain": DOMAIN, "search": "login", "format": "csv"},
    "export_email_leaks": {"email": EMAIL, "search": "github", "is_email": True, "format": "csv"},
    "export_metadata_leaks": {"meta_id": META_ID, "search": "john", "is_email": True, "format": "csv"},
    "export_metadata_search": {"filters": {"url_domain": DOMAIN}, "format": "csv"},
    "export_metadata_urls": {"filters": {"url_domain": DOMAIN}, "format": "txt"},
    "export_notification_run": {"run_id": 1234, "search": "vpn", "is_email": True, "format": "csv"},
    "export_notification_runs": {"run_ids": [1234, 1235], "format": "json"},
    "export_unlocked_advanced": {
        "format": "csv",
        "unlocked_at_min": UNLOCKED_SINCE,
        "list_id": 3,
        "list_none": False,
        "status": "new",
        "**": {"url_domain": DOMAIN, "username": "john.doe"},
    },
    "export_unlocked_combolists": {
        "format": "txt",
        "filters": {"email_domain": DOMAIN},
        "search": "john",
        "is_email": True,
        "list_id": 3,
        "list_none": False,
        "status": "new",
        "unlocked_at_min": UNLOCKED_SINCE,
    },
    "export_unlocked_leaks": {
        "search": "vpn",
        "is_email": True,
        "list_id": 3,
        "list_none": False,
        "format": "csv",
        "unlocked_at_min": UNLOCKED_SINCE,
        "status": "in_progress",
    },
    "extend_monitor_to_all_datasets": {"notification_id": 42, "absorb_ids": [43, 44], "reactivate": True},
    "extend_monitors_to_all_datasets": {"ids": [42, 43], "merge": True},
    "get_application_leaks": {
        "package": PACKAGE, "page": 2, "page_size": 50, "search": "john", "is_email": True, "auto_unlock": False,
    },
    "get_combolist_domain_report": {"domain": DOMAIN, "no_cache": True},
    "get_dark_web_post": {"post_id": "post_12345"},
    "get_dark_web_sources": {},
    "get_dark_web_stats": {},
    "get_domain_all": DOMAIN_LIST,
    "get_domain_customers": DOMAIN_LIST,
    "get_domain_employees": DOMAIN_LIST,
    "get_domain_report": {"domain": DOMAIN, "light": False, "include_search_count": True},
    "get_domain_report_pdf": {"domain": DOMAIN},
    "get_domain_subdomains": {"domain": DOMAIN, "page": 2, "page_size": 250, "search": "dev"},
    "get_domain_third_parties": DOMAIN_LIST,
    "get_domain_urls": {"domain": DOMAIN, "page": 2, "page_size": 500, "search": "login"},
    "get_live_stats": {},
    "get_notification_run": {"run_id": 1234},
    "get_notification_run_items": {"run_id": 1234, "page": 2, "page_size": 200},
    "get_notifications_stats": {},
    "get_preferences": {},
    "get_profile": {},
    "get_raw_download_url": {"download_id": 55, "token": "dlt_8c1f0e"},
    "get_stats": {},
    # A task id in the format of the first unlock tasks: the ":" goes through the path.
    "get_task_status": {"task_id": "oTUltX4IQMOUUVeiohTt8A:4821377"},
    "get_team": {},
    "get_transparency_stats": {},
    "get_unlocked_advanced": {
        "page": 3,
        "page_size": 50,
        "search": "vpn",
        "list_id": 3,
        "list_none": False,
        "status": "fixed",
        "**": {"url_domain": DOMAIN, "is_email": True},
    },
    "get_unlocked_combolists": {
        "page": 2,
        "page_size": 1000,
        "search": "john",
        "is_email": True,
        "list_id": 3,
        "list_none": False,
        "status": "new",
        "unlocked_at_min": UNLOCKED_SINCE,
    },
    "get_unlocked_combolists_advanced": {
        "page": 2,
        "page_size": 50,
        "filters": {"email_domain": DOMAIN, "username": "john"},
        "search": "john",
        "is_email": True,
        "list_id": 3,
        "list_none": False,
        "status": "new",
        "unlocked_at_min": UNLOCKED_SINCE,
    },
    "get_unlocked_leaks": {
        "page": 2, "page_size": 500, "search": "vpn", "is_email": True, "list_id": 3, "list_none": False, "status": "new",
    },
    "get_unlocked_list_task": {"task_id": "8807"},
    "get_usage": {},
    "leave_team": {},
    "list_balance_logs": {
        "page": 2,
        "page_size": 50,
        "order_by": "created_at",
        "order_dir": "asc",
        "q": "unlock",
        "kind": "points",
        "direction": "debit",
        "date_from": "2025-10-01",
        "date_to": "2025-10-31",
    },
    "list_exports": {"page": 2, "page_size": 50, "export_id": 77},
    "list_notification_methods": {},
    "list_notification_runs": {
        "page": 2,
        "page_size": 50,
        "dataset": "stealer_logs",
        "status": "new",
        "sent_after": UNLOCKED_SINCE,
        "sent_before": "2025-11-01T00:00:00Z",
        "notification_id": 42,
        "q": "#1234",
    },
    "list_notifications": {},
    "list_raw_downloads": {"page": 2, "page_size": 50, "search": "Passwords", "status": "available", "file_id": 9001},
    "list_raw_files": {},
    "list_task_history": {
        "page": 2,
        "page_size": 50,
        "order_by": "created_at",
        "order_dir": "asc",
        "q": DOMAIN,
        "status": "COMPLETED",
        "kind": "UNLOCK",
        "scope": "domain",
        "date_from": "2025-10-01",
        "date_to": "2025-10-31",
    },
    "list_unlocked_lists": {"with_counts": False, "dataset": "combolist"},
    "list_unlocked_raw_parts": {"page": 2, "page_size": 50, "q": "Chrome"},
    "metadata_detail": {"meta_id": META_ID},
    "metadata_leaks": {"meta_id": META_ID, "page": 2, "page_size": 50, "search": "john", "is_email": True},
    "metadata_leaks_count": {"meta_id": META_ID},
    "notification_run_leaks": {"run_id": 1234, "page": 2, "page_size": 50, "search": "vpn", "is_email": True},
    "paginate": {
        "method": {"$client": "get_domain_all"},
        "page_size": 250,
        "max_items": 1000,
        "**": {"domain": DOMAIN, "search": "vpn"},
    },
    "password_range": {"prefix": "5BAA6", "limit": 500, "suffix_only": True, "datasets": ["stealer_logs", "combolist"]},
    "queue_advanced_unlock_task": {"filters": {"url_domain": DOMAIN, "username": "john.doe"}, "max_leaks": 50000, "list_id": 3},
    "queue_application_unlock_task": APP_UNLOCK,
    "queue_combolist_unlock_task": {"scope": "domain", "value": DOMAIN, "search": "john", "max_leaks": 10000, "list_id": 3},
    "queue_domain_unlock_task": dict(DOMAIN_UNLOCK, max_leaks=50000),
    "queue_email_unlock_task": {"email": EMAIL, "max_leaks": 20000, "list_id": 3, "search": "github", "is_email": True},
    "queue_metadata_unlock_task": METADATA_UNLOCK,
    "queue_raw_unlock_task": _with(RAW_FILTERS, max_leaks=500),
    "rate_limits": {},
    "raw_count": RAW_FILTERS,
    "raw_download_preview": {"sha256_original": SHA256},
    "raw_download_source": {"sha256_original": SHA256, "expires_in": 600},
    "raw_export": _with(OrderedDict([("export", "parts")]), **RAW_FILTERS),
    "raw_export_preview": {
        "export": "rows",
        "q": "admin@example.com",
        "container_id": 101,
        "exts": ["txt"],
        "categories": ["credentials"],
        "file_name": "Passwords.txt",
    },
    "raw_get_part": {
        "container_id": 101, "entry_path": ENTRY, "seq": 3, "trim_overlap": True, "overlap_chars": 128, "auto_unlock": True,
    },
    "raw_list_parts": {"container_id": 101, "entry_path": ENTRY, "page": 2, "page_size": 50},
    "raw_search": _with(OrderedDict([("page", 2), ("page_size", 20), ("cursor", "eyJwYWdlIjoyfQ")]), **RAW_FILTERS),
    "request": {"method": "GET", "path": "/profile/usage", "headers": {"X-Client-Job": "nightly-sync"}},
    "resend_team_invitation": {"invitation_id": 11},
    "revoke_team_invitation": {"invitation_id": 11},
    "search_advanced": _with(OrderedDict([("page", 2), ("page_size", 50), ("auto_unlock", True)]), **LEAK_FILTERS),
    "search_combolist_advanced": _with(OrderedDict([("page", 2), ("page_size", 200), ("auto_unlock", False)]), **COMBOLIST_FILTERS),
    "search_combolist_domain": {"domain": DOMAIN, "page": 3, "page_size": 1000, "search": "john", "auto_unlock": False},
    "search_combolist_email": {"email": EMAIL, "page": 2, "page_size": 50, "search": "2019", "auto_unlock": False},
    "search_combolist_username": {"username": "jdoe", "page": 2, "page_size": 50, "search": "2019", "auto_unlock": False},
    "search_dark_web": {
        "page": 2,
        "page_size": 50,
        "title": "database",
        "content": DOMAIN,
        "author": "seller01",
        "source_url": "http://forum.example.onion/thread/88",
        "logic": "OR",
        "sources": ["breachforums", "xss"],
        "date_from": SINCE,
        "date_to": UNTIL,
        "sort_by": "published_at",
        "sort_order": "asc",
    },
    "search_email": {"email": EMAIL, "page": 2, "page_size": 50, "search": "github", "is_email": True, "auto_unlock": False},
    "search_metadata": {"filters": {"url_domain": DOMAIN}, "page": 2, "page_size": 50},
    "set_notification_active": {"notification_id": 42, "is_active": True},
    "set_notification_method_active": {"method_id": 7, "is_active": False},
    "set_notification_run_status": {"run_id": 1234, "status": "fixed"},
    "set_unlocked_combolist_list": {"leak_id": CL_ID, "list_id": None},
    "set_unlocked_combolist_status": {"leak_id": CL_ID, "status": "in_progress"},
    "set_unlocked_leak_list": {"leak_id": LEAK_ID, "list_id": 3},
    "set_unlocked_leak_status": {"leak_id": LEAK_ID, "status": "fixed"},
    "suggest_applications": {"q": "bank", "limit": 10},
    "test_notification_method": {"data": {"type": "email", "value": "soc@example.com"}},
    "test_notification_method_with_data": {"method_id": 7, "notification_type": "dark_web"},
    "unlock_all_advanced": {"filters": {"url_domain": DOMAIN, "is_email": True}, "max_leaks": 500, "list_id": 3},
    "unlock_application_leaks": APP_UNLOCK,
    "unlock_combolist_leaks": {"leak_ids": [CL_ID], "target_list_id": 3},
    "unlock_domain_leaks": DOMAIN_UNLOCK,
    "unlock_email_leaks": {"email": EMAIL, "max_leaks": 100, "list_id": 3, "search": "github", "is_email": True},
    "unlock_metadata_leaks": METADATA_UNLOCK,
    "unlock_notification_run_leaks": {"run_id": 1234, "max_leaks": 100, "search": "vpn", "is_email": True, "list_id": 3},
    "unlock_specific_leaks": {"leak_ids": [LEAK_ID, "8d4e0a1f6c"], "target_list_id": 3},
    "update_notification": {"notification_id": 42, "data": {"type": "domain", "value": DOMAIN, "auto_unlock": False}},
    "update_notification_method": {"method_id": 7, "data": {"name": "SOC webhook (prod)"}},
    "update_preferences": {"hide_passwords": True, "language": "fr"},
    "update_profile": {"**": {"first_name": "John", "organization": "Example Corp", "po_number": "PO-2026-0042"}},
    "update_team_member_role": {"member_id": 12, "team_role": "member"},
    "update_unlocked_list": {"list_id": 3, "name": "VIP", "color": "#2196f3"},
    "upsert_unlocked_combolist_comment": {"leak_id": CL_ID, "comment": "Password rotated on 2025-10-02"},
    "upsert_unlocked_leak_comment": {"leak_id": LEAK_ID, "comment": "Reset by IT"},
    # The mock answers {} at once: the waits end on their first read (an error is recorded too).
    "wait_for_export": {"export_id": 77, "interval": 1.0, "timeout": 60.0},
    "wait_for_task": {"task_id": "4321", "interval": 0.5, "timeout": 30.0},
    "wait_for_unlocked_list_task": {"task_id": "8807", "interval": 0.5, "timeout": 30.0},
}

# More calls for a method: (case id, positional arguments, keyword arguments), passed as
# written (no filtering), recorded for every version whose signature accepts them. They pass
# what the call of ARGUMENTS cannot pass with it: the other form of a method (arguments that
# exclude each other) or another value that changes the request.
EXTRA_CASES = {
    # A list to move the selection to (ARGUMENTS takes it out of every list with None).
    "bulk_assign_unlocked_list": (
        ("to_list", [], {"target_list_id": 5, "filters": {"url_domain": DOMAIN}, "list_none": True}),
    ),
    # Scope "email", "username" or "domain" takes value and search, "advanced" takes filters.
    "export_combolist": (("scope_domain", [], {"scope": "domain", "value": DOMAIN, "search": "john", "format": "csv"}),),
    # Runs selected by filters (some left out) instead of by id.
    "export_notification_runs": (
        (
            "by_filters",
            [],
            {
                "filters": {"dataset": "stealer_logs", "status": "new", "sent_after": UNLOCKED_SINCE, "notification_id": 42},
                "exclude_run_ids": [1236, 1237],
                "format": "csv",
            },
        ),
    ),
    # paginate() and request() are new in 0.3.0: recorded once that release is frozen.
    # The listing's own arguments passed by position (ARGUMENTS passes them by name).
    "paginate": (("positional_arguments", [{"$client": "get_domain_all"}, DOMAIN], {"page_size": 100, "max_items": 50}),),
    "queue_combolist_unlock_task": (
        (
            "scope_advanced",
            [],
            {
                "scope": "advanced",
                "filters": {"username": "john", "email_domain": DOMAIN, "password_strength": "weak", "is_email": True},
                "max_leaks": 5000,
                "list_id": 4,
            },
        ),
    ),
    # A raw file named by its container and path instead of its SHA-256.
    "raw_download_preview": (("by_container_entry", [], {"container_id": 101, "entry_path": ENTRY}),),
    "raw_download_source": (("by_container_entry", [], {"container_id": 101, "entry_path": ENTRY, "expires_in": 300}),),
    # A query and a JSON body (ARGUMENTS sends a GET with an extra header).
    "request": (
        (
            "post_json",
            [],
            {"method": "POST", "path": "/search/email", "params": {"page": 2, "page_size": 50}, "json": {"email": EMAIL}},
        ),
    ),
    # Simple mode: one query on title and content (ARGUMENTS uses the advanced fields).
    "search_dark_web": (
        ("simple_query", [], {"query": "example.com database", "page": 3, "page_size": 10, "sources": "breachforums,xss"}),
    ),
    # A list to move the pair to (ARGUMENTS takes it out of its list with None).
    "set_unlocked_combolist_list": (("to_list", [], {"leak_id": CL_ID, "list_id": 4}),),
    # 0.1.x took (method_id, data) and sent `data` as the body; 0.2.0 reads the type instead.
    "test_notification_method_with_data": (
        ("legacy_data", [], {"method_id": 7, "data": {"type": "webhook"}}),
        ("legacy_positional_dict", [7, {"notification_type": "raw"}], {}),
    ),
}

# The internals are public in practice: an integration of 0.1.6 sends ~26,500 GET
# /search/advanced a day (a route no version calls) through them. (target, case id, args, kwargs),
# recorded for every version whose `_request` accepts the arguments.
INTERNAL_CASES = (
    (
        "_request",
        "hidden_get_search_advanced",
        ["GET", "/search/advanced"],
        {"params": {"page": 1, "page_size": 100, "url_domain": DOMAIN, "username_match_type": "starts_with", "auto_unlock": True}},
    ),
    ("_request", "post_json", ["POST", "/search/email"], {"params": {"page": 1}, "json": {"email": EMAIL, "search": None}}),
    ("_request", "extra_header", ["GET", "/profile"], {"headers": {"X-Client-Job": "nightly-sync"}}),
    # How download_raw_file() reaches the file since 0.1.5: the 302 to storage is followed
    # without the API token (0.1.1 to 0.1.3 hand the keyword to httpx, 0.1.4 refuses it).
    (
        "_request",
        "follow_redirects",
        ["POST", "/profile/raw/downloads/55/file"],
        {"params": {"token": "dlt_8c1f0e"}, "follow_redirects": True},
    ),
    ("_client.request", "client_get", ["GET", "/profile/usage"], {}),
)


def git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, check=True, stdout=subprocess.PIPE).stdout


def export_version(version, commit, work):
    """Copy leakradar/ at `commit` to <work>/<version>/<package>/ and return the package name."""
    package = "leakradar_compat_" + version.replace(".", "_")
    target = work / version / package
    for name in git("ls-tree", "-r", "--name-only", commit, "--", "leakradar/").decode("utf-8").split():
        path = target / name[len("leakradar/"):]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(git("show", f"{commit}:{name}"))
    pyproject = git("show", f"{commit}:pyproject.toml").decode("utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.M)
    if match and match.group(1) != version:
        raise SystemExit(f"{commit} is version {match.group(1)} in pyproject.toml, not {version}")
    return package


def export_tree(tree, work):
    """Copy a working tree package (a directory such as leakradar/) like a version."""
    package = "leakradar_compat_tree"
    target = work / "tree" / package
    for path in sorted(pathlib.Path(tree).rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            destination = target / path.relative_to(tree)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(path.read_bytes())
    return package


def load(package, directory):
    sys.path.insert(0, str(directory))
    try:
        return importlib.import_module(package), importlib.import_module(package + ".client")
    finally:
        sys.path.remove(str(directory))


# -------------------------
# Surface
# -------------------------

def parameters(func, skip_first):
    params = list(inspect.signature(func).parameters.values())[1 if skip_first else 0:]
    described, position = [], 0
    for param in params:
        positional = param.kind in (param.POSITIONAL_ONLY, param.POSITIONAL_OR_KEYWORD)
        described.append(
            {
                "name": param.name,
                "kind": param.kind.name,
                "default": None if param.default is param.empty else repr(param.default),
                "position": position if positional else None,
            }
        )
        position += positional
    return described


def method_entry(cls, name):
    raw = inspect.getattr_static(cls, name)
    func = raw.__func__ if isinstance(raw, (staticmethod, classmethod)) else raw
    return {
        "coroutine": inspect.iscoroutinefunction(func),
        "params": parameters(func, skip_first=not isinstance(raw, staticmethod)),
    }


def call_signature(cls, name):
    """The signature a caller sees on an instance (without self)."""
    raw = inspect.getattr_static(cls, name)
    if isinstance(raw, staticmethod):
        return inspect.signature(raw.__func__)
    func = raw.__func__ if isinstance(raw, classmethod) else raw
    signature = inspect.signature(func)
    return signature.replace(parameters=list(signature.parameters.values())[1:])


def module_names(source):
    """Public names a module defines itself (classes, functions, assignments), read from its source."""
    names = set()
    for node in ast.parse(source).body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return sorted(n for n in names if not n.startswith("_"))


async def describe(version, package, client_module, source):
    """The surface fixture of one loaded version."""
    package_names = sorted(
        n for n, v in vars(package).items() if not n.startswith("_") and not inspect.ismodule(v)
    )
    if hasattr(package, "__version__"):
        package_names.append("__version__")
    classes = OrderedDict()
    for name, cls in sorted(vars(client_module).items()):
        if name.startswith("_") or not inspect.isclass(cls) or cls.__module__ != client_module.__name__:
            continue
        entry = {"mro": [c.__name__ for c in cls.__mro__]}
        if issubclass(cls, BaseException):
            entry["init"] = parameters(cls.__init__, skip_first=True)
        if issubclass(cls, client_module.LeakRadarAPIError):
            # The message format integrations parse: "API Error {status}: {detail}".
            sample = cls(404, "Not found")
            entry["sample"] = {
                "args": [404, "Not found"],
                "str": str(sample),
                "attrs": {"status_code": sample.status_code, "detail": sample.detail},
            }
        classes[name] = entry

    client_class = client_module.LeakRadarClient
    methods = OrderedDict()
    for name in sorted(dir(client_class)):
        if (name.startswith("_") and name != "_request") or not callable(getattr(client_class, name)):
            continue
        methods[name] = method_entry(client_class, name)
    instance = client_class(token=TOKEN)
    try:
        instance_attributes = OrderedDict(
            (n, type(v).__name__) for n, v in sorted(vars(instance).items()) if not n.startswith("_") or n == "_client"
        )
    finally:
        await instance.aclose()
    return {
        "version": version,
        "package": {"names": sorted(set(package_names)), "all": sorted(getattr(package, "__all__", []))},
        "client_module": {"names": module_names(source)},
        "classes": classes,
        "client": {
            "attributes": {"BASE_URL": repr(client_class.BASE_URL)},
            "instance_attributes": instance_attributes,
            "init": method_entry(client_class, "__init__")["params"],
            "methods": methods,
        },
    }


# -------------------------
# Requests
# -------------------------

def record(request):
    """What the API sees of one request, in the form the fixtures keep."""
    user_agent = request.headers.get("user-agent", "")
    entry = OrderedDict(
        [
            ("method", request.method),
            ("origin", f"{request.url.scheme}://{request.url.host}" + (f":{request.url.port}" if request.url.port else "")),
            ("raw_path", request.url.raw_path.split(b"?", 1)[0].decode("ascii")),
            ("query", [list(item) for item in request.url.params.multi_items()]),
            ("json", None),
            ("authorization", request.headers.get("authorization")),
            ("content_type", request.headers.get("content-type")),
            ("accept", request.headers.get("accept")),
            ("user_agent_prefix", user_agent.split("/", 1)[0] + "/" if "/" in user_agent else user_agent),
            ("headers", {k: v for k, v in request.headers.items() if k not in STANDARD_HEADERS}),
        ]
    )
    if request.content:
        try:
            entry["json"] = json.loads(request.content.decode("utf-8"))
        except ValueError:
            entry["body"] = request.content.decode("utf-8", "replace")
    return entry


def mock_transport(sent):
    def handle(request):
        sent.append(record(request))
        if request.url.host == API_HOST and DOWNLOAD_FILE.match(request.url.path):
            return httpx.Response(302, headers={"Location": STORAGE_URL})
        return httpx.Response(200, json={})

    return httpx.MockTransport(handle)


class _Patched:
    """Route every httpx.AsyncClient created meanwhile (a client a method builds lazily) to the mock."""

    def __init__(self, transport):
        self.transport = transport
        self.original = httpx.AsyncClient

    def __enter__(self):
        transport, original = self.transport, self.original

        class MockedAsyncClient(original):
            def __init__(self, *args, **kwargs):
                kwargs["transport"] = transport
                super().__init__(*args, **kwargs)

        httpx.AsyncClient = MockedAsyncClient
        return self

    def __exit__(self, *exc_info):
        httpx.AsyncClient = self.original


async def new_client(client_class, transport):
    """A client of one version whose HTTP clients all answer from the mock."""
    with _Patched(transport):
        if "transport" in inspect.signature(client_class.__init__).parameters:
            return client_class(token=TOKEN, transport=transport)
        client = client_class(token=TOKEN)
    # No transport argument before 0.2.0: replace each httpx client the instance holds by one
    # built on the mock, with the same base_url and headers.
    for name, value in list(vars(client).items()):
        if isinstance(value, httpx.AsyncClient):
            setattr(
                client,
                name,
                httpx.AsyncClient(base_url=value.base_url, headers=value.headers, timeout=value.timeout, transport=transport),
            )
            await value.aclose()
    return client


def cases_for(name, signature):
    """The calls of ARGUMENTS and EXTRA_CASES that this version of the method accepts."""
    if name not in ARGUMENTS:
        raise SystemExit(f"{name} has no entry in ARGUMENTS: add a realistic call for it")
    params = signature.parameters
    named = {n for n, p in params.items() if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)}
    var_keyword = any(p.kind == p.VAR_KEYWORD for p in params.values())
    table = dict(ARGUMENTS[name])
    extra = table.pop("**", {})
    kwargs = OrderedDict((k, v) for k, v in table.items() if k in named)
    kwargs.update((k, v) for k, v in extra.items() if k in named or var_keyword)
    required = [n for n in named if params[n].default is params[n].empty]
    cases = []
    if all(n in kwargs for n in required):
        cases.append(("default", [], kwargs))
    for case_id, args, case_kwargs in EXTRA_CASES.get(name, ()):
        if accepts(signature, args, case_kwargs):
            cases.append((case_id, args, case_kwargs))
    if not cases:
        raise SystemExit(f"No call of ARGUMENTS or EXTRA_CASES fits {name}{signature}: adapt the table")
    return cases


def accepts(signature, args, kwargs):
    """Whether a call with these arguments binds to `signature`."""
    try:
        signature.bind(*args, **kwargs)
    except TypeError:
        return False
    return True


def resolve(value, client):
    """A recorded argument, {"$client": name} turned into the client's bound method."""
    if isinstance(value, dict):
        if list(value) == ["$client"]:
            return getattr(client, value["$client"])
        return {k: resolve(v, client) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, client) for v in value]
    return value


async def run_case(client_class, target, args, kwargs, operations):
    """(requests with their spec route, error raised, warning categories) of one call on one version."""
    sent = []
    transport = mock_transport(sent)
    client = await new_client(client_class, transport)
    func = client._client.request if target == "_client.request" else getattr(client, target)
    try:
        with _Patched(transport), warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            try:
                result = func(*resolve(copy.deepcopy(args), client), **resolve(copy.deepcopy(kwargs), client))
                if inspect.isawaitable(result):
                    await result
                elif hasattr(result, "__aiter__"):
                    async for _ in result:
                        pass
                error = None
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
    finally:
        await client.aclose()
    for request in sent:
        api = request["origin"] == f"https://{API_HOST}"
        request["route"] = match_route(request["method"], request["raw_path"], operations) if api else None
    return sent, error, sorted({w.category.__name__ for w in caught})


async def record_requests(version, client_class, operations):
    """The requests fixture of one loaded version."""
    names = [n for n in sorted(dir(client_class)) if not n.startswith("_") and callable(getattr(client_class, n))]
    plan = [(n, c) for n in names for c in cases_for(n, call_signature(client_class, n))]
    plan += [
        (target, (case_id, args, kwargs))
        for target, case_id, args, kwargs in INTERNAL_CASES
        if target == "_client.request" or accepts(call_signature(client_class, target), args, kwargs)
    ]
    methods = OrderedDict()
    for target, (case_id, args, kwargs) in plan:
        sent, error, warned = await run_case(client_class, target, args, kwargs, operations)
        case = OrderedDict([("case", case_id), ("args", args), ("kwargs", kwargs), ("requests", sent)])
        if error:
            case["error"] = error
        if warned:
            case["warnings"] = warned
        methods.setdefault(target, []).append(case)
    return {"version": version, "token": TOKEN, "storage_url": STORAGE_URL, "methods": methods}


# -------------------------
# Spec
# -------------------------

def read_spec(location):
    if re.match(r"^https?://", location):
        request = urllib.request.Request(location, headers={"User-Agent": "leakradar-wrapper-compat"})
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    return json.loads(pathlib.Path(location).read_text(encoding="utf-8"))


def alternatives(schema, schemas, seen=()):
    """The non-null schemas a property may take, $ref and anyOf/oneOf/allOf resolved."""
    if not isinstance(schema, dict):
        return []
    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        return [] if name in seen else alternatives(schemas.get(name), schemas, (*seen, name))
    for key in ("anyOf", "oneOf", "allOf"):
        if key in schema:
            return [a for s in schema[key] for a in alternatives(s, schemas, seen)]
    return [] if schema.get("type") == "null" else [schema]


def collect_fields(schema, schemas, prefix, arrays, required, depth=0):
    """Dotted names of the array and required fields of a body schema (nested objects included)."""
    if depth > 4:
        return
    for option in alternatives(schema, schemas):
        needed = set(option.get("required") or ())
        for name, prop in (option.get("properties") or {}).items():
            path = prefix + name
            if name in needed:
                required.add(path)
            for alt in alternatives(prop, schemas):
                if alt.get("type") == "array":
                    arrays.add(path)
                    collect_fields(alt.get("items"), schemas, path + ".", arrays, required, depth + 1)
                elif alt.get("properties"):
                    collect_fields(alt, schemas, path + ".", arrays, required, depth + 1)


def spec_fields(spec):
    schemas = spec.get("components", {}).get("schemas", {})
    fields = OrderedDict()
    for path, item in sorted(spec["paths"].items()):
        for method in METHODS:
            op = item.get(method)
            if not op:
                continue
            content = (op.get("requestBody") or {}).get("content", {})
            arrays, required = set(), set()
            collect_fields((content.get("application/json") or {}).get("schema"), schemas, "", arrays, required)
            fields[f"{method.upper()} {path}"] = {"array": sorted(arrays), "required": sorted(required)}
    return {"api_version": spec["info"]["version"], "operations": fields}


def match_route(method, raw_path, operations):
    """The spec operation ("METHOD /template") a request hits, else None; ties go to literal segments."""
    segments = [unquote(s) for s in raw_path.split("/")]
    best, best_score = None, -1
    for key in operations:
        op_method, template = key.split(" ", 1)
        parts = template.split("/")
        if op_method != method or len(parts) != len(segments):
            continue
        literal = [p for p in parts if not (p.startswith("{") and p.endswith("}"))]
        if all(p == s or (p.startswith("{") and p.endswith("}") and s) for p, s in zip(parts, segments)):
            if len(literal) > best_score:
                best, best_score = key, len(literal)
    return best


# -------------------------
# Build
# -------------------------

def dump(data, width=110):
    """JSON with one entry per line, containers that fit on a line kept inline (readable diffs)."""

    def render(value, indent):
        flat = json.dumps(value, ensure_ascii=False)
        if not isinstance(value, (dict, list)) or not value or len(flat) + indent <= width:
            return flat
        inner = " " * (indent + 1)
        if isinstance(value, dict):
            items = [f"{inner}{json.dumps(k, ensure_ascii=False)}: {render(v, indent + 1)}" for k, v in value.items()]
            return "{\n" + ",\n".join(items) + "\n" + " " * indent + "}"
        items = [inner + render(v, indent + 1) for v in value]
        return "[\n" + ",\n".join(items) + "\n" + " " * indent + "]"

    return render(data, 0) + "\n"


async def snapshot(version, package_name, directory, operations):
    """Surface and requests of one version, plus its client class (the report replays calls on it)."""
    package, client_module = load(package_name, directory)
    source = pathlib.Path(client_module.__file__).read_text(encoding="utf-8")
    if version != "tree" and getattr(package, "__version__", None) != version:
        raise SystemExit(f"{package_name} says version {getattr(package, '__version__', None)}, expected {version}")
    surface = await describe(version, package, client_module, source)
    requests = await record_requests(version, client_module.LeakRadarClient, operations)
    return {"surface": surface, "requests": requests, "client_class": client_module.LeakRadarClient}


def build(args):
    fields = spec_fields(read_spec(args.spec))
    operations = fields["operations"]
    work_root = pathlib.Path(args.work) if args.work else pathlib.Path(tempfile.mkdtemp(prefix="leakradar-compat-"))
    outputs = OrderedDict([("spec-fields.json", dump(fields))])
    snapshots = OrderedDict()
    for version, commit, _ in VERSIONS:
        package_name = export_version(version, commit, work_root)
        snap = asyncio.run(snapshot(version, package_name, work_root / version, operations))
        snapshots[version] = snap
        outputs[f"surface-{version}.json"] = dump(snap["surface"])
        outputs[f"requests-{version}.json"] = dump(snap["requests"])
        sent = sum(len(c["requests"]) for cases in snap["requests"]["methods"].values() for c in cases)
        print(f"{version}: {len(snap['surface']['client']['methods'])} methods, {sent} requests")
    if args.tree:
        package_name = export_tree(args.tree, work_root)
        snapshots["tree"] = asyncio.run(snapshot("tree", package_name, work_root / "tree", operations))
    return outputs, snapshots, operations


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--spec", default=DEFAULT_SPEC, help="OpenAPI spec, URL or file")
    parser.add_argument("--work", help="where each version is copied (default: a temporary directory)")
    parser.add_argument("--check", action="store_true", help="fail if a fixture would change, write nothing")
    parser.add_argument("--tree", help="a working tree package directory to compare in --report")
    parser.add_argument("--report", help="write a Markdown report of the differences between versions")
    parser.add_argument("--routes", help="write the union of the routes every published version calls")
    args = parser.parse_args()

    outputs, snapshots, operations = build(args)
    if args.check:
        stale = [n for n, text in outputs.items() if not (FIXTURES / n).is_file() or (FIXTURES / n).read_text(encoding="utf-8") != text]
        if stale:
            raise SystemExit("Fixtures out of date: " + ", ".join(stale))
        print("Fixtures up to date.")
    else:
        FIXTURES.mkdir(parents=True, exist_ok=True)
        for name, text in outputs.items():
            (FIXTURES / name).write_text(text, encoding="utf-8")
        print(f"{len(outputs)} files written to {FIXTURES}")
    if args.routes:
        routes = union_routes(snapshots, operations)
        pathlib.Path(args.routes).write_text(dump(routes), encoding="utf-8")
        print(f"{len(routes['routes'])} routes written to {args.routes}")
    if args.report:
        pathlib.Path(args.report).write_text(asyncio.run(report(snapshots, operations)), encoding="utf-8")
        print(f"Report written to {args.report}")


# -------------------------
# Routes and report
# -------------------------

def union_routes(snapshots, operations):
    """Every (method, route) an API request of a published version hits, with what it sends."""
    by_template = {}
    for key in operations:
        method, template = key.split(" ", 1)
        by_template.setdefault(template, []).append(method)
    routes = OrderedDict()
    for version, snap in snapshots.items():
        if version == "tree":
            continue
        for method_name, cases in snap["requests"]["methods"].items():
            for case in cases:
                for request in case["requests"]:
                    if request["origin"] != f"https://{API_HOST}":
                        continue
                    path = "/".join(unquote(s) for s in request["raw_path"].split("/"))
                    template = request["route"].split(" ", 1)[1] if request["route"] else path
                    entry = routes.get((request["method"], template))
                    if entry is None:
                        entry = routes[(request["method"], template)] = OrderedDict(
                            [
                                ("method", request["method"]),
                                ("path_template", template),
                                ("sample_path", path),
                                ("in_spec", request["route"] is not None),
                                ("query_keys", set()),
                                ("body_keys", set()),
                                ("versions", []),
                                ("sdk_methods", set()),
                            ]
                        )
                        if request["route"] is None:
                            other = [
                                t for t, verbs in by_template.items()
                                if match_route(verbs[0], request["raw_path"], [f"{verbs[0]} {t}"])
                            ]
                            if other:
                                entry["spec_template_other_methods"] = {
                                    "path_template": other[0],
                                    "methods": sorted(by_template[other[0]]),
                                }
                    entry["query_keys"].update(k for k, _ in request["query"])
                    if isinstance(request["json"], dict):
                        entry["body_keys"].update(request["json"])
                    if version not in entry["versions"]:
                        entry["versions"].append(version)
                    entry["sdk_methods"].add(method_name)
    listed = []
    for entry in routes.values():
        for key in ("query_keys", "body_keys", "sdk_methods"):
            entry[key] = sorted(entry[key])
        listed.append(entry)
    listed.sort(key=lambda e: (e["path_template"], e["method"]))
    return {
        "note": "Union of the API routes the published SDK versions call (requests recorded on a mock transport by "
        "scripts/build_compat_fixtures.py). path_template is the spec template when the spec lists the route, "
        "else the concrete path; spec_template_other_methods names the spec template the path matches under "
        "other verbs. sdk_methods: SDK methods (or internals) that send it.",
        "versions": [v for v in snapshots if v != "tree"],
        "routes": listed,
    }


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


def canonical(request, operations):
    """A request as the server reads it (the comparison of tests/compat/test_golden_requests.py)."""
    fields = operations.get(request.get("route") or "", {})
    query = {}
    for key, value in request["query"]:
        query.setdefault(key, []).append(value)
    return OrderedDict(
        [
            ("verb", request["method"]),
            ("origin", request["origin"]),
            ("path", [unquote(s) for s in request["raw_path"].split("/")]),
            ("query", query),
            ("body", normalized(request["json"], set(fields.get("array", ())), set(fields.get("required", ())))),
            ("authorization", request["authorization"]),
            ("content_type", request["content_type"]),
            ("accept", request["accept"] if request["accept"] not in ("*/*", "application/json") else None),
            ("headers", request["headers"]),
        ]
    )


def summary(request):
    """One readable line for a request: verb, route, query keys, body keys."""
    route = request["route"].split(" ", 1)[1] if request.get("route") else request["raw_path"]
    if request["origin"] != f"https://{API_HOST}":
        route = request["origin"] + route
    text = f"{request['method']} {route}"
    if request["query"]:
        text += " ?" + ",".join(sorted({k for k, _ in request["query"]}))
    if isinstance(request.get("json"), dict):
        text += " body{" + ",".join(sorted(request["json"])) + "}"
    elif request.get("json") is not None:
        text += " body=" + json.dumps(request["json"])[:40]
    return text


def request_change(old, new, operations):
    """How the requests of one call changed, in one line; None when the server sees the same."""
    old_c = [canonical(r, operations) for r in old]
    new_c = [canonical(r, operations) for r in new]
    if old_c == new_c:
        return None
    old_text, new_text = [summary(r) for r in old], [summary(r) for r in new]
    if old_text != new_text:
        return " + ".join(old_text or ["no request"]) + " -> " + " + ".join(new_text or ["no request"])
    details = []
    for o, n in zip(old_c, new_c):
        details += [f"{k} {json.dumps(o[k])[:80]} -> {json.dumps(n[k])[:80]}" for k in o if o[k] != n[k]]
    return " + ".join(new_text) + ": " + "; ".join(details)


def fmt_default(param):
    return "required" if param["default"] is None else param["default"]


def signature_changes(old, new):
    """Readable differences between two parameter lists of the surface fixtures."""
    changes = []
    old_pos = [p["name"] for p in old if p["position"] is not None]
    new_pos = [p["name"] for p in new if p["position"] is not None]
    if new_pos[: len(old_pos)] != old_pos:
        changes.append(f"POSITIONAL ORDER ({', '.join(old_pos)}) -> ({', '.join(new_pos)})")
    old_by, new_by = {p["name"]: p for p in old}, {p["name"]: p for p in new}
    for name, p in old_by.items():
        q = new_by.get(name)
        if q is None:
            changes.append(f"`{name}` REMOVED")
        elif q["kind"] != p["kind"]:
            changes.append(f"`{name}` {p['kind']} -> {q['kind']}")
        elif q["default"] != p["default"]:
            changes.append(f"`{name}` default {fmt_default(p)} -> {fmt_default(q)}")
    added = [n for n in new_by if n not in old_by]
    if added:
        required = [n for n in added if new_by[n]["default"] is None and not new_by[n]["kind"].startswith("VAR")]
        changes.append("added " + ", ".join(f"`{n}`" + (" (REQUIRED)" if n in required else "") for n in added))
    return changes


async def report(snapshots, operations):
    versions = list(snapshots)
    lines = [
        "# LeakRadar SDK: published versions compared",
        "",
        "Generated by `scripts/build_compat_fixtures.py --report`. Each section lists what changed in the "
        "public surface, then the requests that changed for the same call: every call recorded for the "
        "older version (one realistic call per method, shared table of the script) is replayed on the "
        "newer one and compared the way tests/compat/test_golden_requests.py does.",
        "",
        "## Versions",
        "",
        "| Version | Commit | PyPI upload (UTC) | Default User-Agent |",
        "|---|---|---|---|",
    ]
    for version, uploaded, why in UNMAPPED:
        lines.append(f"| {version} | none ({why}) | {uploaded} | unknown |")
    commits = {v: (f"`{c[:7]}`", t) for v, c, t in VERSIONS}
    for version in versions:
        ua = next(p["default"] for p in snapshots[version]["surface"]["client"]["init"] if p["name"] == "user_agent")
        commit, uploaded = commits.get(version, ("working tree", "not published"))
        lines.append(f"| {version} | {commit} | {uploaded} | {ua} |")
    lines.append("")
    for older, newer in zip(versions, versions[1:]):
        lines += surface_section(older, newer, snapshots[older]["surface"], snapshots[newer]["surface"])
        lines += await requests_section(snapshots[older], snapshots[newer], operations)
    latest = versions[-1]
    lines += [f"## Every version replayed on {latest}", ""]
    for version in versions[:-1]:
        changed = sorted({target for target, _, _ in await replayed(snapshots[version], snapshots[latest], operations)})
        lines.append(f"- {version}: " + (", ".join(f"`{name}`" for name in changed) if changed else "same requests"))
    lines += ["", "## Routes the published versions call that the spec does not list", ""]
    for route in union_routes(snapshots, operations)["routes"]:
        if not route["in_spec"]:
            other = route.get("spec_template_other_methods")
            listed = f"; the spec has {'/'.join(other['methods'])} {other['path_template']}" if other else ""
            lines.append(
                f"- {route['method']} {route['path_template']} ({', '.join(route['versions'])}: "
                f"{', '.join(route['sdk_methods'])}){listed}"
            )
    return "\n".join(lines) + "\n"


def surface_section(older, newer, old_surface, new_surface):
    lines = [f"## {older} -> {newer}", ""]
    for label, key in (("`leakradar` names", "package"), ("`leakradar.client` names", "client_module")):
        old_names, new_names = set(old_surface[key]["names"]), set(new_surface[key]["names"])
        if new_names - old_names:
            lines.append(f"- {label} added: " + ", ".join(f"`{n}`" for n in sorted(new_names - old_names)))
        if old_names - new_names:
            lines.append(f"- {label} REMOVED: " + ", ".join(f"`{n}`" for n in sorted(old_names - new_names)))
    for name, cls in old_surface["classes"].items():
        new_cls = new_surface["classes"].get(name)
        if new_cls and [c for c in cls["mro"] if c not in new_cls["mro"]]:
            lines.append(f"- class `{name}` LOST BASES: {cls['mro']} -> {new_cls['mro']}")
        if new_cls and cls.get("sample") and new_cls.get("sample") and cls["sample"]["str"] != new_cls["sample"]["str"]:
            lines.append(f"- `str({name}(404, 'Not found'))`: {cls['sample']['str']!r} -> {new_cls['sample']['str']!r}")
    for name in sorted(set(new_surface["classes"]) - set(old_surface["classes"])):
        lines.append(f"- class added: `{name}` ({' < '.join(new_surface['classes'][name]['mro'][1:3])})")
    old_methods, new_methods = old_surface["client"]["methods"], new_surface["client"]["methods"]
    added = sorted(set(new_methods) - set(old_methods))
    removed = sorted(set(old_methods) - set(new_methods))
    if added:
        lines.append(f"- methods added ({len(added)}): " + ", ".join(f"`{n}`" for n in added))
    if removed:
        lines.append(f"- methods REMOVED ({len(removed)}): " + ", ".join(f"`{n}`" for n in removed))
    init_changes = signature_changes(old_surface["client"]["init"], new_surface["client"]["init"])
    if init_changes:
        lines.append("- `LeakRadarClient(...)`: " + "; ".join(init_changes))
    for name in sorted(set(old_methods) & set(new_methods)):
        changes = signature_changes(old_methods[name]["params"], new_methods[name]["params"])
        if old_methods[name]["coroutine"] != new_methods[name]["coroutine"]:
            changes.append("COROUTINE CHANGED")
        if changes:
            lines.append(f"- `{name}`: " + "; ".join(changes))
    return lines


async def replayed(old_snap, new_snap, operations):
    """(target, case, change) for every call of the older version whose requests differ on the newer one."""
    client_class = new_snap["client_class"]
    found = []
    for target, cases in old_snap["requests"]["methods"].items():
        if target != "_client.request" and not callable(getattr(client_class, target, None)):
            continue
        for case in cases:
            sent, error, _ = await run_case(client_class, target, case["args"], case["kwargs"], operations)
            change = request_change(case["requests"], sent, operations)
            if error != case.get("error"):
                change = (change + "; " if change else "") + f"error {case.get('error')} -> {error}"
            if change:
                found.append((target, case["case"], change))
    return found


async def requests_section(old_snap, new_snap, operations):
    found = await replayed(old_snap, new_snap, operations)
    lines = []
    if found:
        lines += ["", "Requests that changed for the same call:", ""]
        for target, case_id, change in found:
            label = f"`{target}`" + ("" if case_id == "default" else f" ({case_id})")
            lines.append(f"- {label}: {change}")
    lines.append("")
    return lines


if __name__ == "__main__":
    main()
