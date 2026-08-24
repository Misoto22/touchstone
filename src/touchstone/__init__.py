"""Scheduled agent loops that audit a repository, and the harness that judges it."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("touchstone-agent")
except PackageNotFoundError:  # pragma: no cover - only an unpackaged source tree
    __version__ = "0+unknown"
