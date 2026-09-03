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
import contextlib
import hashlib
import importlib
import io
import sys
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
    runtime = importlib.import_module("contree_client.runtime")
    # a default RetryPolicy transparently absorbs 425 Too Early - e.g.
    # the brief window after an operation goes EXECUTING where the
    # guest control channel is still coming up
    with sync.ContreeClient.from_profile(
        integration_profile, retry=runtime.RetryPolicy()
    ) as client:
        yield client


@pytest.fixture(scope="session")
def permissions(sync_client: Any) -> dict[str, bool]:
    return dict(sync_client.whoami().permissions)


@pytest.fixture
def track_operation(
    request: pytest.FixtureRequest, sync_client: Any
) -> Iterator[Callable[[str], None]]:
    """Register an operation id to dump its event log if the test fails.

    A terminal ``error`` string alone often does not explain a
    backend-side failure - the raw event log (spawn/stdout/stderr/exit
    timing) usually does. Call the fixture value with each operation
    id worth diagnosing; nothing is printed for a passing test.
    """
    tracked: list[str] = []

    yield tracked.append

    report = getattr(request.node, "rep_call", None)
    if report is None or not report.failed:
        return
    # pytest captures and shows stderr for a failing test by default,
    # same as the traceback it wraps around
    for operation_id in tracked:
        sys.stderr.write(f"\n--- events for operation {operation_id} ---\n")
        try:
            for event in sync_client.iter_operation_events(operation_id):
                sys.stderr.write(f"{event}\n")
        except Exception as error:  # diagnostics only, must not mask the real failure
            sys.stderr.write(f"(failed to fetch events: {error!r})\n")


@pytest.fixture(scope="session")
def sample_image(sync_client: Any) -> Any:
    images = sync_client.list_images(tagged=True, limit=100).images
    if not images:
        pytest.skip("no tagged images available in this namespace")
    busybox = next(
        (c for c in images if c.tag and "busybox" in c.tag),
        None,
    )
    if busybox is not None:
        return busybox
    # no busybox tag: some catalogs carry stale entries whose object
    # storage sync never completed, so prefer the first candidate that
    # is demonstrably real (a cheap check_image_file probe) over
    # images[0] blindly - falls back to images[0] if none check out
    return next(
        (
            candidate
            for candidate in images
            if sync_client.check_image_file(str(candidate.uuid), "/etc/passwd")
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


def wait_running(
    client: Any,
    models: ModuleType,
    operation_id: str,
    deadline_seconds: float = 30.0,
) -> None:
    """Wait until the parent instance is EXECUTING/ASSIGNED - a
    freshly spawned operation is 202 Accepted, not yet a live target
    for `operation_subprocess_create`."""
    deadline = time.monotonic() + deadline_seconds
    while True:
        status = client.get_operation_status(operation_id, inflight=True).status
        if status in (
            models.OperationStatus.EXECUTING,
            models.OperationStatus.ASSIGNED,
        ):
            return
        if isinstance(status, models.OperationStatus) and status.is_terminal():
            pytest.fail(f"operation {operation_id} finished before going EXECUTING")
        if time.monotonic() > deadline:
            pytest.fail(f"operation {operation_id} never reached EXECUTING/ASSIGNED")
        time.sleep(1)


def wait_subprocess_terminal(
    client: Any,
    operation_id: str,
    spid: int,
    deadline_seconds: float = 30.0,
) -> Any:
    """Wait until `spid`'s folded result carries a real exit_code or
    signal - both start at -1 (sentinel: still running)."""
    deadline = time.monotonic() + deadline_seconds
    while True:
        result = client.operation_subprocess(operation_id, spid)
        state = result.state
        if state is not ... and (
            state.exit_code != -1 or state.signal not in (..., -1)
        ):
            return result
        if time.monotonic() > deadline:
            pytest.fail(f"subprocess {spid} of {operation_id} did not finish in time")
        time.sleep(1)


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


def test_inspect_image_grep(live: Invoke, sample_image: Any) -> None:
    if not live("check_image_file", str(sample_image.uuid), "/etc/passwd"):
        pytest.skip("/etc/passwd not present in the sample image")
    result = live(
        "inspect_image_grep",
        str(sample_image.uuid),
        "root",
        path="/etc/passwd",
    )
    assert result.path == "/etc/passwd"
    assert "root" in result.patterns
    assert result.matches
    assert all(match.path.endswith("passwd") for match in result.matches)
    assert any("root" in match.line_text for match in result.matches)

    absent = live(
        "inspect_image_grep",
        str(sample_image.uuid),
        f"definitely-not-there-{uuid.uuid4().hex}",
        path="/etc/passwd",
    )
    assert absent.matches == []


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
    except Exception as error:
        if str(operation.status) == "CANCELLED":
            pytest.skip(f"cancelled operation log not available: {error}")
        raise
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
    track_operation: Callable[[str], None],
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
    track_operation(operation_id)
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
    track_operation: Callable[[str], None],
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
    track_operation(str(response.uuid))
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


def test_operation_subprocess_lifecycle(
    sync_client: Any,
    permissions: dict[str, bool],
    sample_image: Any,
    generated_package: ModuleType,
    track_operation: Callable[[str], None],
) -> None:
    """One parent instance hosts three execs in turn: a quick command
    read back through the folded result, a `cat` whose stdin is fed
    out-of-band, and a long sleep killed by signal - mirroring how one
    VM serves multiple subprocesses without spawning three instances."""
    if not (permissions.get("spawn_disposable") or permissions.get("spawn")):
        pytest.skip("token lacks spawn permissions")
    models = importlib.import_module("contree_client.models")
    marker = f"contree-client-subprocess-{uuid.uuid4().hex[:12]}"

    response = sync_client.spawn_instance(
        "sleep 90",
        str(sample_image.uuid),
        shell=True,
        disposable=True,
        timeout=120,
    )
    operation_id = str(response.uuid)
    track_operation(operation_id)
    try:
        wait_running(sync_client, models, operation_id)

        # -- exec a quick subprocess and read its folded result --
        spid = sync_client.operation_subprocess_create(
            operation_id, f"echo {marker}", shell=True
        )
        assert spid >= 2
        result = wait_subprocess_terminal(sync_client, operation_id, spid)
        assert result.state.exit_code == 0
        assert marker in stream_text(result.stdout)

        # -- write to a subprocess's stdin out-of-band, then close it --
        cat_spid = sync_client.operation_subprocess_create(
            operation_id,
            "cat",
            stdin=models.ClosableStreamRepr(value="", close=False),
        )
        sync_client.operation_subprocess_stdin(
            operation_id, cat_spid, f"{marker}-stdin\n", close=True
        )
        cat_result = wait_subprocess_terminal(sync_client, operation_id, cat_spid)
        assert f"{marker}-stdin" in stream_text(cat_result.stdout)

        # -- kill a long-lived subprocess --
        kill_spid = sync_client.operation_subprocess_create(
            operation_id, "sleep 60", shell=True
        )
        sync_client.operation_subprocess_kill(operation_id, kill_spid, signal="TERM")
        killed = wait_subprocess_terminal(sync_client, operation_id, kill_spid)
        # a killed process reports its signal (exit_code stays -1)
        assert killed.state.signal not in (..., None, 0, -1)
    finally:
        # best-effort cleanup: the token may lack `cancel`, and the
        # parent's own `sleep 90` may have already run out by the time
        # we get here - a 409 (already completed) is not a test failure
        exceptions = importlib.import_module("contree_client.exceptions")
        if permissions.get("cancel"):
            with contextlib.suppress(exceptions.ConflictError):
                sync_client.cancel_operation(operation_id)
