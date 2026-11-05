"""
Search an email address with the synchronous client.

    pip install leakradar
    export LEAKRADAR_API_KEY="your API key"
    python examples/quickstart_sync.py john.doe@example.com

Searching spends no points: the credentials stay masked until you unlock them.
"""

import os
import sys

from leakradar import LeakRadar, LeakRadarAPIError


def main(email: str) -> None:
    # LeakRadar() reads the key from LEAKRADAR_API_KEY; LeakRadar(token="...") works too.
    with LeakRadar() as client:
        profile = client.get_profile()
        print("Signed in as", profile["email"])

        results = client.search_email(email, page_size=10)
        print(f"{email}: {results['total']} records, {results['total_unlocked']} unlocked")
        for leak in results["items"]:
            print(" ", leak.get("url") or "-", leak.get("username") or leak.get("username_masked"))

        daily = client.rate_limits()["daily"]
        print("Daily searches:", daily.get("used"), "used out of", daily.get("limit") or "no cap")


if __name__ == "__main__":
    if not os.environ.get("LEAKRADAR_API_KEY"):
        sys.exit("Set LEAKRADAR_API_KEY to your API key first.")
    try:
        main(sys.argv[1] if len(sys.argv) > 1 else "john.doe@example.com")
    except LeakRadarAPIError as error:
        sys.exit(f"LeakRadar answered {error.status_code}: {error.message}")
