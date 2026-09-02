"""Public exception hierarchy and metadata contracts."""

from __future__ import annotations

import importlib
from types import ModuleType

import pytest


def test_public_tree_is_backend_independent(
    generated_package: ModuleType,
    exceptions: ModuleType,
) -> None:
    assert issubclass(exceptions.ContreeTransportError, exceptions.ContreeError)
    assert issubclass(
        exceptions.ContreeConnectionError, exceptions.ContreeTransportError
    )
    assert issubclass(exceptions.ContreeTimeoutError, exceptions.ContreeTransportError)
    assert issubclass(exceptions.DecompressionError, exceptions.ContreeTransportError)
    assert issubclass(exceptions.SSEStreamError, exceptions.ContreeTransportError)
    assert issubclass(exceptions.ContreeAPIError, exceptions.ContreeError)
    assert not issubclass(exceptions.ContreeAPIError, exceptions.ContreeTransportError)
    assert generated_package.ContreeTransportError is exceptions.ContreeTransportError
    for removed in (
        "ContreeHTTPError",
        "ContreeStreamError",
        "ContreeSSLError",
        "ContreeConnectionClosedError",
        "ContreeProtocolError",
    ):
        assert not hasattr(exceptions, removed)
        assert not hasattr(generated_package, removed)


def test_transport_error_defaults_to_nonretryable(exceptions: ModuleType) -> None:
    native = ConnectionError("refused")
    error = exceptions.ContreeConnectionError(original=native)

    assert error.retryable is False
    assert error.original is native
    assert str(error) == "refused"


def test_transport_error_preserves_explicit_cause(exceptions: ModuleType) -> None:
    native = ConnectionError("refused")

    with pytest.raises(exceptions.ContreeConnectionError) as caught:
        raise exceptions.ContreeConnectionError(
            original=native,
            retryable=True,
        ) from native

    assert caught.value.retryable is True
    assert caught.value.original is native
    assert caught.value.__cause__ is native


def test_api_error_metadata_is_unchanged(
    runtime: ModuleType,
    exceptions: ModuleType,
) -> None:
    native = RuntimeError("status failure")
    error = runtime.error_for_response(
        runtime.ResponseData(
            status=410,
            headers={"retry-after": "5"},
            body=b'{"error":"gone","traceback":["frame"]}',
        ),
        original=native,
    )

    assert isinstance(error, exceptions.GoneError)
    assert error.status == 410
    assert error.error == "gone"
    assert error.traceback == ["frame"]
    assert error.retry_after == 5
    assert error.original is native


def test_success_status_cannot_create_api_error(runtime: ModuleType) -> None:
    with pytest.raises(ValueError, match="successful response"):
        runtime.error_for_response(
            runtime.ResponseData(status=200, headers={}, body=b"{}")
        )


@pytest.mark.parametrize(
    ("module_name", "class_name"),
    [
        ("http", "ContreeClient"),
        ("urllib3", "ContreeClient"),
        ("requests", "ContreeClient"),
        ("httpx", "ContreeClient"),
        ("aiohttp", "ContreeAsyncClient"),
    ],
)
def test_unsupported_scheme_fails_before_transport(
    module_name: str,
    class_name: str,
    generated_package: ModuleType,
) -> None:
    module = importlib.import_module(f"contree_client.{module_name}")
    with pytest.raises(ValueError, match="unsupported base_url scheme"):
        getattr(module, class_name)("token", base_url="ftp://example.test")
