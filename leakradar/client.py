import warnings
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Union
from urllib.parse import quote

import httpx

from ._version import __version__

try:
    import ujson as _json
except Exception:
    import json as _json  # type: ignore[no-redef]


StrOrList = Union[str, Sequence[str]]
IntOrList = Union[int, Sequence[int]]
DateLike = Union[str, date, datetime]
"""ISO 8601 string, or a date/datetime (sent as ISO 8601). Always give datetimes a timezone."""


class LeakRadarAPIError(Exception):
    """Base exception for API-related errors."""

    def __init__(self, status_code: int, detail: Any):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"API Error {status_code}: {detail}")

    @property
    def code(self) -> Optional[str]:
        """Machine-readable error code, when the API answers with an object `detail`."""
        if isinstance(self.detail, dict):
            code = self.detail.get("code")
            if isinstance(code, str):
                return code
        return None


class UnauthorizedError(LeakRadarAPIError):
    """Raised when the user is not authorized to access a resource."""


class ForbiddenError(LeakRadarAPIError):
    """Raised when the user does not have permission (forbidden)."""


class BadRequestError(LeakRadarAPIError):
    """Raised when the request is invalid."""


class TooManyRequestsError(LeakRadarAPIError):
    """Raised when rate limits are exceeded."""


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


class ServiceUnavailableError(LeakRadarAPIError):
    """Raised when a search family is under maintenance or the service is temporarily unavailable."""


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
    - No automatic retries by default
    """

    BASE_URL = "https://api.leakradar.io"

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

    _TEST_NOTIFICATION_TYPES = ("leak", "raw", "dark_web")

    def __init__(
        self,
        token: Optional[str] = None,
        user_agent: str = f"LeakRadar-Python-Client/{__version__}",
        timeout: float = 30.0,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ):
        """
        Initialize the client.

        :param token: Bearer token for authenticated endpoints.
        :param user_agent: Custom User-Agent to identify usage.
        :param timeout: Request timeout in seconds.
        :param transport: Optional httpx transport for API calls, e.g.
            httpx.AsyncHTTPTransport(retries=3) to retry failed connections, or
            httpx.MockTransport(handler) in tests.
        """
        self.token = token
        self.user_agent = user_agent
        self._timeout = timeout

        self._client = httpx.AsyncClient(
            base_url=self.BASE_URL,
            headers=self._base_headers(),
            timeout=timeout,
            transport=transport,
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
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.aclose()

    async def aclose(self):
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
        return quote(str(value), safe="")

    @staticmethod
    def _as_list(value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, list):
            return value
        if isinstance(value, (tuple, set)):
            return list(value)
        return [value]

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
        """
        req_headers = self._default_headers()
        if headers:
            req_headers.update(headers)

        content: Optional[bytes] = None
        if json is not None:
            content = self._encode_json(self._jsonable(json))
            req_headers.setdefault("Content-Type", "application/json")

        response = await self._client.request(
            method,
            endpoint,
            params=self._clean(params),
            content=content,
            headers=req_headers,
            follow_redirects=follow_redirects,
        )

        if response.is_error:
            await self._handle_error(response)

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

    async def _handle_error(self, response: httpx.Response):
        """Raise the exception matching the status code. `detail` may be a string or an object."""
        text = response.text
        try:
            body = _json.loads(text) if text else None
        except Exception:
            body = None
        detail: Any = text
        if isinstance(body, dict) and body.get("detail") not in (None, ""):
            detail = body["detail"]

        error_class = _ERRORS_BY_STATUS.get(response.status_code, LeakRadarAPIError)
        raise error_class(response.status_code, detail)

    # -------------------------
    # Core / misc
    # -------------------------

    async def get_profile(self) -> Dict[str, Any]:
        """Retrieve the authenticated user's profile."""
        return await self._request("GET", "/profile")

    async def update_profile(self, **data: Any) -> Dict[str, Any]:
        """Update whitelisted fields on the authenticated user's profile."""
        return await self._request("PATCH", "/profile", json=data)

    async def get_stats(self) -> Dict[str, Any]:
        """Get cached platform statistics snapshot."""
        return await self._request("GET", "/stats")

    async def get_live_stats(self) -> Dict[str, Any]:
        """
        Live landing figures (raw lines, ingestion rate, active sources, dark web posts).
        Public, refreshed every ~2 min. Answers {"message": ...} while the cache is empty.
        """
        return await self._request("GET", "/live-stats")

    async def get_transparency_stats(self) -> Dict[str, Any]:
        """
        Transparency report figures (exclusion rules, suspended accounts, removal and legal requests).
        Public, refreshed every 6 hours. Answers {"message": ...} while the cache is empty.
        """
        return await self._request("GET", "/transparency-stats")

    async def get_task_status(self, task_id: str) -> Dict[str, Any]:
        """Get background task status by /tasks/{task_id} (unlock tasks of every search family)."""
        return await self._request("GET", f"/tasks/{self._seg(task_id)}")

    async def password_range(
        self,
        prefix: str,
        limit: int = 1000,
        suffix_only: bool = False,
    ) -> Dict[str, Any]:
        """
        Query a SHA-1 password hash range.

        :param prefix: SHA-1 prefix (or full hash) to query.
        :param limit: Max matches to return (server-capped).
        :param suffix_only: Return only suffixes (after first 5 chars).
        """
        params = self._clean({"prefix": prefix, "limit": limit, "suffix_only": suffix_only})
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
    ) -> Dict[str, Any]:
        """
        Search stealer logs with advanced filters (LeakSearchFilters, sent as the JSON body).

        - Values inside one field are OR'ed; force_and=True requires all of them.
        - `*_match_type`: "contains" (default when omitted), "starts_with" or "ends_with".
        - password_strength: "too_weak", "weak", "medium" or "strong".
        - Scalars are wrapped into lists where the API expects arrays.
        - auto_unlock=True unlocks the locked items of the returned page with your points.
        - Paging stops at page * page_size = 1,000,000: use export_advanced() for more.
        """
        filters = self._normalize_leak_filters(self._pick(locals(), self._LEAK_FILTER_FIELDS))
        params = self._clean({"page": page, "page_size": page_size, "auto_unlock": auto_unlock})
        return await self._request("POST", "/search/advanced", params=params, json=filters)

    async def unlock_all_advanced(
        self,
        filters: Dict[str, Any],
        max_leaks: Optional[int] = None,
        list_id: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """
        Unlock leaks returned by the same advanced filters.
        Synchronous hard cap 10,000. Use queue_advanced_unlock_task for higher volumes.
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
    ) -> Dict[str, Any]:
        """Queue an async mass-unlock task for advanced search."""
        params: Dict[str, Any] = {}
        if max_leaks is not None:
            params["max"] = max_leaks
        if list_id is not None:
            params["list_id"] = list_id

        filters = self._normalize_leak_filters(dict(filters or {}))
        return await self._request("POST", "/search/advanced/unlock/task", params=params, json=filters)

    async def export_advanced(self, format: Optional[str] = None, **filters: Any) -> Dict[str, Any]:
        """
        Queue an export for advanced search results.
        format: csv|txt|json (default csv)
        Provide LeakSearchFilters in the JSON body via **filters.
        """
        params = self._clean({"format": format})
        payload = self._normalize_leak_filters(self._clean(filters) or {})
        return await self._request("POST", "/search/advanced/export", params=params, json=payload)

    async def export_advanced_urls(self, format: Optional[str] = None, **filters: Any) -> Dict[str, Any]:
        """
        Queue an export of distinct URLs matching advanced filters.
        format: csv|txt|json (default csv)
        Provide LeakSearchFilters in the JSON body via **filters.
        """
        params = self._clean({"format": format})
        payload = self._normalize_leak_filters(self._clean(filters) or {})
        return await self._request("POST", "/search/advanced/export_urls", params=params, json=payload)

    # -------------------------
    # Search (Domain)
    # -------------------------

    async def get_domain_report(
        self,
        domain: str,
        light: Optional[bool] = None,
        include_search_count: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """
        Retrieve a domain leak report. Set light=True for sampled counts.
        include_search_count=True adds `searched_by_count` (full mode only, heavier).
        """
        params = self._clean({"light": light, "include_search_count": include_search_count})
        return await self._request("GET", f"/search/domain/{self._seg(domain)}", params=params)

    async def get_domain_report_pdf(self, domain: str) -> bytes:
        """Download domain report as PDF (binary)."""
        return await self._request(
            "GET",
            f"/search/domain/{self._seg(domain)}/report/pdf",
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
    ) -> Dict[str, Any]:
        """
        List every leak of a domain (employees, customers and third parties together).
        Each item carries a `category` field.
        """
        params = self._clean(
            {"page": page, "page_size": page_size, "search": search, "is_email": is_email, "auto_unlock": auto_unlock}
        )
        return await self._request("GET", f"/search/domain/{self._seg(domain)}/all", params=params)

    async def get_domain_customers(
        self,
        domain: str,
        page: int = 1,
        page_size: int = 100,
        search: Optional[str] = None,
        is_email: Optional[bool] = None,
        auto_unlock: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """List the leaks linked to customers of a domain."""
        params = self._clean(
            {"page": page, "page_size": page_size, "search": search, "is_email": is_email, "auto_unlock": auto_unlock}
        )
        return await self._request("GET", f"/search/domain/{self._seg(domain)}/customers", params=params)

    async def get_domain_employees(
        self,
        domain: str,
        page: int = 1,
        page_size: int = 100,
        search: Optional[str] = None,
        is_email: Optional[bool] = None,
        auto_unlock: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """List the leaks linked to employees of a domain."""
        params = self._clean(
            {"page": page, "page_size": page_size, "search": search, "is_email": is_email, "auto_unlock": auto_unlock}
        )
        return await self._request("GET", f"/search/domain/{self._seg(domain)}/employees", params=params)

    async def get_domain_third_parties(
        self,
        domain: str,
        page: int = 1,
        page_size: int = 100,
        search: Optional[str] = None,
        is_email: Optional[bool] = None,
        auto_unlock: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """List the leaks linked to third parties of a domain."""
        params = self._clean(
            {"page": page, "page_size": page_size, "search": search, "is_email": is_email, "auto_unlock": auto_unlock}
        )
        return await self._request("GET", f"/search/domain/{self._seg(domain)}/third_parties", params=params)

    async def get_domain_subdomains(
        self,
        domain: str,
        page: int = 1,
        page_size: int = 100,
        search: Optional[str] = None,
    ) -> Dict[str, Any]:
        """List the subdomains seen in a domain's leaks."""
        params = self._clean({"page": page, "page_size": page_size, "search": search})
        return await self._request("GET", f"/search/domain/{self._seg(domain)}/subdomains", params=params)

    async def export_domain_subdomains(
        self,
        domain: str,
        search: Optional[str] = None,
        format: Optional[str] = None,
    ) -> Any:
        """
        Export all unique subdomains for a domain.

        Note: as of OpenAPI v1.0.0, this endpoint is POST.
        The API may return raw CSV/TXT/JSON (bytes) or a small JSON payload depending on server configuration.
        """
        params = self._clean({"format": format})
        body = self._clean({"search": search})
        return await self._request(
            "POST", f"/search/domain/{self._seg(domain)}/subdomains/export", params=params, json=body
        )

    async def get_domain_urls(
        self,
        domain: str,
        page: int = 1,
        page_size: int = 100,
        search: Optional[str] = None,
    ) -> Dict[str, Any]:
        """List the URLs seen in a domain's leaks."""
        params = self._clean({"page": page, "page_size": page_size, "search": search})
        return await self._request("GET", f"/search/domain/{self._seg(domain)}/urls", params=params)

    async def export_domain_urls(
        self,
        domain: str,
        search: Optional[str] = None,
        format: Optional[str] = None,
    ) -> Any:
        """
        Export all unique URLs for a domain.

        Note: as of OpenAPI v1.0.0, this endpoint is POST.
        """
        params = self._clean({"format": format})
        body = self._clean({"search": search})
        return await self._request("POST", f"/search/domain/{self._seg(domain)}/urls/export", params=params, json=body)

    async def export_domain_leaks(
        self,
        domain: str,
        leak_type: str,
        search: Optional[str] = None,
        is_email: Optional[bool] = None,
        format: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Queue an export for leaks of a domain and leak type.

        Note: as of OpenAPI v1.0.0, this endpoint is POST.
        """
        params = self._clean({"format": format})
        body = self._clean({"search": search, "is_email": is_email})
        return await self._request(
            "POST",
            f"/search/domain/{self._seg(domain)}/{self._seg(leak_type)}/export",
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
    ) -> List[Dict[str, Any]]:
        """Unlock leaks for a domain + category (sync hard cap 10k)."""
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
            "POST", f"/search/domain/{self._seg(domain)}/{self._seg(leak_type)}/unlock", params=params
        )

    async def queue_domain_unlock_task(
        self,
        domain: str,
        leak_type: str,
        search: Optional[str] = None,
        is_email: Optional[bool] = None,
        max_leaks: Optional[int] = None,
        list_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Queue an async mass unlock task for a domain."""
        params = self._clean({"search": search, "is_email": is_email, "max": max_leaks, "list_id": list_id})
        return await self._request(
            "POST", f"/search/domain/{self._seg(domain)}/{self._seg(leak_type)}/unlock/task", params=params
        )

    async def domains_locked_exists(
        self,
        domains: List[str],
        categories: Optional[List[str]] = None,
        include_counts: bool = False,
    ) -> Dict[str, Any]:
        """
        Batch exists-check of still-locked leaks by domain and category (≤100 domains).
        categories: subset of ['employees','customers','third_parties'] or None for all.
        """
        payload: Dict[str, Any] = {"domains": domains}
        if categories is not None:
            payload["categories"] = categories
        if include_counts:
            payload["include_counts"] = True
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
    ) -> Dict[str, Any]:
        """Search stealer-log leaks for an email or username. auto_unlock=True unlocks the page with points."""
        params = self._clean({"page": page, "page_size": page_size, "auto_unlock": auto_unlock})
        data = self._clean({"email": email, "search": search, "is_email": is_email}) or {"email": email}
        return await self._request("POST", "/search/email", params=params, json=data)

    async def export_email_leaks(
        self,
        email: str,
        search: Optional[str] = None,
        is_email: Optional[bool] = None,
        format: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Queue an export (csv|txt|json, default csv) of the leaks of an email or username."""
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
    ) -> List[Dict[str, Any]]:
        """Unlock the leaks of an email or username (sync hard cap 10k, use queue_email_unlock_task beyond)."""
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
    ) -> Dict[str, Any]:
        """Queue a background unlock of every leak of an email or username. Poll with get_task_status()."""
        params: Dict[str, Any] = {}
        if max_leaks is not None:
            params["max"] = max_leaks
        if list_id is not None:
            params["list_id"] = list_id
        data = self._clean({"email": email, "search": search, "is_email": is_email}) or {"email": email}
        return await self._request("POST", "/search/email/unlock/task", params=params, json=data)

    async def emails_locked_exists(self, emails: List[str], include_counts: bool = False) -> Dict[str, Any]:
        """Batch check which emails or usernames still have locked leaks for your account."""
        payload: Dict[str, Any] = {"emails": emails}
        if include_counts:
            payload["include_counts"] = True
        return await self._request("POST", "/search/emails/locked-exists", json=payload)

    # -------------------------
    # Search (Android applications)
    # -------------------------

    async def suggest_applications(self, q: Optional[str] = None, limit: Optional[int] = None) -> Dict[str, Any]:
        """
        Android apps found in stealer logs, most entries first.
        q (3+ chars) matches the app name or package; omit it for the top apps. limit: 1-50 (default 20).
        Use the returned `package` with get_application_leaks().
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
    ) -> Dict[str, Any]:
        """List the stealer-log entries (android:// URLs) of an Android app, newest first."""
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
    ) -> List[Dict[str, Any]]:
        """Unlock the entries of an Android app (sync hard cap 10k, use queue_application_unlock_task beyond)."""
        params = self._clean({"search": search, "is_email": is_email, "max": max_leaks, "list_id": list_id})
        return await self._request("POST", f"/search/applications/{self._seg(package)}/unlock", params=params)

    async def queue_application_unlock_task(
        self,
        package: str,
        search: Optional[str] = None,
        is_email: Optional[bool] = None,
        max_leaks: Optional[int] = None,
        list_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Queue an async unlock task for an Android app (at most 5 queued or running per account)."""
        params = self._clean({"search": search, "is_email": is_email, "max": max_leaks, "list_id": list_id})
        return await self._request("POST", f"/search/applications/{self._seg(package)}/unlock/task", params=params)

    async def export_application_leaks(
        self,
        package: str,
        search: Optional[str] = None,
        is_email: Optional[bool] = None,
        format: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Queue an export (csv|txt|json, default csv) of the entries of an Android app."""
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
    ) -> Dict[str, Any]:
        """
        Search combolists for an exact email (case-insensitive).

        Combolists are a separate dataset from stealer logs: results carry no URL, and an unlock
        lands in get_unlocked_combolists(), not get_unlocked_leaks(). page_size: 1-100.
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
    ) -> Dict[str, Any]:
        """Search combolists for an exact non-email username (case-insensitive). page_size: 1-100."""
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
    ) -> Dict[str, Any]:
        """
        Search combolist pairs whose email is on a root domain (username-only pairs never match).
        page_size: 1-1000.
        """
        params = self._clean(
            {"page": page, "page_size": page_size, "search": search, "auto_unlock": auto_unlock}
        )
        return await self._request("GET", f"/search/combolist/domain/{self._seg(domain)}", params=params)

    async def get_combolist_domain_report(self, domain: str, no_cache: bool = False) -> Dict[str, Any]:
        """
        Combolist exposure report of a root email domain (counts only, cached one hour).
        no_cache=True forces a recompute.
        """
        headers = {"x-no-cache": "1"} if no_cache else None
        return await self._request("GET", f"/search/combolist/domain/{self._seg(domain)}/report", headers=headers)

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
    ) -> Dict[str, Any]:
        """
        Search combolists with advanced filters.

        Only the fields of the combolist mapping exist (no url_*, email_host or email_tld).
        `*_match_type`: "contains" (default), "starts_with" or "ends_with". page_size: 1-1000.
        The same keys, as a dict, are the `filters` of the advanced scope in
        queue_combolist_unlock_task(), export_combolist() and get_unlocked_combolists_advanced().
        """
        filters = self._normalize_leak_filters(self._pick(locals(), self._COMBOLIST_FILTER_FIELDS))
        params = self._clean({"page": page, "page_size": page_size, "auto_unlock": auto_unlock})
        return await self._request("POST", "/search/combolist/advanced", params=params, json=filters)

    async def unlock_combolist_leaks(
        self,
        leak_ids: List[str],
        target_list_id: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """
        Unlock combolist credentials by ID (`cl1_` IDs only, at most 10,000 per call).
        1 point per pair newly unlocked; pairs you already own are free.
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
    ) -> Dict[str, Any]:
        """
        Queue a mass unlock of every combolist pair in a search scope.

        - scope "email", "username" or "domain" takes `value` (and optionally `search`).
        - scope "advanced" takes `filters` (the keys of search_combolist_advanced()).
        - Capped by your point balance and by max_leaks. One task at a time per account.
        Poll the returned task_id with get_task_status().
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
    ) -> Dict[str, Any]:
        """
        Queue an export (csv|txt|json, default csv) of a combolist search scope.

        Same scope rules as queue_combolist_unlock_task(). On a points plan the file only holds the
        pairs you already unlocked in that scope: unlock first to export the full result set.
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
    ) -> Dict[str, Any]:
        """
        Search dark web and forum posts.

        - Simple mode: `query` searches title and content.
        - Advanced mode: `title`, `content`, `author` (exact), `source_url` combined with logic "AND" (default) or "OR".
        - sources: source names (see get_dark_web_sources()), as a list or a comma-separated string.
        - sort_by: "ingested_at" (default) or "published_at"; sort_order: "desc" (default) or "asc".
        - page_size: 1-100.
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

    async def get_dark_web_post(self, post_id: str) -> Dict[str, Any]:
        """Full content of one dark web post (search results are truncated)."""
        return await self._request("GET", f"/search/dark-web/post/{self._seg(post_id)}")

    async def get_dark_web_sources(self) -> Dict[str, Any]:
        """Scraped forum sources with their post counts."""
        return await self._request("GET", "/search/dark-web/sources")

    async def get_dark_web_stats(self) -> Dict[str, Any]:
        """Overall statistics of the dark web index."""
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
    ) -> Dict[str, Any]:
        """status: remediation status, "new", "in_progress", "fixed" or "accepted_risk"."""
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
    ) -> Dict[str, Any]:
        """Queue an export (csv|txt|json, default csv) of your unlocked stealer-log leaks."""
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
    ) -> Dict[str, Any]:
        """List unlocked leaks with the advanced filter set (**filters, sent as query parameters)."""
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
    ) -> Dict[str, Any]:
        """unlocked_at_min must carry a timezone (a naive value is a 400 on this endpoint)."""
        params = self._clean(
            {"format": format, "unlocked_at_min": unlocked_at_min, "list_id": list_id, "list_none": list_none, "status": status}
        )
        payload = self._normalize_leak_filters(self._clean(filters) or {})
        return await self._request("POST", "/profile/unlocked/advanced/export", params=params, json=payload)

    async def set_unlocked_leak_status(self, leak_id: str, status: str) -> Any:
        """Set the remediation status of one unlocked leak: "new", "in_progress", "fixed" or "accepted_risk"."""
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
    ) -> Dict[str, Any]:
        """
        Background task setting `target_status` on every unlocked record matching the filters.
        dataset: "stealer_logs" (default) or "combolist". Poll with get_unlocked_list_task().
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
    ) -> Dict[str, Any]:
        """
        List the combolist credentials you unlocked, in clear (disjoint from get_unlocked_leaks()).
        unlocked_at_min (with a timezone) pages an incremental sync. page_size: 1-1000.
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
    ) -> Dict[str, Any]:
        """Same listing as get_unlocked_combolists(), plus the advanced `filters` of search_combolist_advanced()."""
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
    ) -> Dict[str, Any]:
        """
        Queue an export (csv|txt|json, default csv) of your unlocked combolist credentials.
        Same filters as get_unlocked_combolists_advanced(); pass the timestamp of your last
        export as unlocked_at_min to get only what came after.
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

    async def set_unlocked_combolist_list(self, leak_id: str, list_id: Optional[int]) -> Any:
        """Assign one unlocked combolist credential to a list (None removes the assignment)."""
        return await self._request(
            "PUT", f"/profile/unlocked/combolists/{self._seg(leak_id)}/list", json={"list_id": list_id}
        )

    async def upsert_unlocked_combolist_comment(self, leak_id: str, comment: str) -> Any:
        """Set or replace the comment of one unlocked combolist credential (500 chars max)."""
        return await self._request(
            "PUT", f"/profile/unlocked/combolists/{self._seg(leak_id)}/comment", json={"comment": comment}
        )

    async def delete_unlocked_combolist_comment(self, leak_id: str) -> Any:
        """Remove the comment of one unlocked combolist credential (idempotent)."""
        return await self._request("DELETE", f"/profile/unlocked/combolists/{self._seg(leak_id)}/comment")

    async def set_unlocked_combolist_status(self, leak_id: str, status: str) -> Any:
        """Set the remediation status of one unlocked combolist credential ("new", "in_progress", "fixed", "accepted_risk")."""
        return await self._request(
            "PUT", f"/profile/unlocked/combolists/{self._seg(leak_id)}/status", json={"status": status}
        )

    # -------------------------
    # Unlocked lists
    # -------------------------

    async def list_unlocked_lists(self, with_counts: bool = True, dataset: Optional[str] = None) -> List[Dict[str, Any]]:
        """Lists are shared by both datasets; dataset ("stealer_logs" or "combolist") picks what items_count counts."""
        params = self._clean({"with_counts": with_counts, "dataset": dataset})
        return await self._request("GET", "/profile/unlocked/lists", params=params)

    async def create_unlocked_list(self, name: str, color: Optional[str] = None) -> Dict[str, Any]:
        """Create an unlocked list (shared by stealer logs and combolists)."""
        payload = self._clean({"name": name, "color": color})
        return await self._request("POST", "/profile/unlocked/lists", json=payload)

    async def update_unlocked_list(self, list_id: int, name: Optional[str] = None, color: Optional[str] = None) -> Dict[str, Any]:
        """Rename or recolor an unlocked list."""
        payload = self._clean({"name": name, "color": color})
        return await self._request("PATCH", f"/profile/unlocked/lists/{list_id}", json=payload)

    async def delete_unlocked_list(self, list_id: int) -> Dict[str, Any]:
        """Delete an unlocked list."""
        return await self._request("DELETE", f"/profile/unlocked/lists/{list_id}")

    async def clear_unlocked_list(self, list_id: int) -> Dict[str, Any]:
        """Unassign every record from a list, keeping the list itself."""
        return await self._request("POST", f"/profile/unlocked/lists/{list_id}/clear")

    async def get_unlocked_list_task(self, task_id: str) -> Dict[str, Any]:
        """Status of a bulk list or status task (bulk_assign_unlocked_list, bulk_update_unlocked_status)."""
        return await self._request("GET", f"/profile/unlocked/list-tasks/{self._seg(task_id)}")

    async def set_unlocked_leak_list(self, leak_id: str, list_id: Optional[int]) -> Any:
        """Assign one unlocked leak to a list (None removes the assignment)."""
        return await self._request("PUT", f"/profile/unlocked/{self._seg(leak_id)}/list", json={"list_id": list_id})

    async def upsert_unlocked_leak_comment(self, leak_id: str, comment: str) -> Any:
        """Set or replace the comment of one unlocked leak."""
        return await self._request("PUT", f"/profile/unlocked/{self._seg(leak_id)}/comment", json={"comment": comment})

    async def delete_unlocked_leak_comment(self, leak_id: str) -> Any:
        """Remove the comment of one unlocked leak."""
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
    ) -> Dict[str, Any]:
        """
        Background task assigning `target_list_id` (None unassigns) to every unlocked record matching
        the filters. dataset: "stealer_logs" (default) or "combolist". Poll with get_unlocked_list_task().
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

    async def unlock_specific_leaks(self, leak_ids: List[str], target_list_id: Optional[int] = None) -> List[Dict[str, Any]]:
        """Unlock stealer-log leaks by ID. Combolist IDs (`cl1_`) go to unlock_combolist_leaks()."""
        data: Dict[str, Any] = {"leak_ids": leak_ids}
        if target_list_id is not None:
            data["target_list_id"] = target_list_id
        return await self._request("POST", "/unlock", json=data)

    # -------------------------
    # Exports
    # -------------------------

    async def list_exports(self, page: int = 1, page_size: int = 20) -> Dict[str, Any]:
        """Your export jobs, with their status and download links."""
        return await self._request("GET", "/exports", params={"page": page, "page_size": page_size})

    # -------------------------
    # Notification methods / notifications / runs
    # -------------------------

    async def list_notification_methods(self) -> List[Dict[str, Any]]:
        """Your notification channels."""
        return await self._request("GET", "/notification_methods")

    async def create_notification_method(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Create a notification channel (email, Slack, Discord, Telegram or webhook)."""
        return await self._request("POST", "/notification_methods", json=data)

    async def update_notification_method(self, method_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        """Update a notification channel (a new destination also resumes a suspended one)."""
        return await self._request("PATCH", f"/notification_methods/{method_id}", json=data)

    async def delete_notification_method(self, method_id: int) -> Any:
        """Delete a notification channel."""
        return await self._request("DELETE", f"/notification_methods/{method_id}")

    async def set_notification_method_active(self, method_id: int, is_active: bool) -> Dict[str, Any]:
        """
        Pause (False) or resume (True) a notification method. Resuming also clears a suspension
        caused by repeated delivery failures.
        """
        return await self._request("PATCH", f"/notification_methods/{method_id}/active", json={"is_active": is_active})

    async def test_notification_method(self, data: Dict[str, Any]) -> Any:
        """Send a test message to a channel configuration before saving it."""
        return await self._request("POST", "/notification_methods/test", json=data)

    async def test_notification_method_with_data(
        self,
        method_id: int,
        notification_type: str = "leak",
        data: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """
        Send a test notification with realistic fake data to an existing method.
        notification_type: "leak" (default), "raw" or "dark_web". Rate limit: 30 tests per hour.

        `data` is deprecated: the API reads no body. A dict passed as second argument (0.1.x style)
        still works and sends the same test as before ("leak"), unless it holds a valid
        "notification_type".
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
            f"/notification_methods/{method_id}/test-with-data",
            params={"notification_type": notification_type},
        )

    async def list_notifications(self) -> List[Dict[str, Any]]:
        """Your monitorings."""
        return await self._request("GET", "/notifications")

    async def create_notification(self, data: Dict[str, Any]) -> Union[Dict[str, Any], List[Dict[str, Any]]]:
        """
        Create a monitoring bound to a notification method.
        type: email, domain, advanced, raw or dark_web. A bulk creation returns a list.
        """
        return await self._request("POST", "/notifications", json=data)

    async def update_notification(self, notification_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        """Update one monitoring."""
        return await self._request("PATCH", f"/notifications/{notification_id}", json=data)

    async def delete_notification(self, notification_id: int) -> Any:
        """Delete one monitoring."""
        return await self._request("DELETE", f"/notifications/{notification_id}")

    async def delete_notifications_bulk(self, ids: List[int]) -> Any:
        """Delete several monitorings at once."""
        return await self._request("DELETE", "/notifications/bulk", json={"ids": ids})

    async def set_notification_active(self, notification_id: int, is_active: bool) -> Dict[str, Any]:
        """Pause (False) or resume (True) one monitoring."""
        return await self._request("PATCH", f"/notifications/{notification_id}/active", json={"is_active": is_active})

    async def get_notifications_stats(self) -> Dict[str, Any]:
        """Monitored assets usage and limits of the account."""
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
    ) -> Dict[str, Any]:
        """
        List monitoring runs, most recent first.

        - dataset: "stealer_logs", "combolist", "databases", "raw_files" or "dark_web".
        - sent_after is inclusive, sent_before exclusive.
        - q: a run or asset number ("123" or "#123"), or text of the monitored asset.
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

    async def get_notification_run(self, run_id: int) -> Dict[str, Any]:
        """One monitoring run (the page alert emails and webhooks link to). Its results: get_notification_run_items()."""
        return await self._request("GET", f"/notification_runs/{run_id}")

    async def get_notification_run_items(self, run_id: int, page: int = 1, page_size: int = 100) -> Dict[str, Any]:
        """Results of a monitoring run, whatever its type (credentials, raw files, dark web posts). `item_type` tells which."""
        params = self._clean({"page": page, "page_size": page_size})
        return await self._request("GET", f"/notification_runs/{run_id}/items", params=params)

    async def notification_run_leaks(
        self,
        run_id: int,
        page: int = 1,
        page_size: int = 100,
        search: Optional[str] = None,
        is_email: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Deprecated: the API replaced /notification_runs/{id}/leaks with get_notification_run_items()."""
        message = "notification_run_leaks() is deprecated, use get_notification_run_items()"
        if search is not None or is_email is not None:
            message += " (search and is_email are no longer supported and are ignored)"
        warnings.warn(message, DeprecationWarning, stacklevel=2)
        return await self.get_notification_run_items(run_id, page=page, page_size=page_size)

    async def set_notification_run_status(self, run_id: int, status: str) -> Any:
        """
        Set the triage status of a run ("new", "in_progress", "fixed", "accepted_risk").
        The status is propagated to every unlocked leak of the run.
        """
        return await self._request("PUT", f"/notification_runs/{run_id}/status", json={"status": status})

    async def unlock_notification_run_leaks(
        self,
        run_id: int,
        max_leaks: Optional[int] = None,
        search: Optional[str] = None,
        is_email: Optional[bool] = None,
        list_id: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Unlock the leaks of a monitoring run (capped by max_leaks and your points)."""
        params = self._clean({"max": max_leaks, "search": search, "is_email": is_email, "list_id": list_id})
        return await self._request("POST", f"/notification_runs/{run_id}/leaks/unlock", params=params)

    async def export_notification_run(
        self,
        run_id: int,
        search: Optional[str] = None,
        is_email: Optional[bool] = None,
        format: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Queue an export (csv|txt|json, default csv) of the leaks of one monitoring run."""
        params = self._clean({"search": search, "is_email": is_email, "format": format})
        return await self._request("GET", f"/notification_runs/{run_id}/export", params=params)

    async def export_notification_runs(
        self,
        run_ids: Optional[List[int]] = None,
        filters: Optional[Dict[str, Any]] = None,
        exclude_run_ids: Optional[List[int]] = None,
        format: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Queue one export (a ZIP with a file per run plus summary.json) of several monitoring runs.

        - Give either run_ids, or filters (the keys of list_notification_runs(): dataset, status,
          sent_after, sent_before, notification_id, q). Filters select Stealer Logs and Combolist runs only.
        - exclude_run_ids deselects runs from a filter selection.
        - At most 1000 runs; only unlocked credentials are included, no points are spent.
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
    ) -> Dict[str, Any]:
        """
        Full-text search across raw leak blocks.

        - q: 4+ characters (quotes ignored). Without q, give at least one selector (container_id,
          exts, categories, file_name, folder_name, include_terms, ingested_at_* or a *_not list).
        - q_exact=True matches q on word boundaries ("4.4.4.4" no longer matches "4.4.4.400").
        - include_terms must ALL be present on top of q; exclude_terms drop a block.
        - file_name/folder_name `*_match_type`: "contains" (default), "starts_with", "ends_with" or "exact".
        - force_and=True requires every file_name / folder_name value instead of any.
        - cursor: the `next_cursor` of the previous response, for deep pagination.
        - dedup=True folds blocks with identical content (per page).
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
    ) -> Dict[str, Any]:
        """
        Count the raw blocks matching the filters of raw_search(): the most parts (1 point each)
        a mass unlock would consume. Heavier than a search page: call it on demand, not per search.

        `exact` False means `total` is an upper bound (q_exact / dedup shrink the real set);
        `capped` True means `total` is a lower bound.
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
    ) -> Dict[str, Any]:
        """
        Queue a mass unlock of the raw blocks matching the filters of raw_search().

        - max_leaks: cap of new unlocks; 0 or None uses all your points. Estimate with raw_count().
        - dedup=True unlocks each identical content once.
        - One running and one queued raw task per account; an identical request returns the existing task.
        Poll the returned task_id with get_task_status().
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
    ) -> Dict[str, Any]:
        """Deprecated: the API removed /search/raw/export/preview. Returns raw_count() for the same filters."""
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
    ) -> Dict[str, Any]:
        """
        Queue a raw export: export="rows" (CSV of matching lines) or "parts" (text snapshot of parts).
        Only blocks you already unlocked are exported (queue_raw_unlock_task() first); no points are spent.
        dedup=True drops duplicate lines (rows) or identical blocks (parts).
        """
        if export not in {"rows", "parts"}:
            raise ValueError('export must be "rows" or "parts"')
        payload = self._raw_filters(locals())
        params = self._clean({"export": export, "dedup": dedup})
        return await self._request("POST", "/search/raw/export", params=params, json=payload)

    async def raw_list_parts(self, container_id: int, entry_path: str, page: int = 1, page_size: int = 100) -> Dict[str, Any]:
        """List the parts of a raw entry."""
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
    ) -> Dict[str, Any]:
        """Read one part of a raw entry. auto_unlock=True unlocks it with your points if needed."""
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
    ) -> Dict[str, Any]:
        """Browse the file tree of a raw container."""
        params = self._clean({"container_id": container_id, "prefix": prefix, "page": page, "page_size": page_size})
        return await self._request("GET", "/container/tree", params=params)

    async def container_subfolders(
        self, container_id: int, prefix: str = "", page: int = 1, page_size: int = 1000
    ) -> Dict[str, Any]:
        """List the immediate subfolders of a container folder."""
        params = self._clean({"container_id": container_id, "prefix": prefix, "page": page, "page_size": page_size})
        return await self._request("GET", "/container/subfolders", params=params)

    async def container_resolve_path(
        self, container_id: int, entry_path: str = "", options: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Resolve a folder chain inside a container and prefetch its contents."""
        payload: Dict[str, Any] = {"container_id": container_id, "entry_path": entry_path}
        if options is not None:
            payload["options"] = options
        return await self._request("POST", "/container/tree/resolve_path", json=payload)

    async def container_file_info(self, container_id: int, entry_path: str) -> Dict[str, Any]:
        """Metadata of one raw entry (path inside its container)."""
        params = {"container_id": container_id, "entry_path": entry_path}
        return await self._request("GET", "/container/file_info", params=params)

    async def list_raw_files(self) -> List[Dict[str, Any]]:
        """Raw files fully indexed and searchable."""
        return await self._request("GET", "/raw/files")

    async def raw_download_preview(
        self,
        sha256_original: Optional[str] = None,
        container_id: Optional[int] = None,
        entry_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Cost (GB quota) of downloading a raw file, before downloading it."""
        params = self._clean({"sha256_original": sha256_original, "container_id": container_id, "entry_path": entry_path})
        return await self._request("GET", "/raw/download/preview", params=params)

    async def raw_download_source(
        self,
        sha256_original: Optional[str] = None,
        container_id: Optional[int] = None,
        entry_path: Optional[str] = None,
        expires_in: Optional[int] = 900,
    ) -> Dict[str, Any]:
        """Generate a download URL for a raw file (spends GB quota). expires_in: 60-3600 s."""
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
    ) -> Dict[str, Any]:
        """search: file name or container ID; status: "available" or "expired"."""
        params = self._clean({"page": page, "page_size": page_size, "search": search, "status": status})
        return await self._request("GET", "/profile/raw/downloads", params=params)

    async def list_unlocked_raw_parts(self, page: int = 1, page_size: int = 20, q: Optional[str] = None) -> Dict[str, Any]:
        """Raw file entries you unlocked, most recent first. q filters on file name, path, source or category."""
        params = self._clean({"page": page, "page_size": page_size, "q": q})
        return await self._request("GET", "/profile/raw/unlocked-parts", params=params)

    async def get_raw_download_url(self, download_id: int, token: Optional[str] = None) -> str:
        """Presigned URL of a raw file download, read from the API redirect (nothing is downloaded)."""
        params = self._clean({"token": token})
        req_headers = self._default_headers()
        response = await self._client.request(
            "POST",
            f"/profile/raw/downloads/{download_id}/file",
            params=params,
            headers=req_headers,
            follow_redirects=False,
        )
        if response.is_error:
            await self._handle_error(response)

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
        """Download a raw file you have access to and return its bytes."""
        url = await self.get_raw_download_url(download_id=download_id, token=token)
        if self._anon_client is None:
            self._anon_client = httpx.AsyncClient(
                headers={"User-Agent": self.user_agent, "Accept": "*/*"},
                timeout=self._timeout,
            )
        resp = await self._anon_client.get(url, follow_redirects=True)
        if resp.status_code >= 400:
            raise LeakRadarAPIError(resp.status_code, resp.text or "Raw file download failed.")
        return resp.content

    # -------------------------
    # Account: usage, preferences, activity
    # -------------------------

    async def get_usage(self) -> Dict[str, Any]:
        """
        Every plan limit with its current consumption (owner-scoped for team members).
        `limit: null` means unlimited, `used: null` means momentarily unreadable. Costs no quota.
        """
        return await self._request("GET", "/profile/usage")

    async def get_preferences(self) -> Dict[str, Any]:
        """Your account preferences (password redaction, interface language)."""
        return await self._request("GET", "/profile/preferences")

    async def update_preferences(
        self,
        hide_passwords: Optional[bool] = None,
        language: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Partial update: only the arguments you pass are changed. language: one of the 13 UI locales ("en", "fr", ...)."""
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
    ) -> Dict[str, Any]:
        """Balance history (points and GB). kind: "points" or "gb"; direction: "credit" or "debit"; dates as YYYY-MM-DD."""
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
    ) -> Dict[str, Any]:
        """
        Background unlock tasks of the account. status: "QUEUED", "RUNNING", "COMPLETED", "FAILED"
        or "CANCELED"; dates as YYYY-MM-DD.
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

    async def get_team(self) -> Dict[str, Any]:
        """Your team: owner, members, pending invitations, plan and member quota."""
        return await self._request("GET", "/profile/team")

    async def create_team_invitation(self, email: str, name: Optional[str] = None, role: str = "member") -> Dict[str, Any]:
        """Invite someone to your team by email."""
        payload = self._clean({"email": email, "name": name, "role": role}) or {"email": email, "role": role}
        return await self._request("POST", "/profile/team/invitations", json=payload)

    async def resend_team_invitation(self, invitation_id: int) -> Dict[str, Any]:
        """Send a team invitation email again."""
        return await self._request("POST", f"/profile/team/invitations/{invitation_id}/resend")

    async def revoke_team_invitation(self, invitation_id: int) -> Dict[str, Any]:
        """Cancel a pending team invitation."""
        return await self._request("POST", f"/profile/team/invitations/{invitation_id}/revoke")

    async def update_team_member_role(self, member_id: int, team_role: str) -> Dict[str, Any]:
        """Change the role of a team member."""
        return await self._request("PATCH", f"/profile/team/members/{member_id}", json={"team_role": team_role})

    async def delete_team_member(self, member_id: int) -> Any:
        """Remove a member from your team."""
        return await self._request("DELETE", f"/profile/team/members/{member_id}")

    async def leave_team(self) -> Any:
        """Leave the team you are a member of (not available to the team owner)."""
        return await self._request("POST", "/profile/team/leave")
