# LeakRadar Python SDK

The official Python client for the [LeakRadar.io](https://leakradar.io) API, with a synchronous
client (`LeakRadar`) and an asynchronous one (`AsyncLeakRadar`).

LeakRadar indexes leaked credentials (stealer logs, combolists, raw leak files) and dark web
posts. With this SDK you search them, unlock and export what you need, follow background jobs,
manage monitors and read their webhook alerts.

- Every operation of the public API as a method, linked to its page of the API reference
- Sync and async clients with the same methods, arguments, results and errors
- Rate limit refusals and transient network failures retried for you, with one wait shared by
  every thread or task of a client
- Helpers for background jobs (`wait_for_task()`, `wait_for_export()`, `download_export()`) and
  for pagination (`paginate()`)
- Typed responses: a `TypedDict` for every answer of the API, for editor completion and mypy
- Webhook alerts read by `leakradar.webhooks`
- Python 3.8 to 3.14, httpx 0.27 and later 0.x releases

## Documentation

- API reference: <https://docs.leakradar.io>
- Changes between versions: [CHANGELOG.md](https://github.com/LeakRadar/leakradar-wrapper/blob/main/CHANGELOG.md)
- Runnable examples: [examples/](https://github.com/LeakRadar/leakradar-wrapper/tree/main/examples)
- Source: <https://github.com/LeakRadar/leakradar-wrapper>

## Install

```bash
pip install leakradar
pip install "leakradar[speedups]"   # optional: faster JSON decoding with ujson
```

The API key is in the dashboard, under
[Settings, API key](https://members.leakradar.io/settings?tab=api-key) (paid plan required).
Keep it out of your code: the clients read it from the `LEAKRADAR_API_KEY` environment variable.

```bash
export LEAKRADAR_API_KEY="your API key"
```

## Quickstart

Synchronous:

```python
from leakradar import LeakRadar

with LeakRadar() as client:  # reads LEAKRADAR_API_KEY
    profile = client.get_profile()
    results = client.search_email("john.doe@example.com")
    print(profile["email"], results["total"], results["total_unlocked"])
```

Asynchronous:

```python
import asyncio

from leakradar import AsyncLeakRadar


async def main() -> None:
    async with AsyncLeakRadar() as client:  # reads LEAKRADAR_API_KEY
        results = await client.search_email("john.doe@example.com")
        print(results["total"], results["total_unlocked"])


asyncio.run(main())
```

Every method returns the decoded JSON answer (a `dict` or a `list`), or `bytes` for a file.
Searching spends no points: credentials stay masked (`username_masked`, no `password`) until you
unlock them, 1 point per new record. `auto_unlock=True` on a search unlocks the page it returns,
the `unlock_*` methods unlock at once (at most 10,000 records per call, except
`unlock_notification_run_leaks()`, capped only by `max_leaks` and your points), and the
`queue_*_unlock_task` methods run the same unlock in the background, for larger ones (see
Background jobs). Without `max_leaks`, `unlock_email_leaks()`, `unlock_domain_leaks()`,
`unlock_all_advanced()` and `unlock_application_leaks()` take your whole point balance as their
cap: with more than 10,000 points, pass `max_leaks` (10,000 or less), or the call raises
`BadRequestError`.

## Configuration

```python
from leakradar import LeakRadar

client = LeakRadar(
    token="...",                                # default: LEAKRADAR_API_KEY
    base_url="https://api.leakradar.io",        # default: LEAKRADAR_BASE_URL, else this address
    timeout=30.0,
    verify="/etc/ssl/certs/corporate-ca.pem",   # a CA bundle, for a network that inspects TLS
    proxy="http://proxy.example.com:3128",
    max_retries=3,
    max_retry_wait=60.0,
)
```

| Argument | Default | What it does |
|---|---|---|
| `token` | `LEAKRADAR_API_KEY` | Your API key, sent as `Authorization: Bearer <key>` without surrounding whitespace (such as the newline of a key read from a file). `token=""` sends none (the public statistics need none). `client.token` can be changed between requests. |
| `base_url` | `LEAKRADAR_BASE_URL`, else `https://api.leakradar.io` | Address of the API, for instance behind a gateway. |
| `timeout` | `30.0` | Seconds before a request times out. |
| `verify` | `True` | TLS verification: `True`, `False`, the path of a CA bundle (or of a directory of CA certificates), or an `ssl.SSLContext`. `None` means the default (verification on); any other type raises `TypeError`. |
| `proxy` | none | Proxy URL, such as `"http://proxy.example.com:3128"`. |
| `transport` | none | An httpx transport: `httpx.HTTPTransport` for `LeakRadar`, `httpx.AsyncHTTPTransport` for the async clients, or `httpx.MockTransport(handler)` in your tests. `verify` and `proxy` are not used with a transport: configure them on it. |
| `user_agent` | `LeakRadar-Python-Client/<version>` | The `User-Agent` header of every request. |
| `max_retries` | `3` | Retries of a refused or failed request, see Rate limits and retries. `0` turns retries off. |
| `max_retry_wait` | `60.0` | Longest announced wait, in seconds, worth a retry: a refusal asking for more is raised at once. |

Pass the arguments by name: `base_url`, `verify` and `proxy` are keyword-only. `LeakRadar` and
`AsyncLeakRadar` read the environment variables only for arguments left out (an empty variable
counts as unset). `verify` and `proxy` also apply to raw file downloads (`download_raw_file()`).

## Which client?

| Class | Calls | Reads the environment | Since |
|---|---|---|---|
| `LeakRadar` | synchronous | yes | 0.3.0 |
| `AsyncLeakRadar` | asynchronous (`await`) | yes | 0.3.0 |
| `LeakRadarClient` | asynchronous (`await`) | no | 0.1.0 |

- **`LeakRadar`** for scripts, Django, Flask, Celery tasks and notebooks. It runs no event loop,
  so it also works where one is already running (Jupyter). One client can be shared by threads:
  they share its connections and its retry wait. Close it with `close()` or a `with` block.
- **`AsyncLeakRadar`** for asyncio code, and for trio code too: its waits (retries, the shared
  wait, `wait_for_*()`) sleep with anyio, which httpx installs. Share one client between the
  tasks of an event loop (`asyncio.gather`), close it with `await client.aclose()` or an
  `async with` block.
- **`LeakRadarClient`** is the asynchronous client of earlier versions, unchanged: same defaults,
  and it never reads the environment (pass `token=`). `AsyncLeakRadar` is a subclass of it, so
  `isinstance(client, LeakRadarClient)` keeps working. Code written for it with 0.1.6 or 0.2.x
  needs no change (older 0.1.x code: see Upgrading).

The three clients have the same methods, with the same arguments, results and errors. The sync
client differs only where Python does: `close()` instead of `aclose()`, `with` instead of
`async with`, `paginate()` returns a plain iterator, and the `on_progress` callbacks of the
`wait_for_*` methods are plain functions (the async clients also await a coroutine function).
`LeakRadar` is generated from the async code; it is not a subclass of `LeakRadarClient`.

## Usage

In the snippets below, `client` is a `LeakRadar`. With `AsyncLeakRadar` or `LeakRadarClient`,
put `await` before each call.

### Email, domain and advanced search (stealer logs)

```python
from datetime import datetime, timezone

client.search_email("john.doe@example.com", page_size=50)
client.search_email("john.doe@example.com", auto_unlock=True)  # unlocks the page with points

client.get_domain_report("example.com")
client.get_domain_all("example.com", page_size=500)  # employees, customers and third parties
client.get_domain_employees("example.com", search="admin")
pdf = client.get_domain_report_pdf("example.com")  # bytes

client.search_advanced(
    username=["john", "jane"],
    username_match_type="starts_with",  # contains (default), starts_with or ends_with
    url_domain="example.com",
    password_strength="weak",
    added_from=datetime(2026, 1, 1, tzinfo=timezone.utc),  # dates are sent as ISO 8601
)

# Which addresses or domains still have records you have not unlocked (up to 100 per call)
client.emails_locked_exists(
    ["john.doe@example.com", "jane.doe@example.com"], datasets=["stealer_logs", "combolist"]
)
client.domains_locked_exists(["example.com"], include_counts=True)
```

### Combolists

Combolists are a separate dataset: their records carry no URL, and they have their own unlock
methods and their own space of unlocked records.

```python
client.search_combolist_email("john.doe@example.com")
client.search_combolist_username("johndoe")
client.search_combolist_domain("example.com", page_size=1000)
client.get_combolist_domain_report("example.com")
client.search_combolist_advanced(email_domain="example.com", password_strength="too_weak")

client.unlock_combolist_leaks(["cl1_..."])

# A whole scope: "email", "username" or "domain" take a value, "advanced" takes filters
task = client.queue_combolist_unlock_task("domain", value="example.com", max_leaks=5000)
client.export_combolist("advanced", filters={"email_domain": "example.com"}, format="csv")

client.get_unlocked_combolists(unlocked_at_min="2026-10-01T00:00:00Z")
```

### Android applications

```python
apps = client.suggest_applications("netflix")
client.get_application_leaks("com.netflix.mediaclient")
client.export_application_leaks("com.netflix.mediaclient", format="json")
```

### Raw files

```python
client.raw_search(
    q="admin@example.com",
    q_exact=True,  # word boundaries
    folder_name="Passwords",
    folder_name_match_type="exact",
    exclude_terms=["test"],
    dedup=True,
)
estimate = client.raw_count(q="admin@example.com")  # parts a mass unlock would cost
task = client.queue_raw_unlock_task(q="admin@example.com", max_leaks=500)
client.raw_export("rows", q="admin@example.com")  # only what you unlocked
```

Without `q`, give at least one selector (`container_id`, `exts`, `exts_not`, `categories`,
`categories_not`, `file_name`, `file_name_not`, `folder_name`, `include_terms`, `ingested_at_min`
or `ingested_at_max`): the client raises `ValueError` before calling the API, as the API would
refuse it. `folder_name_not` and `exclude_terms` only narrow a selection.

### Dark web

```python
client.search_dark_web("example.com", sources=["breachforums"], sort_by="published_at")
client.search_dark_web(title="example", author="seller", logic="AND")
client.get_dark_web_post("post-id")
```

### Password range

A k-anonymity check of a password: send the first 5 characters of its SHA-1 hash, compare the
suffixes locally.

```python
client.password_range("5BAA6", suffix_only=True)  # Stealer Logs only
client.password_range("5BAA6", datasets=["stealer_logs", "combolist"])
# each hash: {"hash": ..., "count": <sum>, "sources": {"stealer_logs": ..., "combolist": ...}}
```

### Unlocked records and remediation

```python
# Stealer log records by their ID (combolist IDs, cl1_..., go to unlock_combolist_leaks())
client.unlock_specific_leaks(["<record id>"])
client.get_unlocked_leaks(status="new")
client.set_unlocked_leak_status("<record id>", "fixed")  # new, in_progress, fixed or accepted_risk
task = client.bulk_update_unlocked_status("accepted_risk", filters={"email_domain": ["example.com"]})
client.wait_for_unlocked_list_task(task)
client.set_unlocked_combolist_status("cl1_...", "in_progress")
client.list_unlocked_lists(dataset="combolist")
```

### Monitors and alerts

The API reference calls them monitors and alert channels; the methods say notifications and
notification methods.

```python
from leakradar import ConflictError

channel = client.create_notification_method({"type": "email", "value": "soc@example.com"})
try:
    monitor = client.create_notification(
        {"type": "domain", "value": "example.com", "dataset": "all", "method_id": channel["id"]}
    )
except ConflictError as error:
    if error.code != "monitor_overlap":
        raise
    print("Already monitored on this channel by", error.conflicts)

client.extend_monitors_to_all_datasets([12, 34])  # every dataset of your plan, one alert per check

runs = client.list_notification_runs(dataset="combolist", sent_after="2026-10-01T00:00:00Z")
items = client.get_notification_run_items(56)
client.set_notification_run_status(56, "fixed")
client.export_notification_runs(filters={"dataset": "stealer_logs"}, format="csv")
client.set_notification_method_active(channel["id"], True)  # resume a suspended channel
client.test_notification_method_with_data(channel["id"], "all")
```

## Errors

Every error the client raises itself derives from `LeakRadarError`:

```text
LeakRadarError
    LeakRadarAPIError              an error answer of the API (other statuses, such as 500)
        BadRequestError            400
        UnauthorizedError          401
        PaymentRequiredError       402
        ForbiddenError             403
        NotFoundError              404
        ConflictError              409
        GoneError                  410
        ValidationError            422
        TooManyRequestsError       429
        ServiceUnavailableError    503
    TaskFailedError                a background task ended FAILED or CANCELED
    ExportFailedError              an export ended with status ERROR
    WaitTimeoutError               a wait_for_*() method ran out of time (also a TimeoutError)
```

`LeakRadarAPIError` and its subclasses carry what the API answered:

| Attribute | Content |
|---|---|
| `status_code` | The HTTP status. |
| `detail` | The `detail` of the answer: a string most of the time, an object (`dict`) when the error carries data, a list of problems for a 422. |
| `message` | `detail` as readable text: the `message` of an object, "field: problem" for each invalid parameter of a 422. |
| `code` | The `code` of an object `detail`, such as `"monitor_overlap"`, else `None`. |
| `error_code` | The machine-readable code of any refusal: the `code` of an object `detail`, else the `code` of the `error` object (`"rate_limited"`), else the top-level `code` of the answer (`"search_unavailable"`, `"search_query_too_complex"`). `None` when the answer has none. |
| `request_id` | The `id` of a 500 answer (quote it when you contact support), else the `X-Request-ID` header, else `None`. |
| `conflicts` | For a 409 `monitor_overlap`: the monitors already covering the value on that channel. |
| `headers` | The response headers. |
| `error` | The `error` object of the answer (rate limit refusals), else `None`. |

`str(error)` reads `API Error <status>: <detail>`, as in every earlier version. Every error
survives `pickle` with its attributes, so it crosses process boundaries (Celery,
multiprocessing, `ProcessPoolExecutor`).

```python
from leakradar import LeakRadarAPIError, NotFoundError

try:
    post = client.get_dark_web_post("post-id")
    print(post.get("title"))
except NotFoundError:
    print("No such post")
except LeakRadarAPIError as error:
    print(error.status_code, error.message, error.request_id)
    raise
```

A network failure that outlasts the retries is raised as the httpx exception (`httpx.ConnectError`,
`httpx.ReadTimeout`...), a subclass of `httpx.TransportError`. Invalid arguments raise `ValueError`
before anything is sent. Import the exceptions from `leakradar`: `leakradar.types.ValidationError`
is the type of one 422 problem, not the exception.

## Rate limits and retries

Limits are per account and per credential: an API key, each connected app and the dashboard
each have their own, and searches can have a daily quota depending on the plan. `rate_limits()`
shows what applies to your key right now (it costs no quota):

```python
limits = client.rate_limits()
limits["families"]  # per family: per_second, burst, scope
limits["daily"]     # limit, used, retry_at (limit None: no daily cap)
limits["pauses"]    # families paused for this key, with their end
limits["last_24h"]  # refusals of the last 24 hours, by reason
```

When the API refuses a request it answers 429 with a reason, and the client reacts by reason:

| `reason` | Meaning | The client |
|---|---|---|
| `rate` | too many requests per second (per hour for the alert channel tests) | waits for `Retry-After`, retries |
| `concurrency` | too many searches running at once for the plan | waits for `Retry-After`, retries |
| `busy` (503) | shared capacity exhausted | waits for `Retry-After`, retries |
| `daily_quota` | daily search quota reached (rolling 24 hours) | raises at once, `retry_at` says when searches resume |
| `paused` | the key kept calling after its refusals: that family is paused for 2 minutes, then 10, then 60 if it happens again within 24 hours | raises at once, `retry_at` is the end of the pause |

Transient failures are retried too:

| Failure | Retried for | Wait |
|---|---|---|
| 503 with a `Retry-After` (search temporarily unavailable) | every request except a synchronous unlock (the `unlock_*()` methods, a path ending in `/unlock`): its points may already be spent, so that 503 is raised | `Retry-After` |
| Connection failure (`httpx.ConnectError`, `httpx.ConnectTimeout`) | every request: nothing was sent | 0.5 s, then 1 s, 2 s... |
| Read failure or timeout, 502 or 504 answer | GET requests only: a POST may have run | 0.5 s, then 1 s, 2 s... |

- A request is retried at most `max_retries` times (3 by default), all causes together, and only
  when the announced wait is at most `max_retry_wait` seconds (60 by default). Each wait gets a
  small random extra delay. A 503 without `Retry-After` (maintenance) is raised at once.
- Once the retries are spent, the last error is raised as it is: the API error, or the httpx
  exception.
- **One wait per client.** While a refusal's wait runs, every other request of the same client
  waits too: a script running 50 searches at once pauses as one instead of collecting 50
  refusals, which would count towards a pause. Use one client per script, shared by its threads
  or tasks.
- A request refused by a limit (a 429, or the 503 `busy`) was not run (no points spent, no
  quota counted), so replaying it is safe, unlocks included.
- A synchronous unlock that raises `ServiceUnavailableError` without the `busy` reason may have
  unlocked records, and spent their points, before the 503: check your unlocked records before
  you unlock again.

```python
from leakradar import LeakRadar

client = LeakRadar(max_retries=5)  # more patience
client = LeakRadar(max_retries=0)  # no retry and no shared wait (the 0.2 behaviour)
```

A refusal that is not retried, or still refused after the retries, raises `TooManyRequestsError`
(or `ServiceUnavailableError` for `busy`), which carries what the refusal says (`None` when the
answer does not carry it):

| Attribute | Content |
|---|---|
| `reason` | `rate`, `concurrency`, `daily_quota`, `paused` or `busy` |
| `retry_after` | seconds to wait (float) |
| `retry_at` | when to try again (timezone-aware UTC `datetime`) |
| `scope` | what the limit counts: `credential`, `team`, `ip`, `global`, `account` or `edge` |
| `family` | `search`, `heavy` (advanced and raw search), `unlock`, `widget`, `channel_test` (alert channel tests) or `default` |
| `limit` | `{"per_second", "burst"}` for `rate`, plus `window_seconds` for the alert channel tests |
| `daily` | `{"limit", "used", "retry_at"}` for `daily_quota` |

```python
from leakradar import ServiceUnavailableError, TooManyRequestsError

try:
    client.raw_search(q="admin@example.com")
except TooManyRequestsError as error:
    if error.reason in ("daily_quota", "paused"):
        print("searches resume at", error.retry_at)
    else:
        raise  # still refused after the retries: slow down
except ServiceUnavailableError as error:
    print(error.message)
```

## Background jobs

Unlock tasks and most exports run on the server: the call answers at once with an ID to follow.
Following them never counts against the daily search quota.

### Unlock tasks

```python
from leakradar import TaskFailedError


def show(status):
    print(status.get("status"), status.get("updated"), "of", status.get("total"))


task = client.queue_domain_unlock_task("example.com", "employees", max_leaks=1000)
try:
    done = client.wait_for_task(task, on_progress=show, timeout=3600)
except TaskFailedError as error:
    print(error.status, error.code, error.message)  # records unlocked before stay unlocked
```

`wait_for_task(task_id, *, interval=2.0, timeout=None, on_progress=None)` reads
`get_task_status()` every `interval` seconds and returns the final status once it is `COMPLETED`.
`task_id` is the task ID, or the dict a `queue_*_unlock_task()` method returned. `on_progress`
is called after every read, the last one included, with the status: `updated` records processed
out of `total`.

A task that ends `FAILED` or `CANCELED` raises `TaskFailedError`, with `task_id`, `status`,
`code`, `message` (a sentence you can show) and `task` (the last status read). Records unlocked
before the failure stay unlocked.

| `code` | Why the task failed |
|---|---|
| `insufficient_points` | not enough points left |
| `timeout` | stopped after the time limit of a task |
| `blocked` | the search is blocked |
| `restart_required` | the blocked results changed while the task ran: start it again |
| `interrupted` | stopped on our side: start it again |
| `unlock_failed` | no record could be unlocked |
| `dataset_unavailable` | the dataset is temporarily unavailable: try again later |
| `internal_error` | anything else: start it again, then contact support with the task ID |
| `None` | a canceled task |

`wait_for_unlocked_list_task()` does the same for the bulk tasks on your unlocked records:
`bulk_assign_unlocked_list()`, `bulk_update_unlocked_status()`, `delete_unlocked_list()` and
`clear_unlocked_list()`.

### Exports

```python
from leakradar import ExportFailedError

queued = client.export_domain_leaks("example.com", "employees", format="csv")
try:
    export = client.wait_for_export(queued, timeout=3600)
except ExportFailedError:
    raise SystemExit("The export failed: queue it again.")
path = client.download_export(export, "example-employees.zip")
print(path or "nothing to export")
```

- The `export_*()` methods queue a job and answer with its `export_id`, except
  `export_domain_subdomains()` and `export_domain_urls()`, which return the file itself (bytes,
  or a list with `format="json"`) and take no export slot. An export holds the records you have
  unlocked (every record on plans where everything is readable). Your account runs at most 5
  exports at once, all types together (one more raises `BadRequestError`), and
  `export_advanced_urls()` only when no other export is pending or running (else
  `BadRequestError`). Queuing an export identical to one in progress raises `ConflictError`.
- `wait_for_export(export_id, *, interval=3.0, timeout=None, on_progress=None)` takes the export
  ID, or the dict an `export_*()` method returned, and returns the export once it is
  `COMPLETED`. It raises `ExportFailedError` when it ends with status `ERROR`, and `NotFoundError`
  when none of your exports has this ID.
- `download_export(export, path=None)` downloads its ZIP archive: the bytes, or with `path`, the
  path of the file. The archive is written in chunks to a temporary file next to `path`, then
  moved into place, so an existing file is only replaced once the download is complete; on POSIX
  systems the file is readable by its owner only. It returns `None` when there was nothing to
  export, and raises `ValueError` when the export is not `COMPLETED` yet. Your API key is only
  sent to the API, never to the storage host the download URL points to.

### Timeouts

With `timeout` (seconds), the `wait_for_*` methods raise `WaitTimeoutError` once it runs out,
with `waited` (seconds) and `last` (the last state read). The job keeps running on the server:
wait for it again later with the same ID. `WaitTimeoutError` is also a `TimeoutError`.

## Pagination

```python
for leak in client.paginate(client.get_domain_all, "example.com", page_size=1000):
    print(leak.get("category"), leak.get("username_masked"))
```

```python
async def employees(client):
    async for leak in client.paginate(client.get_domain_employees, "example.com", max_items=5000):
        print(leak.get("username_masked"))
```

`paginate(method, *args, page_size=None, max_items=None, items_key="items", **kwargs)` calls a
listing method of the client page after page and yields its items. `*args` and `**kwargs` are
the arguments of that method (`page=` sets the first page, except for `raw_search()`, which pages
by cursor only). It stops:

- on an empty page, or a page shorter than the page size (except for the items of a monitoring
  run, see below);
- when the answer says `has_more: false`, or once `page * page_size` reaches `total`;
- after `max_items` items.

The domain listings and the advanced, application and combolist searches are served page by page
only up to 1,000,000 results (`page * page_size`): `paginate()` stops there with a `UserWarning`
naming the export that returns everything, such as `export_domain_leaks(domain, "all")`.
`search_dark_web()` stops the same way at the 10,000 results the API serves, with a
`UserWarning` suggesting narrower filters (`date_from` / `date_to`, `sources`).

`raw_search()` is followed through its `next_cursor` while `has_more` is true (its pages can come
back short, or even empty). The items of a monitoring run (`get_notification_run_items()`) can
also come back short or empty before the end, when records left the index after the run:
`paginate()` reads them until `page * page_size` reaches `total`.

Each page is one request: rate limits apply, and with `auto_unlock=True` every page is unlocked
with points.

## Domains

Every argument that is a domain also accepts a URL: the client searches its host, and says so
with a `UserWarning`.

```python
client.get_domain_report("https://www.example.com/login")
# UserWarning: 'https://www.example.com/login' is a URL: searching the domain 'example.com'.
```

A domain is trimmed, lowercased and loses its final dot. A URL (a scheme, a path, a query, a
fragment or a port) is reduced to its host, without a leading `www.` when the host has 3 labels
or more. An empty result raises `ValueError` before any request. This applies to the domain
searches, reports and exports, the combolist domain search and report, and the combolist scope
`"domain"`. Versions before 0.3.0 sent the URL as it was, which these routes refused (a 404 for a
domain in the URL path, a 422 for the combolist scope).

`domains_locked_exists()` sends its entries as given: the API reduces each one itself (the host
of a URL with a scheme, without `www.`, lowercase) and answers an entry that is no domain as not
found; an entry that reduces to nothing (`""`, `"www."`) makes it refuse the whole call
(`ValidationError`).

To silence the warning:

```python
import warnings

warnings.filterwarnings("ignore", message=".* is a URL: searching the domain")
```

## Typed responses

`leakradar.types` has a `TypedDict` for every answer of the API, and every method is annotated
with the one it returns: editors complete the fields, and mypy or pyright check them.

```python
from typing import List

from leakradar.types import EmailSearchResponse


def masked_usernames(results: EmailSearchResponse) -> List[str]:
    return [leak.get("username_masked") or "" for leak in results["items"]]


masked_usernames(client.search_email("john.doe@example.com"))
```

- Nothing changes at runtime: results are the same plain `dict` and `list` objects as before.
- Every `TypedDict` is `total=False`, as the API may leave a key out: read optional keys with
  `.get()`.
- Type checkers now check results against the API schema, so code that type-checked against
  0.2 (or 0.1.x, which shipped no types) can get new mypy or pyright errors: a result annotated
  `Dict[str, Any]` (a `TypedDict` is not one), a field that can be `None` used without a check
  (`profile["plan"]["name"]`), a key the schema does not list. Annotate such a result as
  `Mapping[str, Any]` to keep the old leniency. Code that runs is never affected.

## Webhooks

A monitor whose alert channel is a webhook POSTs each alert as JSON. `parse_delivery()` reads
one POST:

```python
import hmac

from leakradar.webhooks import parse_delivery

SECRET = b"the secret of your channel header"


def handle(body: bytes, headers) -> None:
    received = (headers.get("X-Webhook-Secret") or "").encode()
    if not hmac.compare_digest(received, SECRET):
        raise PermissionError("not from our LeakRadar channel")
    delivery = parse_delivery(body, headers)
    if delivery.type is None:
        return  # the connection test of the channel
    alert = delivery.alert
    if alert["type"] == "leak":  # "leak" (credentials), "raw" or "dark_web"
        for record in alert["unlocked_leaks"]:
            print(record.get("url"), record.get("username"))
```

- LeakRadar does not sign its deliveries. Give your channel a custom header holding a secret of
  yours, and check it first, in constant time (`hmac.compare_digest`):

  ```python
  client.create_notification_method({
      "type": "webhook",
      "value": "https://hooks.example.com/leakradar",
      "headers": [{"name": "X-Webhook-Secret", "value": "a long random secret"}],
  })
  ```

- Answer with a 2xx status within 20 seconds, and do the long work afterwards. A timeout or a
  connection failure is sent again (twice at most), so the same alert can arrive up to three
  times: `delivery.delivery_id` identifies the POSTs of an all-datasets monitor,
  `alert["run_id"]` those of the other monitors. An endpoint that keeps failing gets its channel
  suspended until it answers again.
- `delivery` also has `check_id`, `section` and the whole decoded `body`. Its `repr()` never
  shows the alert, which holds credentials in clear.

[examples/webhook_receiver.py](https://github.com/LeakRadar/leakradar-wrapper/blob/main/examples/webhook_receiver.py)
is a complete receiver, standard library only.

## Calling other routes

```python
usage = client.request("GET", "/profile/usage")
```

`request(method, path, *, params=None, json=None, headers=None)` calls a route of the API that
has no method yet, through the same steps as the methods: authentication, retries, errors and
decoding. It only calls the API (`base_url`): an absolute URL to another host raises
`ValueError`.

## Logging and deprecations

```python
import logging

logging.basicConfig()
logging.getLogger("leakradar").setLevel(logging.DEBUG)
```

The `leakradar` logger writes one DEBUG line per request (method, path, status, duration) and
per retry. It never writes the API key, bodies or query values, which can hold email addresses.
When the API marks a route as deprecated, the client logs a warning and raises a
`DeprecationWarning`, once per route and client.

## Testing your code

`httpx.MockTransport` answers in place of the API, without network:

```python
import httpx

from leakradar import LeakRadar


def handler(request: httpx.Request) -> httpx.Response:
    assert request.url.path == "/profile"
    return httpx.Response(200, json={"email": "john.doe@example.com"})


client = LeakRadar(token="test", transport=httpx.MockTransport(handler), max_retries=0)
assert client.get_profile()["email"] == "john.doe@example.com"
```

The async clients take `httpx.MockTransport` the same way. `max_retries=0` shows every refusal
as it comes.

## Upgrading from 0.1.x or 0.2.x

Nothing to change from 0.1.6 or 0.2.x: `LeakRadarClient` keeps its methods, arguments, defaults,
results and errors, and `str(error)` keeps its format. What changes by default, and how to keep
the old behaviour:

- **Retries.** Rate limit refusals (`rate`, `concurrency`, 503 `busy`), 503 answers with a
  `Retry-After` (except after a synchronous unlock), connection failures, and read failures or
  502/504 answers on GET requests are retried, and while one request waits for a refusal, the
  other requests of the client wait too. `LeakRadarClient(token=key, max_retries=0)` restores the
  0.2 behaviour: no retry, no shared wait.
- **Domains.** A URL passed as a domain argument searches its host, with a `UserWarning`,
  instead of being refused (see Domains).
- **Exceptions.** They derive from the new `LeakRadarError`; `LeakRadarAPIError` and its
  subclasses are the same classes, so existing `except` clauses keep working. They now survive
  `pickle`.
- **Types.** Type checkers see a `TypedDict` where they saw `Dict[str, Any]` (0.2.x) or no types
  (0.1.x), and may report new errors (see Typed responses); nothing changes at runtime.

Moving to the new clients is optional: `AsyncLeakRadar()` is `LeakRadarClient` reading
`LEAKRADAR_API_KEY`, and `LeakRadar()` serves code without asyncio. Code calling the private
`_request()`, or overriding it or `_handle_error()` in a subclass, keeps working, but the public
`request()` is the supported way to call a route the client has no method for.

From 0.1.x, three methods follow API changes, each with a `DeprecationWarning`:

- `notification_run_leaks()` calls `get_notification_run_items()` (the API removed `/leaks`);
  `search` and `is_email` are ignored.
- `raw_export_preview()` returns `raw_count()` (the API removed the preview).
- `test_notification_method_with_data(method_id, notification_type)`: the API takes the type in
  the query string, not a body. The 0.1.x dict argument still works and sends the same test as
  before.

Code written for earlier 0.1.x releases can also meet what 0.1.2 to 0.2.0 changed (0.3.0
changes none of it):

- Pass arguments by name (code for 0.1.1 to 0.1.4). Releases 0.1.2 to 0.1.5 inserted arguments
  in `search_email()`, `unlock_email_leaks()`, `unlock_domain_leaks()`,
  `export_unlocked_leaks()`, `export_advanced()` and `search_advanced()`: the fourth positional
  argument of `unlock_domain_leaks()` in 0.1.1 to 0.1.3, `max_leaks`, is now `is_email`.
- The metadata methods (`search_metadata()`, `metadata_leaks()`...) were removed in 0.1.6, as
  the API no longer has a metadata search (code for 0.1.1 to 0.1.5).
- Since 0.1.5, a request sent straight through `client._client` carries no API key: call
  `request()` instead (code for 0.1.1 to 0.1.4).
- Since 0.2.0, `client._anon_client` is `None` until the first `download_raw_file()` creates it
  (code for 0.1.5 and 0.1.6).

## Compatibility policy

- From 0.3.0 on, nothing is removed or renamed in 0.x: methods, arguments, exceptions and import
  paths (`leakradar`, `leakradar.client`).
- A new argument comes last, with a default (keyword-only most of the time). The order and the
  defaults of existing arguments never change.
- Results keep their runtime types: `dict`, `list`, `bytes` or `str`.
- `LeakRadarClient` keeps its behaviour and defaults, and never reads the environment.
- The default behaviour only changes to retry transient failures (`max_retries=0` turns that
  off), or to accept an input that used to fail.
- Python 3.8 and later, and httpx 0.27 and later 0.x releases, stay supported throughout 0.x.
- A deprecation is announced in the changelog and with a `DeprecationWarning`, and the deprecated
  call keeps working.
- The public surface and the HTTP requests of every published version since 0.1.1 are recorded
  in `tests/compat` (0.1.0, uploaded before the first commit, cannot be rebuilt), and every change
  is tested against them.

## Method reference

Every method has the same name on `LeakRadar`, `AsyncLeakRadar` and `LeakRadarClient`, except
`aclose()`, which is `close()` on `LeakRadar`. The operation links to its page of the API
reference, where you find its parameters and its answer; the method's docstring ends with the
same link (`help(LeakRadar.search_email)`).

### Email

| Method | Operation |
|---|---|
| `search_email()` | [`search_email`](https://docs.leakradar.io/#tag/search-email/search_email) |
| `export_email_leaks()` | [`export_email`](https://docs.leakradar.io/#tag/search-email/export_email) |
| `unlock_email_leaks()` | [`unlock_email`](https://docs.leakradar.io/#tag/search-email/unlock_email) |
| `queue_email_unlock_task()` | [`queue_email_unlock`](https://docs.leakradar.io/#tag/search-email/queue_email_unlock) |
| `emails_locked_exists()` | [`check_emails_locked`](https://docs.leakradar.io/#tag/search-email/check_emails_locked) |

### Domain

| Method | Operation |
|---|---|
| `get_domain_report()` | [`get_domain_report`](https://docs.leakradar.io/#tag/search-domain/get_domain_report) |
| `get_domain_report_pdf()` | [`get_domain_report_pdf`](https://docs.leakradar.io/#tag/search-domain/get_domain_report_pdf) |
| `get_domain_all()` | [`list_domain_leaks`](https://docs.leakradar.io/#tag/search-domain/list_domain_leaks) |
| `get_domain_customers()` | [`list_domain_customers`](https://docs.leakradar.io/#tag/search-domain/list_domain_customers) |
| `get_domain_employees()` | [`list_domain_employees`](https://docs.leakradar.io/#tag/search-domain/list_domain_employees) |
| `get_domain_third_parties()` | [`list_domain_third_parties`](https://docs.leakradar.io/#tag/search-domain/list_domain_third_parties) |
| `get_domain_subdomains()` | [`list_domain_subdomains`](https://docs.leakradar.io/#tag/search-domain/list_domain_subdomains) |
| `export_domain_subdomains()` | [`export_domain_subdomains`](https://docs.leakradar.io/#tag/search-domain/export_domain_subdomains) |
| `get_domain_urls()` | [`list_domain_urls`](https://docs.leakradar.io/#tag/search-domain/list_domain_urls) |
| `export_domain_urls()` | [`export_domain_urls`](https://docs.leakradar.io/#tag/search-domain/export_domain_urls) |
| `export_domain_leaks()` | [`export_domain`](https://docs.leakradar.io/#tag/search-domain/export_domain) |
| `unlock_domain_leaks()` | [`unlock_domain`](https://docs.leakradar.io/#tag/search-domain/unlock_domain) |
| `queue_domain_unlock_task()` | [`queue_domain_unlock`](https://docs.leakradar.io/#tag/search-domain/queue_domain_unlock) |
| `domains_locked_exists()` | [`check_domains_locked`](https://docs.leakradar.io/#tag/search-domain/check_domains_locked) |

### Advanced

| Method | Operation |
|---|---|
| `search_advanced()` | [`search_advanced`](https://docs.leakradar.io/#tag/search-advanced/search_advanced) |
| `unlock_all_advanced()` | [`unlock_advanced`](https://docs.leakradar.io/#tag/search-advanced/unlock_advanced) |
| `queue_advanced_unlock_task()` | [`queue_advanced_unlock`](https://docs.leakradar.io/#tag/search-advanced/queue_advanced_unlock) |
| `export_advanced()` | [`export_advanced`](https://docs.leakradar.io/#tag/search-advanced/export_advanced) |
| `export_advanced_urls()` | [`export_advanced_urls`](https://docs.leakradar.io/#tag/search-advanced/export_advanced_urls) |

### Android apps

| Method | Operation |
|---|---|
| `suggest_applications()` | [`suggest_applications`](https://docs.leakradar.io/#tag/search-applications/suggest_applications) |
| `get_application_leaks()` | [`list_application_leaks`](https://docs.leakradar.io/#tag/search-applications/list_application_leaks) |
| `unlock_application_leaks()` | [`unlock_application`](https://docs.leakradar.io/#tag/search-applications/unlock_application) |
| `queue_application_unlock_task()` | [`queue_application_unlock`](https://docs.leakradar.io/#tag/search-applications/queue_application_unlock) |
| `export_application_leaks()` | [`export_application`](https://docs.leakradar.io/#tag/search-applications/export_application) |

### Combolists

| Method | Operation |
|---|---|
| `search_combolist_email()` | [`search_combolist_email`](https://docs.leakradar.io/#tag/search-combolists/search_combolist_email) |
| `search_combolist_username()` | [`search_combolist_username`](https://docs.leakradar.io/#tag/search-combolists/search_combolist_username) |
| `search_combolist_domain()` | [`search_combolist_domain`](https://docs.leakradar.io/#tag/search-combolists/search_combolist_domain) |
| `get_combolist_domain_report()` | [`get_combolist_domain_report`](https://docs.leakradar.io/#tag/search-combolists/get_combolist_domain_report) |
| `search_combolist_advanced()` | [`search_combolist_advanced`](https://docs.leakradar.io/#tag/search-combolists/search_combolist_advanced) |
| `unlock_combolist_leaks()` | [`unlock_combolist`](https://docs.leakradar.io/#tag/search-combolists/unlock_combolist) |
| `queue_combolist_unlock_task()` | [`queue_combolist_unlock`](https://docs.leakradar.io/#tag/search-combolists/queue_combolist_unlock) |
| `export_combolist()` | [`export_combolist`](https://docs.leakradar.io/#tag/search-combolists/export_combolist) |

### Raw files

| Method | Operation |
|---|---|
| `raw_search()` | [`search_raw`](https://docs.leakradar.io/#tag/raw-search/search_raw) |
| `raw_count()` | [`count_raw`](https://docs.leakradar.io/#tag/raw-search/count_raw) |
| `queue_raw_unlock_task()` | [`queue_raw_unlock`](https://docs.leakradar.io/#tag/raw-search/queue_raw_unlock) |
| `raw_export_preview()` (deprecated) | [`count_raw`](https://docs.leakradar.io/#tag/raw-search/count_raw) |
| `raw_export()` | [`export_raw`](https://docs.leakradar.io/#tag/raw-search/export_raw) |
| `raw_list_parts()` | [`list_raw_parts`](https://docs.leakradar.io/#tag/raw-search/list_raw_parts) |
| `raw_get_part()` | [`get_raw_part`](https://docs.leakradar.io/#tag/raw-search/get_raw_part) |
| `container_tree()` | [`browse_container_tree`](https://docs.leakradar.io/#tag/raw-search/browse_container_tree) |
| `container_subfolders()` | [`list_container_subfolders`](https://docs.leakradar.io/#tag/raw-search/list_container_subfolders) |
| `container_resolve_path()` | [`resolve_container_path`](https://docs.leakradar.io/#tag/raw-search/resolve_container_path) |
| `container_file_info()` | [`get_container_file_info`](https://docs.leakradar.io/#tag/raw-search/get_container_file_info) |
| `list_raw_files()` | [`list_raw_files`](https://docs.leakradar.io/#tag/raw-search/list_raw_files) |
| `raw_download_preview()` | [`preview_raw_download`](https://docs.leakradar.io/#tag/raw-search/preview_raw_download) |
| `raw_download_source()` | [`create_raw_download`](https://docs.leakradar.io/#tag/raw-search/create_raw_download) |

### Dark web

| Method | Operation |
|---|---|
| `search_dark_web()` | [`search_dark_web`](https://docs.leakradar.io/#tag/dark-web-search/search_dark_web) |
| `get_dark_web_post()` | [`get_dark_web_post`](https://docs.leakradar.io/#tag/dark-web-search/get_dark_web_post) |
| `get_dark_web_sources()` | [`list_dark_web_sources`](https://docs.leakradar.io/#tag/dark-web-search/list_dark_web_sources) |
| `get_dark_web_stats()` | [`get_dark_web_stats`](https://docs.leakradar.io/#tag/dark-web-search/get_dark_web_stats) |

### Password Range

| Method | Operation |
|---|---|
| `password_range()` | [`search_password_range`](https://docs.leakradar.io/#tag/password-range/search_password_range) |

### Unlock by ID

| Method | Operation |
|---|---|
| `unlock_specific_leaks()` | [`unlock_by_id`](https://docs.leakradar.io/#tag/unlock/unlock_by_id) |

### Unlocked leaks

| Method | Operation |
|---|---|
| `get_unlocked_leaks()` | [`list_unlocked_leaks`](https://docs.leakradar.io/#tag/unlocked-leaks/list_unlocked_leaks) |
| `export_unlocked_leaks()` | [`export_unlocked_leaks`](https://docs.leakradar.io/#tag/unlocked-leaks/export_unlocked_leaks) |
| `get_unlocked_advanced()` | [`search_unlocked_leaks`](https://docs.leakradar.io/#tag/unlocked-leaks/search_unlocked_leaks) |
| `export_unlocked_advanced()` | [`export_unlocked_leaks_advanced`](https://docs.leakradar.io/#tag/unlocked-leaks/export_unlocked_leaks_advanced) |
| `set_unlocked_leak_status()` | [`set_unlocked_leak_status`](https://docs.leakradar.io/#tag/unlocked-leaks/set_unlocked_leak_status) |
| `bulk_update_unlocked_status()` | [`bulk_set_unlocked_leak_status`](https://docs.leakradar.io/#tag/unlocked-leaks/bulk_set_unlocked_leak_status) |
| `set_unlocked_leak_list()` | [`set_unlocked_leak_list`](https://docs.leakradar.io/#tag/unlocked-leaks/set_unlocked_leak_list) |
| `upsert_unlocked_leak_comment()` | [`set_unlocked_leak_comment`](https://docs.leakradar.io/#tag/unlocked-leaks/set_unlocked_leak_comment) |
| `delete_unlocked_leak_comment()` | [`delete_unlocked_leak_comment`](https://docs.leakradar.io/#tag/unlocked-leaks/delete_unlocked_leak_comment) |

### Unlocked lists

| Method | Operation |
|---|---|
| `list_unlocked_lists()` | [`list_unlocked_lists`](https://docs.leakradar.io/#tag/unlocked-leaks-lists/list_unlocked_lists) |
| `create_unlocked_list()` | [`create_unlocked_list`](https://docs.leakradar.io/#tag/unlocked-leaks-lists/create_unlocked_list) |
| `update_unlocked_list()` | [`update_unlocked_list`](https://docs.leakradar.io/#tag/unlocked-leaks-lists/update_unlocked_list) |
| `delete_unlocked_list()` | [`delete_unlocked_list`](https://docs.leakradar.io/#tag/unlocked-leaks-lists/delete_unlocked_list) |
| `clear_unlocked_list()` | [`clear_unlocked_list`](https://docs.leakradar.io/#tag/unlocked-leaks-lists/clear_unlocked_list) |
| `get_unlocked_list_task()` | [`get_unlocked_list_task`](https://docs.leakradar.io/#tag/unlocked-leaks-lists/get_unlocked_list_task) |
| `bulk_assign_unlocked_list()` | [`bulk_assign_unlocked_list`](https://docs.leakradar.io/#tag/unlocked-leaks-lists/bulk_assign_unlocked_list) |
| `wait_for_unlocked_list_task()` | [`get_unlocked_list_task`](https://docs.leakradar.io/#tag/unlocked-leaks-lists/get_unlocked_list_task) |

### Unlocked combolists

| Method | Operation |
|---|---|
| `get_unlocked_combolists()` | [`list_unlocked_combolists`](https://docs.leakradar.io/#tag/unlocked-combolists/list_unlocked_combolists) |
| `get_unlocked_combolists_advanced()` | [`search_unlocked_combolists`](https://docs.leakradar.io/#tag/unlocked-combolists/search_unlocked_combolists) |
| `export_unlocked_combolists()` | [`export_unlocked_combolists`](https://docs.leakradar.io/#tag/unlocked-combolists/export_unlocked_combolists) |
| `set_unlocked_combolist_list()` | [`set_unlocked_combolist_list`](https://docs.leakradar.io/#tag/unlocked-combolists/set_unlocked_combolist_list) |
| `upsert_unlocked_combolist_comment()` | [`set_unlocked_combolist_comment`](https://docs.leakradar.io/#tag/unlocked-combolists/set_unlocked_combolist_comment) |
| `delete_unlocked_combolist_comment()` | [`delete_unlocked_combolist_comment`](https://docs.leakradar.io/#tag/unlocked-combolists/delete_unlocked_combolist_comment) |
| `set_unlocked_combolist_status()` | [`set_unlocked_combolist_status`](https://docs.leakradar.io/#tag/unlocked-combolists/set_unlocked_combolist_status) |

### Raw downloads

| Method | Operation |
|---|---|
| `list_raw_downloads()` | [`list_raw_downloads`](https://docs.leakradar.io/#tag/profile-downloads/list_raw_downloads) |
| `list_unlocked_raw_parts()` | [`list_unlocked_raw_parts`](https://docs.leakradar.io/#tag/profile-downloads/list_unlocked_raw_parts) |
| `get_raw_download_url()` | [`download_raw_file`](https://docs.leakradar.io/#tag/profile-downloads/download_raw_file) |
| `download_raw_file()` | [`download_raw_file`](https://docs.leakradar.io/#tag/profile-downloads/download_raw_file) |

### Exports

| Method | Operation |
|---|---|
| `list_exports()` | [`list_exports`](https://docs.leakradar.io/#tag/exports/list_exports) |
| `wait_for_export()` | [`list_exports`](https://docs.leakradar.io/#tag/exports/list_exports) |
| `download_export()` | [`list_exports`](https://docs.leakradar.io/#tag/exports/list_exports) |

### Task status

| Method | Operation |
|---|---|
| `get_task_status()` | [`get_task_status`](https://docs.leakradar.io/#tag/tasks/get_task_status) |
| `wait_for_task()` | [`get_task_status`](https://docs.leakradar.io/#tag/tasks/get_task_status) |

### Monitors

| Method | Operation |
|---|---|
| `list_notifications()` | [`list_monitors`](https://docs.leakradar.io/#tag/notifications/list_monitors) |
| `create_notification()` | [`create_monitor`](https://docs.leakradar.io/#tag/notifications/create_monitor) |
| `update_notification()` | [`update_monitor`](https://docs.leakradar.io/#tag/notifications/update_monitor) |
| `delete_notification()` | [`delete_monitor`](https://docs.leakradar.io/#tag/notifications/delete_monitor) |
| `delete_notifications_bulk()` | [`delete_monitors`](https://docs.leakradar.io/#tag/notifications/delete_monitors) |
| `set_notification_active()` | [`set_monitor_active`](https://docs.leakradar.io/#tag/notifications/set_monitor_active) |
| `extend_monitor_to_all_datasets()` | [`extend_monitor_to_all_datasets`](https://docs.leakradar.io/#tag/notifications/extend_monitor_to_all_datasets) |
| `extend_monitors_to_all_datasets()` | [`extend_monitors_to_all_datasets`](https://docs.leakradar.io/#tag/notifications/extend_monitors_to_all_datasets) |
| `get_notifications_stats()` | [`get_monitoring_stats`](https://docs.leakradar.io/#tag/notifications/get_monitoring_stats) |

### Alert channels

| Method | Operation |
|---|---|
| `list_notification_methods()` | [`list_alert_channels`](https://docs.leakradar.io/#tag/alert-channels/list_alert_channels) |
| `create_notification_method()` | [`create_alert_channel`](https://docs.leakradar.io/#tag/alert-channels/create_alert_channel) |
| `update_notification_method()` | [`update_alert_channel`](https://docs.leakradar.io/#tag/alert-channels/update_alert_channel) |
| `delete_notification_method()` | [`delete_alert_channel`](https://docs.leakradar.io/#tag/alert-channels/delete_alert_channel) |
| `set_notification_method_active()` | [`set_alert_channel_active`](https://docs.leakradar.io/#tag/alert-channels/set_alert_channel_active) |
| `test_notification_method()` | [`test_alert_channel`](https://docs.leakradar.io/#tag/alert-channels/test_alert_channel) |
| `test_notification_method_with_data()` | [`send_alert_channel_test`](https://docs.leakradar.io/#tag/alert-channels/send_alert_channel_test) |

### Monitoring runs

| Method | Operation |
|---|---|
| `list_notification_runs()` | [`list_monitoring_runs`](https://docs.leakradar.io/#tag/notifications-runs/list_monitoring_runs) |
| `get_notification_run()` | [`get_monitoring_run`](https://docs.leakradar.io/#tag/notifications-runs/get_monitoring_run) |
| `get_notification_run_items()` | [`list_monitoring_run_items`](https://docs.leakradar.io/#tag/notifications-runs/list_monitoring_run_items) |
| `notification_run_leaks()` (deprecated) | [`list_monitoring_run_items`](https://docs.leakradar.io/#tag/notifications-runs/list_monitoring_run_items) |
| `set_notification_run_status()` | [`set_monitoring_run_status`](https://docs.leakradar.io/#tag/notifications-runs/set_monitoring_run_status) |
| `unlock_notification_run_leaks()` | [`unlock_monitoring_run`](https://docs.leakradar.io/#tag/notifications-runs/unlock_monitoring_run) |
| `export_notification_run()` | [`export_monitoring_run`](https://docs.leakradar.io/#tag/notifications-runs/export_monitoring_run) |
| `export_notification_runs()` | [`export_monitoring_runs`](https://docs.leakradar.io/#tag/notifications-runs/export_monitoring_runs) |

### Profile

| Method | Operation |
|---|---|
| `get_profile()` | [`get_profile`](https://docs.leakradar.io/#tag/profile/get_profile) |
| `update_profile()` | [`update_profile`](https://docs.leakradar.io/#tag/profile/update_profile) |
| `get_preferences()` | [`get_preferences`](https://docs.leakradar.io/#tag/profile/get_preferences) |
| `update_preferences()` | [`update_preferences`](https://docs.leakradar.io/#tag/profile/update_preferences) |

### Usage and limits

| Method | Operation |
|---|---|
| `get_usage()` | [`get_usage`](https://docs.leakradar.io/#tag/usage-and-limits/get_usage) |
| `rate_limits()` | [`get_rate_limits`](https://docs.leakradar.io/#tag/usage-and-limits/get_rate_limits) |
| `list_balance_logs()` | [`list_balance_history`](https://docs.leakradar.io/#tag/usage-and-limits/list_balance_history) |
| `list_task_history()` | [`list_task_history`](https://docs.leakradar.io/#tag/usage-and-limits/list_task_history) |

### Team

| Method | Operation |
|---|---|
| `get_team()` | [`get_team`](https://docs.leakradar.io/#tag/team/get_team) |
| `create_team_invitation()` | [`invite_team_member`](https://docs.leakradar.io/#tag/team/invite_team_member) |
| `resend_team_invitation()` | [`resend_team_invitation`](https://docs.leakradar.io/#tag/team/resend_team_invitation) |
| `revoke_team_invitation()` | [`revoke_team_invitation`](https://docs.leakradar.io/#tag/team/revoke_team_invitation) |
| `update_team_member_role()` | [`update_team_member_role`](https://docs.leakradar.io/#tag/team/update_team_member_role) |
| `delete_team_member()` | [`remove_team_member`](https://docs.leakradar.io/#tag/team/remove_team_member) |
| `leave_team()` | [`leave_team`](https://docs.leakradar.io/#tag/team/leave_team) |

### Statistics

| Method | Operation |
|---|---|
| `get_stats()` | [`get_statistics`](https://docs.leakradar.io/#tag/stats/get_statistics) |
| `get_live_stats()` | [`get_live_statistics`](https://docs.leakradar.io/#tag/stats/get_live_statistics) |
| `get_transparency_stats()` | [`get_transparency_statistics`](https://docs.leakradar.io/#tag/stats/get_transparency_statistics) |

### Client

| Method | Operation |
|---|---|
| `aclose()`, sync: `close()` | None: closes the connections |
| `request()` | Any route of the API: see Calling other routes |
| `paginate()` | The listing method it is given: see Pagination |

## Development

```bash
git clone https://github.com/LeakRadar/leakradar-wrapper
cd leakradar-wrapper
pip install -r requirements.txt         # httpx, anyio, ujson, ruff and mypy
python -m unittest discover -s tests    # offline
ruff check
mypy leakradar
```

Generated files, never edited by hand:

- `leakradar/_sync_client.py`, the class `LeakRadar`, is generated from `LeakRadarClient` in
  `leakradar/client.py`: after changing it, run `python scripts/generate_sync.py`.
- `leakradar/types.py` comes from the OpenAPI spec of the API: `python scripts/generate_types.py`
  (the live spec by default, `--spec PATH_OR_URL` for another).
- `tests/openapi_ops.json` lists the operations of the spec: `python scripts/update_openapi_snapshot.py`
  refreshes it, then `tests/test_contract.py` names every operation or parameter the client does
  not cover yet.
- `tests/compat/fixtures` records the public surface and the HTTP requests of every published
  version since 0.1.1 (0.1.0, uploaded before the first commit, cannot be rebuilt): once a
  release is on PyPI, add it to `VERSIONS` in `scripts/build_compat_fixtures.py` and run the
  script.

`generate_sync.py` and `generate_types.py` take `--check`, which writes nothing and fails when the
file is out of date. `tests/test_readme.py` checks that the method reference above lists every
method with the operations of its docstring, and that the code of this README and of `examples/`
is valid Python 3.8.

Continuous integration (GitHub Actions):

- `ci.yml`, on every push and pull request: the tests on Python 3.8 to 3.14 with httpx 0.27 and
  with the latest httpx 0.x (and ujson and trio); ruff, mypy and `generate_sync.py --check`; a
  build of the package checked with `twine check --strict`, whose wheel must install and import
  with pre-releases allowed (`pip install --pre`).
- `drift.yml`, every day: regenerates the snapshot, the types and the sync client from the live
  spec, runs the tests, and fails when a file changed, that is when the SDK lags behind the API.
- `publish.yml`, the release: set the version in `leakradar/_version.py`, date its changelog
  entry (`## X.Y.Z (YYYY-MM-DD)` instead of `(unreleased)`), commit, then push a tag `vX.Y.Z`
  equal to that version. CI runs on the tagged commit, the tag is checked against the version and
  the dated changelog entry, then the package is built and published to PyPI with Trusted
  Publishing (no API token).

```bash
git tag v0.3.0
git push origin v0.3.0
```
