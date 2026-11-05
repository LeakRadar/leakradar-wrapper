"""
Search several email addresses at once with the asynchronous client.

    pip install leakradar
    export LEAKRADAR_API_KEY="your API key"
    python examples/quickstart_async.py john.doe@example.com jane.doe@example.com

The searches share one client: after a rate limit refusal, they all wait for the announced
delay together, then go on. Searching spends no points.
"""

import asyncio
import os
import sys
from typing import List

from leakradar import AsyncLeakRadar, LeakRadarAPIError


async def main(emails: List[str]) -> None:
    # AsyncLeakRadar() reads the key from LEAKRADAR_API_KEY; AsyncLeakRadar(token="...") works too.
    async with AsyncLeakRadar() as client:
        profile = await client.get_profile()
        print("Signed in as", profile["email"])

        searches = [client.search_email(email, page_size=10) for email in emails]
        for email, results in zip(emails, await asyncio.gather(*searches)):
            print(f"{email}: {results['total']} records, {results['total_unlocked']} unlocked")


if __name__ == "__main__":
    if not os.environ.get("LEAKRADAR_API_KEY"):
        sys.exit("Set LEAKRADAR_API_KEY to your API key first.")
    try:
        asyncio.run(main(sys.argv[1:] or ["john.doe@example.com", "jane.doe@example.com"]))
    except LeakRadarAPIError as error:
        sys.exit(f"LeakRadar answered {error.status_code}: {error.message}")
