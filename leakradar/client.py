import httpx
from typing import Any, Dict, Optional, List, Union, Mapping

try:
    import ujson as _json
except Exception:
    import json as _json


class LeakRadarAPIError(Exception):
    """Base exception for API-related errors."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"API Error {status_code}: {detail}")


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
    - No automatic retries by default
    """

    BASE_URL = "https://api.leakradar.io"

    _LEAK_FILTER_ARRAY_FIELDS = {
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
    }

    def __init__(
        self,
        token: Optional[str] = None,
        user_agent: str = "LeakRadar-Python-Client/0.1.6",
        timeout: float = 30.0,
    ):
        """
        Initialize the client.

        :param token: Bearer token for authenticated endpoints.
        :param user_agent: Custom User-Agent to identify usage.
        :param timeout: Request timeout in seconds.
        """
        self.token = token
        self.user_agent = user_agent

        self._client = httpx.AsyncClient(
            base_url=self.BASE_URL,
            headers=self._base_headers(),
            timeout=timeout,
        )
        self._anon_client = httpx.AsyncClient(
            headers={"User-Agent": self.user_agent, "Accept": "*/*"},
            timeout=timeout,
        )

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
        await self._anon_client.aclose()

    @staticmethod
    def _clean(params: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
        if params is None:
            return None
        return {k: v for k, v in params.items() if v is not None}

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
            content = self._encode_json(json)
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
        detail = ""
        try:
            text = response.text
            try:
                body = _json.loads(text) if text else {}
            except Exception:
                body = {}
            detail = body.get("detail", "") or text
        except Exception:
            detail = response.text

        if response.status_code == 400:
            raise BadRequestError(response.status_code, detail)
        if response.status_code == 401:
            raise UnauthorizedError(response.status_code, detail)
        if response.status_code == 402:
            raise PaymentRequiredError(response.status_code, detail)
        if response.status_code == 403:
            raise ForbiddenError(response.status_code, detail)
        if response.status_code == 404:
            raise NotFoundError(response.status_code, detail)
        if response.status_code == 409:
            raise ConflictError(response.status_code, detail)
        if response.status_code == 410:
            raise GoneError(response.status_code, detail)
        if response.status_code == 422:
            raise ValidationError(response.status_code, detail)
        if response.status_code == 429:
            raise TooManyRequestsError(response.status_code, detail)

        raise LeakRadarAPIError(response.status_code, detail)

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

    async def get_task_status(self, task_id: str) -> Dict[str, Any]:
        """Get background task status by /tasks/{task_id}."""
        return await self._request("GET", f"/tasks/{task_id}")

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
        username: Optional[Union[str, List[str]]] = None,
        username_not: Optional[Union[str, List[str]]] = None,
        password: Optional[Union[str, List[str]]] = None,
        password_not: Optional[Union[str, List[str]]] = None,
        url: Optional[Union[str, List[str]]] = None,
        url_not: Optional[Union[str, List[str]]] = None,
        url_domain: Optional[Union[str, List[str]]] = None,
        url_domain_not: Optional[Union[str, List[str]]] = None,
        url_host: Optional[Union[str, List[str]]] = None,
        url_host_not: Optional[Union[str, List[str]]] = None,
        username_hash: Optional[Union[str, List[str]]] = None,
        password_hash: Optional[Union[str, List[str]]] = None,
        url_scheme: Optional[Union[str, List[str]]] = None,
        url_scheme_not: Optional[Union[str, List[str]]] = None,
        url_port: Optional[Union[int, List[int]]] = None,
        url_port_not: Optional[Union[int, List[int]]] = None,
        url_tld: Optional[Union[str, List[str]]] = None,
        url_tld_not: Optional[Union[str, List[str]]] = None,
        is_email: Optional[bool] = None,
        email_domain: Optional[Union[str, List[str]]] = None,
        email_domain_not: Optional[Union[str, List[str]]] = None,
        email_host: Optional[Union[str, List[str]]] = None,
        email_host_not: Optional[Union[str, List[str]]] = None,
        email_tld: Optional[Union[str, List[str]]] = None,
        email_tld_not: Optional[Union[str, List[str]]] = None,
        password_strength: Optional[str] = None,
        added_from: Optional[str] = None,
        added_to: Optional[str] = None,
        force_and: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """
        Search leaks with advanced filters (LeakSearchFilters).

        Note: as of OpenAPI v1.0.0, this endpoint is POST and filters are passed in the JSON body.
        Arrays are supported by passing a Python list; scalars are auto-wrapped into a list where needed.
        """
        params = self._clean({"page": page, "page_size": page_size})

        filters = self._clean(
            {
                "username": username,
                "username_not": username_not,
                "password": password,
                "password_not": password_not,
                "url": url,
                "url_not": url_not,
                "url_domain": url_domain,
                "url_domain_not": url_domain_not,
                "url_host": url_host,
                "url_host_not": url_host_not,
                "username_hash": username_hash,
                "password_hash": password_hash,
                "url_scheme": url_scheme,
                "url_scheme_not": url_scheme_not,
                "url_port": url_port,
                "url_port_not": url_port_not,
                "url_tld": url_tld,
                "url_tld_not": url_tld_not,
                "is_email": is_email,
                "email_domain": email_domain,
                "email_domain_not": email_domain_not,
                "email_host": email_host,
                "email_host_not": email_host_not,
                "email_tld": email_tld,
                "email_tld_not": email_tld_not,
                "password_strength": password_strength,
                "added_from": added_from,
                "added_to": added_to,
                "force_and": force_and,
            }
        ) or {}

        filters = self._normalize_leak_filters(filters)
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

    async def get_domain_report(self, domain: str, light: Optional[bool] = None) -> Dict[str, Any]:
        """Retrieve a domain leak report. Set light=True for sampled counts."""
        params = self._clean({"light": light})
        return await self._request("GET", f"/search/domain/{domain}", params=params)

    async def get_domain_report_pdf(self, domain: str) -> bytes:
        """Download domain report as PDF (binary)."""
        return await self._request(
            "GET",
            f"/search/domain/{domain}/report/pdf",
            headers={"Accept": "application/pdf"},
        )

    async def get_domain_customers(
        self,
        domain: str,
        page: int = 1,
        page_size: int = 100,
        search: Optional[str] = None,
        is_email: Optional[bool] = None,
    ) -> Dict[str, Any]:
        params = self._clean({"page": page, "page_size": page_size, "search": search, "is_email": is_email})
        return await self._request("GET", f"/search/domain/{domain}/customers", params=params)

    async def get_domain_employees(
        self,
        domain: str,
        page: int = 1,
        page_size: int = 100,
        search: Optional[str] = None,
        is_email: Optional[bool] = None,
    ) -> Dict[str, Any]:
        params = self._clean({"page": page, "page_size": page_size, "search": search, "is_email": is_email})
        return await self._request("GET", f"/search/domain/{domain}/employees", params=params)

    async def get_domain_third_parties(
        self,
        domain: str,
        page: int = 1,
        page_size: int = 100,
        search: Optional[str] = None,
        is_email: Optional[bool] = None,
    ) -> Dict[str, Any]:
        params = self._clean({"page": page, "page_size": page_size, "search": search, "is_email": is_email})
        return await self._request("GET", f"/search/domain/{domain}/third_parties", params=params)

    async def get_domain_subdomains(
        self,
        domain: str,
        page: int = 1,
        page_size: int = 100,
        search: Optional[str] = None,
    ) -> Dict[str, Any]:
        params = self._clean({"page": page, "page_size": page_size, "search": search})
        return await self._request("GET", f"/search/domain/{domain}/subdomains", params=params)

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
        return await self._request("POST", f"/search/domain/{domain}/subdomains/export", params=params, json=body)

    async def get_domain_urls(
        self,
        domain: str,
        page: int = 1,
        page_size: int = 100,
        search: Optional[str] = None,
    ) -> Dict[str, Any]:
        params = self._clean({"page": page, "page_size": page_size, "search": search})
        return await self._request("GET", f"/search/domain/{domain}/urls", params=params)

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
        return await self._request("POST", f"/search/domain/{domain}/urls/export", params=params, json=body)

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
        return await self._request("POST", f"/search/domain/{domain}/{leak_type}/export", params=params, json=body)

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
        return await self._request("POST", f"/search/domain/{domain}/{leak_type}/unlock", params=params)

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
        return await self._request("POST", f"/search/domain/{domain}/{leak_type}/unlock/task", params=params)

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
    ) -> Dict[str, Any]:
        params = {"page": page, "page_size": page_size}
        data = self._clean({"email": email, "search": search, "is_email": is_email}) or {"email": email}
        return await self._request("POST", "/search/email", params=params, json=data)

    async def export_email_leaks(
        self,
        email: str,
        search: Optional[str] = None,
        is_email: Optional[bool] = None,
        format: Optional[str] = None,
    ) -> Dict[str, Any]:
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
        params: Dict[str, Any] = {}
        if max_leaks is not None:
            params["max"] = max_leaks
        if list_id is not None:
            params["list_id"] = list_id
        data = self._clean({"email": email, "search": search, "is_email": is_email}) or {"email": email}
        return await self._request("POST", "/search/email/unlock/task", params=params, json=data)

    async def emails_locked_exists(self, emails: List[str], include_counts: bool = False) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"emails": emails}
        if include_counts:
            payload["include_counts"] = True
        return await self._request("POST", "/search/emails/locked-exists", json=payload)

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
    ) -> Dict[str, Any]:
        params = self._clean(
            {"page": page, "page_size": page_size, "search": search, "is_email": is_email, "list_id": list_id, "list_none": list_none}
        )
        return await self._request("GET", "/profile/unlocked", params=params)

    async def export_unlocked_leaks(
        self,
        search: Optional[str] = None,
        is_email: Optional[bool] = None,
        list_id: Optional[int] = None,
        list_none: Optional[bool] = None,
        format: Optional[str] = None,
        unlocked_at_min: Optional[str] = None,
    ) -> Dict[str, Any]:
        params = self._clean({"format": format})
        payload = self._clean({"search": search, "is_email": is_email, "list_id": list_id, "list_none": list_none, "unlocked_at_min": unlocked_at_min}) or {}
        return await self._request("POST", "/profile/unlocked/export", params=params, json=payload)

    async def get_unlocked_advanced(
        self,
        page: int = 1,
        page_size: int = 100,
        search: Optional[str] = None,
        list_id: Optional[int] = None,
        list_none: Optional[bool] = None,
        **filters: Any,
    ) -> Dict[str, Any]:
        base = self._clean({"page": page, "page_size": page_size, "search": search, "list_id": list_id, "list_none": list_none})
        params = self._clean({**(base or {}), **filters})
        return await self._request("GET", "/profile/unlocked/advanced", params=params)

    async def export_unlocked_advanced(
        self,
        format: Optional[str] = None,
        unlocked_at_min: Optional[str] = None,
        list_id: Optional[int] = None,
        list_none: Optional[bool] = None,
        **filters: Any,
    ) -> Dict[str, Any]:
        params = self._clean({"format": format, "unlocked_at_min": unlocked_at_min, "list_id": list_id, "list_none": list_none})
        payload = self._normalize_leak_filters(self._clean(filters) or {})
        return await self._request("POST", "/profile/unlocked/advanced/export", params=params, json=payload)

    # -------------------------
    # Unlocked lists
    # -------------------------

    async def list_unlocked_lists(self, with_counts: bool = True) -> List[Dict[str, Any]]:
        return await self._request("GET", "/profile/unlocked/lists", params={"with_counts": with_counts})

    async def create_unlocked_list(self, name: str, color: Optional[str] = None) -> Dict[str, Any]:
        payload = self._clean({"name": name, "color": color})
        return await self._request("POST", "/profile/unlocked/lists", json=payload)

    async def update_unlocked_list(self, list_id: int, name: Optional[str] = None, color: Optional[str] = None) -> Dict[str, Any]:
        payload = self._clean({"name": name, "color": color})
        return await self._request("PATCH", f"/profile/unlocked/lists/{list_id}", json=payload)

    async def delete_unlocked_list(self, list_id: int) -> Dict[str, Any]:
        return await self._request("DELETE", f"/profile/unlocked/lists/{list_id}")

    async def clear_unlocked_list(self, list_id: int) -> Dict[str, Any]:
        return await self._request("POST", f"/profile/unlocked/lists/{list_id}/clear")

    async def get_unlocked_list_task(self, task_id: str) -> Dict[str, Any]:
        return await self._request("GET", f"/profile/unlocked/list-tasks/{task_id}")

    async def set_unlocked_leak_list(self, leak_id: str, list_id: Optional[int]) -> Any:
        return await self._request("PUT", f"/profile/unlocked/{leak_id}/list", json={"list_id": list_id})

    async def upsert_unlocked_leak_comment(self, leak_id: str, comment: str) -> Any:
        return await self._request("PUT", f"/profile/unlocked/{leak_id}/comment", json={"comment": comment})

    async def delete_unlocked_leak_comment(self, leak_id: str) -> Any:
        return await self._request("DELETE", f"/profile/unlocked/{leak_id}/comment")

    async def bulk_assign_unlocked_list(
        self,
        target_list_id: Optional[int],
        filters: Optional[Dict[str, Any]] = None,
        search: Optional[str] = None,
        is_email: Optional[bool] = None,
        list_id_filter: Optional[int] = None,
        list_none: Optional[bool] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"target_list_id": target_list_id}
        if search is not None:
            payload["search"] = search
        if is_email is not None:
            payload["is_email"] = is_email
        if list_id_filter is not None:
            payload["list_id_filter"] = list_id_filter
        if list_none is not None:
            payload["list_none"] = list_none
        if filters is not None:
            payload["filters"] = filters
        return await self._request("POST", "/profile/unlocked/lists/bulk-assign", json=payload)

    # -------------------------
    # Unlock
    # -------------------------

    async def unlock_specific_leaks(self, leak_ids: List[str], target_list_id: Optional[int] = None) -> List[Dict[str, Any]]:
        data: Dict[str, Any] = {"leak_ids": leak_ids}
        if target_list_id is not None:
            data["target_list_id"] = target_list_id
        return await self._request("POST", "/unlock", json=data)

    # -------------------------
    # Exports
    # -------------------------

    async def list_exports(self, page: int = 1, page_size: int = 20) -> Dict[str, Any]:
        return await self._request("GET", "/exports", params={"page": page, "page_size": page_size})

    # -------------------------
    # Notification methods / notifications / runs
    # -------------------------

    async def list_notification_methods(self) -> List[Dict[str, Any]]:
        return await self._request("GET", "/notification_methods")

    async def create_notification_method(self, data: Dict[str, Any]) -> Dict[str, Any]:
        return await self._request("POST", "/notification_methods", json=data)

    async def update_notification_method(self, method_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        return await self._request("PATCH", f"/notification_methods/{method_id}", json=data)

    async def delete_notification_method(self, method_id: int) -> Any:
        return await self._request("DELETE", f"/notification_methods/{method_id}")

    async def test_notification_method(self, data: Dict[str, Any]) -> Any:
        return await self._request("POST", "/notification_methods/test", json=data)

    async def test_notification_method_with_data(self, method_id: int, data: Dict[str, Any]) -> Any:
        """Test an existing notification method with custom data."""
        return await self._request("POST", f"/notification_methods/{method_id}/test-with-data", json=data)

    async def list_notifications(self) -> List[Dict[str, Any]]:
        return await self._request("GET", "/notifications")

    async def create_notification(self, data: Dict[str, Any]) -> Union[Dict[str, Any], List[Dict[str, Any]]]:
        return await self._request("POST", "/notifications", json=data)

    async def update_notification(self, notification_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        return await self._request("PATCH", f"/notifications/{notification_id}", json=data)

    async def delete_notification(self, notification_id: int) -> Any:
        return await self._request("DELETE", f"/notifications/{notification_id}")

    async def delete_notifications_bulk(self, ids: List[int]) -> Any:
        return await self._request("DELETE", "/notifications/bulk", json={"ids": ids})

    async def set_notification_active(self, notification_id: int, is_active: bool) -> Dict[str, Any]:
        return await self._request("PATCH", f"/notifications/{notification_id}/active", json={"is_active": is_active})

    async def get_notifications_stats(self) -> Dict[str, Any]:
        return await self._request("GET", "/notifications/stats")

    async def list_notification_runs(self, page: int = 1, page_size: int = 20) -> Dict[str, Any]:
        return await self._request("GET", "/notification_runs", params={"page": page, "page_size": page_size})

    async def notification_run_leaks(
        self,
        run_id: int,
        page: int = 1,
        page_size: int = 100,
        search: Optional[str] = None,
        is_email: Optional[bool] = None,
    ) -> Dict[str, Any]:
        params = self._clean({"page": page, "page_size": page_size, "search": search, "is_email": is_email})
        return await self._request("GET", f"/notification_runs/{run_id}/leaks", params=params)

    async def unlock_notification_run_leaks(
        self,
        run_id: int,
        max_leaks: Optional[int] = None,
        search: Optional[str] = None,
        is_email: Optional[bool] = None,
    ) -> List[Dict[str, Any]]:
        params = self._clean({"max": max_leaks, "search": search, "is_email": is_email})
        return await self._request("POST", f"/notification_runs/{run_id}/leaks/unlock", params=params)

    async def export_notification_run(
        self,
        run_id: int,
        search: Optional[str] = None,
        is_email: Optional[bool] = None,
        format: Optional[str] = None,
    ) -> Dict[str, Any]:
        params = self._clean({"search": search, "is_email": is_email, "format": format})
        return await self._request("GET", f"/notification_runs/{run_id}/export", params=params)

    # -------------------------
    # Raw search / containers / downloads
    # -------------------------

    async def raw_search(
        self,
        q: Optional[str] = None,
        page: int = 1,
        page_size: int = 10,
        container_id: Optional[int] = None,
        exts: Optional[List[str]] = None,
        categories: Optional[List[str]] = None,
        file_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not any([q, container_id, exts, categories, file_name]):
            raise ValueError("At least one of q, container_id, exts, categories, file_name must be provided.")
        params = {"page": page, "page_size": page_size}
        payload = self._clean({"q": q, "container_id": container_id, "exts": exts, "categories": categories, "file_name": file_name}) or {}
        return await self._request("POST", "/search/raw", params=params, json=payload)

    async def raw_export_preview(
        self,
        export: str,
        q: Optional[str] = None,
        container_id: Optional[int] = None,
        exts: Optional[List[str]] = None,
        categories: Optional[List[str]] = None,
        file_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        if export not in {"rows", "parts"}:
            raise ValueError('export must be "rows" or "parts"')
        if not any([q, container_id, exts, categories, file_name]):
            raise ValueError("At least one of q, container_id, exts, categories, file_name must be provided.")
        payload = self._clean({"q": q, "container_id": container_id, "exts": exts, "categories": categories, "file_name": file_name}) or {}
        return await self._request("POST", "/search/raw/export/preview", params={"export": export}, json=payload)

    async def raw_export(
        self,
        export: str,
        q: Optional[str] = None,
        container_id: Optional[int] = None,
        exts: Optional[List[str]] = None,
        categories: Optional[List[str]] = None,
        file_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        if export not in {"rows", "parts"}:
            raise ValueError('export must be "rows" or "parts"')
        if not any([q, container_id, exts, categories, file_name]):
            raise ValueError("At least one of q, container_id, exts, categories, file_name must be provided.")
        payload = self._clean({"q": q, "container_id": container_id, "exts": exts, "categories": categories, "file_name": file_name}) or {}
        return await self._request("POST", "/search/raw/export", params={"export": export}, json=payload)

    async def raw_list_parts(self, container_id: int, entry_path: str, page: int = 1, page_size: int = 100) -> Dict[str, Any]:
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
        params = self._clean(
            {"container_id": container_id, "entry_path": entry_path, "seq": seq, "trim_overlap": trim_overlap, "overlap_chars": overlap_chars, "auto_unlock": auto_unlock}
        )
        return await self._request("GET", "/search/raw/part", params=params)

    async def container_tree(self, container_id: int, prefix: str = "", page: int = 1, page_size: int = 200) -> Dict[str, Any]:
        params = self._clean({"container_id": container_id, "prefix": prefix, "page": page, "page_size": page_size})
        return await self._request("GET", "/container/tree", params=params)

    async def container_subfolders(self, container_id: int, prefix: str = "", page: int = 1, page_size: int = 1000) -> Dict[str, Any]:
        params = self._clean({"container_id": container_id, "prefix": prefix, "page": page, "page_size": page_size})
        return await self._request("GET", "/container/subfolders", params=params)

    async def container_resolve_path(self, container_id: int, entry_path: str = "", options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = {"container_id": container_id, "entry_path": entry_path}
        if options is not None:
            payload["options"] = options
        return await self._request("POST", "/container/tree/resolve_path", json=payload)

    async def container_file_info(self, container_id: int, entry_path: str) -> Dict[str, Any]:
        return await self._request("GET", "/container/file_info", params={"container_id": container_id, "entry_path": entry_path})

    async def list_raw_files(self) -> List[Dict[str, Any]]:
        return await self._request("GET", "/raw/files")

    async def raw_download_preview(self, sha256_original: Optional[str] = None, container_id: Optional[int] = None, entry_path: Optional[str] = None) -> Dict[str, Any]:
        params = self._clean({"sha256_original": sha256_original, "container_id": container_id, "entry_path": entry_path})
        return await self._request("GET", "/raw/download/preview", params=params)

    async def raw_download_source(
        self,
        sha256_original: Optional[str] = None,
        container_id: Optional[int] = None,
        entry_path: Optional[str] = None,
        expires_in: Optional[int] = 900,
    ) -> Dict[str, Any]:
        payload = self._clean({"sha256_original": sha256_original, "container_id": container_id, "entry_path": entry_path, "expires_in": expires_in}) or {}
        return await self._request("POST", "/raw/download", json=payload)

    async def list_raw_downloads(self, page: int = 1, page_size: int = 20) -> Dict[str, Any]:
        return await self._request("GET", "/profile/raw/downloads", params={"page": page, "page_size": page_size})

    async def get_raw_download_url(self, download_id: int, token: Optional[str] = None) -> str:
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
        url = await self.get_raw_download_url(download_id=download_id, token=token)
        resp = await self._anon_client.get(url, follow_redirects=True)
        if resp.status_code >= 400:
            raise LeakRadarAPIError(resp.status_code, resp.text or "Raw file download failed.")
        return resp.content

    # -------------------------
    # Team
    # -------------------------

    async def get_team(self) -> Dict[str, Any]:
        return await self._request("GET", "/profile/team")

    async def create_team_invitation(self, email: str, name: Optional[str] = None, role: str = "member") -> Dict[str, Any]:
        payload = self._clean({"email": email, "name": name, "role": role}) or {"email": email, "role": role}
        return await self._request("POST", "/profile/team/invitations", json=payload)

    async def resend_team_invitation(self, invitation_id: int) -> Dict[str, Any]:
        return await self._request("POST", f"/profile/team/invitations/{invitation_id}/resend")

    async def revoke_team_invitation(self, invitation_id: int) -> Dict[str, Any]:
        return await self._request("POST", f"/profile/team/invitations/{invitation_id}/revoke")

    async def update_team_member_role(self, member_id: int, team_role: str) -> Dict[str, Any]:
        return await self._request("PATCH", f"/profile/team/members/{member_id}", json={"team_role": team_role})

    async def delete_team_member(self, member_id: int) -> Any:
        return await self._request("DELETE", f"/profile/team/members/{member_id}")
