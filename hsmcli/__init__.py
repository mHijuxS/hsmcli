"""HackSmarter CLI package."""

from .api_client import client_version
from .cli import main

# Read from the installed distribution rather than hardcoded here, so
# pyproject.toml stays the single source of truth for the version.
__version__ = client_version()
__author__ = "mHijuxS"

__all__ = ["main", "__version__"]
