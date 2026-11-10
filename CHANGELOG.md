# Changelog

## Unreleased

### Added

- `locate_combolist_source()`: `GET /search/combolist/{credential_id}/source`, the raw file a
  combolist pair was first seen in and the part of that file holding it, to pass to
  `raw_get_part()`. It needs the Combolists dataset and Raw search, works on locked pairs and
  costs no point.
- `leakradar.types` follows the API: `source` on the combolist results (`CombolistSourceFile`),
  `CombolistSourceLocation`, `CombolistSourceResponse`, and `size_bytes` on the items of
  `list_raw_files()`.

## 0.3.0 (2026-11-05)

A synchronous client, `LeakRadar`, next to the asynchronous ones, helpers for background jobs and
pagination, typed responses, and retries of rate limit refusals and transient failures. Code
written for 0.1.6 or 0.2.x keeps working without a change (for code written for an older 0.1.x,
see "Upgrading" in the README). Two things behave differently by default: failed requests are
retried (`max_retries=0` turns that off, see Changed), and a URL passed as a domain searches its
host instead of being refused (see Fixed). Also includes the Password Range `datasets` argument
prepared for 0.2.1, which was never published.

### Added

- **Synchronous client `LeakRadar`**: the methods, arguments, results and errors of the
  asynchronous client, as plain calls, for scripts, Django, Flask, Celery tasks and notebooks. It
  runs no event loop, so it also works where one is running (Jupyter). One client can be shared
  by threads, which share its connections and its retry wait. Use it in a `with` block or call
  `close()`. It is generated from the asynchronous code by `scripts/generate_sync.py`.
- **`AsyncLeakRadar`**: the recommended name of the asynchronous client, a subclass of
  `LeakRadarClient` with the same methods.
- **Settings from the environment** (`LeakRadar` and `AsyncLeakRadar` only): the API key from
  `LEAKRADAR_API_KEY` and the API address from `LEAKRADAR_BASE_URL`, when the argument is left
  out. `token=""` forces anonymous calls. `LeakRadarClient` does not read the environment.
- Keyword arguments `base_url`, `verify` (`True`, `False`, a CA bundle or a directory of CA
  certificates, or an `ssl.SSLContext`; `None` means the default, verification on, and any other
  type raises `TypeError`) and `proxy` on every client, and the `base_url` attribute. `verify`
  and `proxy` configure the default transport (they are not used with `transport=`) and the raw
  file downloads.
- **Rate limit retries.** A request refused with a 429 `rate` or `concurrency`, or a 503 `busy`,
  is sent again after its `Retry-After` plus a small random delay, at most `max_retries` times
  (new argument, 3 by default) and only when the wait is at most `max_retry_wait` seconds (new
  argument, 60 by default). `daily_quota` and `paused` refusals are never retried.
- **Shared wait.** While a refusal's wait runs, every other request of the same client (all its
  tasks and threads) waits too, instead of collecting its own refusals.
- **Transient failure retries**, within the same `max_retries` budget: a 503 announcing a
  `Retry-After` (search temporarily unavailable) for any request, after that wait, which also
  holds the other requests of the client; connection failures for any request (nothing was
  sent); read failures, read timeouts and 502/504 answers for GET and HEAD requests only, after
  0.5 s, 1 s, 2 s... plus a small random delay. Once the budget is spent, the httpx exception or
  the API error is raised as it is. A synchronous unlock (the `unlock_*()` methods, a path ending
  in `/unlock`) is the exception: its 503 can come after the points were spent, so it is raised
  at once.
- The asynchronous clients sleep with anyio, which httpx installs: their retries, the shared wait
  and the `wait_for_*()` methods run on trio as well as on asyncio.
- `TooManyRequestsError` exposes the refusal: `reason`, `retry_after`, `retry_at` (aware UTC
  datetime), `scope`, `family`, `limit`, `daily` and `headers`. `ServiceUnavailableError` carries
  the same attributes (`reason == "busy"` for the shared anonymous capacity). Answers of an older
  API get their reason from the `X-RateLimit-*` headers.
- `headers` and `error` (the `error` object of the answer, when there is one) on every
  `LeakRadarAPIError`.
- **Errors**: `LeakRadarError`, the base class of every error the client raises itself. On
  `LeakRadarAPIError`: `message` (`detail` as readable text, "field: problem" for each invalid
  parameter of a 422), `request_id` (the `id` of a 500 answer, to quote to support, else the
  `X-Request-ID` header), `conflicts` (the monitors of a 409 `monitor_overlap`) and `error_code`,
  the machine-readable code of any refusal: the `code` of an object `detail` (what `code`
  returns), else the `code` of the `error` object (`"rate_limited"`), else the top-level `code`
  of the answer (`"search_unavailable"`, `"search_query_too_complex"`).
- `wait_for_task()` and `wait_for_unlocked_list_task()` poll a background task until it ends,
  with `interval`, `timeout` and `on_progress`, and return its final status. `TaskFailedError`
  when it ends `FAILED` or `CANCELED`, with the `code` and the `message` the API gives
  (`insufficient_points`, `timeout`...).
- `wait_for_export()` polls an export until it is `COMPLETED` (`ExportFailedError` when it ends
  with status `ERROR`), and `download_export()` downloads its ZIP archive, as bytes or into a
  file: written in chunks to a temporary file next to it, then moved into place, and readable by
  its owner only on POSIX. It never sends the API key to the storage host. The waits raise
  `WaitTimeoutError` (also a `TimeoutError`) when their `timeout` runs out. A wrong task or export
  argument raises `ValueError` naming its type, never its content (a search result passed by
  mistake holds credentials).
- `paginate()` iterates over every item of a listing, page after page (`async for` on the
  asynchronous clients, `for` on `LeakRadar`), with `page_size` and `max_items`. It follows
  `next_cursor` for `raw_search()`, and stops at the 1,000,000 results the API serves page by
  page, with a `UserWarning` naming the export that returns everything, and at the 10,000 results
  of `search_dark_web()`, with a `UserWarning` suggesting narrower filters. The items of a
  monitoring run are read until `total`, past the short or empty pages left by records removed
  from the index after the run.
- `request(method, path, ...)` calls a route the client has no method for yet, with the same
  authentication, retries, errors and decoding as the methods.
- `rate_limits()`: `GET /rate-limits`, the limits of the credential in use (families, daily
  quota, concurrency, pauses in progress, refusals of the last 24 hours and 7 days). It never
  waits for the shared gate.
- `extend_monitor_to_all_datasets()` and `extend_monitors_to_all_datasets()`: turn email or
  domain monitors into all-datasets monitors, merging the monitors of the same value.
- New arguments: `export_id` on `list_exports()`, `file_id` on `list_raw_downloads()`, and
  `datasets` on `emails_locked_exists()` and `domains_locked_exists()` (a list or a
  comma-separated string).
- `datasets` on `password_range`: `"stealer_logs"` and/or `"combolist"` (list or comma-separated
  string). The server keeps searching Stealer Logs only when it is omitted; with both, each hash's
  `count` is the sum and `sources` gives the split per dataset.
- Docstrings for the dataset `"all"` and `absorb_ids` of monitors, the 409 `monitor_overlap` and
  its `conflicts`, `max_points` (10,000 when omitted), the test type `"all"`, `po_number` on
  `update_profile()`, the 10,000-record cap of the synchronous unlocks (with more than 10,000
  points, pass `max_leaks`), and the `download_id` of `get_raw_download_url()` and
  `download_raw_file()` (the number in the `download_url` of a `list_raw_downloads()` item, not
  its `id`). Every method's docstring ends with its operation and its link in the API reference,
  and the README lists them all.
- **Typed responses**: `leakradar.types` has a `TypedDict` for every schema of the API
  (generated by `scripts/generate_types.py`), and every method is annotated with the one it
  returns. `async with AsyncLeakRadar() as client` and `with LeakRadar() as client` are typed.
- `leakradar.webhooks.parse_delivery()` reads a webhook alert of a monitor: its type, the alert,
  and the delivery ID that tells a repeated POST apart. Its documentation shows how to check the
  secret header of your channel.
- A route the API marks as deprecated (`Deprecation` header) is reported once per route and
  client, as a `DeprecationWarning` and a warning of the `leakradar` logger.
- The `leakradar` logger writes one DEBUG line per request (method, path, status, duration), never
  the API key, bodies or query values.
- `pip install "leakradar[speedups]"` installs ujson (5.4 or later) for faster JSON.
- Runnable examples in `examples/`, and a README rewritten for both clients, with the method to
  operation table.
- Tested on Python 3.8 to 3.14, with httpx 0.27 and with the latest httpx 0.x, and on trio.

### Changed

- **Retries by default.** Rate limit refusals (429 `rate` and `concurrency`, 503 `busy`), 503
  answers announcing a `Retry-After` (except after a synchronous unlock), connection failures,
  and read failures or 502/504 answers on GET requests are now retried, and while a refusal's
  wait runs, every request of the client waits. `max_retries=0` restores the 0.2 behaviour: no
  retry, no shared wait. `TooManyRequestsError` stays the same class: existing `except` clauses
  keep working.
- `LeakRadarAPIError` derives from the new `LeakRadarError` (an `Exception`) instead of
  `Exception`; its subclasses are unchanged.
- Return annotations are `TypedDict`s instead of `Dict[str, Any]` (0.1.x shipped no types).
  Nothing changes at runtime, but type checkers now check results against the API schema: code
  that type-checked against 0.2 or 0.1.x can get new mypy or pyright errors, for a result
  annotated `Dict[str, Any]`, a field that can be `None` used without a check, or a key the
  schema does not list. Annotate such a result as `Mapping[str, Any]` to keep the old leniency.
  `export_domain_subdomains()` and `export_domain_urls()` are annotated
  `Union[bytes, List[Dict[str, str]]]` instead of `Any`: bytes for csv and txt, a list for
  `format="json"`, which is what they return.
- httpx is required below 1.0 (`httpx>=0.27,<1`): its 1.0 pre-releases drop `AsyncClient` and the
  transports the clients are built on. `anyio`, which httpx installs, is now declared too.

### Fixed

- Errors survive `pickle` with their attributes. They failed to unpickle (`TypeError`), which
  broke them across Celery, multiprocessing and `ProcessPoolExecutor`.
- A URL passed as a domain (`https://www.example.com/login`) was sent as it was: the API answered
  404 for a domain in the path of the request (domain searches, reports and exports, combolist
  domain search and report), and 422 for the combolist scope `"domain"`. The client now searches
  its host (`example.com`) and says so with a `UserWarning`; a value without a host raises
  `ValueError` before any request. `domains_locked_exists()` keeps sending its entries as given:
  the API reduces each entry of this mass check itself (the host of a URL with a scheme).
- `domains_locked_exists("example.com")` with one domain as a string sends a one-element list,
  which the API accepts.
- An API key with surrounding whitespace, such as the trailing newline of a key read from a file
  or a mounted secret, is stripped before it is sent. Such a key made every request fail, with an
  httpx error whose message held the whole key.
- Every ID placed in the path of a request is percent-encoded: the IDs of unlocked lists, alert
  channels, monitors, monitoring runs, raw downloads, team invitations and team members were not,
  so a string ID such as `"1/../profile"` could reach another route. An ID equal to `"."` or
  `".."`, which would change the route too, raises `ValueError` before any request.

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
