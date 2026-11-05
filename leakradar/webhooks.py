"""
Read the alerts a LeakRadar monitor POSTs to your webhook.

A monitor whose alert channel is a webhook sends each alert as an HTTP POST with a JSON body.
The alert always sits under a "leaks" key, whatever the dataset, and its "type" tells the kinds
apart: "leak" (credentials, from stealer logs or combolists), "raw" (raw file parts) or
"dark_web" (dark web posts). An all-datasets monitor (dataset "all") sends one POST per dataset
with results: the POSTs of one check share check_id and carry the header
X-LeakRadar-Delivery: <check_id>:<section>.

LeakRadar does not sign its deliveries. To make sure a POST comes from your monitor, give its
channel a custom header holding a secret of yours (the "headers" of POST /notification_methods,
for instance X-Webhook-Secret) and check it first, in constant time:

    import hmac

    from leakradar.webhooks import parse_delivery

    def handle(body: bytes, headers) -> None:
        received = headers.get("X-Webhook-Secret") or ""
        if not hmac.compare_digest(received.encode(), SECRET.encode()):
            raise PermissionError("Not from our monitor")
        delivery = parse_delivery(body, headers)
        if delivery.type is None:
            return  # the connection test of the channel
        alert = delivery.alert
        if alert["type"] == "leak":  # type checkers narrow the alert on its "type"
            for record in alert["unlocked_leaks"]:
                ...
        elif alert["type"] == "raw":
            for part in alert["parts"]:
                ...

Answer with a 2xx status within 20 seconds, and do the long work afterwards: a timeout or a
connection failure is sent again (twice at most), so the same alert can arrive up to three times.
delivery_id identifies the POSTs of an all-datasets check, alert["run_id"] those of the other
monitors (it is null in test deliveries). An endpoint that keeps failing gets its channel
suspended until it answers again.
"""

import json
import re
from typing import Any, Dict, Iterable, Mapping, Optional, Protocol, Tuple, Union, cast

from .types import WebhookCredentialAlert, WebhookDarkWebAlert, WebhookRawAlert

__all__ = ["DELIVERY_HEADER", "WebhookAlert", "WebhookDelivery", "parse_delivery"]

DELIVERY_HEADER = "X-LeakRadar-Delivery"
"""Header of the POSTs of an all-datasets check: "<check_id>:<section>" ("test:<section>" in a test)."""

WebhookAlert = Union[WebhookCredentialAlert, WebhookRawAlert, WebhookDarkWebAlert]
"""The `leaks` object of a delivery. Its "type" says which: "leak", "raw" or "dark_web"."""

class _HeaderItems(Protocol):
    """Headers objects that are not a Mapping but have items(), such as http.server's self.headers."""

    def items(self) -> Iterable[Tuple[Any, Any]]: ...


# Request headers: a mapping of any case (starlette, Flask, Django, httpx, a dict), any object with
# items() (http.server) or (name, value) pairs.
_Headers = Union[Mapping[Any, Any], _HeaderItems, Iterable[Tuple[Any, Any]]]


class WebhookDelivery:
    """
    One webhook POST, as parse_delivery reads it.

    - type: "leak" (credentials), "raw" or "dark_web", the "type" of the alert. None for the
      connection test of a channel, whose body is {"leaks": {"test": ...}}. A type this version
      does not know yet is kept as sent.
    - alert: the "leaks" object, a plain dict (WebhookCredentialAlert, WebhookRawAlert or
      WebhookDarkWebAlert, after its "type").
    - delivery_id: the X-LeakRadar-Delivery header, "<check_id>:<section>" ("test:<section>" in a
      test), rebuilt from the body when no headers were given. Use it to ignore a POST already
      processed. None for a single-dataset monitor, whose POSTs carry no such header:
      alert["run_id"] identifies those.
    - check_id: the check of an all-datasets monitor, shared by all its POSTs. None otherwise,
      and in test deliveries.
    - section: position of this POST in its check, from 1 (out of alert["section_count"]). None
      outside an all-datasets check.
    - body: the whole decoded body.
    """

    def __init__(
        self,
        *,
        delivery_id: Optional[str],
        check_id: Optional[int],
        section: Optional[int],
        type: Optional[str],
        alert: WebhookAlert,
        body: Dict[str, Any],
    ) -> None:
        self.delivery_id = delivery_id
        self.check_id = check_id
        self.section = section
        self.type = type
        self.alert = alert
        self.body = body

    def __repr__(self) -> str:
        # Never the alert: unlocked records carry credentials in clear.
        return (
            f"WebhookDelivery(type={self.type!r}, delivery_id={self.delivery_id!r}, "
            f"check_id={self.check_id!r}, section={self.section!r})"
        )


def parse_delivery(
    body: Union[bytes, bytearray, memoryview, str, Mapping[str, Any]],
    headers: Optional[_Headers] = None,
) -> WebhookDelivery:
    """
    Read one webhook POST of a monitor.

    - body: the raw body as received (bytes or str), or the JSON already decoded (dict).
    - headers: the request headers, as any mapping (the case of the names does not matter, the
      WSGI form HTTP_X_LEAKRADAR_DELIVERY works too) or (name, value) pairs, bytes included
      (ASGI). Only X-LeakRadar-Delivery is read: check your secret header yourself, see the
      module documentation.

    Raises ValueError when the body is not a JSON object or has no "leaks" object, TypeError when
    it is neither bytes, str nor a dict.
    """
    data = _load(body)
    alert = data.get("leaks")
    if not isinstance(alert, dict):
        raise ValueError('Not a LeakRadar webhook body: it has no "leaks" object.')
    delivery_id = _header(headers, DELIVERY_HEADER)
    check_id, section = _split(delivery_id)
    if check_id is None:
        check_id = _integer(alert.get("check_id"))
    if section is None:
        section = _integer(alert.get("section_index"))
    if delivery_id is None and section is not None:
        # The value the API puts in the header (app: monitor_check_sender).
        delivery_id = f"{check_id if check_id is not None else 'test'}:{section}"
    kind = alert.get("type")
    return WebhookDelivery(
        delivery_id=delivery_id,
        check_id=check_id,
        section=section,
        type=kind if isinstance(kind, str) else None,
        alert=cast(WebhookAlert, alert),
        body=data,
    )


def _load(body: Any) -> Dict[str, Any]:
    if isinstance(body, Mapping):
        return body if isinstance(body, dict) else dict(body)
    if isinstance(body, (bytes, bytearray, memoryview)):
        try:
            text = bytes(body).decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("Invalid webhook body: not UTF-8 text.") from exc
    elif isinstance(body, str):
        text = body[1:] if body[:1] == chr(0xFEFF) else body
    else:
        raise TypeError(f"body must be bytes, str or a dict, not {type(body).__name__}")
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"Invalid webhook body: not JSON ({exc}).") from exc
    if not isinstance(data, dict):
        raise ValueError("Invalid webhook body: a JSON object is expected.")
    return data


def _header(headers: Optional[_Headers], name: str) -> Optional[str]:
    """A header value, whatever the case of its name; None when it is missing or empty."""
    if headers is None:
        return None
    if isinstance(headers, (str, bytes, bytearray)):
        raise TypeError("headers must be a mapping or (name, value) pairs")
    wanted = (name.lower(), "http_" + name.lower().replace("-", "_"))
    pairs = headers.items() if isinstance(headers, Mapping) or hasattr(headers, "items") else headers
    for key, value in pairs:
        if isinstance(key, (bytes, bytearray)):
            key = bytes(key).decode("latin-1")
        if not isinstance(key, str) or key.lower() not in wanted:
            continue
        if isinstance(value, (list, tuple)):
            value = value[0] if value else None
        if isinstance(value, (bytes, bytearray)):
            value = bytes(value).decode("latin-1")
        text = "" if value is None else str(value).strip()
        if text:
            return text
    return None


def _split(delivery_id: Optional[str]) -> Tuple[Optional[int], Optional[int]]:
    """(check_id, section) of "<check_id>:<section>"; None for a part that is not a number ("test")."""
    if not delivery_id:
        return None, None
    check, _, section = delivery_id.rpartition(":")
    return _number(check), _number(section)


def _number(text: str) -> Optional[int]:
    text = text.strip()
    return int(text) if re.fullmatch(r"[0-9]+", text) else None


def _integer(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None
