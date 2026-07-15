"""Live integration tests against a real Contree API.

Credentials come from the environment first: ``CONTREE_TOKEN`` (or
``NEBIUS_API_KEY``),
``CONTREE_URL`` and optionally ``CONTREE_PROJECT`` (or
``NEBIUS_AI_PROJECT``) - or, failing that, from the active saved
profile under ``$CONTREE_HOME``; without either the whole suite skips. Every
adapter is exercised through `from_profile()` against the resolved
endpoint; write operations are gated on the permissions the token
actually has.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib
import io
import tarfile
import time
import uuid
from collections.abc import Callable, Iterator
from types import ModuleType
from typing import Any

import pytest

from tests.conftest import BACKENDS, client_class, make_invoke

# every test in this module talks to the real API; the marker makes
# `-m "not integration"` an effective opt-out
pytestmark = pytest.mark.integration

Invoke = Callable[..., Any]


@pytest.fixture(scope="session")
def integration_profile(generated_package: ModuleType) -> Any:
    profiles = importlib.import_module("contree_client.profiles")
    # explicit credentials from the environment win over profiles -
    # the standard Contree variable names
    profile = profiles.from_env()
    if profile is not None:
        return profile
    try:
        profile = profiles.resolve_profile()
    except profiles.ProfileError as error:
        pytest.skip(f"no CONTREE_TOKEN/CONTREE_URL and no profile: {error}")
    if profile.token is None:
        pytest.skip(f"profile {profile.name!r} has no token")
    if not profile.url and profile.auth_type != profiles.AUTH_TYPE_IAM:
        pytest.skip(f"profile {profile.name!r} has no URL")
    return profile


@pytest.fixture(params=BACKENDS)
def live(
    request: pytest.FixtureRequest,
    integration_profile: Any,
) -> Invoke:
    backend: str = request.param
    factory = client_class(backend).from_profile
    return make_invoke(backend, lambda: factory(integration_profile))


@pytest.fixture(scope="session")
def sync_client(integration_profile: Any) -> Iterator[Any]:
    sync = importlib.import_module("contree_client.sync")
    with sync.ContreeClient.from_profile(integration_profile) as client:
        yield client


@pytest.fixture(scope="session")
def permissions(sync_client: Any) -> dict[str, bool]:
    return dict(sync_client.whoami().permissions)


@pytest.fixture(scope="session")
def sample_image(sync_client: Any) -> Any:
    images = sync_client.list_images(tagged=True, limit=100).images
    if not images:
        pytest.skip("no tagged images available in this namespace")
    return next(
        (
            candidate
            for candidate in images
            if candidate.tag and "busybox" in candidate.tag
        ),
        images[0],
    )


@pytest.fixture(scope="session")
def sample_image_file(sync_client: Any, sample_image: Any) -> str:
    for path in ("/bin/sh", "/bin/busybox", "/etc/passwd", "/etc/os-release"):
        if sync_client.check_image_file(str(sample_image.uuid), path):
            return path
    pytest.skip(f"no known file found inside image {sample_image.tag}")


def stream_text(stream: Any) -> str:
    if stream is None or stream is ...:
        return ""
    if stream.encoding == "base64":
        return base64.b64decode(stream.value).decode("utf-8", "replace")
    return str(stream.value)


def wait_terminal(
    client: Any,
    models: ModuleType,
    operation_id: str,
    deadline_seconds: float = 180.0,
    inflight: bool = False,
) -> Any:
    deadline = time.monotonic() + deadline_seconds
    while True:
        operation = client.get_operation_status(operation_id, inflight=inflight)
        status = operation.status
        if isinstance(status, models.OperationStatus) and status.is_terminal():
            return operation
        if time.monotonic() > deadline:
            pytest.fail(f"operation {operation_id} did not finish in time")
        time.sleep(2)


# -- read-only API across every adapter --------------------------------------


def test_whoami(live: Invoke) -> None:
    me = live("whoami")
    assert me.token_uuid
    assert isinstance(me.permissions, dict)
    assert me.permissions  # the server reports all known permissions


def test_list_images(live: Invoke) -> None:
    result = live("list_images", limit=5)
    assert isinstance(result.images, list)
    for image in result.images:
        assert image.uuid


def test_list_operations(
    live: Invoke,
    generated_package: ModuleType,
) -> None:
    models = importlib.import_module("contree_client.models")
    operations = live("list_operations", limit=5)
    assert isinstance(operations, list)
    for operation in operations:
        assert isinstance(operation.status, models.OperationStatus)


def test_check_file_exists_for_unknown_hash(live: Invoke) -> None:
    unknown = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
    assert live("check_file_exists", unknown) is False


def test_upload_get_and_list_files(live: Invoke) -> None:
    payload = f"contree-client integration {uuid.uuid4()}\n".encode()
    uploaded = live("upload_file", payload)
    assert uploaded.sha256 == hashlib.sha256(payload).hexdigest()
    assert live("check_file_exists", uploaded.sha256) is True

    info = live("get_file", uploaded.sha256)
    assert info.sha256 == uploaded.sha256
    assert info.size == len(payload)

    files = live("list_files", limit=1000, since="15m").files
    shas = {item.sha256 for item in files}
    if uploaded.sha256 not in shas and len(files) >= 1000:
        pytest.skip("namespace has too many recent files to assert the listing")
    assert uploaded.sha256 in shas


def test_inspect_find_image_by_tag(live: Invoke, sample_image: Any) -> None:
    resolved = live("inspect_find_image_by_tag", str(sample_image.tag))
    assert resolved == str(sample_image.uuid)


def test_inspect_image(live: Invoke, sample_image: Any) -> None:
    image = live("inspect_image", str(sample_image.uuid))
    assert str(image.uuid) == str(sample_image.uuid)


def test_inspect_image_list_root(live: Invoke, sample_image: Any) -> None:
    listing = live("inspect_image_list", str(sample_image.uuid), "/")
    assert listing.path == "/"
    assert listing.files
    assert any(item.is_dir for item in listing.files)


def test_check_image_file(
    live: Invoke,
    sample_image: Any,
    sample_image_file: str,
) -> None:
    assert live("check_image_file", str(sample_image.uuid), sample_image_file)
    absent = f"/definitely/not/here-{uuid.uuid4().hex[:8]}"
    assert live("check_image_file", str(sample_image.uuid), absent) is False


def test_inspect_image_download_and_stream(
    live: Invoke,
    sample_image: Any,
    sample_image_file: str,
) -> None:
    content = live(
        "inspect_image_download",
        str(sample_image.uuid),
        sample_image_file,
    )
    assert content
    chunks = live(
        "inspect_image_download_stream",
        str(sample_image.uuid),
        sample_image_file,
        collect=True,
    )
    assert b"".join(chunks) == content


def test_inspect_image_archive_plain_and_compressed(
    live: Invoke,
    sample_image: Any,
) -> None:
    if not live("check_image_archive", str(sample_image.uuid), "/etc"):
        pytest.skip("/etc is not archivable in the sample image")
    plain = b"".join(
        live(
            "inspect_image_archive",
            str(sample_image.uuid),
            "/etc",
            collect=True,
        )
    )
    with tarfile.open(fileobj=io.BytesIO(plain), mode="r:") as tar:
        names = tar.getnames()
    assert names

    packed = b"".join(
        live(
            "inspect_image_archive",
            str(sample_image.uuid),
            "/etc",
            compressed=True,
            collect=True,
        )
    )
    # compressed=True yields the body exactly as served: a tar.gz when
    # the server compresses the response, a plain tar otherwise -
    # either way it must be a readable archive of the same tree
    with tarfile.open(fileobj=io.BytesIO(packed), mode="r:*") as tar:
        assert tar.getnames() == names


def test_events_of_finished_operation(
    live: Invoke,
    generated_package: ModuleType,
) -> None:
    exceptions = importlib.import_module("contree_client.exceptions")
    candidates = [
        operation
        for operation in live("list_operations", limit=20, kind="instance")
        if operation.status is not ... and operation.status.is_terminal()
    ]
    if not candidates:
        pytest.skip("no finished instance operations in this namespace")
    operation = candidates[0]
    try:
        events = live("iter_operation_events", operation.uuid, collect=True)
    except (exceptions.GoneError, exceptions.TooEarlyError) as error:
        pytest.skip(f"event log not available: {error}")
    assert events
    assert all(event.type for event in events)


# -- autodetected clients -----------------------------------------------------


def test_async_autodetect_client(integration_profile: Any) -> None:
    asyncio_module = importlib.import_module("contree_client.asyncio")

    async def main() -> None:
        client_factory = asyncio_module.ContreeAsyncClient.from_profile
        async with client_factory(integration_profile) as client:
            me = await client.whoami()
            assert me.token_uuid

    asyncio.run(main())


# -- gated write operations (single backend) ----------------------------------


def test_tag_lifecycle(
    sync_client: Any,
    permissions: dict[str, bool],
    sample_image: Any,
) -> None:
    if not permissions.get("set_image_tag"):
        pytest.skip("token lacks set_image_tag permission")
    tag = f"contree-client-it/{uuid.uuid4().hex[:10]}"
    image = sync_client.update_image_tag(str(sample_image.uuid), tag=tag)
    try:
        assert image.tag == tag
        assert str(image.uuid) == str(sample_image.uuid)
    finally:
        sync_client.delete_image_tag(str(sample_image.uuid), tag=tag)


def test_import_image_idempotent(
    sync_client: Any,
    permissions: dict[str, bool],
    generated_package: ModuleType,
) -> None:
    """Importing an unchanged public image completes and yields a
    result image (the spec promises a fast no-op re-import)."""
    if not permissions.get("import"):
        pytest.skip("token lacks import permission")
    models = importlib.import_module("contree_client.models")
    registry = models.ImageImportRegistry(
        url="docker://docker.io/library/busybox:latest"
    )
    operation_id = sync_client.import_image(registry, timeout=240)
    operation = wait_terminal(
        sync_client,
        models,
        operation_id,
        deadline_seconds=300,
    )
    assert operation.status is models.OperationStatus.SUCCESS, operation.error
    result_image = operation.result_image_uuid
    if (result_image is ... or result_image is None) and operation.result not in (
        ...,
        None,
    ):
        result_image = operation.result.image
    assert result_image


def test_cancel_running_operation(
    sync_client: Any,
    permissions: dict[str, bool],
    sample_image: Any,
    generated_package: ModuleType,
) -> None:
    if not permissions.get("cancel"):
        pytest.skip("token lacks cancel permission")
    if not (permissions.get("spawn_disposable") or permissions.get("spawn")):
        pytest.skip("token lacks spawn permissions")
    models = importlib.import_module("contree_client.models")
    # long enough that the command cannot finish before the cancel
    # lands (a `sleep 60` once raced the cancel and won: SUCCESS);
    # wait_terminal still gives up after 180s if the cancel is lost
    response = sync_client.spawn_instance(
        "sleep 600",
        str(sample_image.uuid),
        shell=True,
        disposable=True,
        timeout=630,
    )
    sync_client.cancel_operation(str(response.uuid))
    operation = wait_terminal(
        sync_client,
        models,
        str(response.uuid),
        inflight=True,
    )
    assert operation.status is models.OperationStatus.CANCELLED


def test_disposable_spawn_roundtrip(
    sync_client: Any,
    permissions: dict[str, bool],
    sample_image: Any,
    generated_package: ModuleType,
) -> None:
    """One full sandbox run: spawn a disposable echo, poll to a
    terminal state, verify stdout and replay the event log."""
    if not (permissions.get("spawn_disposable") or permissions.get("spawn")):
        pytest.skip("token lacks spawn permissions")
    models = importlib.import_module("contree_client.models")
    marker = f"contree-client-integration-{uuid.uuid4().hex[:12]}"
    response = sync_client.spawn_instance(
        f"echo {marker}",
        str(sample_image.uuid),
        shell=True,
        disposable=True,
        timeout=60,
    )
    operation = wait_terminal(sync_client, models, str(response.uuid))

    assert operation.status is models.OperationStatus.SUCCESS, operation.error
    assert isinstance(operation.metadata, models.OperationInstanceMetadata)
    result = operation.metadata.result
    assert result is not ... and result is not None
    assert marker in stream_text(result.stdout)

    events = list(sync_client.iter_operation_events(str(response.uuid)))
    assert events
    assert any(
        event.type == "stdout"
        and isinstance(event.data, models.EventDataStream)
        and marker in stream_text(event.data)
        for event in events
    )
    assert events[-1].type == "completion"


def test_wait_and_follow_helpers(
    sync_client: Any,
    permissions: dict[str, bool],
    sample_image: Any,
    generated_package: ModuleType,
) -> None:
    """The convenience helpers against a real run: follow the event
    stream to the completion frame, then wait_operation returns the
    same terminal status immediately."""
    if not (permissions.get("spawn_disposable") or permissions.get("spawn")):
        pytest.skip("token lacks spawn permissions")
    models = importlib.import_module("contree_client.models")
    marker = f"contree-client-integration-{uuid.uuid4().hex[:12]}"
    response = sync_client.spawn_instance(
        f"echo {marker}",
        str(sample_image.uuid),
        shell=True,
        disposable=True,
        timeout=60,
    )
    operation_id = str(response.uuid)

    events = list(sync_client.follow_operation_events(operation_id))
    assert events
    assert events[-1].type == "completion"
    assert any(
        marker in models.decode_chunk(event.data).decode("utf-8", "replace")
        for event in events
        if event.type == "stdout"
    )

    operation = sync_client.wait_operation(operation_id, timeout=180)
    assert operation.status is models.OperationStatus.SUCCESS, operation.error


def test_resolve_image_live(sync_client: Any, sample_image: Any) -> None:
    assert sync_client.resolve_image(str(sample_image.uuid)) == str(sample_image.uuid)
    if sample_image.tag:
        resolved = sync_client.resolve_image(f"tag:{sample_image.tag}")
        assert resolved == str(sample_image.uuid)
