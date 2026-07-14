from __future__ import annotations

import importlib
import logging
from types import ModuleType

import pytest

from tests.conftest import PROJECT, TOKEN, make_client
from tests.stub_server import IMAGE_UUID, OPERATION_UUID, StubServer


def test_base_logger_defaults(generated_package: ModuleType) -> None:
    types_module = importlib.import_module("contree_client.types")
    assert types_module.logger.name == "contree_client"
    # explicitly ERROR: silent unless the user opts in
    assert types_module.logger.level == logging.ERROR


def test_set_log_level(generated_package: ModuleType) -> None:
    types_module = importlib.import_module("contree_client.types")
    try:
        types_module.set_log_level(logging.DEBUG)
        assert types_module.logger.level == logging.DEBUG
        types_module.set_log_level("INFO")
        assert types_module.logger.level == logging.INFO
    finally:
        types_module.set_log_level(logging.ERROR)


def test_transports_use_child_loggers(generated_package: ModuleType) -> None:
    types_logger = importlib.import_module("contree_client.types").logger
    cases = {
        "http": ("ContreeClient",),
        "urllib3": ("ContreeClient",),
        "requests": ("ContreeClient",),
        "httpx": ("ContreeClient", "ContreeAsyncClient"),
        "aiohttp": ("ContreeAsyncClient",),
    }
    for backend, class_names in cases.items():
        module = importlib.import_module(f"contree_client.{backend}")
        for class_name in class_names:
            log = getattr(module, class_name).log
            assert log.name == f"contree_client.{backend}"
            assert log.parent is types_logger


def test_silent_by_default(
    generated_package: ModuleType,
    stub_server: StubServer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Root logging at DEBUG is not enough: the package logger level
    is ERROR, so nothing is emitted without an explicit opt-in."""
    with caplog.at_level(logging.DEBUG):
        client = make_client("requests", stub_server.base_url)
        try:
            client.whoami()
        finally:
            client.close()
    assert not [
        record for record in caplog.records if record.name.startswith("contree_client")
    ]


def test_debug_logging_when_enabled(
    generated_package: ModuleType,
    stub_server: StubServer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG, logger="contree_client"):
        client = make_client("requests", stub_server.base_url)
        try:
            client.whoami()
        finally:
            client.close()
    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == "contree_client.requests"
    ]
    request_line = next(m for m in messages if m.startswith("GET http://"))
    # raw headers are logged, but Authorization is redacted
    assert "'Authorization': '<redacted>'" in request_line
    assert f"'Project': '{PROJECT}'" in request_line
    assert not any(TOKEN in message for message in messages)
    # the raw response is logged too: status, headers and body
    response_line = next(m for m in messages if "-> 200" in m)
    assert "token_uuid" in response_line


def test_debug_logs_request_and_response_bodies(
    generated_package: ModuleType,
    stub_server: StubServer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG, logger="contree_client"):
        client = make_client("requests", stub_server.base_url)
        try:
            client.update_image_tag(IMAGE_UUID, tag="busybox:custom")
            client.upload_file(b"\x00\x01\x02binary")
        finally:
            client.close()
    messages = [record.getMessage() for record in caplog.records]
    # JSON request body is logged verbatim
    assert any('body={"tag": "busybox:custom"}' in m for m in messages)
    # JSON response body is logged verbatim
    assert any('"tag": "busybox:custom"' in m and "-> 200" in m for m in messages)
    # binary bodies are logged as a size marker, not raw bytes
    assert any(
        "body=<binary 9B Content-Type='application/octet-stream'>" in m
        for m in messages
    )


def test_debug_never_logs_credentials(
    generated_package: ModuleType,
    models: ModuleType,
    stub_server: StubServer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """CG-001: secrets inside JSON bodies must be structurally
    redacted, exactly like sensitive headers."""
    secret = "s3cr3t-registry-password"
    registry = models.ImageImportRegistry(
        url="docker://registry.example.com/busybox:latest",
        credentials=models.ImageImportRegistryCredentials(
            username="deploy-user",
            password=secret,
        ),
    )
    with caplog.at_level(logging.DEBUG, logger="contree_client"):
        client = make_client("requests", stub_server.base_url)
        try:
            client.import_image(registry, tag="busybox:latest")
        finally:
            client.close()
    messages = [record.getMessage() for record in caplog.records]
    assert messages
    # the actual secret value never appears in any record
    assert not any(secret in message for message in messages)
    # while the rest of the request body stays useful
    request_line = next(m for m in messages if "images/import" in m and "body=" in m)
    assert '"url": "docker://registry.example.com/busybox:latest"' in request_line
    assert '"credentials": "<redacted>"' in request_line


def test_debug_logs_sse_events(
    generated_package: ModuleType,
    stub_server: StubServer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG, logger="contree_client"):
        client = make_client("requests", stub_server.base_url)
        try:
            list(client.iter_operation_events(OPERATION_UUID))
        finally:
            client.close()
    events = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("sse event:")
    ]
    assert len(events) == 3
    assert "EventDataInit" in events[0]
    assert "EventDataSpawn" in events[1]
    assert "EventDataExit" in events[2]


def test_autodetect_logs_choice(
    generated_package: ModuleType,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sync = importlib.import_module("contree_client.sync")
    with caplog.at_level(logging.DEBUG, logger="contree_client"):
        sync.detect_backend()
    assert any(
        "autodetected backend: requests" in record.getMessage()
        for record in caplog.records
        if record.name == "contree_client.sync"
    )
