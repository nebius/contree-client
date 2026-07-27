"""The opt-in retry policy for buffered requests."""

from __future__ import annotations

import importlib
import io
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from types import ModuleType
from typing import Any

import pytest

from tests.conftest import (
    BACKENDS,
    PROJECT,
    TOKEN,
    client_class,
    make_client,
    make_invoke,
)
from tests.stub_server import (
    FLAKY_OPERATION_UUID,
    RETRY_AFTER_OPERATION_UUID,
    StubServer,
)


@pytest.fixture(params=BACKENDS)
def invoke_retry(
    request: pytest.FixtureRequest,
    generated_package: ModuleType,
    stub_server: StubServer,
) -> Callable[..., Any]:
    """Like ``invoke``, but the clients carry a fast retry policy."""
    runtime = importlib.import_module("contree_client.runtime")
    policy = runtime.RetryPolicy(delays=(0.01,))
    backend: str = request.param

    def factory() -> Any:
        return client_class(backend)(
            TOKEN,
            base_url=stub_server.base_url,
            project=PROJECT,
            retry=policy,
        )

    return make_invoke(backend, factory)


def test_retries_5xx_until_success(
    invoke_retry: Callable[..., Any], stub_server: StubServer
) -> None:
    operation = invoke_retry("get_operation_status", FLAKY_OPERATION_UUID)

    assert str(operation.status) == "SUCCESS"
    requests = [c for c in stub_server.captured if FLAKY_OPERATION_UUID in c.path]
    assert len(requests) == 3


def test_retries_425_honoring_retry_after(
    invoke_retry: Callable[..., Any], stub_server: StubServer
) -> None:
    operation = invoke_retry("get_operation_status", RETRY_AFTER_OPERATION_UUID)

    assert str(operation.status) == "SUCCESS"
    requests = [c for c in stub_server.captured if RETRY_AFTER_OPERATION_UUID in c.path]
    assert len(requests) == 2


def test_no_retry_without_policy(
    invoke: Callable[..., Any],
    stub_server: StubServer,
    exceptions: ModuleType,
) -> None:
    with pytest.raises(exceptions.ServerError):
        invoke("get_operation_status", FLAKY_OPERATION_UUID)

    requests = [c for c in stub_server.captured if FLAKY_OPERATION_UUID in c.path]
    assert len(requests) == 1


def test_max_attempts_bounds_retries(
    generated_package: ModuleType,
    stub_server: StubServer,
    exceptions: ModuleType,
) -> None:
    runtime = importlib.import_module("contree_client.runtime")
    http = importlib.import_module("contree_client.http")
    policy = runtime.RetryPolicy(delays=(0.01,), max_attempts=2)
    with (
        http.ContreeClient(
            TOKEN,
            base_url=stub_server.base_url,
            retry=policy,
        ) as client,
        pytest.raises(exceptions.ServerError),
    ):
        client.get_operation_status(FLAKY_OPERATION_UUID)

    requests = [c for c in stub_server.captured if FLAKY_OPERATION_UUID in c.path]
    assert len(requests) == 2


UA_TRANSPORT_TOKENS = {
    "http": "http.client",
    "urllib3": "urllib3/",
    "requests": "requests/",
    "httpx": "httpx/",
    "httpx_async": "httpx/",
    "aiohttp": "aiohttp/",
}


@pytest.mark.parametrize("backend", BACKENDS)
def test_user_agent_names_transport_library(
    backend: str,
    generated_package: ModuleType,
    stub_server: StubServer,
) -> None:
    invoke = make_invoke(backend, lambda: make_client(backend, stub_server.base_url))
    invoke("whoami")

    ua = stub_server.last.headers["user-agent"]
    assert ua.startswith("contree-client/")
    assert UA_TRANSPORT_TOKENS[backend] in ua
    assert " Python/" in ua


def test_identity_leads_user_agent(
    generated_package: ModuleType, stub_server: StubServer
) -> None:
    http = importlib.import_module("contree_client.http")
    with http.ContreeClient(
        TOKEN,
        base_url=stub_server.base_url,
        identity="my-app/9.9",
    ) as client:
        client.whoami()

    ua = stub_server.last.headers["user-agent"]
    assert ua.startswith("my-app/9.9 contree-client/")
    assert "http.client" in ua


def test_retry_replays_body_from_initial_offset(
    generated_package: ModuleType,
) -> None:
    """P1-09: a retry must resend exactly the bytes of the first attempt."""
    base = importlib.import_module("contree_client.base")
    runtime = importlib.import_module("contree_client.runtime")

    class FlakyClient(base.ContreeSyncClient):
        retryable_errors = (ConnectionError,)

        def __init__(self) -> None:
            super().__init__("token", retry=runtime.RetryPolicy(delays=(0.0,)))
            self.attempts: list[bytes] = []

        def request(self, spec: runtime.RequestSpec) -> runtime.ResponseData:
            self.attempts.append(spec.body.read())
            if len(self.attempts) == 1:
                raise ConnectionError("dropped mid-flight")
            return runtime.ResponseData(status=200, headers={}, body=b"{}")

        def stream(self, spec, auto_decompress=True):
            raise NotImplementedError

        def close(self) -> None:
            pass

    body = io.BytesIO(b"abcdef")
    body.seek(2)  # the caller deliberately starts mid-stream
    client = FlakyClient()

    client.call(
        runtime.RequestSpec(method="PUT", path="/x", body=body, idempotent=True)
    )

    # both attempts transmitted the same bytes, from the initial offset
    assert client.attempts == [b"cdef", b"cdef"]


def test_retry_policy_validation(generated_package: ModuleType) -> None:
    """P2-14: nonsense retry configuration must fail at construction."""
    runtime = importlib.import_module("contree_client.runtime")

    with pytest.raises(ValueError, match="delays"):
        runtime.RetryPolicy(delays=())
    with pytest.raises(ValueError, match="finite"):
        runtime.RetryPolicy(delays=(float("inf"),))
    with pytest.raises(ValueError, match="finite"):
        runtime.RetryPolicy(delays=(-1.0,))
    with pytest.raises(ValueError, match="max_attempts"):
        runtime.RetryPolicy(max_attempts=0)
    with pytest.raises(ValueError, match="max_attempts"):
        runtime.RetryPolicy(max_attempts=2.5)
    with pytest.raises(ValueError, match="max_attempts"):
        runtime.RetryPolicy(max_attempts=float("nan"))
    runtime.RetryPolicy(delays=(0.0, 1.0), max_attempts=1)  # valid


def test_parse_retry_after(generated_package: ModuleType) -> None:
    """P2-14: delta-seconds, zero, negatives and HTTP-date."""
    runtime = importlib.import_module("contree_client.runtime")

    assert runtime.parse_retry_after("7") == 7.0
    assert runtime.parse_retry_after("0") == 0.0  # zero is a real value
    assert runtime.parse_retry_after("-5") == 0.0
    assert runtime.parse_retry_after("soon") is None
    assert runtime.parse_retry_after(None) is None
    # float() parses these, but an infinite sleep must never happen
    assert runtime.parse_retry_after("inf") is None
    assert runtime.parse_retry_after("Infinity") is None
    assert runtime.parse_retry_after("nan") is None

    moment = datetime.now(timezone.utc) + timedelta(seconds=30)
    delay = runtime.parse_retry_after(format_datetime(moment, usegmt=True))
    assert delay is not None
    assert 25 <= delay <= 31
    # a date in the past clamps to zero
    past = datetime.now(timezone.utc) - timedelta(seconds=30)
    assert runtime.parse_retry_after(format_datetime(past, usegmt=True)) == 0.0


def test_retry_policy_finite_default_budget(
    generated_package: ModuleType,
) -> None:
    """P1-05: unbounded retries are an explicit choice, not a default."""
    runtime = importlib.import_module("contree_client.runtime")
    assert runtime.RetryPolicy().max_attempts == 10
    assert runtime.RetryPolicy(max_attempts=None).max_attempts is None


def test_post_is_not_retried_by_default(
    invoke_retry: Callable[..., Any],
    stub_server: StubServer,
    exceptions: ModuleType,
) -> None:
    """P1-05: a lost/failed response after POST could mean the server
    already executed it - replaying must be an explicit opt-in."""
    with pytest.raises(exceptions.ServerError):
        invoke_retry("spawn_instance", "flaky", "tag:busybox:latest")

    spawns = [c for c in stub_server.captured if c.path == "/v1/instances"]
    assert len(spawns) == 1


@pytest.mark.parametrize("command", ["flaky-425", "flaky-429"])
def test_post_retried_on_425_and_429_without_retry_unsafe(
    command: str,
    invoke_retry: Callable[..., Any],
    stub_server: StubServer,
) -> None:
    """425/429 are a backend contract: the request was rejected before
    any processing, so even a POST replays without `retry_unsafe`."""
    response = invoke_retry("spawn_instance", command, "tag:busybox:latest")
    assert str(response.uuid)

    spawns = [c for c in stub_server.captured if c.path == "/v1/instances"]
    assert len(spawns) == 2


def test_post_retried_with_explicit_retry_unsafe(
    generated_package: ModuleType,
    stub_server: StubServer,
) -> None:
    runtime = importlib.import_module("contree_client.runtime")
    http = importlib.import_module("contree_client.http")
    policy = runtime.RetryPolicy(delays=(0.01,), retry_unsafe=True)
    with http.ContreeClient(
        TOKEN, base_url=stub_server.base_url, retry=policy
    ) as client:
        response = client.spawn_instance("flaky", "tag:busybox:latest")
    assert str(response.uuid)

    spawns = [c for c in stub_server.captured if c.path == "/v1/instances"]
    assert len(spawns) == 2
