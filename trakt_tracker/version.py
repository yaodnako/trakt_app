from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version


def app_version() -> str:
    try:
        return version("trakt-tracker")
    except PackageNotFoundError:
        return "0.0.0"
