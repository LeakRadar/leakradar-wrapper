# LeakRadar Async Python Client

A user-friendly, asynchronous Python 3 wrapper for the [LeakRadar.io](https://leakradar.io) API.

> **Highlights**
>
> - Async API via `httpx`
> - Covers every operation of the public API: Email, Domain, Advanced, Combolists, Android applications,
>   Raw search, Dark web, Unlocked leaks and lists, Exports, Monitoring, Tasks, Team, Usage, Stats
> - Automatic JSON decoding (prefers `ujson` if installed)
> - Binary-safe downloads (CSV/TXT/PDF/ZIP)
> - `date` / `datetime` arguments sent as ISO 8601
> - Checked against the published OpenAPI spec by a contract test

## Documentation

- API reference: <https://docs.leakradar.io>
- Production endpoint: `https://api.leakradar.io`
- Changes between versions: [CHANGELOG.md](CHANGELOG.md)

## Requirements

- Python 3.8+
- `httpx`
- Optional (recommended): `ujson`

Install:

```bash
pip install leakradar
# optional
pip install ujson
```

## Quickstart

```python
import asyncio
from leakradar import LeakRadarClient

async def main():
    async with LeakRadarClient(token="YOUR_API_KEY") as client:
        profile = await client.get_profile()
        usage = await client.get_usage()
        results = await client.search_email("john.doe@example.com", page_size=50)
        print(profile["email"], results["total"])

asyncio.run(main())
```

Every method is a coroutine and returns the decoded JSON (`dict` or `list`), or `bytes` for files.
Credentials are masked until you unlock them with points: methods named `unlock_*` spend points
synchronously (up to 10,000 items), `queue_*_unlock_task` runs the same unlock in the background.

## Searching

### Email, domain, advanced (stealer logs)

```python
from datetime import datetime, timezone

await client.search_email("john.doe@example.com", auto_unlock=True)   # unlock the page with points

await client.get_domain_report("example.com")
await client.get_domain_all("example.com", page_size=500)             # employees + customers + third parties
await client.get_domain_employees("example.com", search="admin")

await client.search_advanced(
    username=["john", "jane"],
    username_match_type="starts_with",     # contains (default), starts_with, ends_with
    url_domain="example.com",
    password_strength="weak",
    added_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
)
```

### Combolists

Combolists are a separate dataset: results carry no URL, they have their own unlock endpoint and
their own unlocked space.

```python
await client.search_combolist_email("john.doe@example.com")
await client.search_combolist_username("johndoe")
await client.search_combolist_domain("example.com", page_size=1000)
await client.get_combolist_domain_report("example.com")
await client.search_combolist_advanced(email_domain="example.com", password_strength="too_weak")

await client.unlock_combolist_leaks(["cl1_..."])

# Unlock or export a whole scope: "email", "username" or "domain" take value, "advanced" takes filters
task = await client.queue_combolist_unlock_task("domain", value="example.com", max_leaks=5000)
await client.export_combolist("advanced", filters={"email_domain": "example.com"}, format="csv")

await client.get_unlocked_combolists(unlocked_at_min="2026-10-01T00:00:00Z")
```

### Android applications

```python
apps = await client.suggest_applications("netflix")
await client.get_application_leaks("com.netflix.mediaclient")
await client.export_application_leaks("com.netflix.mediaclient", format="json")
```

### Raw search

```python
await client.raw_search(
    q="admin@example.com",
    q_exact=True,                       # word boundaries
    folder_name="Passwords",
    folder_name_match_type="exact",
    exclude_terms=["test"],
    dedup=True,
)
estimate = await client.raw_count(q="admin@example.com")   # parts a mass unlock would cost
task = await client.queue_raw_unlock_task(q="admin@example.com", max_leaks=500)
await client.raw_export("rows", q="admin@example.com")      # only what you unlocked
```

Without `q`, give at least one selector (`container_id`, `exts`, `categories`, `file_name`,
`folder_name`, `include_terms`, `ingested_at_min/max` or a `*_not` list): the client raises
`ValueError` before calling the API, like the API would.

### Dark web

```python
await client.search_dark_web("example.com", sources=["breachforums"], sort_by="published_at")
await client.search_dark_web(title="example", author="seller", logic="AND")
await client.get_dark_web_post(post_id)
```

## Background jobs

Unlock tasks return a `task_id`, exports an `export_id`:

```python
task = await client.queue_email_unlock_task("john.doe@example.com")
status = await client.get_task_status(task["task_id"])

await client.export_domain_leaks("example.com", "employees", format="csv")
exports = await client.list_exports()          # download links appear when ready
```

Bulk list or status changes (`bulk_assign_unlocked_list`, `bulk_update_unlocked_status`) are polled
with `get_unlocked_list_task()`.

## Unlocked data and remediation

```python
await client.get_unlocked_leaks(status="new")
await client.set_unlocked_leak_status(leak_id, "fixed")    # new, in_progress, fixed, accepted_risk
await client.bulk_update_unlocked_status("accepted_risk", filters={"email_domain": "example.com"})
await client.set_unlocked_combolist_status("cl1_...", "in_progress")
await client.list_unlocked_lists(dataset="combolist")
```

## Monitoring

```python
runs = await client.list_notification_runs(dataset="combolist", sent_after="2026-10-01T00:00:00Z")
run = await client.get_notification_run(run_id)
items = await client.get_notification_run_items(run_id)
await client.set_notification_run_status(run_id, "fixed")
await client.export_notification_runs(filters={"dataset": "stealer_logs"}, format="csv")
await client.set_notification_method_active(method_id, True)  # resume a suspended channel
await client.test_notification_method_with_data(method_id, "dark_web")
```

## Errors

Every API error raises a subclass of `LeakRadarAPIError` with `status_code` and `detail`:

| Status | Exception |
|---|---|
| 400 | `BadRequestError` |
| 401 | `UnauthorizedError` |
| 402 | `PaymentRequiredError` |
| 403 | `ForbiddenError` |
| 404 | `NotFoundError` |
| 409 | `ConflictError` |
| 410 | `GoneError` |
| 422 | `ValidationError` |
| 429 | `TooManyRequestsError` |
| 503 | `ServiceUnavailableError` (search maintenance) |

`detail` is usually a string. Some endpoints answer an object (raw search cursors, maintenance):
`error.code` then holds its machine-readable code.

```python
from leakradar import TooManyRequestsError, ServiceUnavailableError

try:
    await client.raw_search(q="admin@example.com")
except TooManyRequestsError:
    ...  # slow down
except ServiceUnavailableError as error:
    print(error.detail)
```

## Retries, proxies, tests

The client does not retry by default. Pass any `httpx` transport:

```python
import httpx

client = LeakRadarClient(token="...", transport=httpx.AsyncHTTPTransport(retries=3))
```

`httpx.MockTransport(handler)` works the same way in your own tests.

## Upgrading from 0.1.x

- `notification_run_leaks()` now calls `get_notification_run_items()` (the API removed `/leaks`);
  `search` and `is_email` are ignored.
- `raw_export_preview()` now returns `raw_count()` (the API removed the preview).
- `test_notification_method_with_data(method_id, notification_type)`: the API takes the type in the
  query string, not a body. The 0.1.x dict argument still works and sends the same test as before.

All three emit a `DeprecationWarning`. See [CHANGELOG.md](CHANGELOG.md) for the full list.

## Development

```bash
python -m unittest discover -s tests          # no network needed
python scripts/update_openapi_snapshot.py     # refresh tests/openapi_ops.json from the live spec
```

After a snapshot refresh, `tests/test_contract.py` lists every operation or parameter the client
does not cover yet.
