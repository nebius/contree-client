"""A terminal status must not discard unread output or process exits."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from typing import Any

import pytest

from tests import stub_server as server


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
    ) -> None:
        status_attempts = 0

        def route(request: server.Captured, attempts: Counter[str]) -> server.Reply:
            nonlocal status_attempts
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
                    if replay_error is not None:
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
                    chunks = [
                        server.sse_frame(event)
                        for event in events
                        if event["id"] > cursor
                    ]
                return server.Reply(
                    status=200,
                    content_type="text/event-stream",
                    stream_chunks=chunks,
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


def test_filtered_retained_log_can_end_without_completion(
    invoke: Callable[..., Any],
    stub_server: server.StubServer,
    terminal_log: Callable[..., None],
) -> None:
    terminal_log(completion=False)
    events = invoke(
        "follow_operation_events",
        server.OPERATION_UUID,
        last_event_id=0,
        spid=1,
        since=123,
        collect=True,
    )
    assert events[-1].type == "exit"
    requests = [r for r in stub_server.captured if r.path.endswith("/events")]
    assert len(requests) >= 2
    assert requests[0].headers["last-event-id"] == "0"
    assert requests[-1].headers["last-event-id"] == "1"
    assert requests[-1].query == {"spid": ["1"], "since": ["123"]}


def test_unavailable_retained_log_does_not_retry_forever(
    invoke: Callable[..., Any],
    terminal_log: Callable[..., None],
    stub_server: server.StubServer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("WARNING", logger="contree_client")
    terminal_log(replay_error=500)
    events = invoke(
        "follow_operation_events", server.OPERATION_UUID, timeout=2.0, collect=True
    )
    assert [event.id for event in events] == [0, 1]
    assert "stream broken" in caplog.text
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
