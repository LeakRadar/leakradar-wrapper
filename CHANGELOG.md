# Changelog

## 0.2.0 (2026-10-15)

The client now covers every operation of the published API (122 operations, up from 79).

### Added

- **Combolists**: `search_combolist_email`, `search_combolist_username`, `search_combolist_domain`,
  `get_combolist_domain_report`, `search_combolist_advanced`, `unlock_combolist_leaks`,
  `queue_combolist_unlock_task`, `export_combolist`.
- **Unlocked combolists**: `get_unlocked_combolists`, `get_unlocked_combolists_advanced`,
  `export_unlocked_combolists`, `set_unlocked_combolist_list`, `upsert_unlocked_combolist_comment`,
  `delete_unlocked_combolist_comment`, `set_unlocked_combolist_status`.
- **Android applications**: `suggest_applications`, `get_application_leaks`, `unlock_application_leaks`,
  `queue_application_unlock_task`, `export_application_leaks`.
- **Dark web**: `search_dark_web`, `get_dark_web_post`, `get_dark_web_sources`, `get_dark_web_stats`.
- **Raw search**: `raw_count` (mass unlock estimate), `queue_raw_unlock_task`, `list_unlocked_raw_parts`.
- **Domain**: `get_domain_all` (every category in one list).
- **Remediation**: `set_unlocked_leak_status`, `bulk_update_unlocked_status`.
- **Monitoring**: `get_notification_run`, `get_notification_run_items`, `set_notification_run_status`,
  `export_notification_runs` (several runs in one ZIP), `set_notification_method_active`.
- **Account**: `get_usage`, `get_preferences`, `update_preferences`, `list_balance_logs`,
  `list_task_history`, `leave_team`.
- **Public stats**: `get_live_stats`, `get_transparency_stats`.
- New arguments on existing methods:
  - `auto_unlock` on `search_advanced`, `search_email` and the domain category listings.
  - The 14 `*_match_type` filters on `search_advanced`.
  - Raw search filters `q_exact`, `exts_not`, `categories_not`, `file_name_not`, `folder_name`,
    `folder_name_not`, the four `*_match_type`, `force_and`, `include_terms`, `exclude_terms`,
    `ingested_at_min`, `ingested_at_max`, plus `cursor` and `dedup`.
  - `status` on the unlocked leak listings and exports; `dataset` and `status_filter` on
    `bulk_assign_unlocked_list`; `dataset` on `list_unlocked_lists`.
  - `dataset`, `status`, `sent_after`, `sent_before`, `notification_id`, `q` on `list_notification_runs`.
  - `list_id` on `unlock_notification_run_leaks`; `search`, `status` on `list_raw_downloads`;
    `include_search_count` on `get_domain_report`.
- `ServiceUnavailableError` (503, search maintenance) and `LeakRadarAPIError.code` for object details.
- `GoneError` is now exported from the package.
- `transport` argument on `LeakRadarClient` (retries, proxies, `httpx.MockTransport` in tests).
- `py.typed` marker: mypy and pyright now use the package's type hints.
- A docstring on every public method.
- `date` / `datetime` arguments are sent as ISO 8601.
- Contract test against the OpenAPI spec (`tests/test_contract.py`) and
  `scripts/update_openapi_snapshot.py` to refresh it.

### Changed

- `test_notification_method_with_data(method_id, notification_type="leak")`: the API reads the type
  from the query string and never read the 0.1.x body. Passing the 0.1.x dict still works, with a
  warning, and sends the same "leak" test as before (unless the dict holds a valid `notification_type`).
- Domains, packages and IDs are percent-encoded in URL paths, so an input can never change the route.
- The unauthenticated client used for raw file downloads is created on the first download.

### Deprecated

- `notification_run_leaks()`: the API removed `/notification_runs/{id}/leaks`. It now calls
  `get_notification_run_items()`; `search` and `is_email` are ignored.
- `raw_export_preview()`: the API removed `/search/raw/export/preview`. It now returns `raw_count()`.

### Fixed

- `notification_run_leaks()` and `raw_export_preview()` called endpoints that no longer exist (404).

## 0.1.6 and earlier

See the git history.
