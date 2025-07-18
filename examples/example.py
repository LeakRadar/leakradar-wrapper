
import asyncio
from leakradar import LeakRadarClient

async def main():
    # Replace with your actual Bearer token
    token = "YOUR_BEARER_TOKEN"

    async with LeakRadarClient(token=token) as client:
        # Fetch the authenticated user's profile
        profile = await client.get_profile()
        print("Profile:", profile)

        # Perform an advanced search with a username filter
        advanced_search_results = await client.search_advanced(username="john.doe@example.com")
        print("Advanced Search Results:", advanced_search_results)

        # Unlock all leaks matching advanced search filters
        unlocked_leaks = await client.unlock_all_advanced(
            {"username": ["john.doe@example.com"]},
            max_leaks=10
        )
        print("Unlocked Leaks:", unlocked_leaks)

        # Request an export of advanced search results
        export_info = await client.export_advanced(username="john.doe@example.com")
        print("Advanced export created:", export_info)

        # Fetch domain report
        domain_report = await client.get_domain_report(domain="tesla.com")
        print("Domain Report:", domain_report)

        # Get domain customers
        domain_customers = await client.get_domain_customers(domain="tesla.com", page=1, page_size=10)
        print("Domain Customers:", domain_customers)

        # Get domain employees
        domain_employees = await client.get_domain_employees(domain="tesla.com", page=1, page_size=10)
        print("Domain Employees:", domain_employees)

        # Unlock specific leaks by ID
        leaks_to_unlock = ["bcaeef31cd13a480fc577cc3bb9f71077679f65c39e513f5549a964be3755ad0", "03e9134b776b2cb09b08d990874e6f6aeb7a953a663e515143325fb4fa708734"]  # Replace with actual leak IDs
        unlocked_specific_leaks = await client.unlock_specific_leaks(leaks_to_unlock)
        print("Unlocked Specific Leaks:", unlocked_specific_leaks)

        # Search for leaks associated with an email
        email_search_results = await client.search_email(email="john.doe@example.com")
        print("Email Search Results:", email_search_results)

        # Request an export for the email leaks
        email_export = await client.export_email_leaks(email="john.doe@example.com")
        print("Email export created:", email_export)

        # Search some page metadata
        meta_results = await client.search_metadata({"title": "Tesla"})
        print("Metadata Search:", meta_results)

        # Unlock all email leaks
        unlocked_email_leaks = await client.unlock_email_leaks(email="john.doe@example.com", max_leaks=10)
        print("Unlocked Email Leaks:", unlocked_email_leaks)

if __name__ == "__main__":
    asyncio.run(main())
