"""Autodetected synchronous client.

Tries the installed backends in priority order and exposes the first
importable one as ``ContreeClient``::

    from contree_client.sync import ContreeClient

The stdlib ``http.client`` backend needs no third-party packages, so
detection always succeeds; install ``contree-client[requests]``,
``[urllib3]`` or ``[httpx]`` to get a higher-priority backend.
"""

from __future__ import annotations

from importlib import import_module

from . import base
from .types import logger

log = logger.getChild("sync")

# Ordered by ecosystem popularity - PyPI download totals per ClickPy
# (https://clickpy.clickhouse.com, PyPI analytics on ClickHouse):
# requests ~34B, urllib3 ~39B (mostly transitive - it ships inside
# requests/botocore), httpx ~7.6B. requests must precede urllib3
# regardless: requests depends on urllib3, so the urllib3 import
# always succeeds and would shadow the higher-level library the user
# actually chose.
BACKEND_PRIORITY = ("requests", "urllib3", "httpx", "http")


def detect_backend() -> tuple[str, type[base.ContreeSyncClient]]:
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
        client_class: type[base.ContreeSyncClient] = module.ContreeClient
        log.debug("autodetected backend: %s", name)
        return name, client_class
    raise ImportError(
        "no synchronous contree-client backend could be imported;"
        " install one of:\n"
        "  pip install contree-client[requests]\n"
        "  pip install contree-client[urllib3]\n"
        "  pip install contree-client[httpx]"
    )


BACKEND, ContreeClient = detect_backend()
