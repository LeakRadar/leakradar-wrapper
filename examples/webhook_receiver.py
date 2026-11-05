"""
Receive the alerts of a LeakRadar monitor on a webhook, with the standard library only.

1. Run the receiver. LeakRadar posts to a public http or https address on port 80 or 443: put
   it behind your HTTPS reverse proxy (it listens on 127.0.0.1), or set WEBHOOK_HOST=0.0.0.0.

       export WEBHOOK_SECRET="a long random secret"
       python examples/webhook_receiver.py 8080

2. Create a webhook alert channel pointing at it, with the same secret in a custom header, then
   attach a monitor to the channel, or send a test alert:

       channel = client.create_notification_method({
           "type": "webhook",
           "value": "https://hooks.example.com/leakradar",
           "headers": [{"name": "X-Webhook-Secret", "value": "a long random secret"}],
       })
       client.test_notification_method_with_data(channel["id"], "all")

LeakRadar does not sign its deliveries: the secret header is what tells the POSTs of your channel
apart. LeakRadar waits 20 seconds at most for the answer, and sends a POST again (twice at most)
after a timeout or a connection failure: the receiver answers at once, drops a POST it already
received, and handles the alert in a worker thread.
"""

import hmac
import os
import queue
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional, Set

from leakradar.webhooks import WebhookDelivery, parse_delivery

SECRET_HEADER = "X-Webhook-Secret"
SECRET = os.environ.get("WEBHOOK_SECRET", "").encode()
HOST = os.environ.get("WEBHOOK_HOST", "127.0.0.1")

alerts: "queue.Queue[WebhookDelivery]" = queue.Queue()
handled: Set[str] = set()  # the POSTs already received: keep them in a database in production
handled_lock = threading.Lock()


def delivery_key(delivery: WebhookDelivery) -> Optional[str]:
    """What identifies a POST, so that a repeated one is dropped. None for a test alert."""
    if delivery.check_id is not None:
        return delivery.delivery_id  # an all-datasets monitor: "<check_id>:<section>"
    run_id = delivery.alert.get("run_id")
    return None if run_id is None else f"run:{run_id}"


def handle(delivery: WebhookDelivery) -> None:
    """The work an alert calls for. Unlocked records carry passwords in clear: never log them."""
    alert = delivery.alert
    if alert["type"] == "leak":
        print(f"{alert.get('new_leaks_count')} new credentials for {alert.get('target')}: {alert.get('run_url')}")
        for record in alert.get("unlocked_leaks") or []:
            print("  unlocked:", record.get("url") or "-", record.get("username"))
    elif alert["type"] == "raw":
        print(f"{alert.get('new_parts_count')} new raw file parts for {alert.get('target')}: {alert.get('run_url')}")
    elif alert["type"] == "dark_web":
        print(f"{alert.get('new_items_count')} new dark web posts for {alert.get('target')}: {alert.get('run_url')}")


def worker() -> None:
    while True:
        delivery = alerts.get()
        try:
            handle(delivery)
        except Exception as exc:  # keep the worker running
            print(f"Could not handle {delivery!r}: {exc}", file=sys.stderr)


class Receiver(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        # The secret first, in constant time, before reading the body.
        received = (self.headers.get(SECRET_HEADER) or "").encode()
        if not SECRET or not hmac.compare_digest(received, SECRET):
            self.answer(403)
            return
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            delivery = parse_delivery(body, self.headers)
        except ValueError:
            self.answer(400)
            return
        self.answer(204)  # answer at once, the work happens in the worker thread
        if delivery.type is None:
            print("Connection test of the channel: OK")
            return
        key = delivery_key(delivery)
        if key is not None:
            with handled_lock:
                if key in handled:
                    return  # sent again after a timeout: already queued
                handled.add(key)
        alerts.put(delivery)

    def answer(self, status: int) -> None:
        self.send_response(status)
        if status != 204:
            self.send_header("Content-Length", "0")
        self.end_headers()


def main(port: int) -> None:
    if not SECRET:
        sys.exit("Set WEBHOOK_SECRET to the secret of your channel's header first.")
    threading.Thread(target=worker, daemon=True).start()
    server = ThreadingHTTPServer((HOST, port), Receiver)
    print(f"Listening on {HOST}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 8080)
