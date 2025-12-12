"""Asynchronous Python client for LeakRadar.io"""

from .client import (
    LeakRadarClient,
    LeakRadarAPIError,
    UnauthorizedError,
    ForbiddenError,
    BadRequestError,
    TooManyRequestsError,
    NotFoundError,
    ValidationError,
    ConflictError,
    PaymentRequiredError,
)

__all__ = [
    "LeakRadarClient",
    "LeakRadarAPIError",
    "UnauthorizedError",
    "ForbiddenError",
    "BadRequestError",
    "TooManyRequestsError",
    "NotFoundError",
    "ValidationError",
    "ConflictError",
    "PaymentRequiredError",
    "__version__",
]

# Bumped because the updated OpenAPI introduces breaking changes (notably several GET -> POST).
__version__ = "0.1.5"
