"""A terminal status must not discard unread output or process exits."""

from __future__ import annotations

import time
import zlib
from collections import Counter
from collections.abc import Callable
from typing import Any

import aiohttp
import httpx
import pytest
import requests
import urllib3

from contree_client import RetryPolicy, testing
from contree_client.exceptions import APIConnectionError
from tests import stub_server as server
from tests.conftest import BACKENDS, TOKEN, client_class, make_invoke


@pytest.fixture
def terminal_log(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    def configure(
        *,
        prefix: str = "eof",
        status: str = "SUCCESS",
        completion: bool = True,
        replay_error: int | None = None,
        replay_hang: float = 0.0,
        status_failures: int = 0,
        replay_interruptions: int = 0,
        replay_failures: int | None = None,
        replay_malformed: bool = False,
        replay_chunked: bool = False,
        replay_corrupt_gzip: bool = False,
    ) -> None:
        status_attempts = 0
        replay_attempts = 0

        def route(request: server.Captured, attempts: Counter[str]) -> server.Reply:
            nonlocal status_attempts, replay_attempts
            if request.path.endswith("/events"):
                cursor = int(request.headers.get("last-event-id", "-1"))
                if request.query.get("follow") == ["1"]:
                    prefix_events = {
                        "empty": [],
                        "eof": [server.EVENT_INIT, server.EVENT_SPAWN],
                        "error": [server.EVENT_INIT, server.EVENT_SPAWN],
                    }[prefix]
                    chunks = [
                        server.sse_frame(event)
                        for event in prefix_events
                        if event["id"] > cursor
                    ]
                    if prefix == "error":
                        chunks += server.BROKEN_SSE_FRAMES[2:]
                else:
                    replay_attempts += 1
                    if replay_error is not None and (
                        replay_failures is None or replay_attempts <= replay_failures
                    ):
                        return server.json_reply(
                            replay_error,
                            {"error": "unreadable log", "status": replay_error},
                        )
                    events = [
                        server.EVENT_INIT,
                        server.EVENT_SPAWN,
                        {
                            **server.EVENT_EXIT,
                            "data": {**server.EVENT_EXIT["data"], "code": 7},
                        },
                    ]
                    if completion:
                        events.append(
                            {
                                **server.EVENT_COMPLETION,
                                "data": {
                                    **server.EVENT_COMPLETION["data"],
                                    "status": status,
                                },
                            }
                        )
                    if replay_attempts <= replay_interruptions:
                        # Deliver exit before the interruption: recovery must
                        # resume after it, without losing or duplicating events.
                        events = [
                            event for event in events if event["type"] != "completion"
                        ]
                    chunks = [
                        server.sse_frame(event)
                        for event in events
                        if event["id"] > cursor
                    ]
                    if replay_attempts <= replay_interruptions and not replay_chunked:
                        chunks.append(b"event: sse_error\ndata: interrupted\n\n")
                    if replay_malformed:
                        chunks.append(b"data: invalid json\n\n")
                return server.Reply(
                    status=200,
                    content_type="text/event-stream",
                    stream_chunks=chunks,
                    corrupt_gzip=(
                        replay_corrupt_gzip and request.query.get("follow") != ["1"]
                    ),
                    truncate_chunked=(
                        replay_chunked
                        and request.query.get("follow") != ["1"]
                        and replay_attempts <= replay_interruptions
                    ),
                    hang=replay_hang if request.query.get("follow") != ["1"] else 0.0,
                )
            status_attempts += 1
            if status_attempts <= status_failures:
                return server.json_reply(
                    500, {"error": "status temporarily unavailable", "status": 500}
                )
            return server.json_reply(
                200, {**server.OPERATION_RESPONSE, "status": status}
            )

        monkeypatch.setattr(server, "route", route)

    return configure


@pytest.mark.parametrize("prefix", ["empty", "eof", "error"])
@pytest.mark.parametrize("status", ["SUCCESS", "FAILED", "CANCELLED"])
def test_terminal_probe_drains_retained_log(
    invoke: Callable[..., Any],
    stub_server: server.StubServer,
    terminal_log: Callable[..., None],
    prefix: str,
    status: str,
) -> None:
    terminal_log(prefix=prefix, status=status)
    events = invoke(
        "follow_operation_events", server.OPERATION_UUID, timeout=2.0, collect=True
    )

    assert [event.id for event in events] == [0, 1, 2, 3]
    assert events[2].type == "exit"
    assert events[2].data.code == 7
    assert events[3].data["status"] == status
    requests = [r for r in stub_server.captured if r.path.endswith("/events")]
    assert len(requests) >= 2
    assert "follow" not in requests[-1].query
    assert requests[-1].headers.get("last-event-id") == (
        None if prefix == "empty" else "1"
    )


def test_transient_status_failure_resumes_without_duplicate_events(
    invoke: Callable[..., Any],
    stub_server: server.StubServer,
    terminal_log: Callable[..., None],
) -> None:
    terminal_log(status_failures=1)
    events = invoke(
        "follow_operation_events", server.OPERATION_UUID, timeout=2.0, collect=True
    )

    assert [event.id for event in events] == [0, 1, 2, 3]
    requests = [r for r in stub_server.captured if r.path.endswith("/events")]
    assert len(requests) == 3
    assert requests[1].query.get("follow") == ["1"]
    assert requests[1].headers.get("last-event-id") == "1"


@pytest.mark.parametrize("filters", [{"spid": 1}, {"since": 0}, {"last_event_id": 0}])
def test_filtered_retained_log_can_end_without_completion(
    invoke: Callable[..., Any],
    stub_server: server.StubServer,
    terminal_log: Callable[..., None],
    filters: dict[str, int],
) -> None:
    terminal_log(completion=False)
    events = invoke(
        "follow_operation_events", server.OPERATION_UUID, **filters, collect=True
    )
    assert events[-1].type == "exit"
    requests = [r for r in stub_server.captured if r.path.endswith("/events")]
    assert len(requests) >= 2
    assert requests[-1].headers["last-event-id"] == "1"
    assert requests[-1].query == {
        key: [str(value)] for key, value in filters.items() if key != "last_event_id"
    }


@pytest.mark.parametrize("method", ["follow_operation_events", "wait_operation"])
def test_unfiltered_terminal_log_requires_completion(
    invoke: Callable[..., Any],
    terminal_log: Callable[..., None],
    stub_server: server.StubServer,
    method: str,
) -> None:
    terminal_log(completion=False)
    with pytest.raises(APIConnectionError, match="ended without completion"):
        invoke(
            method, server.OPERATION_UUID, collect=method == "follow_operation_events"
        )
    # A status fetch must not turn the incomplete event log into success.
    assert len([r for r in stub_server.captured if not r.path.endswith("/events")]) == 1


@pytest.mark.parametrize("status", [404, 500])
def test_unavailable_retained_log_raises_without_retrying_forever(
    invoke: Callable[..., Any],
    terminal_log: Callable[..., None],
    stub_server: server.StubServer,
    status: int,
) -> None:
    terminal_log(replay_error=status)
    with pytest.raises(APIConnectionError, match="event log") as caught:
        invoke("follow_operation_events", server.OPERATION_UUID, collect=True)
    assert caught.value.__cause__ is not None
    requests = [r for r in stub_server.captured if r.path.endswith("/events")]
    assert len(requests) == (2 if status == 404 else 4)


@pytest.mark.parametrize("status", [404, 500])
def test_wait_operation_fails_when_terminal_log_is_unavailable(
    invoke: Callable[..., Any],
    terminal_log: Callable[..., None],
    status: int,
) -> None:
    terminal_log(replay_error=status)
    with pytest.raises(APIConnectionError, match="event log"):
        invoke("wait_operation", server.OPERATION_UUID, timeout=2.0)


@pytest.mark.parametrize("failure", ["interrupted", "chunked", "http"])
def test_interrupted_retained_log_resumes_without_duplicates(
    invoke: Callable[..., Any],
    terminal_log: Callable[..., None],
    stub_server: server.StubServer,
    failure: str,
) -> None:
    if failure in ("interrupted", "chunked"):
        terminal_log(replay_interruptions=1, replay_chunked=failure == "chunked")
    else:
        terminal_log(replay_error=500, replay_failures=1)
    events = invoke(
        "follow_operation_events",
        server.OPERATION_UUID,
        spid=1,
        since=123,
        timeout=2.0,
        collect=True,
    )
    assert [event.id for event in events] == [0, 1, 2, 3]
    requests = [r for r in stub_server.captured if r.path.endswith("/events")]
    assert len(requests) == 3
    assert requests[-1].headers["last-event-id"] == ("2" if failure != "http" else "1")
    assert requests[-1].query == {"spid": ["1"], "since": ["123"]}


def test_malformed_retained_log_is_not_retried_or_hidden(
    invoke: Callable[..., Any],
    terminal_log: Callable[..., None],
    stub_server: server.StubServer,
) -> None:
    terminal_log(completion=False, replay_malformed=True)
    with pytest.raises(ValueError):
        invoke("wait_operation", server.OPERATION_UUID)
    assert len([r for r in stub_server.captured if r.path.endswith("/events")]) == 2


def test_retained_log_read_respects_deadline(
    invoke: Callable[..., Any],
    terminal_log: Callable[..., None],
) -> None:
    terminal_log(replay_hang=2.0, completion=False)
    with pytest.raises(TimeoutError):
        invoke(
            "follow_operation_events", server.OPERATION_UUID, timeout=0.2, collect=True
        )


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("budget", [1, 2])
def test_retained_log_uses_configured_retry_budget(
    backend: str,
    budget: int,
    generated_package: Any,
    terminal_log: Callable[..., None],
    stub_server: server.StubServer,
) -> None:
    terminal_log(replay_error=500)
    invoke = make_invoke(
        backend,
        lambda: client_class(backend)(
            TOKEN,
            base_url=stub_server.base_url,
            retry=RetryPolicy(max_attempts=budget, delays=(0.0,)),
        ),
    )
    with pytest.raises(APIConnectionError):
        invoke("follow_operation_events", server.OPERATION_UUID, collect=True)
    assert (
        len([r for r in stub_server.captured if r.path.endswith("/events")])
        == 1 + budget
    )


@pytest.mark.parametrize("async_mode", [False, True])
def test_retry_delay_respects_original_deadline(async_mode: bool) -> None:
    cls = testing.ContreeAsyncClient if async_mode else testing.ContreeClient
    client = cls(retry=RetryPolicy(delays=(5.0,)))
    client.mock("iter_operation_events", [])
    client.mock("iter_operation_events", error=ConnectionError("interrupted drain"))
    client.mock("operation_terminal", True)
    invoke = make_invoke("httpx_async" if async_mode else "http", lambda: client)

    started = time.monotonic()
    with pytest.raises(TimeoutError):
        invoke(
            "follow_operation_events", server.OPERATION_UUID, timeout=0.25, collect=True
        )
    assert time.monotonic() - started < 2.0
    assert any(
        not call.kwargs["follow"] for call in client.calls_for("iter_operation_events")
    )


@pytest.mark.parametrize("backend", BACKENDS)
def test_corrupt_gzip_is_not_retried_or_hidden(
    backend: str,
    generated_package: Any,
    terminal_log: Callable[..., None],
    stub_server: server.StubServer,
) -> None:
    terminal_log(replay_corrupt_gzip=True)
    invoke = make_invoke(
        backend, lambda: client_class(backend)(TOKEN, base_url=stub_server.base_url)
    )
    native_error = {
        "http": zlib.error,
        "urllib3": urllib3.exceptions.DecodeError,
        "requests": requests.exceptions.ContentDecodingError,
        "httpx": httpx.DecodingError,
        "httpx_async": httpx.DecodingError,
        "aiohttp": aiohttp.ClientPayloadError,
    }[backend]
    with pytest.raises(native_error):
        invoke("wait_operation", server.OPERATION_UUID, timeout=2.0)
    assert len([r for r in stub_server.captured if r.path.endswith("/events")]) == 2
    assert len([r for r in stub_server.captured if not r.path.endswith("/events")]) == 1
