"""Autodetected asynchronous client.

Tries the installed backends in priority order and exposes the first
importable one as ``ContreeAsyncClient``::

    from contree_client.asyncio import ContreeAsyncClient

Raises an ImportError with an installation suggestion when neither
aiohttp nor httpx is available.
"""

from __future__ import annotations

from importlib import import_module

from . import base
from .types import logger

log = logger.getChild("asyncio")

# Ordered by ecosystem popularity - PyPI download totals per ClickPy
# (https://clickpy.clickhouse.com): aiohttp ~10B, httpx ~7.6B.
BACKEND_PRIORITY = ("aiohttp", "httpx")


def detect_backend() -> tuple[str, type[base.ContreeAsyncClient]]:
    for name in BACKEND_PRIORITY:
        try:
            module = import_module(f"contree_client.{name}")
        except ModuleNotFoundError as exc:
            # only the backend's own missing dependency (or a wholly
            # absent backend module) means "not installed"; anything
            # else is a real error to surface
            if exc.name not in (name, f"contree_client.{name}"):
                raise
            continue
        client_class: type[base.ContreeAsyncClient] = module.ContreeAsyncClient
        log.debug("autodetected backend: %s", name)
        return name, client_class
    raise ImportError(
        "no asynchronous contree-client backend could be imported;"
        " install one of:\n"
        "  pip install contree-client[aiohttp]\n"
        "  pip install contree-client[httpx]"
    )


BACKEND, ContreeAsyncClient = detect_backend()
