import inspect
import logging
import math
import os
import random
import ssl
import sys
import tempfile
import threading
import time
import warnings
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import (
    Any,
    AsyncIterator,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
    Type,
    TypeVar,
    Union,
    cast,
)
from urllib.parse import quote, urlsplit

import anyio
import httpx

from . import types as _types
from ._version import __version__

try:
    import ujson as _json
except Exception:
    import json as _json  # type: ignore[no-redef]


logger = logging.getLogger("leakradar")

StrOrList = Union[str, Sequence[str]]
IntOrList = Union[int, Sequence[int]]
DateLike = Union[str, date, datetime]
"""ISO 8601 string, or a date/datetime (sent as ISO 8601). Always give datetimes a timezone."""

# The client's own class, which `async with` hands back (a subclass keeps its type).
_ClientT = TypeVar("_ClientT")

# Why the API refused a request (the `error.reason` of a 429, or of the 503 "busy").
RATE_LIMIT_REASONS = ("rate", "concurrency", "daily_quota", "paused", "busy")


def _is_sdk_frame(frame: Any) -> bool:
    name = frame.f_globals.get("__name__") or ""
    return name == "leakradar" or name.startswith("leakradar.")


def _warn(message: str, category: Type[Warning]) -> None:
    """warnings.warn pointed at the caller's code: the first frame outside this package."""
    level = 2
    try:
        frame: Any = sys._getframe(1)
        while frame is not None and _is_sdk_frame(frame):
            frame = frame.f_back
            level += 1
    except Exception:  # sys._getframe is CPython (and PyPy) only
        level = 2
    warnings.warn(message, category, stacklevel=level)


def _domain(value: Any) -> str:
    """
    The domain to search for `value`: trimmed, lowercase, without a final dot.

    A URL (scheme, path, query, fragment or port) is reduced to its host, without a leading
    "www." when the host has 3 labels or more, with a UserWarning saying what is searched.
    Anything else (an "@", a "*") is left to the API.
    """
    entry = str(value).strip()
    domain = entry.lower()
    if any(mark in domain for mark in "/?#:"):
        try:
            host = urlsplit(domain if "://" in domain else "//" + domain).hostname or ""
        except ValueError:
            host = ""
        if host.startswith("www.") and host.count(".") >= 2:
            host = host[4:]
        domain = host
    domain = domain.rstrip(".")
    if not domain:
        raise ValueError("Pass a domain such as example.com")
    if domain != entry.lower().rstrip("."):
        _warn(f"'{entry}' is a URL: searching the domain '{domain}'.", UserWarning)
    return domain


def _verify_option(verify: Any) -> Any:
    """httpx's `verify`: a CA bundle path becomes an SSL context (httpx 0.28 deprecates paths)."""
    if verify is None:
        # The default, as in requests: httpx 0.27 would read None (any falsy value) as False.
        return True
    if isinstance(verify, (str, os.PathLike)):
        path = os.fspath(verify)
        if os.path.isdir(path):
            return ssl.create_default_context(capath=path)
        return ssl.create_default_context(cafile=path)
    if not isinstance(verify, (bool, ssl.SSLContext)):
        raise TypeError("verify must be True, False, the path of a CA bundle or an ssl.SSLContext")
    return verify


def _rebuild_error(cls: Type[BaseException], args: Tuple[Any, ...], state: Dict[str, Any]) -> BaseException:
    """Unpickle an error of this module (see LeakRadarError.__reduce__)."""
    error = cls.__new__(cls)
    error.args = args
    error.__dict__.update(state)
    return error


class LeakRadarError(Exception):
    """
    Base class of every error the client raises itself: API errors (LeakRadarAPIError and its
    subclasses), failed tasks (TaskFailedError), failed exports (ExportFailedError) and waits
    that ran out of time (WaitTimeoutError). Network failures stay httpx exceptions.

    Every error survives pickle with its attributes (Celery, multiprocessing, ProcessPoolExecutor).
    """

    def __reduce__(self) -> Tuple[Any, ...]:
        # Rebuilt without calling __init__ (a subclass may change its signature), with every
        # attribute kept as it is: retry_at or reason are not read again from the headers.
        return (_rebuild_error, (self.__class__, self.args, dict(self.__dict__)))


class LeakRadarAPIError(LeakRadarError):
    """
    Base exception for API-related errors.

    - status_code: the HTTP status.
    - detail: the API's `detail` (usually a string, sometimes an object with a `code`).
    - code: the `code` of an object `detail` (such as "monitor_overlap"), else None.
    - error_code: the machine-readable code of any refusal: the `code` of an object `detail`,
      else the one of the `error` object ("rate_limited"), else the top-level `code` of the
      answer ("search_unavailable"); None when the answer has none.
    - message: `detail` as readable text: the `message` of an object `detail`, "field: problem"
      for each invalid parameter of a 422.
    - headers: the response headers (case-insensitive), empty when the error was built by hand.
    - error: the `error` object of the answer when there is one (rate limits), else None.
    - request_id: the `id` of a 500 answer (quote it when you contact support), else the
      X-Request-ID response header, else None.
    - conflicts: the `conflicts` of an object `detail`, such as the 409 `monitor_overlap`
      (the monitors already covering the value on that channel), else None.
    """

    # Top-level `code` of the answer (503 "search_unavailable", 422 "search_query_too_complex").
    _answer_code: Optional[str] = None

    def __init__(
        self,
        status_code: int,
        detail: Any,
        *,
        headers: Optional[Mapping[str, str]] = None,
        error: Optional[Mapping[str, Any]] = None,
        request_id: Optional[str] = None,
    ):
        self.status_code = status_code
        self.detail = detail
        self.headers = httpx.Headers(headers or {})
        self.error: Optional[Dict[str, Any]] = dict(error) if isinstance(error, Mapping) else None
        self.request_id: Optional[str] = request_id or self.headers.get("x-request-id") or None
        super().__init__(f"API Error {status_code}: {detail}")

    @property
    def code(self) -> Optional[str]:
        """Machine-readable error code, when the API answers with an object `detail`."""
        if isinstance(self.detail, dict):
            code = self.detail.get("code")
            if isinstance(code, str):
                return code
        return None

    @property
    def error_code(self) -> Optional[str]:
        """
        Machine-readable code of the refusal, whatever the shape of the answer: the `code` of
        an object `detail` ("monitor_overlap"), else the one of the `error` object
        ("rate_limited" on every rate limit refusal), else the top-level `code` of the answer
        ("search_unavailable" on a 503, "search_query_too_complex" on a 422).
        """
        code = self.code
        if code is not None:
            return code
        if self.error is not None:
            code = self.error.get("code")
            if isinstance(code, str):
                return code
        return self._answer_code

    @property
    def message(self) -> str:
        """`detail` as readable text."""
        if "_message" in self.__dict__:
            return self.__dict__["_message"]
        detail = self.detail
        if isinstance(detail, str):
            return detail
        if isinstance(detail, Mapping):
            for key in ("message", "detail"):
                text = detail.get(key)
                if isinstance(text, str) and text:
                    return text
        elif isinstance(detail, list):
            # 422: [{"loc": ["query", "page_size"], "msg": "...", "type": "..."}, ...]
            problems = []
            for item in detail:
                if not isinstance(item, Mapping) or item.get("msg") is None:
                    continue
                loc = item.get("loc")
                where = ".".join(str(part) for part in loc) if isinstance(loc, (list, tuple)) else loc
                problems.append(f"{where}: {item['msg']}" if where not in (None, "") else str(item["msg"]))
            if problems:
                return "; ".join(problems)
        return str(detail)

    @message.setter
    def message(self, value: str) -> None:
        # Subclasses written before 0.3.0 may set their own `message`.
        self.__dict__["_message"] = value

    @property
    def conflicts(self) -> Optional[Any]:
        """The `conflicts` of an object `detail` (409 `monitor_overlap`), else None."""
        if "_conflicts" in self.__dict__:
            return self.__dict__["_conflicts"]
        if isinstance(self.detail, Mapping):
            return self.detail.get("conflicts")
        return None

    @conflicts.setter
    def conflicts(self, value: Any) -> None:
        self.__dict__["_conflicts"] = value


def _label(value: Any) -> Optional[str]:
    """A short lowercase label (reason, scope, family), None for anything else."""
    if isinstance(value, str) and 0 < len(value) <= 32 and value.replace("_", "").isalnum() and value.islower():
        return value
    return None


def _wait_seconds(value: Any) -> Optional[float]:
    """A wait in seconds: a number, or a Retry-After value (seconds or an HTTP date)."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) and value >= 0 else None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        try:
            moment = parsedate_to_datetime(raw)
        except Exception:
            return None
        if moment is None:
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return max(0.0, (moment - datetime.now(timezone.utc)).total_seconds())
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def _instant(value: Any) -> Optional[datetime]:
    """An aware UTC datetime from ISO 8601 (the API's retry_at) or epoch seconds."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, timezone.utc)
        raw = str(value).strip()
        if raw.isdigit():
            return datetime.fromtimestamp(int(raw), timezone.utc)
        # Python 3.8-3.10 fromisoformat does not read the "Z" suffix.
        moment = datetime.fromisoformat(raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw)
    except (ValueError, OverflowError, OSError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _legacy_reason(status_code: int, detail: Any, headers: httpx.Headers) -> Optional[str]:
    """The reason of a 429 from an API older than the `error` object, read from its headers."""
    if status_code != 429:
        return None
    if headers.get("x-ratelimit-inflight-kind"):
        return "concurrency"
    if isinstance(detail, dict) and detail.get("code") == "raw_unlock_concurrency_limit":
        return "concurrency"
    if headers.get("x-ratelimit-daily-limit") and headers.get("x-ratelimit-daily-remaining") == "0":
        return "daily_quota"
    if headers.get("x-ratelimit-remaining") == "0":
        return "rate"
    return None


class _RefusalDetails:
    """What a rate limit refusal says, shared by TooManyRequestsError (429) and
    ServiceUnavailableError (503 "busy"). Every attribute is None when the
    answer does not carry it."""

    reason: Optional[str] = None
    retry_after: Optional[float] = None
    retry_at: Optional[datetime] = None
    scope: Optional[str] = None
    family: Optional[str] = None
    limit: Optional[Dict[str, Any]] = None
    daily: Optional[Dict[str, Any]] = None

    def _read_refusal(self, status_code: int, detail: Any, headers: httpx.Headers, error: Optional[Dict[str, Any]]) -> None:
        error = error or {}
        reason = _label(error.get("reason")) or _label(headers.get("x-lr-ratelimit-reason"))
        if reason not in RATE_LIMIT_REASONS:
            reason = _legacy_reason(status_code, detail, headers) if not error else None
        self.reason = reason
        self.scope = _label(error.get("scope")) or _label(headers.get("x-lr-ratelimit-scope"))
        self.family = _label(error.get("family"))
        retry_after = _wait_seconds(headers.get("retry-after"))
        if retry_after is None:
            retry_after = _wait_seconds(error.get("retry_after"))
        retry_at = _instant(error.get("retry_at")) or _instant(headers.get("x-lr-ratelimit-retry-at"))
        if retry_at is None and retry_after is not None:
            retry_at = datetime.now(timezone.utc) + timedelta(seconds=retry_after)
        if retry_after is None and retry_at is not None:
            retry_after = max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
        self.retry_after = retry_after
        self.retry_at = retry_at
        self.limit = dict(error["limit"]) if isinstance(error.get("limit"), Mapping) else None
        self.daily = dict(error["daily"]) if isinstance(error.get("daily"), Mapping) else None


class UnauthorizedError(LeakRadarAPIError):
    """Raised when the user is not authorized to access a resource."""


class ForbiddenError(LeakRadarAPIError):
    """Raised when the user does not have permission (forbidden)."""


class BadRequestError(LeakRadarAPIError):
    """Raised when the request is invalid."""


class TooManyRequestsError(_RefusalDetails, LeakRadarAPIError):
    """
    Raised when a limit refuses the request (429).

    - reason: "rate" (too many requests per second; per hour for the alert channel tests),
      "concurrency" (searches running at once), "daily_quota" (searches over 24 hours),
      "paused" (this credential kept calling after its refusals: its family is paused for 2
      minutes, then 10, then 60 if it happens again within 24 hours); None when unknown.
    - retry_after: seconds to wait before trying again (float).
    - retry_at: when to try again (aware UTC datetime).
    - scope: what the limit counts: "credential", "team", "ip", "global", "account" or "edge".
    - family: the family of the refused request: "search", "heavy", "unlock", "widget",
      "default", or "channel_test" (alert channel tests).
    - limit: {"per_second", "burst"} for reason "rate", plus "window_seconds" for the alert
      channel tests (3600: their budget is per hour); daily: {"limit", "used", "retry_at"} for
      reason "daily_quota".
    - headers: the response headers (Retry-After, X-RateLimit-*, X-LR-RateLimit-*).

    The client already retried "rate" and "concurrency" refusals (see `max_retries`) before
    raising, unless they announced a wait longer than `max_retry_wait` (an alert channel test
    refused for the hour); "daily_quota" and "paused" are never retried.
    """

    def __init__(
        self,
        status_code: int,
        detail: Any,
        *,
        headers: Optional[Mapping[str, str]] = None,
        error: Optional[Mapping[str, Any]] = None,
        request_id: Optional[str] = None,
    ):
        super().__init__(status_code, detail, headers=headers, error=error, request_id=request_id)
        self._read_refusal(status_code, detail, self.headers, self.error)


class NotFoundError(LeakRadarAPIError):
    """Raised when the requested resource is not found."""


class ValidationError(LeakRadarAPIError):
    """Raised when the request fails parameter validation."""


class ConflictError(LeakRadarAPIError):
    """Raised on conflict (e.g., duplicate resource)."""


class PaymentRequiredError(LeakRadarAPIError):
    """Raised when an action requires more quota (e.g., raw GB)."""


class GoneError(LeakRadarAPIError):
    """Raised when a resource is no longer available (e.g., expired raw download)."""


class ServiceUnavailableError(_RefusalDetails, LeakRadarAPIError):
    """
    Raised when a search family is under maintenance or the service is temporarily unavailable (503).

    `reason` is "busy" when the shared capacity for anonymous searches is exhausted (retried by
    the client like a 429 "rate"); `retry_after` / `retry_at` come from Retry-After when present.
    A 503 announcing a Retry-After (search temporarily unavailable) was already retried by the
    client (see `max_retries`) before raising, except on a synchronous unlock (a POST to
    .../unlock): the API may answer it once the records are unlocked and the points spent, so it
    is raised at once. Check your unlocked records before unlocking again. A maintenance 503
    carries no Retry-After and is raised at once.
    """

    def __init__(
        self,
        status_code: int,
        detail: Any,
        *,
        headers: Optional[Mapping[str, str]] = None,
        error: Optional[Mapping[str, Any]] = None,
        request_id: Optional[str] = None,
    ):
        super().__init__(status_code, detail, headers=headers, error=error, request_id=request_id)
        self._read_refusal(status_code, detail, self.headers, self.error)


class TaskFailedError(LeakRadarError):
    """
    Raised by wait_for_task() and wait_for_unlocked_list_task() when a task ends without completing.

    - task_id: the task ID.
    - status: "FAILED" or "CANCELED".
    - code: why it failed: "insufficient_points", "timeout", "blocked", "restart_required",
      "interrupted", "unlock_failed", "dataset_unavailable" or "internal_error"; None when the
      API does not say (a canceled task, an older answer).
    - message: what happened, readable as is.
    - task: the last status read (dict). Records unlocked before a failure stay unlocked.
    """

    def __init__(
        self,
        task_id: str,
        status: str,
        code: Optional[str] = None,
        message: Optional[str] = None,
        task: Optional[Mapping[str, Any]] = None,
    ):
        self.task_id = task_id
        self.status = status
        self.code = code
        if not message:
            message = f"Task {task_id} was canceled." if status == "CANCELED" else f"Task {task_id} did not complete."
        self.message = message
        self.task: Dict[str, Any] = dict(task) if isinstance(task, Mapping) else {}
        super().__init__(f"Task {task_id} {status}" + (f" ({code})" if code else "") + f": {message}")


class ExportFailedError(LeakRadarError):
    """
    Raised by wait_for_export() when an export ends with status "ERROR". Queue it again.

    - export_id: the export ID.
    - status: "ERROR".
    - export: the last state read (dict, as listed by list_exports()).
    """

    def __init__(self, export_id: int, status: str, export: Optional[Mapping[str, Any]] = None):
        self.export_id = export_id
        self.status = status
        self.export: Dict[str, Any] = dict(export) if isinstance(export, Mapping) else {}
        super().__init__(f"Export {export_id} ended with status {status}.")


class WaitTimeoutError(LeakRadarError, TimeoutError):
    """
    Raised by the wait_for_* methods when `timeout` runs out first. The task or the export
    keeps running on the server: wait for it again later. Also a TimeoutError.

    - waited: seconds spent waiting.
    - last: the last state read (dict), None when nothing was read.
    """

    def __init__(self, message: str, waited: float = 0.0, last: Optional[Mapping[str, Any]] = None):
        self.waited = waited
        self.last: Optional[Dict[str, Any]] = dict(last) if isinstance(last, Mapping) else None
        super().__init__(message)


_ERRORS_BY_STATUS = {
    400: BadRequestError,
    401: UnauthorizedError,
    402: PaymentRequiredError,
    403: ForbiddenError,
    404: NotFoundError,
    409: ConflictError,
    410: GoneError,
    422: ValidationError,
    429: TooManyRequestsError,
    503: ServiceUnavailableError,
}


def _is_binary_content_type(ct: str) -> bool:
    """Decide whether content-type should be returned as raw bytes."""
    if not ct:
        return False
    c = ct.split(";", 1)[0].lower().strip()
    return c in {
        "text/csv",
        "text/plain",
        "application/pdf",
        "application/octet-stream",
        "application/zip",
        "application/x-zip-compressed",
    }


class LeakRadarClient:
    """
    Asynchronous client for the LeakRadar.io API.

    Features:
    - Auth via Bearer Token
    - Custom User-Agent
    - Robust error handling
    - Optional ujson encoding/decoding for JSON
    - Binary-safe downloads (CSV/TXT/PDF/ZIP)
    - date/datetime arguments sent as ISO 8601
    - Rate limit refusals ("rate", "concurrency", 503 "busy") retried after Retry-After,
      behind one gate shared by every task using the client (`max_retries`, 0 = off)
    - Transient failures retried: connection errors (any request), read errors and 502/504
      answers (GET only) after a short backoff, a 503 after its Retry-After (never after a
      synchronous unlock, which may already have spent points)
    - wait_for_task(), wait_for_export(), download_export() and paginate() helpers
    - A URL passed where a domain is expected is reduced to its domain, with a warning
    - Runs on asyncio and on trio: it waits with anyio, which httpx installs

    AsyncLeakRadar is the same client, reading its key and address from the environment.
    """

    BASE_URL = "https://api.leakradar.io"

    # Whether a missing token / base_url is read from LEAKRADAR_API_KEY / LEAKRADAR_BASE_URL.
    # Off here so that LeakRadarClient keeps its 0.x defaults; on in AsyncLeakRadar.
    _READ_ENV = False

    # Refusals worth waiting for: short by nature. "daily_quota" and "paused" last
    # minutes to hours and every refused attempt counts towards a pause.
    _RETRY_REASONS = {429: ("rate", "concurrency"), 503: ("busy",)}
    # Wait used when a retryable refusal names none (Retry-After is always sent by the API).
    _DEFAULT_RETRY_WAIT = {"rate": 1.0, "concurrency": 2.0, "busy": 5.0}

    # Transient network failures. A connection that could not be opened sent nothing, so any
    # request is replayed; a failure after sending, or a 502/504, only on a GET or a HEAD.
    _CONNECT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout)
    _READ_ERRORS = (httpx.ReadTimeout, httpx.ReadError, httpx.RemoteProtocolError)
    _GATEWAY_STATUSES = (502, 504)
    _IDEMPOTENT_METHODS = ("GET", "HEAD")
    # Routes that never wait for the gate: never rate limited (rate_limits() reads the limits).
    _UNGATED_PATHS = frozenset({"/rate-limits"})

    # Listings the API serves page by page only while page * page_size <= 1,000,000 (a 400
    # beyond), with the export that returns the whole result set.
    _DEEP_PAGING_LIMIT = 1_000_000
    _DEEP_PAGING_EXPORTS = {
        "search_advanced": "export_advanced()",
        "get_application_leaks": "export_application_leaks()",
        "get_domain_all": 'export_domain_leaks(domain, "all")',
        "get_domain_customers": 'export_domain_leaks(domain, "customers")',
        "get_domain_employees": 'export_domain_leaks(domain, "employees")',
        "get_domain_third_parties": 'export_domain_leaks(domain, "third_parties")',
        "search_combolist_email": "export_combolist()",
        "search_combolist_username": "export_combolist()",
        "search_combolist_domain": "export_combolist()",
        "search_combolist_advanced": "export_combolist()",
    }
    # Listings served page by page within a smaller window (a 400 beyond), and how to get the rest.
    _PAGING_WINDOWS = {
        "search_dark_web": (10_000, "Run several searches with narrower filters (date_from / date_to, sources)"),
    }
    # Listings whose pages can come back short, or even empty, before the end: the results of a
    # monitoring run skip the records removed from the index since the run, while `total`
    # still counts them. paginate() reads them up to `total`.
    _SPARSE_LISTINGS = frozenset({"get_notification_run_items", "notification_run_leaks"})

    _LEAK_FILTER_ARRAY_FIELDS = frozenset({
        "username",
        "username_not",
        "password",
        "password_not",
        "url",
        "url_not",
        "url_domain",
        "url_domain_not",
        "url_host",
        "url_host_not",
        "username_hash",
        "password_hash",
        "url_scheme",
        "url_scheme_not",
        "url_port",
        "url_port_not",
        "url_tld",
        "url_tld_not",
        "email_domain",
        "email_domain_not",
        "email_host",
        "email_host_not",
        "email_tld",
        "email_tld_not",
    })

    # Body fields of LeakSearchFilters (stealer-log advanced search).
    _LEAK_FILTER_FIELDS = (
        "username",
        "username_not",
        "username_match_type",
        "username_not_match_type",
        "password",
        "password_not",
        "password_match_type",
        "password_not_match_type",
        "url",
        "url_not",
        "url_match_type",
        "url_not_match_type",
        "url_domain",
        "url_domain_not",
        "url_domain_match_type",
        "url_domain_not_match_type",
        "url_host",
        "url_host_not",
        "url_host_match_type",
        "url_host_not_match_type",
        "username_hash",
        "password_hash",
        "url_scheme",
        "url_scheme_not",
        "url_port",
        "url_port_not",
        "url_tld",
        "url_tld_not",
        "is_email",
        "email_domain",
        "email_domain_not",
        "email_domain_match_type",
        "email_domain_not_match_type",
        "email_host",
        "email_host_not",
        "email_host_match_type",
        "email_host_not_match_type",
        "email_tld",
        "email_tld_not",
        "password_strength",
        "added_from",
        "added_to",
        "force_and",
    )

    # Body fields of CombolistAdvancedSearchRequest: the part of the stealer-log
    # filters that exists in the combolist mapping (combolist records carry no URL).
    _COMBOLIST_FILTER_FIELDS = (
        "username",
        "username_not",
        "username_match_type",
        "username_not_match_type",
        "password",
        "password_not",
        "password_match_type",
        "password_not_match_type",
        "email_domain",
        "email_domain_not",
        "email_domain_match_type",
        "email_domain_not_match_type",
        "username_hash",
        "password_hash",
        "is_email",
        "password_strength",
        "added_from",
        "added_to",
        "force_and",
    )

    # Body fields shared by /search/raw, /search/raw/count, /search/raw/export and
    # /search/raw/unlock/task. The deprecated file_name_exact / folder_name_exact
    # flags are left out on purpose: the *_match_type fields replace them.
    _RAW_FILTER_FIELDS = (
        "q",
        "q_exact",
        "container_id",
        "exts",
        "exts_not",
        "categories",
        "categories_not",
        "file_name",
        "file_name_not",
        "file_name_match_type",
        "file_name_not_match_type",
        "folder_name",
        "folder_name_not",
        "folder_name_match_type",
        "folder_name_not_match_type",
        "force_and",
        "include_terms",
        "exclude_terms",
        "ingested_at_min",
        "ingested_at_max",
    )

    _RAW_FILTER_ARRAY_FIELDS = frozenset({
        "exts",
        "exts_not",
        "categories",
        "categories_not",
        "file_name",
        "file_name_not",
        "folder_name",
        "folder_name_not",
        "include_terms",
        "exclude_terms",
    })

    # What the API accepts as a raw selection without `q`. folder_name_not and
    # exclude_terms only narrow a selection, they never make one on their own.
    _RAW_SELECTOR_FIELDS = (
        "container_id",
        "exts",
        "exts_not",
        "categories",
        "categories_not",
        "file_name",
        "file_name_not",
        "folder_name",
        "include_terms",
        "ingested_at_min",
        "ingested_at_max",
    )

    _COMBOLIST_SCOPES = ("email", "username", "domain", "advanced")

    # notification_type values of test_notification_method_with_data().
    _TEST_NOTIFICATION_TYPES = ("leak", "raw", "dark_web", "all")

    def __init__(
        self,
        token: Optional[str] = None,
        user_agent: str = f"LeakRadar-Python-Client/{__version__}",
        timeout: float = 30.0,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        max_retries: int = 3,
        max_retry_wait: float = 60.0,
        *,
        base_url: Optional[str] = None,
        verify: Optional[Union[bool, str, ssl.SSLContext]] = True,
        proxy: Optional[Union[str, httpx.URL, httpx.Proxy]] = None,
    ):
        """
        Initialize the client.

        :param token: Bearer token for authenticated endpoints. AsyncLeakRadar reads
            LEAKRADAR_API_KEY when it is None; token="" forces anonymous calls.
        :param user_agent: Custom User-Agent to identify usage.
        :param timeout: Request timeout in seconds.
        :param transport: Optional httpx transport for API calls, e.g.
            httpx.AsyncHTTPTransport(local_address="0.0.0.0"), or httpx.MockTransport(handler)
            in tests. Failed connections are already retried (see max_retries).
        :param max_retries: Retries of a request refused with a 429 "rate" or "concurrency"
            or a 503 "busy", each after the announced Retry-After plus a small jitter. While
            such a wait runs, every other request of this client waits too. The same budget
            covers a 503 announcing a Retry-After (search temporarily unavailable), except
            after a synchronous unlock (a POST to .../unlock), which may
            already have spent points: that 503 is raised at once. It also covers connection
            failures (any request), and read failures and 502/504 answers (GET only), retried
            after 0.5 s, 1 s, 2 s... 0 turns every retry and the shared wait off (the 0.2
            behaviour). "daily_quota" and "paused" are never retried.
        :param max_retry_wait: Longest wait (seconds) worth a retry; a refusal announcing
            more is raised at once.
        :param base_url: API address, for a staging server or a gateway. Default: BASE_URL
            (AsyncLeakRadar reads LEAKRADAR_BASE_URL first).
        :param verify: TLS verification: True (default; None means the default too), False,
            the path of a CA bundle (or a directory of CA certificates), or an ssl.SSLContext.
            For networks that inspect TLS. Any other value raises TypeError.
        :param proxy: Proxy URL, such as "http://proxy.example.com:3128".
            verify and proxy configure the default transport: with `transport`, they are not
            used, configure TLS and the proxy on that transport. They also apply to the raw file
            downloads (download_raw_file()).
        """
        if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 0:
            raise ValueError("max_retries must be an integer >= 0")
        if isinstance(max_retry_wait, bool) or not isinstance(max_retry_wait, (int, float)) or not max_retry_wait >= 0:
            raise ValueError("max_retry_wait must be a number of seconds >= 0")
        if token is None and self._READ_ENV:
            token = os.environ.get("LEAKRADAR_API_KEY", "").strip() or None
        if base_url is None and self._READ_ENV:
            base_url = os.environ.get("LEAKRADAR_BASE_URL", "").strip() or None
        self.token = token
        self.user_agent = user_agent
        self.base_url = str(base_url or self.BASE_URL).rstrip("/")
        self._timeout = timeout
        self.max_retries = max_retries
        self.max_retry_wait = float(max_retry_wait)
        # Same TLS and proxy settings for the anonymous client of the raw file downloads.
        self._verify = _verify_option(verify)
        self._proxy = proxy

        # The gate: monotonic time before which no request of this client leaves.
        # Closed by a retryable refusal, shared by every task (and thread) using it.
        self._gate_until = 0.0
        self._gate_lock = threading.Lock()
        # (method, path) whose Deprecation header was already reported.
        self._deprecations_seen: Set[Tuple[str, str]] = set()
        # Seams for the tests.
        self._monotonic = time.monotonic
        self._sleep = anyio.sleep
        self._random = random.random

        # With a transport, httpx would route every request through a proxy mount instead of it.
        network: Dict[str, Any] = {} if transport is not None else {"verify": self._verify, "proxy": proxy}
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=self._base_headers(),
            timeout=timeout,
            transport=transport,
            **network,
        )
        # Unauthenticated client for presigned storage URLs, created on first raw file download.
        self._anon_client: Optional[httpx.AsyncClient] = None

    def _base_headers(self) -> Dict[str, str]:
        return {
            "User-Agent": self.user_agent,
            "Accept": "*/*",
        }

    def _default_headers(self) -> Dict[str, str]:
        headers = self._base_headers()
        # A key read from a file or a mounted secret often ends with a newline: httpx would refuse
        # the header and repeat the whole key in its error. self.token stays as it was given.
        token = self.token.strip() if isinstance(self.token, str) else self.token
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    async def __aenter__(self: _ClientT) -> _ClientT:
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying HTTP clients."""
        await self._client.aclose()
        if self._anon_client is not None:
            await self._anon_client.aclose()

    @classmethod
    def _jsonable(cls, value: Any) -> Any:
        """Turn date/datetime values (at any depth) into ISO 8601 strings."""
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        if isinstance(value, Mapping):
            return {k: cls._jsonable(v) for k, v in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [cls._jsonable(v) for v in value]
        return value

    @classmethod
    def _clean(cls, params: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
        if params is None:
            return None
        return {k: cls._jsonable(v) for k, v in params.items() if v is not None}

    @staticmethod
    def _pick(values: Mapping[str, Any], fields: Iterable[str]) -> Dict[str, Any]:
        """Keep the listed fields of `values` (a method's locals()) that are set."""
        return {k: values[k] for k in fields if values.get(k) is not None}

    @staticmethod
    def _seg(value: Any) -> str:
        """Percent-encode one path segment, so a domain or an ID can never change the route."""
        segment = quote(str(value), safe="")
        # quote() keeps "." and "..", which httpx resolves as dot segments: another route.
        if segment in (".", ".."):
            raise ValueError(f"{segment!r} is not an ID")
        return segment

    @staticmethod
    def _as_list(value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, list):
            return value
        if isinstance(value, (tuple, set)):
            return list(value)
        return [value]

    @staticmethod
    def _dataset_list(datasets: StrOrList) -> List[str]:
        """`datasets` as the JSON array of the mass checks, from a list or a comma-separated string."""
        if isinstance(datasets, str):
            return [part.strip() for part in datasets.split(",") if part.strip()]
        return list(datasets)

    @classmethod
    def _normalize_leak_filters(cls, filters: Dict[str, Any]) -> Dict[str, Any]:
        """
        Normalize LeakSearchFilters to match the OpenAPI schema:
        array-like fields must be arrays, not scalars.
        """
        for key in cls._LEAK_FILTER_ARRAY_FIELDS:
            if key in filters and filters[key] is not None:
                filters[key] = cls._as_list(filters[key])
        return filters

    @classmethod
    def _raw_filters(cls, values: Mapping[str, Any]) -> Dict[str, Any]:
        """Build a raw search body from a method's locals() and check that it selects something."""
        payload = cls._pick(values, cls._RAW_FILTER_FIELDS)
        for key in cls._RAW_FILTER_ARRAY_FIELDS:
            if key in payload:
                payload[key] = cls._as_list(payload[key])
        # Same truthiness test as 0.1.x: the API answers the finer cases (quotes-only q...) itself.
        if not payload.get("q") and not any(payload.get(k) for k in cls._RAW_SELECTOR_FIELDS):
            raise ValueError(
                "Provide q or at least one of: " + ", ".join(cls._RAW_SELECTOR_FIELDS) + "."
            )
        return payload

    @classmethod
    def _combolist_scope_body(
        cls,
        scope: str,
        value: Optional[str],
        search: Optional[str],
        filters: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Body of the combolist mass unlock and export jobs (same rules as the API)."""
        if scope not in cls._COMBOLIST_SCOPES:
            raise ValueError("scope must be one of: " + ", ".join(cls._COMBOLIST_SCOPES))
        if scope == "advanced":
            if filters is None:
                raise ValueError('scope "advanced" requires filters')
            if value is not None or search is not None:
                raise ValueError('scope "advanced" takes filters, not value or search')
            return {"scope": scope, "filters": cls._normalize_leak_filters(cls._clean(filters) or {})}
        if filters is not None:
            raise ValueError('filters is reserved to scope "advanced"')
        if not value:
            raise ValueError(f'scope "{scope}" requires value')
        if scope == "domain":
            value = _domain(value)
        return cls._clean({"scope": scope, "value": value, "search": search}) or {}

    @staticmethod
    def _encode_json(payload: Any) -> bytes:
        return _json.dumps(payload).encode("utf-8")

    async def _request(
        self,
        method: str,
        endpoint: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Any] = None,
        headers: Optional[Dict[str, str]] = None,
        follow_redirects: bool = False,
    ) -> Any:
        """
        Low-level request wrapper with error handling and smart content handling.

        - Returns dict/list for JSON responses (decoded via ujson if available).
        - Returns raw bytes for CSV/TXT/PDF/ZIP/octet-stream.
        - Rate limit refusals and transient failures are retried by `_send` (see `max_retries`).
        """
        req_headers = self._default_headers()
        if headers:
            req_headers.update(headers)

        content: Optional[bytes] = None
        if json is not None:
            content = self._encode_json(self._jsonable(json))
            req_headers.setdefault("Content-Type", "application/json")

        response = await self._send(
            method,
            endpoint,
            params=self._clean(params),
            content=content,
            headers=req_headers,
            follow_redirects=follow_redirects,
        )

        content_type = response.headers.get("content-type", "")
        if _is_binary_content_type(content_type):
            return response.content

        text = response.text
        if not text:
            return ""

        try:
            return _json.loads(text)
        except Exception:
            return text

    async def _send(self, method: str, endpoint: str, **kwargs: Any) -> httpx.Response:
        """
        Send one API request and return its response. An error answer that is not retried goes
        to `_handle_error`, which raises the matching error (an override of `_handle_error` that
        returns gets the answer returned, as before 0.3).

        A refusal worth waiting for (`_retry_wait`) closes the client's gate for the announced
        wait, then the request is sent again, at most `max_retries` times. Every request but
        those of `_UNGATED_PATHS` first waits for the gate, so tasks sharing the client stop
        together instead of each collecting its own 429s. A rate limit refusal (429, 503 "busy")
        comes before the request runs: replaying it, POST included, is safe. A 503 that only
        says when to come back is replayed too, except on a synchronous unlock (a POST to
        .../unlock): the API may answer it after spending the points, so it is not replayed.

        Transient failures use the same budget, with a backoff of 0.5 s, 1 s, 2 s... that does
        not close the gate: a connection that could not be opened (any request), a read
        failure or a 502/504 answer (GET and HEAD only, a POST may have run). Once the budget
        is spent, the httpx exception is raised as is, and an error answer goes to
        `_handle_error`.
        """
        verb = method.upper()
        # Logged without the query string: it can hold emails and other personal data.
        path = str(endpoint).split("?", 1)[0]
        gated = path not in self._UNGATED_PATHS
        retries = 0
        while True:
            if gated:
                await self._wait_for_gate()
            started = self._monotonic()
            try:
                response = await self._client.request(method, endpoint, **kwargs)
            except httpx.TransportError as exc:
                transient = isinstance(exc, self._CONNECT_ERRORS) or (
                    isinstance(exc, self._READ_ERRORS) and verb in self._IDEMPOTENT_METHODS
                )
                if not transient or retries >= self.max_retries:
                    logger.debug("LeakRadar %s %s failed: %s", verb, path, type(exc).__name__)
                    raise
                retries += 1
                await self._back_off(retries, verb, path, type(exc).__name__)
                continue
            logger.debug(
                "LeakRadar %s %s -> %d in %.0f ms",
                verb, path, response.status_code, (self._monotonic() - started) * 1000,
            )
            if not response.is_error:
                self._report_deprecation(verb, path, response)
                return response
            error = self._error_from(response)
            wait = self._retry_wait(error)
            if (
                wait is not None
                and error.status_code == 503
                and getattr(error, "reason", None) is None
                and verb not in self._IDEMPOTENT_METHODS
                and path.endswith("/unlock")
            ):
                # A synchronous unlock spends the points, then reads the search index again: a
                # 503 there can come after the unlock was done, and a replay would unlock (and
                # charge) the next records. The /unlock/task routes queue a task and are replayed.
                wait = None
            if wait is not None:
                self._close_gate(wait)
                if retries >= self.max_retries:
                    await self._handle_error(response)
                    return response
                retries += 1
                logger.debug(
                    "LeakRadar API %s (%s) on %s %s, retry %d/%d in %.1fs",
                    response.status_code, getattr(error, "reason", None), verb, path, retries, self.max_retries, wait,
                )
                if not gated:
                    await self._sleep(wait + self._jitter(wait))
                continue
            if (
                response.status_code in self._GATEWAY_STATUSES
                and verb in self._IDEMPOTENT_METHODS
                and retries < self.max_retries
            ):
                retries += 1
                await self._back_off(retries, verb, path, str(response.status_code))
                continue
            # Every release since 0.1 hands the final error answer to this hook: a subclass may
            # raise its own exceptions, or return to get the answer back.
            await self._handle_error(response)
            return response

    def _retry_wait(self, error: LeakRadarAPIError) -> Optional[float]:
        """Seconds to wait before a retry of this refusal, None when it is not retried."""
        if self.max_retries <= 0:
            return None
        reason = getattr(error, "reason", None)
        wait = getattr(error, "retry_after", None)
        if reason in self._RETRY_REASONS.get(error.status_code, ()):
            if wait is None:
                wait = self._DEFAULT_RETRY_WAIT[reason]
        elif not (error.status_code == 503 and reason is None and wait is not None):
            # Only a 503 that says when to come back (search temporarily unavailable; a
            # maintenance 503 does not) is worth a retry beyond the rate limit refusals. It
            # usually comes before anything ran, but not always: _send does not replay it after
            # a synchronous unlock, which may have spent the points first.
            return None
        if wait > self.max_retry_wait:
            return None
        return float(wait)

    async def _back_off(self, attempt: int, method: str, path: str, cause: str) -> None:
        """Sleep before retry number `attempt` (1, 2...) of a transient failure: 0.5 s, 1 s, 2 s... plus jitter."""
        delay = min(self.max_retry_wait, 0.5 * 2 ** (attempt - 1))
        logger.debug(
            "LeakRadar %s %s failed (%s), retry %d/%d in %.1fs", method, path, cause, attempt, self.max_retries, delay
        )
        await self._sleep(delay + self._jitter(delay))

    def _report_deprecation(self, method: str, path: str, response: httpx.Response) -> None:
        """Warn once per route and client when the API marks it deprecated (Deprecation header)."""
        if not response.headers.get("deprecation"):
            return
        key = (method, path)
        with self._gate_lock:
            if key in self._deprecations_seen:
                return
            self._deprecations_seen.add(key)
        message = f"The LeakRadar API deprecates {method} {path}"
        sunset = response.headers.get("sunset")
        if sunset:
            message += f", removal planned on {sunset}"
        link = response.headers.get("link")
        if link:
            message += f" (see {link})"
        message += "."
        logger.warning(message)
        _warn(message, DeprecationWarning)

    def _jitter(self, wait: float) -> float:
        """A small random extra wait, so the requests released by the gate do not all leave at once."""
        return self._random() * min(1.0, 0.1 + 0.1 * wait)

    def _close_gate(self, wait: float) -> None:
        until = self._monotonic() + wait
        with self._gate_lock:
            if until > self._gate_until:
                self._gate_until = until

    async def _wait_for_gate(self) -> None:
        while True:
            wait = self._gate_until - self._monotonic()
            if wait <= 0:
                return
            await self._sleep(wait + self._jitter(wait))

    def _error_from(self, response: httpx.Response) -> LeakRadarAPIError:
        """The exception matching the status code. `detail` may be a string or an object."""
        text = response.text
        try:
            body = _json.loads(text) if text else None
        except Exception:
            body = None
        detail: Any = text
        error: Optional[Dict[str, Any]] = None
        request_id: Optional[str] = None
        if isinstance(body, dict):
            if body.get("detail") not in (None, ""):
                detail = body["detail"]
            if isinstance(body.get("error"), dict):
                error = body["error"]
            # An unhandled server error carries the id support asks for.
            if response.status_code >= 500 and isinstance(body.get("id"), str) and body["id"]:
                request_id = body["id"]

        error_class = _ERRORS_BY_STATUS.get(response.status_code, LeakRadarAPIError)
        exc = error_class(response.status_code, detail, headers=response.headers, error=error, request_id=request_id)
        if isinstance(body, dict) and isinstance(body.get("code"), str) and body["code"]:
            exc._answer_code = body["code"]
        return exc

    async def _handle_error(self, response: httpx.Response):
        """
        Raise the exception matching the status code. `_send` calls it with every API error answer
        it does not retry (the last one once the retries are spent); download_export() and
        download_raw_file() raise the errors of their file download themselves.
        """
        raise self._error_from(response)

    def _same_origin(self, url: httpx.URL) -> bool:
        """Whether an absolute URL points at the API (same scheme, host and port as base_url)."""
        base = self._client.base_url
        default_ports = {"http": 80, "https": 443}
        return (url.scheme, url.host, url.port or default_ports.get(url.scheme)) == (
            base.scheme,
            base.host,
            base.port or default_ports.get(base.scheme),
        )

    # -------------------------
    # Generic access
    # -------------------------

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Any] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Any:
        """
        Call an API route this client has no method for yet, through the same pipeline as the
        methods: authentication, retries, errors (LeakRadarAPIError and its subclasses) and
        decoding (dict or list for JSON, bytes for files).

        :param method: HTTP method, such as "GET" or "POST".
        :param path: Route of the API, such as "/profile/usage".
        :param params: Query parameters (None values are left out, dates sent as ISO 8601).
        :param json: JSON body.
        :param headers: Extra request headers.
        """
        url = httpx.URL(path)
        if url.is_absolute_url and not self._same_origin(url):
            raise ValueError(f"request() only calls the LeakRadar API ({self.base_url}): pass a path such as /profile")
        return await self._request(method.upper(), path, params=params, json=json, headers=headers)

    # -------------------------
    # Core / misc
    # -------------------------

    async def get_profile(self) -> _types.UserBase:
        """
        Retrieve the authenticated user's profile.

        API: get_profile, https://docs.leakradar.io/#tag/profile/get_profile
        """
        return await self._request("GET", "/profile")

    async def update_profile(self, **data: Any) -> _types.UserBase:
        """
        Update whitelisted fields on the authenticated user's profile: first_name, last_name,
        organization, address, address_line2, phone, zip_code, region, city, country,
        tva_number, other_tax_id, and po_number (a purchase order number printed on your
        invoices, quotes and pro forma; not verified).

        API: update_profile, https://docs.leakradar.io/#tag/profile/update_profile
        """
        return await self._request("PATCH", "/profile", json=data)

    async def get_stats(self) -> Union[_types.StatsResponse, _types.StatsUnavailableResponse]:
        """
        Get cached platform statistics snapshot.

        API: get_statistics, https://docs.leakradar.io/#tag/stats/get_statistics
        """
        return await self._request("GET", "/stats")

    async def get_live_stats(self) -> Union[_types.LiveStatsResponse, _types.StatsUnavailableResponse]:
        """
        Live landing figures (raw lines, ingestion rate, active sources, dark web posts).
        Public, refreshed every ~2 min. Answers {"message": ...} while the cache is empty.

        API: get_live_statistics, https://docs.leakradar.io/#tag/stats/get_live_statistics
        """
        return await self._request("GET", "/live-stats")

    async def get_transparency_stats(self) -> Union[_types.TransparencyStatsResponse, _types.StatsUnavailableResponse]:
        """
        Transparency report figures (exclusion rules, suspended accounts, removal and legal requests).
        Public, refreshed every 6 hours. Answers {"message": ...} while the cache is empty.

        API: get_transparency_statistics, https://docs.leakradar.io/#tag/stats/get_transparency_statistics
        """
        return await self._request("GET", "/transparency-stats")

    async def get_task_status(self, task_id: str) -> _types.TaskStatusOut:
        """
        Get background task status by /tasks/{task_id} (unlock tasks of every search family).
        wait_for_task() polls it until the task finishes.

        API: get_task_status, https://docs.leakradar.io/#tag/tasks/get_task_status
        """
        return await self._request("GET", f"/tasks/{self._seg(task_id)}")

    async def password_range(
        self,
        prefix: str,
        limit: int = 1000,
        suffix_only: bool = False,
        datasets: Optional[StrOrList] = None,
    ) -> _types.PasswordRangeResponse:
        """
        Query a SHA-1 password hash range.

        :param prefix: SHA-1 prefix (or full hash) to query.
        :param limit: Max matches to return (server-capped).
        :param suffix_only: Return only suffixes (after first 5 chars).
        :param datasets: "stealer_logs" and/or "combolist", as a list or a comma-separated
            string. The server searches Stealer Logs only when omitted. With both, each
            hash's `count` is the sum and `sources` gives the split.

        API: search_password_range, https://docs.leakradar.io/#tag/password-range/search_password_range
        """
        if datasets is not None and not isinstance(datasets, str):
            datasets = ",".join(datasets)
        params = self._clean(
            {"prefix": prefix, "limit": limit, "suffix_only": suffix_only, "datasets": datasets}
        )
        return await self._request("GET", "/password-range", params=params)

    # -------------------------
    # Search (Advanced) - POST (OpenAPI v1.0.0)
    # -------------------------

    async def search_advanced(
        self,
        page: int = 1,
        page_size: int = 100,
        username: Optional[StrOrList] = None,
        username_not: Optional[StrOrList] = None,
        password: Optional[StrOrList] = None,
        password_not: Optional[StrOrList] = None,
        url: Optional[StrOrList] = None,
        url_not: Optional[StrOrList] = None,
        url_domain: Optional[StrOrList] = None,
        url_domain_not: Optional[StrOrList] = None,
        url_host: Optional[StrOrList] = None,
        url_host_not: Optional[StrOrList] = None,
        username_hash: Optional[StrOrList] = None,
        password_hash: Optional[StrOrList] = None,
        url_scheme: Optional[StrOrList] = None,
        url_scheme_not: Optional[StrOrList] = None,
        url_port: Optional[IntOrList] = None,
        url_port_not: Optional[IntOrList] = None,
        url_tld: Optional[StrOrList] = None,
        url_tld_not: Optional[StrOrList] = None,
        is_email: Optional[bool] = None,
        email_domain: Optional[StrOrList] = None,
        email_domain_not: Optional[StrOrList] = None,
        email_host: Optional[StrOrList] = None,
        email_host_not: Optional[StrOrList] = None,
        email_tld: Optional[StrOrList] = None,
        email_tld_not: Optional[StrOrList] = None,
        password_strength: Optional[str] = None,
        added_from: Optional[DateLike] = None,
        added_to: Optional[DateLike] = None,
        force_and: Optional[bool] = None,
        *,
        auto_unlock: Optional[bool] = None,
        username_match_type: Optional[str] = None,
        username_not_match_type: Optional[str] = None,
        password_match_type: Optional[str] = None,
        password_not_match_type: Optional[str] = None,
        url_match_type: Optional[str] = None,
        url_not_match_type: Optional[str] = None,
        url_domain_match_type: Optional[str] = None,
        url_domain_not_match_type: Optional[str] = None,
        url_host_match_type: Optional[str] = None,
        url_host_not_match_type: Optional[str] = None,
        email_domain_match_type: Optional[str] = None,
        email_domain_not_match_type: Optional[str] = None,
        email_host_match_type: Optional[str] = None,
        email_host_not_match_type: Optional[str] = None,
    ) -> _types.PaginatedLeaksResponse:
        """
        Search stealer logs with advanced filters (LeakSearchFilters, sent as the JSON body).

        - Values inside one field are OR'ed; force_and=True requires all of them.
        - `*_match_type`: "contains" (default when omitted), "starts_with" or "ends_with".
        - password_strength: "too_weak", "weak", "medium" or "strong".
        - Scalars are wrapped into lists where the API expects arrays.
        - auto_unlock=True unlocks the locked items of the returned page with your points.
        - Paging stops at page * page_size = 1,000,000: use export_advanced() for more.

        API: search_advanced, https://docs.leakradar.io/#tag/search-advanced/search_advanced
        """
        filters = self._normalize_leak_filters(self._pick(locals(), self._LEAK_FILTER_FIELDS))
        params = self._clean({"page": page, "page_size": page_size, "auto_unlock": auto_unlock})
        return await self._request("POST", "/search/advanced", params=params, json=filters)

    async def unlock_all_advanced(
        self,
        filters: Dict[str, Any],
        max_leaks: Optional[int] = None,
        list_id: Optional[int] = None,
    ) -> List[_types.LeakDetails]:
        """
        Unlock leaks returned by the same advanced filters.

        Synchronous, at most 10,000 records: the cap is max_leaks or your point balance,
        whichever is smaller (the whole balance when max_leaks is omitted or 0), and a cap
        above 10,000 raises BadRequestError (400). With more than 10,000 points, pass max_leaks
        (10,000 or less), or use queue_advanced_unlock_task().

        API: unlock_advanced, https://docs.leakradar.io/#tag/search-advanced/unlock_advanced
        """
        params: Dict[str, Any] = {}
        if max_leaks is not None:
            params["max"] = max_leaks
        if list_id is not None:
            params["list_id"] = list_id

        filters = self._normalize_leak_filters(dict(filters or {}))
        return await self._request("POST", "/search/advanced/unlock", params=params, json=filters)

    async def queue_advanced_unlock_task(
        self,
        filters: Dict[str, Any],
        max_leaks: Optional[int] = None,
        list_id: Optional[int] = None,
    ) -> _types.UnlockTaskQueuedResponse:
        """
        Queue an async mass-unlock task for advanced search.

        API: queue_advanced_unlock, https://docs.leakradar.io/#tag/search-advanced/queue_advanced_unlock
        """
        params: Dict[str, Any] = {}
        if max_leaks is not None:
            params["max"] = max_leaks
        if list_id is not None:
            params["list_id"] = list_id

        filters = self._normalize_leak_filters(dict(filters or {}))
        return await self._request("POST", "/search/advanced/unlock/task", params=params, json=filters)

    async def export_advanced(self, format: Optional[str] = None, **filters: Any) -> _types.ExportResponse:
        """
        Queue an export for advanced search results.
        format: csv|txt|json (default csv)
        Provide LeakSearchFilters in the JSON body via **filters.

        API: export_advanced, https://docs.leakradar.io/#tag/search-advanced/export_advanced
        """
        params = self._clean({"format": format})
        payload = self._normalize_leak_filters(self._clean(filters) or {})
        return await self._request("POST", "/search/advanced/export", params=params, json=payload)

    async def export_advanced_urls(self, format: Optional[str] = None, **filters: Any) -> _types.ExportResponse:
        """
        Queue an export of distinct URLs matching advanced filters.
        format: csv|txt|json (default csv)
        Provide LeakSearchFilters in the JSON body via **filters.

        API: export_advanced_urls, https://docs.leakradar.io/#tag/search-advanced/export_advanced_urls
        """
        params = self._clean({"format": format})
        payload = self._normalize_leak_filters(self._clean(filters) or {})
        return await self._request("POST", "/search/advanced/export_urls", params=params, json=payload)

    # -------------------------
    # Search (Domain)
    # -------------------------
    # Every `domain` argument goes through _domain(): a URL such as "https://www.example.com/fr"
    # searches "example.com", with a UserWarning saying so. The entries of domains_locked_exists()
    # are sent as given: the API reduces them itself.

    async def get_domain_report(
        self,
        domain: str,
        light: Optional[bool] = None,
        include_search_count: Optional[bool] = None,
    ) -> Union[_types.DomainSearchLightResponse, _types.DomainSearchResponse]:
        """
        Retrieve a domain leak report. Set light=True for sampled counts.
        include_search_count=True adds `searched_by_count` (full mode only, heavier).

        API: get_domain_report, https://docs.leakradar.io/#tag/search-domain/get_domain_report
        """
        params = self._clean({"light": light, "include_search_count": include_search_count})
        return await self._request("GET", f"/search/domain/{self._seg(_domain(domain))}", params=params)

    async def get_domain_report_pdf(self, domain: str) -> bytes:
        """
        Download domain report as PDF (binary).

        API: get_domain_report_pdf, https://docs.leakradar.io/#tag/search-domain/get_domain_report_pdf
        """
        return await self._request(
            "GET",
            f"/search/domain/{self._seg(_domain(domain))}/report/pdf",
            headers={"Accept": "application/pdf"},
        )

    async def get_domain_all(
        self,
        domain: str,
        page: int = 1,
        page_size: int = 100,
        search: Optional[str] = None,
        is_email: Optional[bool] = None,
        auto_unlock: Optional[bool] = None,
    ) -> _types.PaginatedAllLeaksResponse:
        """
        List every leak of a domain (employees, customers and third parties together).
        Each item carries a `category` field.

        API: list_domain_leaks, https://docs.leakradar.io/#tag/search-domain/list_domain_leaks
        """
        params = self._clean(
            {"page": page, "page_size": page_size, "search": search, "is_email": is_email, "auto_unlock": auto_unlock}
        )
        return await self._request("GET", f"/search/domain/{self._seg(_domain(domain))}/all", params=params)

    async def get_domain_customers(
        self,
        domain: str,
        page: int = 1,
        page_size: int = 100,
        search: Optional[str] = None,
        is_email: Optional[bool] = None,
        auto_unlock: Optional[bool] = None,
    ) -> _types.PaginatedLeaksResponse:
        """
        List the leaks linked to customers of a domain.

        API: list_domain_customers, https://docs.leakradar.io/#tag/search-domain/list_domain_customers
        """
        params = self._clean(
            {"page": page, "page_size": page_size, "search": search, "is_email": is_email, "auto_unlock": auto_unlock}
        )
        return await self._request("GET", f"/search/domain/{self._seg(_domain(domain))}/customers", params=params)

    async def get_domain_employees(
        self,
        domain: str,
        page: int = 1,
        page_size: int = 100,
        search: Optional[str] = None,
        is_email: Optional[bool] = None,
        auto_unlock: Optional[bool] = None,
    ) -> _types.PaginatedLeaksResponse:
        """
        List the leaks linked to employees of a domain.

        API: list_domain_employees, https://docs.leakradar.io/#tag/search-domain/list_domain_employees
        """
        params = self._clean(
            {"page": page, "page_size": page_size, "search": search, "is_email": is_email, "auto_unlock": auto_unlock}
        )
        return await self._request("GET", f"/search/domain/{self._seg(_domain(domain))}/employees", params=params)

    async def get_domain_third_parties(
        self,
        domain: str,
        page: int = 1,
        page_size: int = 100,
        search: Optional[str] = None,
        is_email: Optional[bool] = None,
        auto_unlock: Optional[bool] = None,
    ) -> _types.PaginatedLeaksResponse:
        """
        List the leaks linked to third parties of a domain.

        API: list_domain_third_parties, https://docs.leakradar.io/#tag/search-domain/list_domain_third_parties
        """
        params = self._clean(
            {"page": page, "page_size": page_size, "search": search, "is_email": is_email, "auto_unlock": auto_unlock}
        )
        return await self._request("GET", f"/search/domain/{self._seg(_domain(domain))}/third_parties", params=params)

    async def get_domain_subdomains(
        self,
        domain: str,
        page: int = 1,
        page_size: int = 100,
        search: Optional[str] = None,
    ) -> _types.PaginatedResponseSubdomainOccurrences:
        """
        List the subdomains seen in a domain's leaks.

        API: list_domain_subdomains, https://docs.leakradar.io/#tag/search-domain/list_domain_subdomains
        """
        params = self._clean({"page": page, "page_size": page_size, "search": search})
        return await self._request("GET", f"/search/domain/{self._seg(_domain(domain))}/subdomains", params=params)

    async def export_domain_subdomains(
        self,
        domain: str,
        search: Optional[str] = None,
        format: Optional[str] = None,
    ) -> Union[bytes, List[Dict[str, str]]]:
        """
        Export all unique subdomains for a domain.

        Note: as of OpenAPI v1.0.0, this endpoint is POST.
        Returns the file as bytes for format "csv" (default) and "txt"; with format="json" the
        answer is JSON, decoded into a list of {"Subdomain": ...} dicts.

        API: export_domain_subdomains, https://docs.leakradar.io/#tag/search-domain/export_domain_subdomains
        """
        params = self._clean({"format": format})
        body = self._clean({"search": search})
        return await self._request(
            "POST", f"/search/domain/{self._seg(_domain(domain))}/subdomains/export", params=params, json=body
        )

    async def get_domain_urls(
        self,
        domain: str,
        page: int = 1,
        page_size: int = 100,
        search: Optional[str] = None,
    ) -> _types.PaginatedResponseURLOccurrences:
        """
        List the URLs seen in a domain's leaks.

        API: list_domain_urls, https://docs.leakradar.io/#tag/search-domain/list_domain_urls
        """
        params = self._clean({"page": page, "page_size": page_size, "search": search})
        return await self._request("GET", f"/search/domain/{self._seg(_domain(domain))}/urls", params=params)

    async def export_domain_urls(
        self,
        domain: str,
        search: Optional[str] = None,
        format: Optional[str] = None,
    ) -> Union[bytes, List[Dict[str, str]]]:
        """
        Export all unique URLs for a domain.

        Note: as of OpenAPI v1.0.0, this endpoint is POST.
        Returns the file as bytes for format "csv" (default) and "txt"; with format="json" the
        answer is JSON, decoded into a list of {"URL": ...} dicts.

        API: export_domain_urls, https://docs.leakradar.io/#tag/search-domain/export_domain_urls
        """
        params = self._clean({"format": format})
        body = self._clean({"search": search})
        return await self._request("POST", f"/search/domain/{self._seg(_domain(domain))}/urls/export", params=params, json=body)

    async def export_domain_leaks(
        self,
        domain: str,
        leak_type: str,
        search: Optional[str] = None,
        is_email: Optional[bool] = None,
        format: Optional[str] = None,
    ) -> _types.ExportResponse:
        """
        Queue an export for leaks of a domain and leak type.

        Note: as of OpenAPI v1.0.0, this endpoint is POST.

        API: export_domain, https://docs.leakradar.io/#tag/search-domain/export_domain
        """
        params = self._clean({"format": format})
        body = self._clean({"search": search, "is_email": is_email})
        return await self._request(
            "POST",
            f"/search/domain/{self._seg(_domain(domain))}/{self._seg(leak_type)}/export",
            params=params,
            json=body,
        )

    async def unlock_domain_leaks(
        self,
        domain: str,
        leak_type: str,
        search: Optional[str] = None,
        is_email: Optional[bool] = None,
        max_leaks: Optional[int] = None,
        list_id: Optional[int] = None,
    ) -> List[_types.LeakDetails]:
        """
        Unlock leaks for a domain + category.

        Synchronous, at most 10,000 records: the cap is max_leaks or your point balance,
        whichever is smaller (the whole balance when max_leaks is omitted or 0), and a cap
        above 10,000 raises BadRequestError (400). With more than 10,000 points, pass max_leaks
        (10,000 or less), or use queue_domain_unlock_task().

        API: unlock_domain, https://docs.leakradar.io/#tag/search-domain/unlock_domain
        """
        params: Dict[str, Any] = {}
        if search is not None:
            params["search"] = search
        if is_email is not None:
            params["is_email"] = is_email
        if max_leaks is not None:
            params["max"] = max_leaks
        if list_id is not None:
            params["list_id"] = list_id
        return await self._request(
            "POST", f"/search/domain/{self._seg(_domain(domain))}/{self._seg(leak_type)}/unlock", params=params
        )

    async def queue_domain_unlock_task(
        self,
        domain: str,
        leak_type: str,
        search: Optional[str] = None,
        is_email: Optional[bool] = None,
        max_leaks: Optional[int] = None,
        list_id: Optional[int] = None,
    ) -> _types.UnlockTaskQueuedResponse:
        """
        Queue an async mass unlock task for a domain.

        API: queue_domain_unlock, https://docs.leakradar.io/#tag/search-domain/queue_domain_unlock
        """
        params = self._clean({"search": search, "is_email": is_email, "max": max_leaks, "list_id": list_id})
        return await self._request(
            "POST", f"/search/domain/{self._seg(_domain(domain))}/{self._seg(leak_type)}/unlock/task", params=params
        )

    async def domains_locked_exists(
        self,
        domains: List[str],
        categories: Optional[List[str]] = None,
        include_counts: bool = False,
        datasets: Optional[StrOrList] = None,
    ) -> _types.DomainMassResponse:
        """
        Batch exists-check of still-locked leaks by domain and category (≤100 domains).
        categories: subset of ['employees','customers','third_parties'] or None for all.
        datasets: "stealer_logs" and/or "combolist", as a list or a comma-separated string.
        Omitted, only Stealer Logs is checked and the answer keeps its historical shape; set,
        the counts are summed over the listed datasets and the answer adds the split per
        dataset, `datasets` and `unavailable_datasets`.

        The entries are sent as given: the API reduces each one itself (the host of a URL with a
        scheme, without "www.", lowercase) and answers an entry that is no domain as not found; an
        entry that reduces to nothing ("", "www.") makes it refuse the whole call (ValidationError).

        API: check_domains_locked, https://docs.leakradar.io/#tag/search-domain/check_domains_locked
        """
        if isinstance(domains, str):
            domains = [domains]
        # Not through _domain(): one entry it refuses would fail the whole batch of up to 100.
        payload: Dict[str, Any] = {"domains": domains}
        if categories is not None:
            payload["categories"] = categories
        if include_counts:
            payload["include_counts"] = True
        if datasets is not None:
            payload["datasets"] = self._dataset_list(datasets)
        return await self._request("POST", "/search/domains/locked-exists", json=payload)

    # -------------------------
    # Search (Email)
    # -------------------------

    async def search_email(
        self,
        email: str,
        page: int = 1,
        page_size: int = 100,
        search: Optional[str] = None,
        is_email: Optional[bool] = None,
        auto_unlock: Optional[bool] = None,
    ) -> _types.EmailSearchResponse:
        """
        Search stealer-log leaks for an email or username. auto_unlock=True unlocks the page with points.

        API: search_email, https://docs.leakradar.io/#tag/search-email/search_email
        """
        params = self._clean({"page": page, "page_size": page_size, "auto_unlock": auto_unlock})
        data = self._clean({"email": email, "search": search, "is_email": is_email}) or {"email": email}
        return await self._request("POST", "/search/email", params=params, json=data)

    async def export_email_leaks(
        self,
        email: str,
        search: Optional[str] = None,
        is_email: Optional[bool] = None,
        format: Optional[str] = None,
    ) -> _types.ExportResponse:
        """
        Queue an export (csv|txt|json, default csv) of the leaks of an email or username.

        API: export_email, https://docs.leakradar.io/#tag/search-email/export_email
        """
        params = self._clean({"format": format})
        data = self._clean({"email": email, "search": search, "is_email": is_email}) or {"email": email}
        return await self._request("POST", "/search/email/export", params=params, json=data)

    async def unlock_email_leaks(
        self,
        email: str,
        max_leaks: Optional[int] = None,
        list_id: Optional[int] = None,
        search: Optional[str] = None,
        is_email: Optional[bool] = None,
    ) -> List[_types.LeakDetails]:
        """
        Unlock the leaks of an email or username.

        Synchronous, at most 10,000 records: the cap is max_leaks or your point balance,
        whichever is smaller (the whole balance when max_leaks is omitted or 0), and a cap
        above 10,000 raises BadRequestError (400). With more than 10,000 points, pass max_leaks
        (10,000 or less), or use queue_email_unlock_task().

        API: unlock_email, https://docs.leakradar.io/#tag/search-email/unlock_email
        """
        params: Dict[str, Any] = {}
        if max_leaks is not None:
            params["max"] = max_leaks
        if list_id is not None:
            params["list_id"] = list_id
        data = self._clean({"email": email, "search": search, "is_email": is_email}) or {"email": email}
        return await self._request("POST", "/search/email/unlock", params=params, json=data)

    async def queue_email_unlock_task(
        self,
        email: str,
        max_leaks: Optional[int] = None,
        list_id: Optional[int] = None,
        search: Optional[str] = None,
        is_email: Optional[bool] = None,
    ) -> _types.UnlockTaskQueuedResponse:
        """
        Queue a background unlock of every leak of an email or username. Poll with get_task_status().

        API: queue_email_unlock, https://docs.leakradar.io/#tag/search-email/queue_email_unlock
        """
        params: Dict[str, Any] = {}
        if max_leaks is not None:
            params["max"] = max_leaks
        if list_id is not None:
            params["list_id"] = list_id
        data = self._clean({"email": email, "search": search, "is_email": is_email}) or {"email": email}
        return await self._request("POST", "/search/email/unlock/task", params=params, json=data)

    async def emails_locked_exists(
        self,
        emails: List[str],
        include_counts: bool = False,
        datasets: Optional[StrOrList] = None,
    ) -> _types.EmailMassResponse:
        """
        Batch check which emails or usernames still have locked leaks for your account (≤100).
        datasets: "stealer_logs" and/or "combolist", as a list or a comma-separated string.
        Omitted, only Stealer Logs is checked and the answer keeps its historical shape; set,
        the counts are summed over the listed datasets and the answer adds the split per
        dataset, `datasets` and `unavailable_datasets`.

        API: check_emails_locked, https://docs.leakradar.io/#tag/search-email/check_emails_locked
        """
        payload: Dict[str, Any] = {"emails": emails}
        if include_counts:
            payload["include_counts"] = True
        if datasets is not None:
            payload["datasets"] = self._dataset_list(datasets)
        return await self._request("POST", "/search/emails/locked-exists", json=payload)

    # -------------------------
    # Search (Android applications)
    # -------------------------

    async def suggest_applications(
        self, q: Optional[str] = None, limit: Optional[int] = None
    ) -> _types.ApplicationCatalogResponse:
        """
        Android apps found in stealer logs, most entries first.
        q (3+ chars) matches the app name or package; omit it for the top apps. limit: 1-50 (default 20).
        Use the returned `package` with get_application_leaks().

        API: suggest_applications, https://docs.leakradar.io/#tag/search-applications/suggest_applications
        """
        params = self._clean({"q": q, "limit": limit})
        return await self._request("GET", "/search/applications", params=params)

    async def get_application_leaks(
        self,
        package: str,
        page: int = 1,
        page_size: int = 100,
        search: Optional[str] = None,
        is_email: Optional[bool] = None,
        auto_unlock: Optional[bool] = None,
    ) -> _types.PaginatedLeaksResponse:
        """
        List the stealer-log entries (android:// URLs) of an Android app, newest first.

        API: list_application_leaks, https://docs.leakradar.io/#tag/search-applications/list_application_leaks
        """
        params = self._clean(
            {"page": page, "page_size": page_size, "search": search, "is_email": is_email, "auto_unlock": auto_unlock}
        )
        return await self._request("GET", f"/search/applications/{self._seg(package)}/leaks", params=params)

    async def unlock_application_leaks(
        self,
        package: str,
        search: Optional[str] = None,
        is_email: Optional[bool] = None,
        max_leaks: Optional[int] = None,
        list_id: Optional[int] = None,
    ) -> List[_types.LeakDetails]:
        """
        Unlock the entries of an Android app.

        Synchronous, at most 10,000 records: the cap is max_leaks or your point balance,
        whichever is smaller (the whole balance when max_leaks is omitted or 0), and a cap
        above 10,000 raises BadRequestError (400). With more than 10,000 points, pass max_leaks
        (10,000 or less), or use queue_application_unlock_task().

        API: unlock_application, https://docs.leakradar.io/#tag/search-applications/unlock_application
        """
        params = self._clean({"search": search, "is_email": is_email, "max": max_leaks, "list_id": list_id})
        return await self._request("POST", f"/search/applications/{self._seg(package)}/unlock", params=params)

    async def queue_application_unlock_task(
        self,
        package: str,
        search: Optional[str] = None,
        is_email: Optional[bool] = None,
        max_leaks: Optional[int] = None,
        list_id: Optional[int] = None,
    ) -> _types.UnlockTaskQueuedResponse:
        """
        Queue an async unlock task for an Android app (at most 5 queued or running per account).

        API: queue_application_unlock, https://docs.leakradar.io/#tag/search-applications/queue_application_unlock
        """
        params = self._clean({"search": search, "is_email": is_email, "max": max_leaks, "list_id": list_id})
        return await self._request("POST", f"/search/applications/{self._seg(package)}/unlock/task", params=params)

    async def export_application_leaks(
        self,
        package: str,
        search: Optional[str] = None,
        is_email: Optional[bool] = None,
        format: Optional[str] = None,
    ) -> _types.ExportResponse:
        """
        Queue an export (csv|txt|json, default csv) of the entries of an Android app.

        API: export_application, https://docs.leakradar.io/#tag/search-applications/export_application
        """
        params = self._clean({"format": format})
        body = self._clean({"search": search, "is_email": is_email})
        return await self._request(
            "POST", f"/search/applications/{self._seg(package)}/export", params=params, json=body
        )

    # -------------------------
    # Search (Combolists)
    # -------------------------

    async def search_combolist_email(
        self,
        email: str,
        page: int = 1,
        page_size: int = 100,
        search: Optional[str] = None,
        auto_unlock: Optional[bool] = None,
    ) -> _types.CombolistSearchResponse:
        """
        Search combolists for an exact email (case-insensitive).

        Combolists are a separate dataset from stealer logs: results carry no URL, and an unlock
        lands in get_unlocked_combolists(), not get_unlocked_leaks(). page_size: 1-100.

        API: search_combolist_email, https://docs.leakradar.io/#tag/search-combolists/search_combolist_email
        """
        params = self._clean({"page": page, "page_size": page_size, "auto_unlock": auto_unlock})
        body = self._clean({"email": email, "search": search})
        return await self._request("POST", "/search/combolist/email", params=params, json=body)

    async def search_combolist_username(
        self,
        username: str,
        page: int = 1,
        page_size: int = 100,
        search: Optional[str] = None,
        auto_unlock: Optional[bool] = None,
    ) -> _types.CombolistSearchResponse:
        """
        Search combolists for an exact non-email username (case-insensitive). page_size: 1-100.

        API: search_combolist_username, https://docs.leakradar.io/#tag/search-combolists/search_combolist_username
        """
        params = self._clean({"page": page, "page_size": page_size, "auto_unlock": auto_unlock})
        body = self._clean({"username": username, "search": search})
        return await self._request("POST", "/search/combolist/username", params=params, json=body)

    async def search_combolist_domain(
        self,
        domain: str,
        page: int = 1,
        page_size: int = 100,
        search: Optional[str] = None,
        auto_unlock: Optional[bool] = None,
    ) -> _types.CombolistSearchResponse:
        """
        Search combolist pairs whose email is on a root domain (username-only pairs never match).
        page_size: 1-1000.

        API: search_combolist_domain, https://docs.leakradar.io/#tag/search-combolists/search_combolist_domain
        """
        params = self._clean(
            {"page": page, "page_size": page_size, "search": search, "auto_unlock": auto_unlock}
        )
        return await self._request("GET", f"/search/combolist/domain/{self._seg(_domain(domain))}", params=params)

    async def get_combolist_domain_report(
        self, domain: str, no_cache: bool = False
    ) -> _types.CombolistDomainReportResponse:
        """
        Combolist exposure report of a root email domain (counts only, cached one hour).
        no_cache=True forces a recompute.

        API: get_combolist_domain_report, https://docs.leakradar.io/#tag/search-combolists/get_combolist_domain_report
        """
        headers = {"x-no-cache": "1"} if no_cache else None
        return await self._request("GET", f"/search/combolist/domain/{self._seg(_domain(domain))}/report", headers=headers)

    async def search_combolist_advanced(
        self,
        page: int = 1,
        page_size: int = 100,
        *,
        auto_unlock: Optional[bool] = None,
        username: Optional[StrOrList] = None,
        username_not: Optional[StrOrList] = None,
        username_match_type: Optional[str] = None,
        username_not_match_type: Optional[str] = None,
        password: Optional[StrOrList] = None,
        password_not: Optional[StrOrList] = None,
        password_match_type: Optional[str] = None,
        password_not_match_type: Optional[str] = None,
        email_domain: Optional[StrOrList] = None,
        email_domain_not: Optional[StrOrList] = None,
        email_domain_match_type: Optional[str] = None,
        email_domain_not_match_type: Optional[str] = None,
        username_hash: Optional[StrOrList] = None,
        password_hash: Optional[StrOrList] = None,
        is_email: Optional[bool] = None,
        password_strength: Optional[str] = None,
        added_from: Optional[DateLike] = None,
        added_to: Optional[DateLike] = None,
        force_and: Optional[bool] = None,
    ) -> _types.CombolistSearchResponse:
        """
        Search combolists with advanced filters.

        Only the fields of the combolist mapping exist (no url_*, email_host or email_tld).
        `*_match_type`: "contains" (default), "starts_with" or "ends_with". page_size: 1-1000.
        The same keys, as a dict, are the `filters` of the advanced scope in
        queue_combolist_unlock_task(), export_combolist() and get_unlocked_combolists_advanced().

        API: search_combolist_advanced, https://docs.leakradar.io/#tag/search-combolists/search_combolist_advanced
        """
        filters = self._normalize_leak_filters(self._pick(locals(), self._COMBOLIST_FILTER_FIELDS))
        params = self._clean({"page": page, "page_size": page_size, "auto_unlock": auto_unlock})
        return await self._request("POST", "/search/combolist/advanced", params=params, json=filters)

    async def unlock_combolist_leaks(
        self,
        leak_ids: List[str],
        target_list_id: Optional[int] = None,
    ) -> List[_types.CombolistUnlockResult]:
        """
        Unlock combolist credentials by ID (`cl1_` IDs only, at most 10,000 per call).
        1 point per pair newly unlocked; pairs you already own are free.

        API: unlock_combolist, https://docs.leakradar.io/#tag/search-combolists/unlock_combolist
        """
        data: Dict[str, Any] = {"leak_ids": leak_ids}
        if target_list_id is not None:
            data["target_list_id"] = target_list_id
        return await self._request("POST", "/search/combolist/unlock", json=data)

    async def queue_combolist_unlock_task(
        self,
        scope: str,
        value: Optional[str] = None,
        search: Optional[str] = None,
        filters: Optional[Dict[str, Any]] = None,
        max_leaks: Optional[int] = None,
        list_id: Optional[int] = None,
    ) -> _types.TaskStatusOut:
        """
        Queue a mass unlock of every combolist pair in a search scope.

        - scope "email", "username" or "domain" takes `value` (and optionally `search`).
        - scope "advanced" takes `filters` (the keys of search_combolist_advanced()).
        - Capped by your point balance and by max_leaks.
        - At most 2 combolist unlock tasks queued or running per account: a third raises
          TooManyRequestsError (429).
        Poll the returned task_id with get_task_status().

        API: queue_combolist_unlock, https://docs.leakradar.io/#tag/search-combolists/queue_combolist_unlock
        """
        body = self._combolist_scope_body(scope, value, search, filters)
        params = self._clean({"max": max_leaks, "list_id": list_id})
        return await self._request("POST", "/search/combolist/unlock/task", params=params, json=body)

    async def export_combolist(
        self,
        scope: str,
        value: Optional[str] = None,
        search: Optional[str] = None,
        filters: Optional[Dict[str, Any]] = None,
        format: Optional[str] = None,
    ) -> _types.ExportResponse:
        """
        Queue an export (csv|txt|json, default csv) of a combolist search scope.

        Same scope rules as queue_combolist_unlock_task(). On a points plan the file only holds the
        pairs you already unlocked in that scope: unlock first to export the full result set.

        API: export_combolist, https://docs.leakradar.io/#tag/search-combolists/export_combolist
        """
        body = self._combolist_scope_body(scope, value, search, filters)
        params = self._clean({"format": format})
        return await self._request("POST", "/search/combolist/export", params=params, json=body)

    # -------------------------
    # Search (Dark web)
    # -------------------------

    async def search_dark_web(
        self,
        query: Optional[str] = None,
        page: int = 1,
        page_size: int = 25,
        title: Optional[str] = None,
        content: Optional[str] = None,
        author: Optional[str] = None,
        source_url: Optional[str] = None,
        logic: Optional[str] = None,
        sources: Optional[StrOrList] = None,
        date_from: Optional[DateLike] = None,
        date_to: Optional[DateLike] = None,
        sort_by: Optional[str] = None,
        sort_order: Optional[str] = None,
    ) -> _types.DarkWebSearchResponse:
        """
        Search dark web and forum posts.

        - Simple mode: `query` searches title and content.
        - Advanced mode: `title`, `content`, `author` (exact), `source_url` combined with logic "AND" (default) or "OR".
        - sources: source names (see get_dark_web_sources()), as a list or a comma-separated string.
        - sort_by: "ingested_at" (default) or "published_at"; sort_order: "desc" (default) or "asc".
        - page_size: 1-100. Paging stops at page * page_size = 10,000 (BadRequestError beyond):
          narrow the search (date_from / date_to, sources) to reach the other results.

        API: search_dark_web, https://docs.leakradar.io/#tag/dark-web-search/search_dark_web
        """
        if sources is not None and not isinstance(sources, str):
            sources = ",".join(sources)
        params = self._clean(
            {
                "page": page,
                "page_size": page_size,
                "sources": sources,
                "date_from": date_from,
                "date_to": date_to,
                "sort_by": sort_by,
                "sort_order": sort_order,
            }
        )
        body = self._clean(
            {
                "query": query,
                "title": title,
                "content": content,
                "author": author,
                "source_url": source_url,
                "logic": logic,
            }
        )
        return await self._request("POST", "/search/dark-web", params=params, json=body)

    async def get_dark_web_post(self, post_id: str) -> _types.DarkWebItem:
        """
        Full content of one dark web post (search results are truncated).

        API: get_dark_web_post, https://docs.leakradar.io/#tag/dark-web-search/get_dark_web_post
        """
        return await self._request("GET", f"/search/dark-web/post/{self._seg(post_id)}")

    async def get_dark_web_sources(self) -> _types.DarkWebSourcesResponse:
        """
        Scraped forum sources with their post counts.

        API: list_dark_web_sources, https://docs.leakradar.io/#tag/dark-web-search/list_dark_web_sources
        """
        return await self._request("GET", "/search/dark-web/sources")

    async def get_dark_web_stats(self) -> _types.DarkWebStatsResponse:
        """
        Overall statistics of the dark web index.

        API: get_dark_web_stats, https://docs.leakradar.io/#tag/dark-web-search/get_dark_web_stats
        """
        return await self._request("GET", "/search/dark-web/stats")

    # -------------------------
    # Unlocked leaks (profile)
    # -------------------------

    async def get_unlocked_leaks(
        self,
        page: int = 1,
        page_size: int = 100,
        search: Optional[str] = None,
        is_email: Optional[bool] = None,
        list_id: Optional[int] = None,
        list_none: Optional[bool] = None,
        status: Optional[str] = None,
    ) -> _types.PaginatedUnlockedLeaksResponse:
        """
        status: remediation status, "new", "in_progress", "fixed" or "accepted_risk".

        API: list_unlocked_leaks, https://docs.leakradar.io/#tag/unlocked-leaks/list_unlocked_leaks
        """
        params = self._clean(
            {
                "page": page,
                "page_size": page_size,
                "search": search,
                "is_email": is_email,
                "list_id": list_id,
                "list_none": list_none,
                "status": status,
            }
        )
        return await self._request("GET", "/profile/unlocked", params=params)

    async def export_unlocked_leaks(
        self,
        search: Optional[str] = None,
        is_email: Optional[bool] = None,
        list_id: Optional[int] = None,
        list_none: Optional[bool] = None,
        format: Optional[str] = None,
        unlocked_at_min: Optional[DateLike] = None,
        status: Optional[str] = None,
    ) -> _types.ExportResponse:
        """
        Queue an export (csv|txt|json, default csv) of your unlocked stealer-log leaks.

        API: export_unlocked_leaks, https://docs.leakradar.io/#tag/unlocked-leaks/export_unlocked_leaks
        """
        params = self._clean({"format": format})
        payload = self._clean(
            {
                "search": search,
                "is_email": is_email,
                "list_id": list_id,
                "list_none": list_none,
                "unlocked_at_min": unlocked_at_min,
                "status": status,
            }
        ) or {}
        return await self._request("POST", "/profile/unlocked/export", params=params, json=payload)

    async def get_unlocked_advanced(
        self,
        page: int = 1,
        page_size: int = 100,
        search: Optional[str] = None,
        list_id: Optional[int] = None,
        list_none: Optional[bool] = None,
        status: Optional[str] = None,
        **filters: Any,
    ) -> _types.PaginatedUnlockedLeaksResponse:
        """
        List unlocked leaks with the advanced filter set (**filters, sent as query parameters).

        API: search_unlocked_leaks, https://docs.leakradar.io/#tag/unlocked-leaks/search_unlocked_leaks
        """
        base = self._clean(
            {"page": page, "page_size": page_size, "search": search, "list_id": list_id, "list_none": list_none, "status": status}
        )
        params = self._clean({**(base or {}), **filters})
        return await self._request("GET", "/profile/unlocked/advanced", params=params)

    async def export_unlocked_advanced(
        self,
        format: Optional[str] = None,
        unlocked_at_min: Optional[DateLike] = None,
        list_id: Optional[int] = None,
        list_none: Optional[bool] = None,
        status: Optional[str] = None,
        **filters: Any,
    ) -> _types.ExportResponse:
        """
        unlocked_at_min must carry a timezone (a naive value is a 400 on this endpoint).

        API: export_unlocked_leaks_advanced, https://docs.leakradar.io/#tag/unlocked-leaks/export_unlocked_leaks_advanced
        """
        params = self._clean(
            {"format": format, "unlocked_at_min": unlocked_at_min, "list_id": list_id, "list_none": list_none, "status": status}
        )
        payload = self._normalize_leak_filters(self._clean(filters) or {})
        return await self._request("POST", "/profile/unlocked/advanced/export", params=params, json=payload)

    async def set_unlocked_leak_status(self, leak_id: str, status: str) -> _types.StatusSuccessResponse:
        """
        Set the remediation status of one unlocked leak: "new", "in_progress", "fixed" or "accepted_risk".

        API: set_unlocked_leak_status, https://docs.leakradar.io/#tag/unlocked-leaks/set_unlocked_leak_status
        """
        return await self._request("PUT", f"/profile/unlocked/{self._seg(leak_id)}/status", json={"status": status})

    async def bulk_update_unlocked_status(
        self,
        target_status: str,
        filters: Optional[Dict[str, Any]] = None,
        search: Optional[str] = None,
        is_email: Optional[bool] = None,
        list_id_filter: Optional[int] = None,
        list_none: Optional[bool] = None,
        status_filter: Optional[str] = None,
        dataset: Optional[str] = None,
    ) -> _types.TaskStatusOut:
        """
        Background task setting `target_status` on every unlocked record matching the filters.
        dataset: "stealer_logs" (default) or "combolist". Poll with get_unlocked_list_task().

        API: bulk_set_unlocked_leak_status, https://docs.leakradar.io/#tag/unlocked-leaks/bulk_set_unlocked_leak_status
        """
        payload: Dict[str, Any] = {"target_status": target_status}
        payload.update(
            self._clean(
                {
                    "dataset": dataset,
                    "search": search,
                    "is_email": is_email,
                    "list_id_filter": list_id_filter,
                    "list_none": list_none,
                    "status_filter": status_filter,
                }
            )
            or {}
        )
        if filters is not None:
            payload["filters"] = self._normalize_leak_filters(dict(filters))
        return await self._request("POST", "/profile/unlocked/bulk-status", json=payload)

    # -------------------------
    # Unlocked combolists (profile)
    # -------------------------

    async def get_unlocked_combolists(
        self,
        page: int = 1,
        page_size: int = 100,
        search: Optional[str] = None,
        is_email: Optional[bool] = None,
        list_id: Optional[int] = None,
        list_none: Optional[bool] = None,
        status: Optional[str] = None,
        unlocked_at_min: Optional[DateLike] = None,
    ) -> _types.PaginatedUnlockedCombolistsResponse:
        """
        List the combolist credentials you unlocked, in clear (disjoint from get_unlocked_leaks()).
        unlocked_at_min (with a timezone) pages an incremental sync. page_size: 1-1000.

        API: list_unlocked_combolists, https://docs.leakradar.io/#tag/unlocked-combolists/list_unlocked_combolists
        """
        params = self._clean(
            {
                "page": page,
                "page_size": page_size,
                "search": search,
                "is_email": is_email,
                "list_id": list_id,
                "list_none": list_none,
                "status": status,
                "unlocked_at_min": unlocked_at_min,
            }
        )
        return await self._request("GET", "/profile/unlocked/combolists", params=params)

    async def get_unlocked_combolists_advanced(
        self,
        page: int = 1,
        page_size: int = 100,
        filters: Optional[Dict[str, Any]] = None,
        search: Optional[str] = None,
        is_email: Optional[bool] = None,
        list_id: Optional[int] = None,
        list_none: Optional[bool] = None,
        status: Optional[str] = None,
        unlocked_at_min: Optional[DateLike] = None,
    ) -> _types.PaginatedUnlockedCombolistsResponse:
        """
        Same listing as get_unlocked_combolists(), plus the advanced `filters` of search_combolist_advanced().

        API: search_unlocked_combolists, https://docs.leakradar.io/#tag/unlocked-combolists/search_unlocked_combolists
        """
        params = self._clean({"page": page, "page_size": page_size})
        body = self._clean(
            {
                "search": search,
                "is_email": is_email,
                "list_id": list_id,
                "list_none": list_none,
                "status": status,
                "unlocked_at_min": unlocked_at_min,
            }
        ) or {}
        if filters is not None:
            body["filters"] = self._normalize_leak_filters(self._clean(filters) or {})
        return await self._request("POST", "/profile/unlocked/combolists/advanced", params=params, json=body)

    async def export_unlocked_combolists(
        self,
        format: Optional[str] = None,
        filters: Optional[Dict[str, Any]] = None,
        search: Optional[str] = None,
        is_email: Optional[bool] = None,
        list_id: Optional[int] = None,
        list_none: Optional[bool] = None,
        status: Optional[str] = None,
        unlocked_at_min: Optional[DateLike] = None,
    ) -> _types.ExportResponse:
        """
        Queue an export (csv|txt|json, default csv) of your unlocked combolist credentials.
        Same filters as get_unlocked_combolists_advanced(); pass the timestamp of your last
        export as unlocked_at_min to get only what came after.

        API: export_unlocked_combolists, https://docs.leakradar.io/#tag/unlocked-combolists/export_unlocked_combolists
        """
        params = self._clean({"format": format})
        body = self._clean(
            {
                "search": search,
                "is_email": is_email,
                "list_id": list_id,
                "list_none": list_none,
                "status": status,
                "unlocked_at_min": unlocked_at_min,
            }
        ) or {}
        if filters is not None:
            body["filters"] = self._normalize_leak_filters(self._clean(filters) or {})
        return await self._request("POST", "/profile/unlocked/combolists/export", params=params, json=body)

    async def set_unlocked_combolist_list(self, leak_id: str, list_id: Optional[int]) -> _types.StatusSuccessResponse:
        """
        Assign one unlocked combolist credential to a list (None removes the assignment).

        API: set_unlocked_combolist_list, https://docs.leakradar.io/#tag/unlocked-combolists/set_unlocked_combolist_list
        """
        return await self._request(
            "PUT", f"/profile/unlocked/combolists/{self._seg(leak_id)}/list", json={"list_id": list_id}
        )

    async def upsert_unlocked_combolist_comment(self, leak_id: str, comment: str) -> _types.StatusSuccessResponse:
        """
        Set or replace the comment of one unlocked combolist credential (500 chars max).

        API: set_unlocked_combolist_comment, https://docs.leakradar.io/#tag/unlocked-combolists/set_unlocked_combolist_comment
        """
        return await self._request(
            "PUT", f"/profile/unlocked/combolists/{self._seg(leak_id)}/comment", json={"comment": comment}
        )

    async def delete_unlocked_combolist_comment(self, leak_id: str) -> _types.StatusSuccessResponse:
        """
        Remove the comment of one unlocked combolist credential (idempotent).

        API: delete_unlocked_combolist_comment, https://docs.leakradar.io/#tag/unlocked-combolists/delete_unlocked_combolist_comment
        """
        return await self._request("DELETE", f"/profile/unlocked/combolists/{self._seg(leak_id)}/comment")

    async def set_unlocked_combolist_status(self, leak_id: str, status: str) -> _types.StatusSuccessResponse:
        """
        Set the remediation status of one unlocked combolist credential ("new", "in_progress", "fixed", "accepted_risk").

        API: set_unlocked_combolist_status, https://docs.leakradar.io/#tag/unlocked-combolists/set_unlocked_combolist_status
        """
        return await self._request(
            "PUT", f"/profile/unlocked/combolists/{self._seg(leak_id)}/status", json={"status": status}
        )

    # -------------------------
    # Unlocked lists
    # -------------------------

    async def list_unlocked_lists(
        self, with_counts: bool = True, dataset: Optional[str] = None
    ) -> List[_types.UnlockedListOut]:
        """
        Lists are shared by both datasets; dataset ("stealer_logs" or "combolist") picks what items_count counts.

        API: list_unlocked_lists, https://docs.leakradar.io/#tag/unlocked-leaks-lists/list_unlocked_lists
        """
        params = self._clean({"with_counts": with_counts, "dataset": dataset})
        return await self._request("GET", "/profile/unlocked/lists", params=params)

    async def create_unlocked_list(self, name: str, color: Optional[str] = None) -> _types.UnlockedListOut:
        """
        Create an unlocked list (shared by stealer logs and combolists).

        API: create_unlocked_list, https://docs.leakradar.io/#tag/unlocked-leaks-lists/create_unlocked_list
        """
        payload = self._clean({"name": name, "color": color})
        return await self._request("POST", "/profile/unlocked/lists", json=payload)

    async def update_unlocked_list(
        self, list_id: int, name: Optional[str] = None, color: Optional[str] = None
    ) -> _types.UnlockedListOut:
        """
        Rename or recolor an unlocked list.

        API: update_unlocked_list, https://docs.leakradar.io/#tag/unlocked-leaks-lists/update_unlocked_list
        """
        payload = self._clean({"name": name, "color": color})
        return await self._request("PATCH", f"/profile/unlocked/lists/{self._seg(list_id)}", json=payload)

    async def delete_unlocked_list(self, list_id: int) -> _types.ListCleanupTasksOut:
        """
        Delete an unlocked list.

        API: delete_unlocked_list, https://docs.leakradar.io/#tag/unlocked-leaks-lists/delete_unlocked_list
        """
        return await self._request("DELETE", f"/profile/unlocked/lists/{self._seg(list_id)}")

    async def clear_unlocked_list(self, list_id: int) -> _types.ListCleanupTasksOut:
        """
        Unassign every record from a list, keeping the list itself.

        API: clear_unlocked_list, https://docs.leakradar.io/#tag/unlocked-leaks-lists/clear_unlocked_list
        """
        return await self._request("POST", f"/profile/unlocked/lists/{self._seg(list_id)}/clear")

    async def get_unlocked_list_task(self, task_id: str) -> _types.TaskStatusOut:
        """
        Status of a bulk list or status task (bulk_assign_unlocked_list, bulk_update_unlocked_status).

        API: get_unlocked_list_task, https://docs.leakradar.io/#tag/unlocked-leaks-lists/get_unlocked_list_task
        """
        return await self._request("GET", f"/profile/unlocked/list-tasks/{self._seg(task_id)}")

    async def set_unlocked_leak_list(self, leak_id: str, list_id: Optional[int]) -> _types.StatusSuccessResponse:
        """
        Assign one unlocked leak to a list (None removes the assignment).

        API: set_unlocked_leak_list, https://docs.leakradar.io/#tag/unlocked-leaks/set_unlocked_leak_list
        """
        return await self._request("PUT", f"/profile/unlocked/{self._seg(leak_id)}/list", json={"list_id": list_id})

    async def upsert_unlocked_leak_comment(self, leak_id: str, comment: str) -> _types.StatusSuccessResponse:
        """
        Set or replace the comment of one unlocked leak.

        API: set_unlocked_leak_comment, https://docs.leakradar.io/#tag/unlocked-leaks/set_unlocked_leak_comment
        """
        return await self._request("PUT", f"/profile/unlocked/{self._seg(leak_id)}/comment", json={"comment": comment})

    async def delete_unlocked_leak_comment(self, leak_id: str) -> _types.StatusSuccessResponse:
        """
        Remove the comment of one unlocked leak.

        API: delete_unlocked_leak_comment, https://docs.leakradar.io/#tag/unlocked-leaks/delete_unlocked_leak_comment
        """
        return await self._request("DELETE", f"/profile/unlocked/{self._seg(leak_id)}/comment")

    async def bulk_assign_unlocked_list(
        self,
        target_list_id: Optional[int],
        filters: Optional[Dict[str, Any]] = None,
        search: Optional[str] = None,
        is_email: Optional[bool] = None,
        list_id_filter: Optional[int] = None,
        list_none: Optional[bool] = None,
        status_filter: Optional[str] = None,
        dataset: Optional[str] = None,
    ) -> _types.TaskStatusOut:
        """
        Background task assigning `target_list_id` (None unassigns) to every unlocked record matching
        the filters. dataset: "stealer_logs" (default) or "combolist". Poll with get_unlocked_list_task().

        API: bulk_assign_unlocked_list, https://docs.leakradar.io/#tag/unlocked-leaks-lists/bulk_assign_unlocked_list
        """
        payload: Dict[str, Any] = {"target_list_id": target_list_id}
        if dataset is not None:
            payload["dataset"] = dataset
        if search is not None:
            payload["search"] = search
        if is_email is not None:
            payload["is_email"] = is_email
        if list_id_filter is not None:
            payload["list_id_filter"] = list_id_filter
        if list_none is not None:
            payload["list_none"] = list_none
        if status_filter is not None:
            payload["status_filter"] = status_filter
        if filters is not None:
            payload["filters"] = self._normalize_leak_filters(dict(filters))
        return await self._request("POST", "/profile/unlocked/lists/bulk-assign", json=payload)

    # -------------------------
    # Unlock
    # -------------------------

    async def unlock_specific_leaks(
        self, leak_ids: List[str], target_list_id: Optional[int] = None
    ) -> List[_types.LeakDetails]:
        """
        Unlock stealer-log leaks by ID. Combolist IDs (`cl1_`) go to unlock_combolist_leaks().

        API: unlock_by_id, https://docs.leakradar.io/#tag/unlock/unlock_by_id
        """
        data: Dict[str, Any] = {"leak_ids": leak_ids}
        if target_list_id is not None:
            data["target_list_id"] = target_list_id
        return await self._request("POST", "/unlock", json=data)

    # -------------------------
    # Exports
    # -------------------------

    async def list_exports(
        self, page: int = 1, page_size: int = 20, export_id: Optional[int] = None
    ) -> _types.PaginatedResponseExportBase:
        """
        Your export jobs, most recent first, with their status ("PENDING", "IN_PROGRESS",
        "COMPLETED" or "ERROR") and, once completed, the download URL of a ZIP in `filename`
        (empty when there was nothing to export). export_id lists that export only (no item
        when it is unknown). wait_for_export() and download_export() build on it.

        API: list_exports, https://docs.leakradar.io/#tag/exports/list_exports
        """
        params = {"page": page, "page_size": page_size, "export_id": export_id}
        return await self._request("GET", "/exports", params=params)

    # -------------------------
    # Notification methods / notifications / runs
    # -------------------------

    async def list_notification_methods(self) -> List[_types.NotificationMethodOut]:
        """
        Your notification channels.

        API: list_alert_channels, https://docs.leakradar.io/#tag/alert-channels/list_alert_channels
        """
        return await self._request("GET", "/notification_methods")

    async def create_notification_method(self, data: Dict[str, Any]) -> _types.NotificationMethodOut:
        """
        Create a notification channel (email, Slack, Discord, Telegram or webhook).

        API: create_alert_channel, https://docs.leakradar.io/#tag/alert-channels/create_alert_channel
        """
        return await self._request("POST", "/notification_methods", json=data)

    async def update_notification_method(self, method_id: int, data: Dict[str, Any]) -> _types.NotificationMethodOut:
        """
        Update a notification channel (a new destination also resumes a suspended one).

        API: update_alert_channel, https://docs.leakradar.io/#tag/alert-channels/update_alert_channel
        """
        return await self._request("PATCH", f"/notification_methods/{self._seg(method_id)}", json=data)

    async def delete_notification_method(self, method_id: int) -> _types.MessageResponse:
        """
        Delete a notification channel.

        API: delete_alert_channel, https://docs.leakradar.io/#tag/alert-channels/delete_alert_channel
        """
        return await self._request("DELETE", f"/notification_methods/{self._seg(method_id)}")

    async def set_notification_method_active(self, method_id: int, is_active: bool) -> _types.NotificationMethodOut:
        """
        Pause (False) or resume (True) a notification method. Resuming also clears a suspension
        caused by repeated delivery failures.

        API: set_alert_channel_active, https://docs.leakradar.io/#tag/alert-channels/set_alert_channel_active
        """
        return await self._request(
            "PATCH", f"/notification_methods/{self._seg(method_id)}/active", json={"is_active": is_active}
        )

    async def test_notification_method(self, data: Dict[str, Any]) -> _types.NotificationMethodTestResponse:
        """
        Send a test message to a channel configuration before saving it.

        API: test_alert_channel, https://docs.leakradar.io/#tag/alert-channels/test_alert_channel
        """
        return await self._request("POST", "/notification_methods/test", json=data)

    async def test_notification_method_with_data(
        self,
        method_id: int,
        notification_type: str = "leak",
        data: Optional[Dict[str, Any]] = None,
    ) -> _types.NotificationMethodTestResponse:
        """
        Send a test notification with realistic fake data to an existing method.
        notification_type: "leak" (default), "raw", "dark_web" or "all" (one check of an
        all-datasets monitor: one message with every dataset, except on a webhook channel, which
        receives one POST per dataset, four in this test). Rate limit: 30 tests per hour.

        `data` is deprecated: the API reads no body. A dict passed as second argument (0.1.x style)
        still works and sends the same test as before ("leak"), unless it holds a valid
        "notification_type" (one of the four above).

        API: send_alert_channel_test, https://docs.leakradar.io/#tag/alert-channels/send_alert_channel_test
        """
        if isinstance(notification_type, dict):
            data, notification_type = notification_type, "leak"
        if data is not None:
            warnings.warn(
                "test_notification_method_with_data(data=...) is deprecated: the API reads no body, "
                "pass notification_type instead.",
                DeprecationWarning,
                stacklevel=2,
            )
            # Only an explicit, valid type is honoured: a 0.1.x dict often carries the method's own
            # "type" (email, slack...), which the API never read and would now reject.
            if data.get("notification_type") in self._TEST_NOTIFICATION_TYPES:
                notification_type = data["notification_type"]
        return await self._request(
            "POST",
            f"/notification_methods/{self._seg(method_id)}/test-with-data",
            params={"notification_type": notification_type},
        )

    async def list_notifications(self) -> List[_types.NotificationOut]:
        """
        Your monitorings.

        API: list_monitors, https://docs.leakradar.io/#tag/notifications/list_monitors
        """
        return await self._request("GET", "/notifications")

    async def create_notification(
        self, data: Dict[str, Any]
    ) -> Union[List[_types.NotificationOut], _types.NotificationOut]:
        """
        Create a monitoring bound to a notification method.
        type: email, domain, advanced, raw or dark_web. A bulk creation returns a list.

        - dataset: "stealer_logs" (default) or "combolist"; "all" (email and domain monitors,
          paid plans) covers every dataset of your plan in one monitor, one alert per check
          (see `coverage` in the answer).
        - absorb_ids: when creating one "all" monitor, IDs of your monitors of the same value
          to merge into it: their history moves over and they are deleted.
        - max_points: daily points budget of auto_unlock, 10,000 when omitted or 0; required
          for an "all" monitor with auto_unlock.
        - A value already monitored on the same channel raises ConflictError with code
          "monitor_overlap": its `conflicts` list the monitors concerned. Extend one with
          extend_monitor_to_all_datasets(), or create the "all" monitor with absorb_ids.

        API: create_monitor, https://docs.leakradar.io/#tag/notifications/create_monitor
        """
        return await self._request("POST", "/notifications", json=data)

    async def update_notification(self, notification_id: int, data: Dict[str, Any]) -> _types.NotificationOut:
        """
        Update one monitoring. Same fields as create_notification(); an omitted max_points keeps
        the current budget, an omitted dataset the current dataset. An overlap with another
        monitor of the channel raises ConflictError "monitor_overlap" (see `conflicts`).

        API: update_monitor, https://docs.leakradar.io/#tag/notifications/update_monitor
        """
        return await self._request("PATCH", f"/notifications/{self._seg(notification_id)}", json=data)

    async def delete_notification(self, notification_id: int) -> _types.MessageResponse:
        """
        Delete one monitoring.

        API: delete_monitor, https://docs.leakradar.io/#tag/notifications/delete_monitor
        """
        return await self._request("DELETE", f"/notifications/{self._seg(notification_id)}")

    async def delete_notifications_bulk(self, ids: List[int]) -> _types.MessageResponse:
        """
        Delete several monitorings at once.

        API: delete_monitors, https://docs.leakradar.io/#tag/notifications/delete_monitors
        """
        return await self._request("DELETE", "/notifications/bulk", json={"ids": ids})

    async def set_notification_active(self, notification_id: int, is_active: bool) -> _types.NotificationActiveResponse:
        """
        Pause (False) or resume (True) one monitoring.

        API: set_monitor_active, https://docs.leakradar.io/#tag/notifications/set_monitor_active
        """
        return await self._request(
            "PATCH", f"/notifications/{self._seg(notification_id)}/active", json={"is_active": is_active}
        )

    async def extend_monitor_to_all_datasets(
        self,
        notification_id: int,
        absorb_ids: Optional[List[int]] = None,
        reactivate: Optional[bool] = None,
    ) -> _types.NotificationOut:
        """
        Turn an email or domain monitor into an all-datasets monitor (dataset "all", paid
        plans): it keeps its channel, settings and history and covers every dataset of your
        plan. Returns the monitor.

        - absorb_ids: up to 10 other monitors of the same value to merge into it: their
          history moves over and they are DELETED.
        - reactivate=True also resumes the monitor if it is paused.
        - ConflictError: "already_all", or "monitor_overlap" with another monitor of the
          channel (see `conflicts`).

        API: extend_monitor_to_all_datasets, https://docs.leakradar.io/#tag/notifications/extend_monitor_to_all_datasets
        """
        body = self._clean({"absorb_ids": absorb_ids, "reactivate": reactivate}) or {}
        return await self._request("POST", f"/notifications/{self._seg(notification_id)}/scope_all", json=body)

    async def extend_monitors_to_all_datasets(
        self, ids: List[int], merge: Optional[bool] = None
    ) -> _types.ScopeAllBulkResponse:
        """
        Turn several email or domain monitors (at most 200) into all-datasets monitors.

        merge (True on the API when omitted): the monitors of the same value on the same channel
        become ONE monitor, the oldest; the others are DELETED and their history moves over.
        Returns `converted` (IDs), `merged` ({"kept", "absorbed"}) and `skipped` ({"id",
        "reason"}: "not_found", "not_eligible", "overlap", "free", "invalid_value",
        "blacklisted" or "max_points_required").

        API: extend_monitors_to_all_datasets, https://docs.leakradar.io/#tag/notifications/extend_monitors_to_all_datasets
        """
        body = self._clean({"ids": self._as_list(ids), "merge": merge})
        return await self._request("POST", "/notifications/scope_all/bulk", json=body)

    async def get_notifications_stats(self) -> _types.NotificationStatsResponse:
        """
        Monitored assets usage and limits of the account.

        API: get_monitoring_stats, https://docs.leakradar.io/#tag/notifications/get_monitoring_stats
        """
        return await self._request("GET", "/notifications/stats")

    async def list_notification_runs(
        self,
        page: int = 1,
        page_size: int = 20,
        dataset: Optional[str] = None,
        status: Optional[str] = None,
        sent_after: Optional[DateLike] = None,
        sent_before: Optional[DateLike] = None,
        notification_id: Optional[int] = None,
        q: Optional[str] = None,
    ) -> _types.PaginatedNotificationRuns:
        """
        List monitoring runs, most recent first.

        - dataset: "stealer_logs", "combolist", "databases", "raw_files" or "dark_web".
        - sent_after is inclusive, sent_before exclusive.
        - q: a run or asset number ("123" or "#123"), or text of the monitored asset.

        API: list_monitoring_runs, https://docs.leakradar.io/#tag/notifications-runs/list_monitoring_runs
        """
        params = self._clean(
            {
                "page": page,
                "page_size": page_size,
                "dataset": dataset,
                "status": status,
                "sent_after": sent_after,
                "sent_before": sent_before,
                "notification_id": notification_id,
                "q": q,
            }
        )
        return await self._request("GET", "/notification_runs", params=params)

    async def get_notification_run(self, run_id: int) -> _types.NotificationRunBase:
        """
        One monitoring run (the page alert emails and webhooks link to). Its results: get_notification_run_items().

        API: get_monitoring_run, https://docs.leakradar.io/#tag/notifications-runs/get_monitoring_run
        """
        return await self._request("GET", f"/notification_runs/{self._seg(run_id)}")

    async def get_notification_run_items(
        self, run_id: int, page: int = 1, page_size: int = 100
    ) -> _types.PaginatedRunItemsResponse:
        """
        Results of a monitoring run, whatever its type (credentials, raw files, dark web posts). `item_type` tells which.

        API: list_monitoring_run_items, https://docs.leakradar.io/#tag/notifications-runs/list_monitoring_run_items
        """
        params = self._clean({"page": page, "page_size": page_size})
        return await self._request("GET", f"/notification_runs/{self._seg(run_id)}/items", params=params)

    async def notification_run_leaks(
        self,
        run_id: int,
        page: int = 1,
        page_size: int = 100,
        search: Optional[str] = None,
        is_email: Optional[bool] = None,
    ) -> _types.PaginatedRunItemsResponse:
        """
        Deprecated: the API replaced /notification_runs/{id}/leaks with get_notification_run_items().

        API: list_monitoring_run_items, https://docs.leakradar.io/#tag/notifications-runs/list_monitoring_run_items
        """
        message = "notification_run_leaks() is deprecated, use get_notification_run_items()"
        if search is not None or is_email is not None:
            message += " (search and is_email are no longer supported and are ignored)"
        warnings.warn(message, DeprecationWarning, stacklevel=2)
        return await self.get_notification_run_items(run_id, page=page, page_size=page_size)

    async def set_notification_run_status(self, run_id: int, status: str) -> _types.StatusSuccessResponse:
        """
        Set the triage status of a run ("new", "in_progress", "fixed", "accepted_risk").
        The status is propagated to every unlocked leak of the run.

        API: set_monitoring_run_status, https://docs.leakradar.io/#tag/notifications-runs/set_monitoring_run_status
        """
        return await self._request("PUT", f"/notification_runs/{self._seg(run_id)}/status", json={"status": status})

    async def unlock_notification_run_leaks(
        self,
        run_id: int,
        max_leaks: Optional[int] = None,
        search: Optional[str] = None,
        is_email: Optional[bool] = None,
        list_id: Optional[int] = None,
    ) -> List[Union[_types.LeakDetails, _types.CombolistUnlockResult, _types.DatabaseResult]]:
        """
        Unlock the leaks of a monitoring run (capped by max_leaks and your points).

        API: unlock_monitoring_run, https://docs.leakradar.io/#tag/notifications-runs/unlock_monitoring_run
        """
        params = self._clean({"max": max_leaks, "search": search, "is_email": is_email, "list_id": list_id})
        return await self._request("POST", f"/notification_runs/{self._seg(run_id)}/leaks/unlock", params=params)

    async def export_notification_run(
        self,
        run_id: int,
        search: Optional[str] = None,
        is_email: Optional[bool] = None,
        format: Optional[str] = None,
    ) -> _types.ExportResponse:
        """
        Queue an export (csv|txt|json, default csv) of the leaks of one monitoring run.

        API: export_monitoring_run, https://docs.leakradar.io/#tag/notifications-runs/export_monitoring_run
        """
        params = self._clean({"search": search, "is_email": is_email, "format": format})
        return await self._request("GET", f"/notification_runs/{self._seg(run_id)}/export", params=params)

    async def export_notification_runs(
        self,
        run_ids: Optional[List[int]] = None,
        filters: Optional[Dict[str, Any]] = None,
        exclude_run_ids: Optional[List[int]] = None,
        format: Optional[str] = None,
    ) -> _types.ExportResponse:
        """
        Queue one export (a ZIP with a file per run plus summary.json) of several monitoring runs.

        - Give either run_ids, or filters (the keys of list_notification_runs(): dataset, status,
          sent_after, sent_before, notification_id, q). Filters select Stealer Logs and Combolist runs only.
        - exclude_run_ids deselects runs from a filter selection.
        - At most 1000 runs; only unlocked credentials are included, no points are spent.

        API: export_monitoring_runs, https://docs.leakradar.io/#tag/notifications-runs/export_monitoring_runs
        """
        if (run_ids is None) == (filters is None):
            raise ValueError("Provide either run_ids or filters")
        if run_ids is not None and exclude_run_ids:
            raise ValueError("exclude_run_ids requires filters")
        body = self._clean(
            {"run_ids": run_ids, "filters": filters, "exclude_run_ids": exclude_run_ids, "format": format}
        )
        return await self._request("POST", "/notification_runs/export", json=body)

    # -------------------------
    # Raw search / containers / downloads
    # -------------------------

    async def raw_search(
        self,
        q: Optional[str] = None,
        page: int = 1,
        page_size: int = 10,
        container_id: Optional[int] = None,
        exts: Optional[StrOrList] = None,
        categories: Optional[StrOrList] = None,
        file_name: Optional[StrOrList] = None,
        *,
        cursor: Optional[str] = None,
        dedup: Optional[bool] = None,
        q_exact: Optional[bool] = None,
        exts_not: Optional[StrOrList] = None,
        categories_not: Optional[StrOrList] = None,
        file_name_not: Optional[StrOrList] = None,
        file_name_match_type: Optional[str] = None,
        file_name_not_match_type: Optional[str] = None,
        folder_name: Optional[StrOrList] = None,
        folder_name_not: Optional[StrOrList] = None,
        folder_name_match_type: Optional[str] = None,
        folder_name_not_match_type: Optional[str] = None,
        force_and: Optional[bool] = None,
        include_terms: Optional[StrOrList] = None,
        exclude_terms: Optional[StrOrList] = None,
        ingested_at_min: Optional[DateLike] = None,
        ingested_at_max: Optional[DateLike] = None,
    ) -> _types.RawSearchResponse:
        """
        Full-text search across raw leak blocks.

        - q: 4+ characters (quotes ignored). Without q, give at least one selector: container_id,
          exts, exts_not, categories, categories_not, file_name, file_name_not, folder_name,
          include_terms or ingested_at_* (folder_name_not and exclude_terms only narrow a
          selection).
        - q_exact=True matches q on word boundaries ("4.4.4.4" no longer matches "4.4.4.400").
        - include_terms must ALL be present on top of q; exclude_terms drop a block.
        - file_name/folder_name `*_match_type`: "contains" (default), "starts_with", "ends_with" or "exact".
        - force_and=True requires every file_name / folder_name value instead of any.
        - page: echoed back, not used by the API: raw results are paged by cursor only.
        - cursor: the `next_cursor` of the previous answer, the only way to the next page (while
          `has_more` is true). paginate() follows it.
        - dedup=True folds blocks with identical content (per page).

        API: search_raw, https://docs.leakradar.io/#tag/raw-search/search_raw
        """
        payload = self._raw_filters(locals())
        params = self._clean({"page": page, "page_size": page_size, "cursor": cursor, "dedup": dedup})
        return await self._request("POST", "/search/raw", params=params, json=payload)

    async def raw_count(
        self,
        q: Optional[str] = None,
        container_id: Optional[int] = None,
        exts: Optional[StrOrList] = None,
        categories: Optional[StrOrList] = None,
        file_name: Optional[StrOrList] = None,
        *,
        dedup: Optional[bool] = None,
        q_exact: Optional[bool] = None,
        exts_not: Optional[StrOrList] = None,
        categories_not: Optional[StrOrList] = None,
        file_name_not: Optional[StrOrList] = None,
        file_name_match_type: Optional[str] = None,
        file_name_not_match_type: Optional[str] = None,
        folder_name: Optional[StrOrList] = None,
        folder_name_not: Optional[StrOrList] = None,
        folder_name_match_type: Optional[str] = None,
        folder_name_not_match_type: Optional[str] = None,
        force_and: Optional[bool] = None,
        include_terms: Optional[StrOrList] = None,
        exclude_terms: Optional[StrOrList] = None,
        ingested_at_min: Optional[DateLike] = None,
        ingested_at_max: Optional[DateLike] = None,
    ) -> _types.RawCountResponse:
        """
        Count the raw blocks matching the filters of raw_search(): the most parts (1 point each)
        a mass unlock would consume. Heavier than a search page: call it on demand, not per search.

        `exact` False means `total` is an upper bound (q_exact / dedup shrink the real set);
        `capped` True means `total` is a lower bound.

        API: count_raw, https://docs.leakradar.io/#tag/raw-search/count_raw
        """
        payload = self._raw_filters(locals())
        params = self._clean({"dedup": dedup})
        return await self._request("POST", "/search/raw/count", params=params, json=payload)

    async def queue_raw_unlock_task(
        self,
        q: Optional[str] = None,
        container_id: Optional[int] = None,
        exts: Optional[StrOrList] = None,
        categories: Optional[StrOrList] = None,
        file_name: Optional[StrOrList] = None,
        *,
        max_leaks: Optional[int] = None,
        dedup: Optional[bool] = None,
        q_exact: Optional[bool] = None,
        exts_not: Optional[StrOrList] = None,
        categories_not: Optional[StrOrList] = None,
        file_name_not: Optional[StrOrList] = None,
        file_name_match_type: Optional[str] = None,
        file_name_not_match_type: Optional[str] = None,
        folder_name: Optional[StrOrList] = None,
        folder_name_not: Optional[StrOrList] = None,
        folder_name_match_type: Optional[str] = None,
        folder_name_not_match_type: Optional[str] = None,
        force_and: Optional[bool] = None,
        include_terms: Optional[StrOrList] = None,
        exclude_terms: Optional[StrOrList] = None,
        ingested_at_min: Optional[DateLike] = None,
        ingested_at_max: Optional[DateLike] = None,
    ) -> _types.UnlockTaskQueuedResponse:
        """
        Queue a mass unlock of the raw blocks matching the filters of raw_search().

        - max_leaks: cap of new unlocks; 0 or None uses all your points. Estimate with raw_count().
        - dedup=True unlocks each identical content once.
        - One running and one queued raw task per account; an identical request returns the existing task.
        Poll the returned task_id with get_task_status().

        API: queue_raw_unlock, https://docs.leakradar.io/#tag/raw-search/queue_raw_unlock
        """
        payload = self._raw_filters(locals())
        params = self._clean({"max": max_leaks, "dedup": dedup})
        return await self._request("POST", "/search/raw/unlock/task", params=params, json=payload)

    async def raw_export_preview(
        self,
        export: str,
        q: Optional[str] = None,
        container_id: Optional[int] = None,
        exts: Optional[List[str]] = None,
        categories: Optional[List[str]] = None,
        file_name: Optional[str] = None,
    ) -> _types.RawCountResponse:
        """
        Deprecated: the API removed /search/raw/export/preview. Returns raw_count() for the same filters.

        API: count_raw, https://docs.leakradar.io/#tag/raw-search/count_raw
        """
        warnings.warn(
            "raw_export_preview() is deprecated: the API removed the export preview, use raw_count()",
            DeprecationWarning,
            stacklevel=2,
        )
        if export not in {"rows", "parts"}:
            raise ValueError('export must be "rows" or "parts"')
        return await self.raw_count(
            q=q, container_id=container_id, exts=exts, categories=categories, file_name=file_name
        )

    async def raw_export(
        self,
        export: str,
        q: Optional[str] = None,
        container_id: Optional[int] = None,
        exts: Optional[StrOrList] = None,
        categories: Optional[StrOrList] = None,
        file_name: Optional[StrOrList] = None,
        *,
        dedup: Optional[bool] = None,
        q_exact: Optional[bool] = None,
        exts_not: Optional[StrOrList] = None,
        categories_not: Optional[StrOrList] = None,
        file_name_not: Optional[StrOrList] = None,
        file_name_match_type: Optional[str] = None,
        file_name_not_match_type: Optional[str] = None,
        folder_name: Optional[StrOrList] = None,
        folder_name_not: Optional[StrOrList] = None,
        folder_name_match_type: Optional[str] = None,
        folder_name_not_match_type: Optional[str] = None,
        force_and: Optional[bool] = None,
        include_terms: Optional[StrOrList] = None,
        exclude_terms: Optional[StrOrList] = None,
        ingested_at_min: Optional[DateLike] = None,
        ingested_at_max: Optional[DateLike] = None,
    ) -> _types.ExportResponse:
        """
        Queue a raw export: export="rows" (CSV of matching lines) or "parts" (text snapshot of parts).
        Only blocks you already unlocked are exported (queue_raw_unlock_task() first); no points are spent.
        dedup=True drops duplicate lines (rows) or identical blocks (parts).

        API: export_raw, https://docs.leakradar.io/#tag/raw-search/export_raw
        """
        if export not in {"rows", "parts"}:
            raise ValueError('export must be "rows" or "parts"')
        payload = self._raw_filters(locals())
        params = self._clean({"export": export, "dedup": dedup})
        return await self._request("POST", "/search/raw/export", params=params, json=payload)

    async def raw_list_parts(
        self, container_id: int, entry_path: str, page: int = 1, page_size: int = 100
    ) -> _types.PartsResponse:
        """
        List the parts of a raw entry.

        API: list_raw_parts, https://docs.leakradar.io/#tag/raw-search/list_raw_parts
        """
        params = self._clean({"container_id": container_id, "entry_path": entry_path, "page": page, "page_size": page_size})
        return await self._request("GET", "/search/raw/parts", params=params)

    async def raw_get_part(
        self,
        container_id: int,
        entry_path: str,
        seq: int,
        trim_overlap: bool = False,
        overlap_chars: int = 256,
        auto_unlock: bool = False,
    ) -> _types.PartResponse:
        """
        Read one part of a raw entry. auto_unlock=True unlocks it with your points if needed.

        API: get_raw_part, https://docs.leakradar.io/#tag/raw-search/get_raw_part
        """
        params = self._clean(
            {
                "container_id": container_id,
                "entry_path": entry_path,
                "seq": seq,
                "trim_overlap": trim_overlap,
                "overlap_chars": overlap_chars,
                "auto_unlock": auto_unlock,
            }
        )
        return await self._request("GET", "/search/raw/part", params=params)

    async def container_tree(
        self, container_id: int, prefix: str = "", page: int = 1, page_size: int = 200
    ) -> _types.ContainerTreeResponse:
        """
        Browse the file tree of a raw container.

        API: browse_container_tree, https://docs.leakradar.io/#tag/raw-search/browse_container_tree
        """
        params = self._clean({"container_id": container_id, "prefix": prefix, "page": page, "page_size": page_size})
        return await self._request("GET", "/container/tree", params=params)

    async def container_subfolders(
        self, container_id: int, prefix: str = "", page: int = 1, page_size: int = 1000
    ) -> _types.SubfoldersResponse:
        """
        List the immediate subfolders of a container folder.

        API: list_container_subfolders, https://docs.leakradar.io/#tag/raw-search/list_container_subfolders
        """
        params = self._clean({"container_id": container_id, "prefix": prefix, "page": page, "page_size": page_size})
        return await self._request("GET", "/container/subfolders", params=params)

    async def container_resolve_path(
        self, container_id: int, entry_path: str = "", options: Optional[Dict[str, Any]] = None
    ) -> _types.ResolvePathResponse:
        """
        Resolve a folder chain inside a container and prefetch its contents.

        API: resolve_container_path, https://docs.leakradar.io/#tag/raw-search/resolve_container_path
        """
        payload: Dict[str, Any] = {"container_id": container_id, "entry_path": entry_path}
        if options is not None:
            payload["options"] = options
        return await self._request("POST", "/container/tree/resolve_path", json=payload)

    async def container_file_info(self, container_id: int, entry_path: str) -> _types.RawFileInfoResponse:
        """
        Metadata of one raw entry (path inside its container).

        API: get_container_file_info, https://docs.leakradar.io/#tag/raw-search/get_container_file_info
        """
        params = {"container_id": container_id, "entry_path": entry_path}
        return await self._request("GET", "/container/file_info", params=params)

    async def list_raw_files(self) -> List[_types.RawFileAddedSummary]:
        """
        Raw files fully indexed and searchable.

        API: list_raw_files, https://docs.leakradar.io/#tag/raw-search/list_raw_files
        """
        return await self._request("GET", "/raw/files")

    async def raw_download_preview(
        self,
        sha256_original: Optional[str] = None,
        container_id: Optional[int] = None,
        entry_path: Optional[str] = None,
    ) -> _types.RawDownloadPreviewResponse:
        """
        Cost (GB quota) of downloading a raw file, before downloading it.

        API: preview_raw_download, https://docs.leakradar.io/#tag/raw-search/preview_raw_download
        """
        params = self._clean({"sha256_original": sha256_original, "container_id": container_id, "entry_path": entry_path})
        return await self._request("GET", "/raw/download/preview", params=params)

    async def raw_download_source(
        self,
        sha256_original: Optional[str] = None,
        container_id: Optional[int] = None,
        entry_path: Optional[str] = None,
        expires_in: Optional[int] = 900,
    ) -> _types.DownloadSourceResponse:
        """
        Generate a download URL for a raw file (spends GB quota). expires_in: 60-3600 s.

        API: create_raw_download, https://docs.leakradar.io/#tag/raw-search/create_raw_download
        """
        payload = self._clean(
            {
                "sha256_original": sha256_original,
                "container_id": container_id,
                "entry_path": entry_path,
                "expires_in": expires_in,
            }
        ) or {}
        return await self._request("POST", "/raw/download", json=payload)

    async def list_raw_downloads(
        self,
        page: int = 1,
        page_size: int = 20,
        search: Optional[str] = None,
        status: Optional[str] = None,
        file_id: Optional[int] = None,
    ) -> _types.PaginatedResponseDownloadLogItem:
        """
        Raw files you unlocked for download. search: file name or container ID; status:
        "available" or "expired"; file_id: that unlocked raw file only.

        API: list_raw_downloads, https://docs.leakradar.io/#tag/profile-downloads/list_raw_downloads
        """
        params = self._clean(
            {"page": page, "page_size": page_size, "search": search, "status": status, "file_id": file_id}
        )
        return await self._request("GET", "/profile/raw/downloads", params=params)

    async def list_unlocked_raw_parts(
        self, page: int = 1, page_size: int = 20, q: Optional[str] = None
    ) -> _types.PaginatedResponseRawUnlockedPartItem:
        """
        Raw file entries you unlocked, most recent first. q filters on file name, path, source or category.

        API: list_unlocked_raw_parts, https://docs.leakradar.io/#tag/profile-downloads/list_unlocked_raw_parts
        """
        params = self._clean({"page": page, "page_size": page_size, "q": q})
        return await self._request("GET", "/profile/raw/unlocked-parts", params=params)

    async def get_raw_download_url(self, download_id: int, token: Optional[str] = None) -> str:
        """
        Presigned URL of a raw file download, read from the API redirect (nothing is downloaded).

        - download_id: the number in the `download_url` of a list_raw_downloads() item
          ("/profile/raw/downloads/<download_id>/file?token=..."), not the item's `id`.
        - token: that item's `download_token` (also in its `download_url`). It authenticates the
          call on its own until the download expires: pass it when the API key is not the
          account owner's (a team member's key) or when calling without a key.

        API: download_raw_file, https://docs.leakradar.io/#tag/profile-downloads/download_raw_file
        """
        params = self._clean({"token": token})
        req_headers = self._default_headers()
        response = await self._send(
            "POST",
            f"/profile/raw/downloads/{self._seg(download_id)}/file",
            params=params,
            headers=req_headers,
            follow_redirects=False,
        )

        if response.status_code in {301, 302, 303, 307, 308}:
            url = response.headers.get("location") or response.headers.get("Location")
            if not url:
                raise LeakRadarAPIError(response.status_code, "Missing Location header for download redirect.")
            return url

        ct = response.headers.get("content-type", "")
        if ct.startswith("application/json") and response.text:
            try:
                body = _json.loads(response.text)
                if isinstance(body, dict) and isinstance(body.get("url"), str):
                    return body["url"]
            except Exception:
                pass

        raise LeakRadarAPIError(response.status_code, "Unexpected response while requesting raw download URL.")

    async def download_raw_file(self, download_id: int, token: Optional[str] = None) -> bytes:
        """
        Download a raw file you have access to and return its bytes. download_id and token as in
        get_raw_download_url(): the number in the `download_url` of a list_raw_downloads() item
        (not its `id`), and its `download_token` (needed with a team member's key).

        API: download_raw_file, https://docs.leakradar.io/#tag/profile-downloads/download_raw_file
        """
        url = await self.get_raw_download_url(download_id=download_id, token=token)
        # Under the lock: two first downloads at once must not each open a client (one would never be closed).
        with self._gate_lock:
            if self._anon_client is None:
                self._anon_client = httpx.AsyncClient(
                    headers={"User-Agent": self.user_agent, "Accept": "*/*"},
                    timeout=self._timeout,
                    verify=self._verify,
                    proxy=self._proxy,
                )
            anon_client = self._anon_client
        resp = await anon_client.get(url, follow_redirects=True)
        if resp.status_code >= 400:
            raise LeakRadarAPIError(resp.status_code, resp.text or "Raw file download failed.")
        return resp.content

    # -------------------------
    # Account: usage, preferences, activity
    # -------------------------

    async def get_usage(self) -> _types.PlanUsageResponse:
        """
        Every plan limit with its current consumption (owner-scoped for team members).
        `limit: null` means unlimited, `used: null` means momentarily unreadable. Costs no quota.

        API: get_usage, https://docs.leakradar.io/#tag/usage-and-limits/get_usage
        """
        return await self._request("GET", "/profile/usage")

    async def rate_limits(self) -> _types.RateLimitsResponse:
        """
        The rate limits that apply to this credential right now. Costs no quota and is never
        rate limited, so it does not wait for the client's retry gate.

        Returns `lane` ("ui" for the dashboard, "auto" for API keys and connected apps),
        `credential_kind`, `families` (per family: `per_second`, `burst`, `scope`), `daily`
        (`limit`, `used`, `retry_at`, `legacy`; `limit: null` means no daily cap), `concurrency`
        (`raw` and `advanced`: `limit`, `inflight`), `pauses` in progress (`family`, `level`,
        `until`), `last_24h` (`total`, `by_reason`) and `last_7d` (per day: `day`, `total`,
        `by_reason`).

        API: get_rate_limits, https://docs.leakradar.io/#tag/usage-and-limits/get_rate_limits
        """
        return await self._request("GET", "/rate-limits")

    async def get_preferences(self) -> _types.UserPreferencesRead:
        """
        Your account preferences (password redaction, interface language).

        API: get_preferences, https://docs.leakradar.io/#tag/profile/get_preferences
        """
        return await self._request("GET", "/profile/preferences")

    async def update_preferences(
        self,
        hide_passwords: Optional[bool] = None,
        language: Optional[str] = None,
    ) -> _types.UserPreferencesRead:
        """
        Partial update: only the arguments you pass are changed. language: one of the 13 UI locales ("en", "fr", ...).

        API: update_preferences, https://docs.leakradar.io/#tag/profile/update_preferences
        """
        payload = self._clean({"hide_passwords": hide_passwords, "language": language}) or {}
        return await self._request("PATCH", "/profile/preferences", json=payload)

    async def list_balance_logs(
        self,
        page: int = 1,
        page_size: int = 20,
        order_by: Optional[str] = None,
        order_dir: Optional[str] = None,
        q: Optional[str] = None,
        kind: Optional[str] = None,
        direction: Optional[str] = None,
        date_from: Optional[DateLike] = None,
        date_to: Optional[DateLike] = None,
    ) -> _types.PaginatedResponseBalanceLogItem:
        """
        Balance history (points and GB). kind: "points" or "gb"; direction: "credit" or "debit"; dates as YYYY-MM-DD.

        API: list_balance_history, https://docs.leakradar.io/#tag/usage-and-limits/list_balance_history
        """
        params = self._clean(
            {
                "page": page,
                "page_size": page_size,
                "order_by": order_by,
                "order_dir": order_dir,
                "q": q,
                "kind": kind,
                "direction": direction,
                "date_from": date_from,
                "date_to": date_to,
            }
        )
        return await self._request("GET", "/profile/activities/balance-logs", params=params)

    async def list_task_history(
        self,
        page: int = 1,
        page_size: int = 20,
        order_by: Optional[str] = None,
        order_dir: Optional[str] = None,
        q: Optional[str] = None,
        status: Optional[str] = None,
        kind: Optional[str] = None,
        scope: Optional[str] = None,
        date_from: Optional[DateLike] = None,
        date_to: Optional[DateLike] = None,
    ) -> _types.PaginatedResponseUserTaskItem:
        """
        Background unlock tasks of the account. status: "QUEUED", "RUNNING", "COMPLETED", "FAILED"
        or "CANCELED"; dates as YYYY-MM-DD.

        API: list_task_history, https://docs.leakradar.io/#tag/usage-and-limits/list_task_history
        """
        params = self._clean(
            {
                "page": page,
                "page_size": page_size,
                "order_by": order_by,
                "order_dir": order_dir,
                "q": q,
                "status": status,
                "kind": kind,
                "scope": scope,
                "date_from": date_from,
                "date_to": date_to,
            }
        )
        return await self._request("GET", "/profile/activities/tasks", params=params)

    # -------------------------
    # Team
    # -------------------------

    async def get_team(self) -> _types.TeamOverviewResponse:
        """
        Your team: owner, members, pending invitations, plan and member quota.

        API: get_team, https://docs.leakradar.io/#tag/team/get_team
        """
        return await self._request("GET", "/profile/team")

    async def create_team_invitation(
        self, email: str, name: Optional[str] = None, role: str = "member"
    ) -> _types.TeamInvitationResponse:
        """
        Invite someone to your team by email.

        API: invite_team_member, https://docs.leakradar.io/#tag/team/invite_team_member
        """
        payload = self._clean({"email": email, "name": name, "role": role}) or {"email": email, "role": role}
        return await self._request("POST", "/profile/team/invitations", json=payload)

    async def resend_team_invitation(self, invitation_id: int) -> _types.TeamInvitationResponse:
        """
        Send a team invitation email again.

        API: resend_team_invitation, https://docs.leakradar.io/#tag/team/resend_team_invitation
        """
        return await self._request("POST", f"/profile/team/invitations/{self._seg(invitation_id)}/resend")

    async def revoke_team_invitation(self, invitation_id: int) -> _types.TeamInvitationResponse:
        """
        Cancel a pending team invitation.

        API: revoke_team_invitation, https://docs.leakradar.io/#tag/team/revoke_team_invitation
        """
        return await self._request("POST", f"/profile/team/invitations/{self._seg(invitation_id)}/revoke")

    async def update_team_member_role(self, member_id: int, team_role: str) -> _types.TeamMemberResponse:
        """
        Change the role of a team member.

        API: update_team_member_role, https://docs.leakradar.io/#tag/team/update_team_member_role
        """
        return await self._request(
            "PATCH", f"/profile/team/members/{self._seg(member_id)}", json={"team_role": team_role}
        )

    async def delete_team_member(self, member_id: int) -> _types.TeamMemberResponse:
        """
        Remove a member from your team.

        API: remove_team_member, https://docs.leakradar.io/#tag/team/remove_team_member
        """
        return await self._request("DELETE", f"/profile/team/members/{self._seg(member_id)}")

    async def leave_team(self) -> _types.MessageResponse:
        """
        Leave the team you are a member of (not available to the team owner).

        API: leave_team, https://docs.leakradar.io/#tag/team/leave_team
        """
        return await self._request("POST", "/profile/team/leave")

    # -------------------------
    # Waiting for tasks and exports, pagination
    # -------------------------

    async def wait_for_task(
        self,
        task_id: Union[str, int, Mapping[str, Any]],
        *,
        interval: float = 2.0,
        timeout: Optional[float] = None,
        on_progress: Optional[Callable[[Dict[str, Any]], Any]] = None,
    ) -> _types.TaskStatusOut:
        """
        Wait for a background task (the unlock task of any search family) and return its final
        status, read with get_task_status() every `interval` seconds.

        :param task_id: The task ID, or the dict a queue_*_unlock_task() method returned.
        :param interval: Seconds between two reads.
        :param timeout: Seconds after which WaitTimeoutError is raised (None: as long as it
            takes). The task keeps running on the server.
        :param on_progress: Called with each status read (`updated` items out of `total`),
            the last one included; a coroutine function is awaited.
        :raises TaskFailedError: The task ended "FAILED" (`code` and `message` say why) or
            "CANCELED". Records unlocked before a failure stay unlocked.

        API: get_task_status, https://docs.leakradar.io/#tag/tasks/get_task_status
        """
        task = await self._wait_for_task(self.get_task_status, self._task_id(task_id), interval, timeout, on_progress)
        return cast(_types.TaskStatusOut, task)

    async def wait_for_unlocked_list_task(
        self,
        task_id: Union[str, int, Mapping[str, Any]],
        *,
        interval: float = 2.0,
        timeout: Optional[float] = None,
        on_progress: Optional[Callable[[Dict[str, Any]], Any]] = None,
    ) -> _types.TaskStatusOut:
        """
        Wait for a bulk task on your unlocked records (bulk_assign_unlocked_list(),
        bulk_update_unlocked_status(), deleting or clearing a list) and return its final status,
        read with get_unlocked_list_task(). Same arguments and errors as wait_for_task(); a dict
        with several `task_ids` is waited for through its `task_id` only.

        API: get_unlocked_list_task, https://docs.leakradar.io/#tag/unlocked-leaks-lists/get_unlocked_list_task
        """
        task = await self._wait_for_task(
            self.get_unlocked_list_task, self._task_id(task_id), interval, timeout, on_progress
        )
        return cast(_types.TaskStatusOut, task)

    async def wait_for_export(
        self,
        export_id: Union[int, str, Mapping[str, Any]],
        *,
        interval: float = 3.0,
        timeout: Optional[float] = None,
        on_progress: Optional[Callable[[Dict[str, Any]], Any]] = None,
    ) -> _types.ExportBase:
        """
        Wait for an export job and return it once "COMPLETED", read every `interval` seconds
        as list_exports(export_id=...) reads it. Its `filename` is the download URL of a ZIP
        archive (empty when there was nothing to export): download_export() fetches it.

        :param export_id: The export ID, or the dict an export_*() method returned.
        :param interval: Seconds between two reads.
        :param timeout: Seconds after which WaitTimeoutError is raised (None: as long as it takes).
        :param on_progress: Called with each state read; a coroutine function is awaited.
        :raises ExportFailedError: The export ended with status "ERROR".
        :raises NotFoundError: None of your exports has this ID.

        API: list_exports, https://docs.leakradar.io/#tag/exports/list_exports
        """
        export_id = self._export_id(export_id)
        self._check_wait(interval, timeout)
        started = self._monotonic()
        while True:
            export = await self._read_export(export_id)
            await self._progress(on_progress, export)
            status = str(export.get("status") or "").upper()
            if status == "COMPLETED":
                return cast(_types.ExportBase, export)
            if status == "ERROR":
                raise ExportFailedError(export_id, status, export)
            await self._wait_again(started, interval, timeout, f"export {export_id}", export)

    async def download_export(
        self,
        export: Union[int, str, Mapping[str, Any]],
        path: Optional[Union[str, os.PathLike]] = None,
        *,
        chunk_size: int = 1048576,
    ) -> Union[bytes, str, None]:
        """
        Download the file of a completed export (a ZIP archive): its bytes, or with `path`, the
        path of the file written there. None when there was nothing to export (a raw export
        explains why in params["result"]).

        :param export: The export (an item of list_exports(), or what wait_for_export()
            returned), the dict an export_*() method returned, or the export ID.
        :param path: File to write, in chunks, without holding the archive in memory: the
            download goes to a temporary file next to it, moved into place once complete (an
            existing file is only replaced then). On POSIX the archive is readable by its
            owner only (mode 0600).
        :param chunk_size: Bytes per chunk written.
        :raises ValueError: The export is not COMPLETED yet: wait_for_export() first.

        The API key is only sent when the file is served by the API itself, never to the
        storage host of the download URL.

        API: list_exports, https://docs.leakradar.io/#tag/exports/list_exports
        """
        if isinstance(chunk_size, bool) or not isinstance(chunk_size, int) or chunk_size < 1:
            raise ValueError("chunk_size must be a positive number of bytes")
        if isinstance(export, Mapping) and "filename" in export and str(export.get("status") or "").upper() == "COMPLETED":
            item = dict(export)
        else:
            item = await self._read_export(self._export_id(export))
        status = str(item.get("status") or "").upper()
        if status != "COMPLETED":
            raise ValueError(
                f"Export {item.get('id')} is {status or 'not ready'}, not COMPLETED: wait for it with wait_for_export()."
            )
        url = item.get("filename") or ""
        if not url:
            return None
        target = httpx.URL(url)
        on_api = not target.is_absolute_url or self._same_origin(target)
        headers = self._default_headers() if on_api else self._base_headers()
        async with self._client.stream("GET", url, headers=headers, follow_redirects=True) as response:
            logger.debug(
                "LeakRadar export %s download from %s -> %d",
                item.get("id"), "the API" if on_api else target.host, response.status_code,
            )
            if response.status_code >= 400:
                await response.aread()
                raise self._error_from(response)
            if path is None:
                return await response.aread()
            destination = os.fsdecode(os.fspath(path))
            folder, name = os.path.split(destination)
            # A new file of our own next to the destination, never a name known in advance that
            # someone else could have created (or linked) first; mkstemp makes it owner-only.
            descriptor, partial = tempfile.mkstemp(prefix=name + ".", suffix=".part", dir=folder or os.curdir)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    async for chunk in response.aiter_bytes(chunk_size):
                        handle.write(chunk)
                os.replace(partial, destination)
            except BaseException:
                if os.path.exists(partial):
                    os.remove(partial)
                raise
            return destination

    async def paginate(
        self,
        method: Callable[..., Any],
        *args: Any,
        page_size: Optional[int] = None,
        max_items: Optional[int] = None,
        items_key: str = "items",
        **kwargs: Any,
    ) -> AsyncIterator[Dict[str, Any]]:
        """
        Iterate over every item of a paginated listing, one request per page:

            async for leak in client.paginate(client.get_domain_all, "example.com", page_size=1000):
                ...

        :param method: A listing method of this client, one that takes `page` (or `cursor`):
            get_domain_all, search_advanced, search_combolist_domain, get_unlocked_leaks,
            list_notification_runs, list_exports, raw_search... *args and **kwargs are its own
            arguments (`page` sets the first page, except for raw_search(), which pages by
            cursor only).
        :param page_size: Items per page (default: the method's).
        :param max_items: Stop after this many items.
        :param items_key: Key of the list in each page: "items" for every listing of the API.

        Stops on an empty page, when `has_more` is false, on a page shorter than the page size,
        once page * page_size reaches `total`, and at max_items. raw_search follows `next_cursor`
        while `has_more` is true (exact and dedup pages can come back short, or even empty).
        The results of a monitoring run (get_notification_run_items()) skip the records removed
        since the run, so a page can come back short, or even empty, before the end: they are
        read until page * page_size reaches `total`.
        The domain listings, advanced, application and combolist searches are served page by
        page only up to 1,000,000 results: the iteration stops there with a UserWarning naming
        the export that returns everything. search_dark_web stops the same way at 10,000
        results: narrow the search (date_from / date_to, sources) to get the others. Each page
        is a request: rate limits apply, and so does auto_unlock when you pass it.
        """
        if max_items is not None and (isinstance(max_items, bool) or not isinstance(max_items, int) or max_items < 0):
            raise ValueError("max_items must be None or an integer >= 0")
        if page_size is not None and (isinstance(page_size, bool) or not isinstance(page_size, int) or page_size < 1):
            raise ValueError("page_size must be a positive integer")
        name = getattr(method, "__name__", "the method")
        try:
            signature = inspect.signature(method)
        except (TypeError, ValueError):
            raise TypeError("paginate() takes a listing method of the client, such as client.get_domain_all") from None
        parameters = signature.parameters
        takes_page = "page" in parameters
        takes_cursor = "cursor" in parameters
        if not takes_page and not takes_cursor:
            raise TypeError(f"{name}() is not a paginated listing: it takes no page")
        bound = signature.bind_partial(*args, **kwargs)
        arguments = bound.arguments
        if page_size is not None:
            if "page_size" not in parameters:
                raise TypeError(f"{name}() takes no page_size")
            arguments["page_size"] = page_size
        requested_size = arguments.get("page_size")
        default = parameters["page_size"].default if "page_size" in parameters else None
        default_size = default if isinstance(default, int) and not isinstance(default, bool) else None
        known_size: Optional[int] = None
        page = arguments.get("page", 1) if takes_page else 1
        cursor = arguments.get("cursor") if takes_cursor else None
        # The most results the API serves page by page (page * page_size), and how to get the rest.
        window: Optional[int] = None
        advice = ""
        if name in self._PAGING_WINDOWS:
            window, advice = self._PAGING_WINDOWS[name]
        elif name in self._DEEP_PAGING_EXPORTS:
            window, advice = self._DEEP_PAGING_LIMIT, f"Use {self._DEEP_PAGING_EXPORTS[name]}"
        yielded = 0
        first = True
        if max_items == 0:
            return
        while True:
            size = requested_size or known_size or default_size
            if window is not None and cursor is None and size and page * size > window:
                _warn(
                    f"{name}() stopped after {yielded} items: the API serves at most "
                    f"{window:,} results page by page (page * page_size). {advice} to get them all.",
                    UserWarning,
                )
                return
            if takes_page:
                arguments["page"] = page
            if takes_cursor and cursor is not None:
                arguments["cursor"] = cursor
            try:
                response = await method(*bound.args, **bound.kwargs)
            except NotFoundError as error:
                # "Page out of range" after a page was read: the listing shrank meanwhile.
                if first or "out of range" not in error.message.lower():
                    raise
                return
            first = False
            items = response.get(items_key) if isinstance(response, Mapping) else None
            if not isinstance(items, list):
                raise TypeError(f"{name}() did not answer a page with an {items_key!r} list")
            for item in items:
                yield item
                yielded += 1
                if max_items is not None and yielded >= max_items:
                    return
            reported_size = response.get("page_size")
            if isinstance(reported_size, int) and not isinstance(reported_size, bool) and reported_size > 0:
                known_size = reported_size
            size = requested_size or known_size or default_size
            has_more = response.get("has_more")
            if takes_cursor and has_more is not None and "next_cursor" in response:
                # Cursor mode (raw_search): the API positions on the cursor alone and only
                # echoes `page`; has_more decides, a page can be short or empty.
                next_cursor = response.get("next_cursor")
                if not has_more or not next_cursor or next_cursor == cursor:
                    return
                cursor = next_cursor
                page += 1
                continue
            total = response.get("total")
            if (
                name in self._SPARSE_LISTINGS
                and has_more is None
                and isinstance(total, int)
                and not isinstance(total, bool)
                and total >= 0
                and size
            ):
                # A short or empty page is not the end here: `total` decides.
                if page * size >= total:
                    return
                page += 1
                continue
            if not items:
                return
            if has_more is not None:
                if not has_more:
                    return
            else:
                if size and len(items) < size:
                    return
                if isinstance(total, int) and not isinstance(total, bool) and total >= 0 and size and page * size >= total:
                    return
            page += 1

    async def _wait_for_task(
        self,
        read: Callable[[str], Any],
        task_id: str,
        interval: float,
        timeout: Optional[float],
        on_progress: Optional[Callable[[Dict[str, Any]], Any]],
    ) -> Dict[str, Any]:
        """Poll a task with `read` (get_task_status or get_unlocked_list_task) until it ends."""
        self._check_wait(interval, timeout)
        started = self._monotonic()
        while True:
            task = await read(task_id)
            await self._progress(on_progress, task)
            if self._task_done(task_id, task):
                return task
            await self._wait_again(started, interval, timeout, f"task {task_id}", task)

    @staticmethod
    def _task_done(task_id: str, task: Any) -> bool:
        """True once the task completed, False while it runs; TaskFailedError when it ended otherwise."""
        if not isinstance(task, Mapping):
            raise TypeError(f"Unexpected status of task {task_id}: {task!r}")
        status = task.get("status")
        status = status.upper() if isinstance(status, str) else None
        if status == "COMPLETED":
            return True
        if status in ("FAILED", "CANCELED"):
            error = task.get("error")
            error = error if isinstance(error, Mapping) else {}
            raise TaskFailedError(task_id, status, error.get("code"), error.get("message"), task)
        if status in ("QUEUED", "RUNNING"):
            return False
        # No status (an older answer, or a task ID containing ":"): the flags tell.
        if task.get("completed"):
            return True
        if task.get("running"):
            return False
        raise TaskFailedError(task_id, "FAILED", None, f"Task {task_id} stopped without completing.", task)

    async def _read_export(self, export_id: int) -> Dict[str, Any]:
        """One export of the account, read with the request of list_exports(export_id=...)."""
        # Through _request, not list_exports(): an override of list_exports() written for 0.2
        # takes no export_id.
        params = {"page": 1, "page_size": 20, "export_id": export_id}
        listing = await self._request("GET", "/exports", params=params)
        items = listing.get("items") if isinstance(listing, Mapping) else None
        for item in items or ():
            # Matched by ID: an API older than the export_id filter lists the latest exports.
            if isinstance(item, Mapping) and item.get("id") == export_id:
                return dict(item)
        raise NotFoundError(404, f"Export {export_id} not found.")

    @staticmethod
    async def _progress(on_progress: Optional[Callable[[Dict[str, Any]], Any]], state: Dict[str, Any]) -> None:
        if on_progress is None:
            return
        result = on_progress(state)
        if inspect.isawaitable(result):
            await result

    @staticmethod
    def _check_wait(interval: float, timeout: Optional[float]) -> None:
        if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not interval > 0:
            raise ValueError("interval must be a number of seconds > 0")
        if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not timeout >= 0):
            raise ValueError("timeout must be None or a number of seconds >= 0")

    async def _wait_again(
        self,
        started: float,
        interval: float,
        timeout: Optional[float],
        what: str,
        last: Dict[str, Any],
    ) -> None:
        """Sleep until the next read, or raise WaitTimeoutError once `timeout` is spent."""
        delay = float(interval)
        if timeout is not None:
            waited = self._monotonic() - started
            if waited >= timeout:
                status = last.get("status") or ("RUNNING" if last.get("running") else None)
                raise WaitTimeoutError(
                    f"Stopped waiting for {what} after {waited:.1f} s (last status: {status or 'unknown'}); "
                    "it keeps running on the server.",
                    waited=waited,
                    last=last,
                )
            delay = min(delay, timeout - waited)
        await self._sleep(delay)

    @staticmethod
    def _task_id(task: Any) -> str:
        """A task ID, from an ID or from the dict that queued the task."""
        value = task.get("task_id") if isinstance(task, Mapping) else task
        if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value).strip():
            # The type only: a wrong argument can be a list of unlocked records, passwords included.
            raise ValueError(f"Pass a task ID, or the dict that queued the task (got {type(task).__name__})")
        return str(value).strip()

    @staticmethod
    def _export_id(export: Any) -> int:
        """An export ID, from an ID or from the dict of an export."""
        value = export
        if isinstance(export, Mapping):
            value = export.get("export_id", export.get("id"))
        if isinstance(value, str) and value.strip().isdigit():
            value = int(value)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"Pass an export ID, or the dict of an export (got {type(export).__name__})")
        return value


class AsyncLeakRadar(LeakRadarClient):
    """
    Asynchronous client for the LeakRadar.io API: the recommended name, with the same methods,
    arguments and errors as LeakRadarClient.

    Settings left out are read from the environment: the API key from LEAKRADAR_API_KEY
    (token="" forces anonymous calls) and the API address from LEAKRADAR_BASE_URL.

        async with AsyncLeakRadar() as client:
            profile = await client.get_profile()

    For code without asyncio (scripts, Django, Flask, Celery tasks, notebooks), LeakRadar is the
    synchronous client with the same methods.
    """

    _READ_ENV = True
