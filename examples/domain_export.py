"""
Export the stealer log records of a domain to a ZIP file: queue the export, wait, download.

    export LEAKRADAR_API_KEY="your API key"
    python examples/domain_export.py example.com employees example-employees.zip

The category is "all", "employees", "customers" or "third_parties". The export holds the records
you have unlocked (every record on plans where everything is readable) and spends no points. An
account runs at most 5 exports at once.
"""

import os
import sys
from typing import Any, Mapping, Optional

from leakradar import ExportFailedError, LeakRadar, LeakRadarAPIError, WaitTimeoutError


def main(domain: str, category: str, path: str) -> None:
    with LeakRadar() as client:
        queued = client.export_domain_leaks(domain, category, format="csv")
        print("Export", queued["export_id"], "queued")

        status: Optional[str] = None

        def show(export: Mapping[str, Any]) -> None:
            # Called after every read (every 3 seconds): print the status when it changes.
            nonlocal status
            if export.get("status") != status:
                status = export.get("status")
                print("Status:", status)

        try:
            export = client.wait_for_export(queued, timeout=60 * 60, on_progress=show)
        except ExportFailedError:
            sys.exit("The export failed on the server: queue it again.")
        except WaitTimeoutError as error:
            sys.exit(f"Still not done after {error.waited:.0f} s; it keeps running, check list_exports() later.")

        if client.download_export(export, path) is None:
            print("Nothing to export: no record matched.")
        else:
            print("Saved to", path)


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    if not os.environ.get("LEAKRADAR_API_KEY"):
        sys.exit("Set LEAKRADAR_API_KEY to your API key first.")
    try:
        main(sys.argv[1], sys.argv[2], sys.argv[3])
    except LeakRadarAPIError as error:
        sys.exit(f"LeakRadar answered {error.status_code}: {error.message}")
