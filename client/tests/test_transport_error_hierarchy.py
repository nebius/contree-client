"""Backend exceptions do not leak into the public Contree hierarchy."""

from __future__ import annotations

import importlib
from types import ModuleType

import aiohttp
import httpx
import requests
import urllib3


def test_transport_types_have_only_contree_bases(exceptions: ModuleType) -> None:
    assert exceptions.ContreeTransportError.__bases__ == (exceptions.ContreeError,)
    assert exceptions.ContreeConnectionError.__bases__ == (
        exceptions.ContreeTransportError,
    )
    assert exceptions.ContreeTimeoutError.__bases__ == (
        exceptions.ContreeTransportError,
    )
    assert exceptions.DecompressionError.__bases__ == (
        exceptions.ContreeTransportError,
    )
    assert exceptions.SSEStreamError.__bases__ == (exceptions.ContreeTransportError,)


def test_adapters_export_no_backend_specific_contree_errors() -> None:
    removed = {
        "http": ("ContreeHttpConnectionError", "ContreeHttpTimeoutError"),
        "urllib3": (
            "ContreeUrllib3ConnectionError",
            "ContreeUrllib3TimeoutError",
        ),
        "requests": (
            "ContreeRequestsConnectionError",
            "ContreeRequestsTimeoutError",
        ),
        "httpx": ("ContreeHttpxConnectionError", "ContreeHttpxTimeoutError"),
        "aiohttp": (
            "ContreeAiohttpConnectionError",
            "ContreeAiohttpTimeoutError",
            "ContreeAiohttpServerTimeoutError",
            "ContreeAiohttpStreamError",
            "ContreeAiohttpSSLError",
            "ContreeAiohttpFingerprintError",
            "ContreeAiohttpAPIError",
        ),
    }
    for module_name, names in removed.items():
        module = importlib.import_module(f"contree_client.{module_name}")
        for name in names:
            assert not hasattr(module, name)


def test_common_errors_do_not_inherit_native_backend_types(
    exceptions: ModuleType,
) -> None:
    connection = exceptions.ContreeConnectionError(original=OSError("failed"))
    timeout = exceptions.ContreeTimeoutError(original=TimeoutError("timed out"))
    transport = exceptions.ContreeTransportError(original=ValueError("bad URL"))

    assert not isinstance(connection, OSError)
    assert not isinstance(timeout, TimeoutError)
    assert not isinstance(transport, urllib3.exceptions.HTTPError)
    assert not isinstance(transport, requests.RequestException)
    assert not isinstance(transport, httpx.HTTPError)
    assert not isinstance(transport, aiohttp.ClientError)
