import asyncio
from typing import Dict, Any

from leakradar import (
    LeakRadarClient,
    LeakRadarAPIError,
    ForbiddenError,
)

def plan_flag(plan: Dict[str, Any], key: str) -> bool:
    return bool((plan or {}).get(key) is True)

def print_skip(feature: str, plan_name: str) -> None:
    print(f"SKIP {feature}: not allowed on plan '{plan_name}'")

async def main():
    token = "YOUR_BEARER_TOKEN"

    async with LeakRadarClient(token=token) as client:
        profile = await client.get_profile()
        plan = profile.get("plan") or {}
        plan_name = plan.get("name") or plan.get("plan_name") or "Unknown"
        gates = {
            "advanced": plan_flag(plan, "advanced_search"),
            "domain": plan_flag(plan, "domain_search"),
            "email": plan_flag(plan, "email_search"),
            "raw": plan_flag(plan, "raw_search"),
            "combolist": plan_flag(plan, "combolist_search"),
            "dark_web": plan_flag(plan, "dark_web_search"),
        }
        print(f"Profile: {profile['email']} | Plan: {plan_name} | Gates: {gates}")

        usage = await client.get_usage()
        print("Usage:", usage)

        if gates["advanced"]:
            try:
                adv = await client.search_advanced(
                    username=["john.doe@example.com"],
                    url_scheme=["https"],
                    url_port=[443],
                    email_tld=["com"],
                    password_strength="strong",
                    force_and=True,
                    page_size=50,
                )
                total = adv.get("total", 0)
                page = adv.get("page", 1)
                page_size = adv.get("page_size", 50)
                pages = (total // page_size) + (1 if total % page_size else 0)
                print(f"Advanced: {total} items (page {page}/{pages})")

                export_adv = await client.export_advanced(
                    format="csv",
                    username=["john.doe@example.com"],
                )
                print("Advanced export queued:", export_adv)
                # Optional unlock example (kept commented for safety)
                # unlocked = await client.unlock_all_advanced({"username": ["john.doe@example.com"]}, max_leaks=10, list_id=123)
                # print("Unlocked (advanced):", len(unlocked))
            except LeakRadarAPIError as e:
                print(f"Advanced error: {e}")
        else:
            print_skip("Advanced", plan_name)

        if gates["domain"]:
            try:
                report = await client.get_domain_report("tesla.com", light=False)
                print("Domain report employees:", report.get("employees_compromised"))

                pdf_bytes = await client.get_domain_report_pdf("tesla.com")
                with open("tesla-report.pdf", "wb") as f:
                    f.write(pdf_bytes)
                print("Saved tesla-report.pdf")
            except LeakRadarAPIError as e:
                print(f"Domain error: {e}")
        else:
            print_skip("Domain", plan_name)

        if gates["email"]:
            try:
                email_results = await client.search_email(email="john.doe@example.com", page_size=50)
                print("Email total:", email_results.get("total", 0))

                email_export = await client.export_email_leaks("john.doe@example.com", format="csv")
                print("Email export queued:", email_export)
            except LeakRadarAPIError as e:
                print(f"Email error: {e}")
        else:
            print_skip("Email", plan_name)

        if gates["raw"]:
            try:
                raw = await client.raw_search(q='admin@admin.com', page_size=50, dedup=True)
                print("Raw hits:", raw.get("total", 0))

                estimate = await client.raw_count(q='admin@admin.com')
                print("Raw mass unlock estimate (parts):", estimate.get("total"))
            except LeakRadarAPIError as e:
                print(f"Raw error: {e}")
        else:
            print_skip("Raw", plan_name)

        if gates["combolist"]:
            try:
                combos = await client.search_combolist_email("john.doe@example.com", page_size=50)
                print("Combolist pairs:", combos.get("total", 0))

                report = await client.get_combolist_domain_report("example.com")
                print("Combolist domain report:", report)

                # Optional unlock / export of a whole scope (kept commented for safety: unlocks spend points)
                # task = await client.queue_combolist_unlock_task("email", value="john.doe@example.com", max_leaks=10)
                # print("Combolist unlock task:", task)
                # print(await client.export_combolist("email", value="john.doe@example.com", format="csv"))
            except LeakRadarAPIError as e:
                print(f"Combolist error: {e}")
        else:
            print_skip("Combolist", plan_name)

        if gates["dark_web"]:
            try:
                posts = await client.search_dark_web("example.com", page_size=10, sort_by="published_at")
                print("Dark web posts:", posts.get("total", 0))
            except LeakRadarAPIError as e:
                print(f"Dark web error: {e}")
        else:
            print_skip("Dark web", plan_name)

        try:
            runs = await client.list_notification_runs()
            print("Runs total:", runs.get("total", 0))
            items = runs.get("items") or []
            if items:
                run_id = items[0]["id"]
                run_items = await client.get_notification_run_items(run_id=run_id, page_size=10)
                print("Run items:", run_items.get("total", 0), run_items.get("item_type"))
                queue = await client.export_notification_run(run_id=run_id, format="csv")
                print("Run export queued:", queue)
        except ForbiddenError:
            print("SKIP Notification runs: not allowed for this account")
        except LeakRadarAPIError as e:
            print(f"Notification runs error: {e}")

        try:
            exports = await client.list_exports(page=1, page_size=20)
            print("Exports:", exports.get("total", 0))
        except LeakRadarAPIError as e:
            print(f"Exports error: {e}")

if __name__ == "__main__":
    asyncio.run(main())
