"""The in-memory test doubles from ``contree_client.testing``."""

from __future__ import annotations

import asyncio
import importlib
import io
import tarfile
from datetime import datetime, timezone
from types import ModuleType

import pytest

from tests.stub_server import build_archive

UUID = "12345678-9abc-baba-deda-0123456789ab"


def whoami_response(models: ModuleType, **overrides: object) -> object:
    fields: dict[str, object] = {
        "token_uuid": UUID,
        "token_expiration": None,
        "permissions": {"spawn": True},
        "operations_stat": {},
    }
    fields.update(overrides)
    return models.WhoAmIResponse(**fields)


def event(models: ModuleType, event_id: int, event_type: str) -> object:
    return models.OperationEvent(
        id=event_id,
        ts=datetime(2026, 1, 1, tzinfo=timezone.utc),
        type=event_type,
        data={},
    )


@pytest.fixture
def testing(generated_package: ModuleType) -> ModuleType:
    return importlib.import_module("contree_client.testing")


@pytest.fixture
def models(generated_package: ModuleType) -> ModuleType:
    return importlib.import_module("contree_client.models")


def test_plain_method_and_call_recording(
    testing: ModuleType, models: ModuleType
) -> None:
    client = testing.ContreeClient()
    client.mock("whoami", whoami_response(models))

    assert client.whoami().permissions["spawn"] is True

    (call,) = client.calls_for("whoami")
    assert call.operation == "whoami"
    assert call.args == ()
    assert call.kwargs == {}


def test_call_arguments_recorded(testing: ModuleType, models: ModuleType) -> None:
    client = testing.ContreeClient()
    client.mock("get_operation_status", models.OperationResponse(uuid=UUID))

    client.get_operation_status(UUID)

    (call,) = client.calls
    assert call.args == (UUID,)


def test_sequential_results_last_sticky(
    testing: ModuleType, models: ModuleType
) -> None:
    client = testing.ContreeClient()
    running = models.OperationResponse(
        uuid=UUID, status=models.OperationStatus.EXECUTING
    )
    success = models.OperationResponse(uuid=UUID, status=models.OperationStatus.SUCCESS)
    client.mock("get_operation_status", running)
    client.mock("get_operation_status", success)

    statuses = [client.get_operation_status(UUID).status for _ in range(3)]

    assert statuses == [
        models.OperationStatus.EXECUTING,
        models.OperationStatus.SUCCESS,
        models.OperationStatus.SUCCESS,
    ]
    assert len(client.calls_for("get_operation_status")) == 3


def test_error_outcome(testing: ModuleType, generated_package: ModuleType) -> None:
    exceptions = importlib.import_module("contree_client.exceptions")
    client = testing.ContreeClient()
    client.mock("get_operation_status", error=exceptions.NotFoundError(404, "no such"))

    with pytest.raises(exceptions.NotFoundError):
        client.get_operation_status(UUID)


def test_iterator_method(testing: ModuleType, models: ModuleType) -> None:
    client = testing.ContreeClient()
    events = [event(models, 1, "stdout"), event(models, 2, "exit")]
    client.mock("iter_operation_events", events)

    received = list(client.iter_operation_events(UUID, follow=True))

    assert received == events
    (call,) = client.calls
    assert call.kwargs == {"follow": True}


def test_iterator_error_after_items(
    testing: ModuleType, models: ModuleType, generated_package: ModuleType
) -> None:
    client = testing.ContreeClient()
    client.mock(
        "iter_operation_events",
        [event(models, 1, "stdout")],
        error=ConnectionError("broken"),
    )

    stream = client.iter_operation_events(UUID)
    assert next(stream).id == 1
    with pytest.raises(ConnectionError):
        next(stream)


def test_archive_chunks_open_with_tarfile(testing: ModuleType) -> None:
    archive = build_archive()
    chunks = [archive[:100], archive[100:]]
    client = testing.ContreeClient()
    client.mock("inspect_image_archive", chunks)

    body = b"".join(client.inspect_image_archive(UUID, "/etc"))

    with tarfile.open(fileobj=io.BytesIO(body), mode="r:") as tar:
        assert "etc/hosts" in tar.getnames()


def test_unmocked_method_raises(testing: ModuleType) -> None:
    client = testing.ContreeClient()
    with pytest.raises(testing.NotMockedError, match="GET /whoami"):
        client.whoami()


def test_unmocked_stream_raises(testing: ModuleType) -> None:
    client = testing.ContreeClient()
    with pytest.raises(testing.NotMockedError):
        list(client.inspect_image_archive(UUID, "/etc"))


def test_mock_validation(testing: ModuleType) -> None:
    client = testing.ContreeClient()
    with pytest.raises(ValueError, match="no_such_method"):
        client.mock("no_such_method")
    for reserved in ("request", "stream", "close", "mock", "from_profile", "token"):
        with pytest.raises(ValueError):
            client.mock(reserved)


def test_context_manager(testing: ModuleType, models: ModuleType) -> None:
    with testing.ContreeClient() as client:
        client.mock("whoami", whoami_response(models))
        assert client.whoami() == whoami_response(models)


def test_async_plain_method(testing: ModuleType, models: ModuleType) -> None:
    client = testing.ContreeAsyncClient()
    client.mock("whoami", whoami_response(models, permissions={"spawn": False}))

    response = asyncio.run(client.whoami())

    assert response.permissions == {"spawn": False}
    assert client.calls_for("whoami")


def test_async_iterator_method(testing: ModuleType, models: ModuleType) -> None:
    client = testing.ContreeAsyncClient()
    events = [event(models, 1, "stdout")]
    client.mock("iter_operation_events", events)

    async def collect() -> list[object]:
        return [event async for event in client.iter_operation_events(UUID)]

    assert asyncio.run(collect()) == events


def test_async_error_and_context_manager(
    testing: ModuleType, generated_package: ModuleType
) -> None:
    exceptions = importlib.import_module("contree_client.exceptions")

    async def scenario() -> None:
        async with testing.ContreeAsyncClient() as client:
            client.mock(
                "cancel_operation",
                error=exceptions.ConflictError(409, "operation already finished"),
            )
            with pytest.raises(exceptions.ConflictError):
                await client.cancel_operation(UUID)

    asyncio.run(scenario())


def test_async_unmocked_raises(testing: ModuleType) -> None:
    client = testing.ContreeAsyncClient()
    with pytest.raises(testing.NotMockedError):
        asyncio.run(client.whoami())


def test_constructor_kwargs_recorded(
    testing: ModuleType, generated_package: ModuleType
) -> None:
    runtime = importlib.import_module("contree_client.runtime")
    policy = runtime.RetryPolicy(max_attempts=3)
    client = testing.ContreeClient(
        "tok",
        project="proj",
        retry=policy,
        identity="my-cli/1.2.3",
    )

    assert client.constructed_with["token"] == "tok"
    assert client.constructed_with["project"] == "proj"
    assert client.constructed_with["retry"] is policy
    assert client.constructed_with["identity"] == "my-cli/1.2.3"
    assert client.user_agent().startswith("my-cli/1.2.3 contree-client/")
