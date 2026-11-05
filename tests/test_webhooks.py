"""
Reading the POSTs a monitor sends to a webhook (leakradar.webhooks).

The bodies below follow the API's payload builders (app/services/notification_sender.py and
monitor_check_sender.py) and their documented models (app/webhook_docs.py).
"""

import codecs
import json
import typing
import unittest
from typing import Any, Dict

import httpx

from leakradar import types
from leakradar.webhooks import DELIVERY_HEADER, WebhookDelivery, parse_delivery

PASSWORD = "Winter2026!"


def credential_alert(**extra: Any) -> Dict[str, Any]:
    alert = {
        "type": "leak",
        "dataset": "stealer_logs",
        "run_id": 812,
        "notification_id": 77,
        "run_url": "https://members.leakradar.io/monitoring?run=812",
        "message": "New leaks found for acme.example",
        "target": "acme.example",
        "new_leaks_count": 2,
        "unlocked_leaks": [
            {
                "id": "a1b2c3",
                "url": "https://login.acme.example",
                "username": "jdoe@acme.example",
                "password": PASSWORD,
                "password_strength": 6,
                "unlocked": True,
                "is_email": True,
                "added_at": "2026-10-28T09:12:41Z",
                "status": "new",
                "category": "employees",
            }
        ],
        "previews": [
            {
                "id": "d4e5f6",
                "url": "https://shop.acme.example",
                "username": None,
                "username_masked": "m***@acme.example",
                "password": None,
                "password_strength": 3,
                "unlocked": False,
                "is_email": True,
                "added_at": "2026-10-28T09:12:41Z",
                "status": None,
                "category": "customers",
            }
        ],
        "auto_unlock": True,
    }
    alert.update(extra)
    return {"leaks": alert}


def raw_alert(**extra: Any) -> Dict[str, Any]:
    alert = {
        "type": "raw",
        "dataset": "raw_files",
        "run_id": 913,
        "notification_id": 78,
        "run_url": "https://members.leakradar.io/monitoring?run=913",
        "target": "acme.example",
        "filters": {"q": "acme.example"},
        "new_parts_count": 1,
        "parts": [
            {
                "container_id": 5,
                "entry_path": "logs/passwords.txt",
                "seq": 2,
                "ingested_at": "2026-10-28T09:12:41Z",
                "matching_lines": [{"line_index": 14, "line_text": "URL: https://login.acme.example"}],
            }
        ],
        "auto_unlock": True,
        "matching_lines_count": 1,
    }
    alert.update(extra)
    return {"leaks": alert}


def dark_web_alert(**extra: Any) -> Dict[str, Any]:
    alert = {
        "type": "dark_web",
        "dataset": "dark_web",
        "run_id": 1014,
        "notification_id": 79,
        "run_url": "https://members.leakradar.io/monitoring?run=1014",
        "target": "acme",
        "filters": {"q": "acme"},
        "new_items_count": 1,
        "items": [
            {
                "id": "post-1",
                "title": "Selling acme access",
                "author": "seller",
                "source": "forum",
                "source_ref": "http://forum.example/thread/1",
                "content": "Access to acme for sale.",
                "published_at": "2026-10-27T20:00:00Z",
                "ingested_at": "2026-10-28T09:12:41Z",
            }
        ],
    }
    alert.update(extra)
    return {"leaks": alert}


# The keys an all-datasets check adds to the alert of each section (one POST per dataset).
def section(check_id: Any, index: int, count: int = 3) -> Dict[str, Any]:
    return {
        "monitor_dataset": "all",
        "check_id": check_id,
        "section_index": index,
        "section_count": count,
        "covered_datasets": ["stealer_logs", "combolist", "raw_files", "dark_web"],
    }


def raw(body: Dict[str, Any]) -> bytes:
    return json.dumps(body).encode("utf-8")


class BodyTest(unittest.TestCase):
    def test_bytes_str_and_dict_bodies(self):
        body = credential_alert()
        for given in (raw(body), raw(body).decode("utf-8"), body, bytearray(raw(body)), memoryview(raw(body))):
            delivery = parse_delivery(given)
            self.assertIsInstance(delivery, WebhookDelivery)
            self.assertEqual(delivery.body, body)
            self.assertEqual(delivery.alert, body["leaks"])
            self.assertEqual(delivery.type, "leak")

    def test_a_dict_body_is_used_as_is(self):
        body = credential_alert()
        delivery = parse_delivery(body)
        self.assertIs(delivery.body, body)
        self.assertIs(delivery.alert, body["leaks"])

    def test_byte_order_marks_and_non_ascii_text(self):
        body = credential_alert(target="caf" + chr(0xE9) + ".example")
        self.assertEqual(parse_delivery(codecs.BOM_UTF8 + raw(body)).alert["target"], body["leaks"]["target"])
        self.assertEqual(parse_delivery(chr(0xFEFF) + json.dumps(body)).alert["target"], body["leaks"]["target"])
        compact = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.assertEqual(parse_delivery(compact).alert["target"], body["leaks"]["target"])

    def test_invalid_json(self):
        for given in (b"", b"{", "not json", b'{"leaks": {"password": "' + PASSWORD.encode() + b'"'):
            with self.assertRaises(ValueError) as caught:
                parse_delivery(given)
            self.assertIn("not JSON", str(caught.exception))
            self.assertNotIn(PASSWORD, str(caught.exception))
        with self.assertRaises(ValueError):
            parse_delivery(bytes((0x7B, 0xFF, 0x7D)))  # not UTF-8

    def test_a_body_that_is_not_an_object(self):
        for given in (b"[]", b"null", b'"leaks"', b"42"):
            with self.assertRaises(ValueError):
                parse_delivery(given)

    def test_missing_leaks(self):
        for body in ({}, {"leak": {"type": "leak"}}, {"leaks": None}, {"leaks": []}, {"leaks": "x"}):
            with self.assertRaises(ValueError) as caught:
                parse_delivery(raw(body))
            self.assertIn('"leaks"', str(caught.exception))

    def test_unsupported_body_types(self):
        for given in (None, 42, ["leaks"]):
            with self.assertRaises(TypeError):
                parse_delivery(given)  # type: ignore[arg-type]


class HeadersTest(unittest.TestCase):
    body = raw(credential_alert(**section(40, 2)))

    def test_header_names_are_case_insensitive(self):
        for name in (DELIVERY_HEADER, "x-leakradar-delivery", "X-LEAKRADAR-DELIVERY", "HTTP_X_LEAKRADAR_DELIVERY"):
            delivery = parse_delivery(self.body, {"Content-Type": "application/json", name: "41:3"})
            self.assertEqual((delivery.delivery_id, delivery.check_id, delivery.section), ("41:3", 41, 3), name)

    def test_header_containers(self):
        containers = (
            httpx.Headers({"X-LeakRadar-Delivery": "41:3"}),
            [(b"content-type", b"application/json"), (b"x-leakradar-delivery", b"41:3")],  # ASGI
            [("X-LeakRadar-Delivery", "41:3")],
            {"x-leakradar-delivery": ["41:3"]},
            {"x-leakradar-delivery": b" 41:3 "},
        )
        for headers in containers:
            self.assertEqual(parse_delivery(self.body, headers).delivery_id, "41:3", headers)

    def test_an_empty_header_counts_as_missing(self):
        delivery = parse_delivery(self.body, {"X-LeakRadar-Delivery": "  "})
        self.assertEqual((delivery.delivery_id, delivery.check_id, delivery.section), ("40:2", 40, 2))

    def test_headers_must_be_a_mapping_or_pairs(self):
        with self.assertRaises(TypeError):
            parse_delivery(self.body, "X-LeakRadar-Delivery: 41:3")  # type: ignore[arg-type]


class DeliveryIdTest(unittest.TestCase):
    def test_all_datasets_check(self):
        delivery = parse_delivery(raw(raw_alert(**section(40, 2))), {"X-LeakRadar-Delivery": "40:2"})
        self.assertEqual(delivery.delivery_id, "40:2")
        self.assertEqual(delivery.check_id, 40)
        self.assertEqual(delivery.section, 2)
        self.assertEqual(delivery.type, "raw")

    def test_test_deliveries(self):
        delivery = parse_delivery(raw(dark_web_alert(run_id=None, notification_id=None, **section(None, 1))), {DELIVERY_HEADER: "test:1"})
        self.assertEqual((delivery.delivery_id, delivery.check_id, delivery.section), ("test:1", None, 1))

    def test_missing_header_is_rebuilt_from_the_body(self):
        delivery = parse_delivery(raw(credential_alert(**section(40, 3))))
        self.assertEqual((delivery.delivery_id, delivery.check_id, delivery.section), ("40:3", 40, 3))
        delivery = parse_delivery(raw(credential_alert(**section(None, 2))), {"Content-Type": "application/json"})
        self.assertEqual((delivery.delivery_id, delivery.check_id, delivery.section), ("test:2", None, 2))

    def test_single_dataset_monitors_have_no_delivery_id(self):
        for body in (credential_alert(), raw_alert(), dark_web_alert()):
            delivery = parse_delivery(raw(body), {"User-Agent": "LeakRadar.io"})
            self.assertEqual((delivery.delivery_id, delivery.check_id, delivery.section), (None, None, None))
            self.assertIsNotNone(delivery.alert["run_id"])

    def test_the_header_wins_and_a_malformed_one_is_kept_as_sent(self):
        delivery = parse_delivery(raw(credential_alert(**section(40, 2))), {DELIVERY_HEADER: "41:3"})
        self.assertEqual((delivery.check_id, delivery.section), (41, 3))
        delivery = parse_delivery(raw(credential_alert(**section(40, 2))), {DELIVERY_HEADER: "garbage"})
        self.assertEqual((delivery.delivery_id, delivery.check_id, delivery.section), ("garbage", 40, 2))

    def test_numbers_must_be_integers(self):
        delivery = parse_delivery(raw(credential_alert(check_id=True, section_index="2")))
        self.assertEqual((delivery.delivery_id, delivery.check_id, delivery.section), (None, None, None))


class AlertTypesTest(unittest.TestCase):
    def test_the_three_alert_types(self):
        for body, kind, items in (
            (credential_alert(), "leak", "unlocked_leaks"),
            (raw_alert(), "raw", "parts"),
            (dark_web_alert(), "dark_web", "items"),
        ):
            delivery = parse_delivery(raw(body))
            self.assertEqual(delivery.type, kind)
            self.assertEqual(delivery.alert["type"], kind)
            self.assertEqual(delivery.alert, body["leaks"])
            self.assertEqual(len(delivery.alert[items]), 1)  # type: ignore[literal-required]

    def test_the_bodies_match_the_documented_types(self):
        for body, delivery_type, alert_type in (
            (credential_alert(**section(40, 1)), types.CredentialAlertDelivery, types.WebhookCredentialAlert),
            (raw_alert(**section(40, 2)), types.RawAlertDelivery, types.WebhookRawAlert),
            (dark_web_alert(**section(40, 3)), types.DarkWebAlertDelivery, types.WebhookDarkWebAlert),
        ):
            self.assertEqual(set(body), set(delivery_type.__annotations__))
            self.assertLessEqual(set(body["leaks"]), set(alert_type.__annotations__))
        record = credential_alert()["leaks"]["unlocked_leaks"][0]
        self.assertLessEqual(set(record), set(types.WebhookCredential.__annotations__))
        part = raw_alert()["leaks"]["parts"][0]
        self.assertLessEqual(set(part), set(types.WebhookRawPart.__annotations__))
        self.assertLessEqual(set(part["matching_lines"][0]), set(types.WebhookRawLine.__annotations__))
        post = dark_web_alert()["leaks"]["items"][0]
        self.assertLessEqual(set(post), set(types.WebhookDarkWebPost.__annotations__))

    def test_the_connection_test_of_a_channel(self):
        delivery = parse_delivery(raw({"leaks": {"test": "LeakRadar"}}))
        self.assertIsNone(delivery.type)
        self.assertEqual(delivery.alert, {"test": "LeakRadar"})
        self.assertIsNone(delivery.delivery_id)

    def test_an_unknown_type_is_kept(self):
        delivery = parse_delivery(raw({"leaks": {"type": "something_new", "run_id": 1}}))
        self.assertEqual(delivery.type, "something_new")
        delivery = parse_delivery(raw({"leaks": {"type": 3}}))
        self.assertIsNone(delivery.type)

    def test_annotations_resolve(self):
        hints = typing.get_type_hints(parse_delivery)
        self.assertIs(hints["return"], WebhookDelivery)
        self.assertEqual(
            typing.get_type_hints(WebhookDelivery.__init__)["alert"],
            typing.Union[types.WebhookCredentialAlert, types.WebhookRawAlert, types.WebhookDarkWebAlert],
        )

    def test_repr_never_shows_the_alert(self):
        delivery = parse_delivery(raw(credential_alert(**section(40, 1))))
        text = repr(delivery)
        self.assertEqual(text, "WebhookDelivery(type='leak', delivery_id='40:1', check_id=40, section=1)")
        self.assertNotIn(PASSWORD, text)


if __name__ == "__main__":
    unittest.main()
