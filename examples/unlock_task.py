"""
Unlock the stealer log records of an email address in the background, following the task.

    export LEAKRADAR_API_KEY="your API key"
    python examples/unlock_task.py john.doe@example.com 100

Unlocking spends points: 1 per record you had not unlocked yet. The second argument caps what
this task may unlock (and spend). Records unlocked before a failure stay unlocked.
"""

import os
import sys
from typing import Any, Mapping

from leakradar import LeakRadar, LeakRadarAPIError, TaskFailedError, WaitTimeoutError


def show(status: Mapping[str, Any]) -> None:
    # Called after every read of the task (every 2 seconds), the last one included.
    print(f"{status.get('status')}: {status.get('updated') or 0} of at most {status.get('total') or 0}")


def main(email: str, max_leaks: int) -> None:
    with LeakRadar() as client:
        task = client.queue_email_unlock_task(email, max_leaks=max_leaks)
        print("Task", task["task_id"], "queued")
        try:
            done = client.wait_for_task(task, on_progress=show, timeout=30 * 60)
        except TaskFailedError as error:
            # error.code: insufficient_points, timeout, blocked, interrupted... (None when canceled)
            sys.exit(f"Task {error.task_id} {error.status} ({error.code}): {error.message}")
        except WaitTimeoutError:
            sys.exit("The task is still running on the server: wait for it again later.")
        print("Done:", done.get("updated"), "records processed")

        results = client.search_email(email, page_size=100)
        for leak in results["items"]:
            if leak.get("unlocked"):
                # Unlocked records carry the password in clear: here only the URL and the username.
                print(" ", leak.get("url") or "-", leak.get("username"))


if __name__ == "__main__":
    if len(sys.argv) != 3 or not sys.argv[2].isdigit():
        sys.exit(__doc__)
    if not os.environ.get("LEAKRADAR_API_KEY"):
        sys.exit("Set LEAKRADAR_API_KEY to your API key first.")
    try:
        main(sys.argv[1], int(sys.argv[2]))
    except LeakRadarAPIError as error:
        sys.exit(f"LeakRadar answered {error.status_code}: {error.message}")
