
# LeakRadar Async Python Client

A user-friendly, asynchronous Python 3 wrapper for the [LeakRadar.io](https://leakradar.io) API.

## Documentation

See the full API documentation here: <https://api.leakradar.io/docs>

## Features

- Async/await support using `httpx`.
- Authentication via Bearer token.
- Customizable User-Agent.
- Error handling with custom exceptions (no retries).
- Easy-to-use methods for:
  - Advanced, email and domain search
  - Unlocking leaks (all or specific)
  - Requesting CSV exports for leaks
  - Metadata search
  - Managing notifications
  - Fetching and updating profile information

## Requirements

- Python 3.7+
- `httpx` library (installed automatically)

## Installation

```bash
pip install leakradar
```

## Usage

1. Retrieve your API key from [LeakRadar.io Profile](https://leakradar.io/profile).
2. Create a client instance, passing your Bearer token.

```python
import asyncio
from leakradar import LeakRadarClient

async def main():
    token = "YOUR_BEARER_TOKEN"
    async with LeakRadarClient(token=token) as client:
        profile = await client.get_profile()
        print("User Profile:", profile)

        # Perform an advanced search
        results = await client.search_advanced(username="john.doe@example.com")
        print("Search Results:", results)

        # Unlock all matching leaks
        unlocked = await client.unlock_all_advanced({"username": "john.doe@example.com"})
        print("Unlocked leaks:", unlocked)

        # Request an export of the leaks
        export_info = await client.export_advanced(username="john.doe@example.com")
        print("Export created:", export_info)

asyncio.run(main())
```

## Error Handling

All errors raise a `LeakRadarAPIError` subclass. For example:
- `BadRequestError` (400)
- `UnauthorizedError` (401)
- `ForbiddenError` (403)
- `NotFoundError` (404)
- `ValidationError` (422)
- `TooManyRequestsError` (429)

## Contributing

Please open an issue or PR if you have suggestions or improvements.

## License

[MIT](https://opensource.org/licenses/MIT)
