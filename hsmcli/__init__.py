"""HackSmarter CLI package."""

from .api_client import (
    APIError,
    AuthError,
    ForbiddenError,
    HsmcliError,
    HttpError,
    NotEnrolledError,
    TransportError,
    client_version,
)
from .cli import main

# Read from the installed distribution rather than hardcoded here, so
# pyproject.toml stays the single source of truth for the version.
__version__ = client_version()
__author__ = "mHijuxS"

__all__ = [
    "main",
    "__version__",
    # Catch these when using hsmcli as a library; all subclass HsmcliError.
    "HsmcliError",
    "HttpError",
    "AuthError",
    "ForbiddenError",
    "NotEnrolledError",
    "APIError",
    "TransportError",
]
