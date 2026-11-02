"""Fixtures for the documentation test suite (markdown-pytest).

Annotated code blocks in docs/*.md request these by name
(``fixtures: client, image_uuid``): ``client`` / ``async_client`` are
testing doubles pre-armed with canned happy-path responses for every
operation the examples call, so the hidden setup inside the pages
shrinks to example-specific re-arming.
"""

from __future__ import annotations

import hashlib
import importlib
import io
import sys
import tarfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "client"))


@pytest.fixture(scope="session")
def live_image_uuid() -> str:
    """Resolve the shared Alpine base once without importing or changing tags."""
    profiles = importlib.import_module("contree_client.profiles")
    sync = importlib.import_module("contree_client.sync")
    runtime = importlib.import_module("contree_client.runtime")
    profile = profiles.from_env()
    if profile is None:
        try:
            profile = profiles.resolve_profile()
        except profiles.ProfileError as error:
            pytest.skip(f"no live API credentials: {error}")

    with sync.ContreeClient.from_profile(
        profile, retry=runtime.RetryPolicy()
    ) as client:
        image_uuid = client.resolve_image("tag:alpine:latest")
        assert client.check_image_file(image_uuid, "/bin/sh"), (
            "alpine:latest has no /bin/sh"
        )
        return image_uuid


@pytest.fixture(scope="session")
def network_probe_command() -> str:
    return r"""
set -eu
probe() {
    url="$1"
    expected="$2"
    rc=0
    metrics=$(curl --noproxy '*' --silent --show-error --connect-timeout 5 \
        --max-time 15 --output /dev/null --write-out '%{num_connects} %{http_code}' \
        "$url") || rc=$?
    printf '%s rc=%s connections/status=%s\n' "$url" "$rc" "$metrics"
    if [ "$expected" = allowed ]; then
        [ "$rc" -eq 0 ]
        case "$metrics" in
            '1 '[23][0-9][0-9]) ;;
            *) return 1 ;;
        esac
    else
        [ "$metrics" = '0 000' ]
        [ "$rc" -eq 7 ] || [ "$rc" -eq 28 ]
    fi
}
probe https://1.1.1.1/ allowed
probe http://1.1.1.1/ blocked
probe https://1.1.1.1/ allowed
printf 'network-policy-probe-ok\n'
"""


@pytest.fixture(scope="session")
def live_network_client(live_image_uuid: str) -> Iterator[Any]:
    profiles = importlib.import_module("contree_client.profiles")
    sync = importlib.import_module("contree_client.sync")
    runtime = importlib.import_module("contree_client.runtime")
    profile = profiles.from_env() or profiles.resolve_profile()
    with sync.ContreeClient.from_profile(
        profile, retry=runtime.RetryPolicy()
    ) as client:
        permissions = client.whoami().permissions
        assert permissions.get("network_policy"), (
            "live network tests require network_policy permission"
        )
        assert permissions.get("spawn"), (
            "live network tests require spawn permission to prepare the image"
        )
        yield client


@pytest.fixture(scope="session")
def live_network_policy(live_network_client: Any) -> Iterator[str]:
    models = importlib.import_module("contree_client.models")
    existing = set()
    offset = 0
    while True:
        page = live_network_client.list_network_policies(limit=1000, offset=offset)
        existing.update(policy.uuid for policy in page.policies)
        if len(page.policies) < 1000:
            break
        offset += len(page.policies)
    policy = live_network_client.create_network_policy(
        [models.NetworkPolicyCIDRRule(cidr="1.1.1.1/32", proto="tcp", ports=[443])]
    )
    try:
        assert [rule.to_dict() for rule in policy.cidrs] == [
            {"cidr": "1.1.1.1/32", "proto": "tcp", "ports": [443]}
        ]
        yield str(policy.uuid)
    finally:
        if policy.uuid not in existing:
            live_network_client.delete_network_policy(str(policy.uuid))


@pytest.fixture(scope="session")
def network_probe_image(
    live_network_client: Any,
    live_network_policy: str,
    live_image_uuid: str,
    network_probe_command: str,
) -> str:
    models = importlib.import_module("contree_client.models")
    baseline = network_probe_command.replace(
        "probe http://1.1.1.1/ blocked", "probe http://1.1.1.1/ allowed"
    )
    spawned = live_network_client.spawn_instance(
        "set -e\napk add --no-cache curl ca-certificates\n" + baseline,
        live_image_uuid,
        shell=True,
        timeout=180,
        networking=models.InstanceNetworking(enabled=True, policy=[]),
    )
    try:
        operation = live_network_client.wait_operation(str(spawned.uuid), timeout=240)
        assert operation.status is models.OperationStatus.SUCCESS, operation.to_dict()
        assert (
            "network-policy-probe-ok" in operation.metadata.result.stdout.as_text()
        ), operation.to_dict()
        assert isinstance(operation.result.image, str)
        return operation.result.image
    finally:
        current = live_network_client.get_operation_status(str(spawned.uuid))
        if not current.status.is_terminal():
            live_network_client.cancel_operation(str(spawned.uuid))
            live_network_client.wait_operation(str(spawned.uuid), timeout=60)


OPERATION_ID = "87654321-9abc-baba-deda-0123456789ab"
IMAGE_UUID = "12345678-9abc-baba-deda-0123456789ab"
FILE_UUID = "a9165a5d-5c86-4bd8-8ee4-ae46c19cf45d"
PAYLOAD = b"hello world\n"
CREATED_AT = "2024-01-01T12:00:00+00:00"

FILE_INFO = {
    "uuid": FILE_UUID,
    "sha256": hashlib.sha256(PAYLOAD).hexdigest(),
    "size": len(PAYLOAD),
    "created_at": CREATED_AT,
    "updated_at": CREATED_AT,
}

EXIT_DATA = {
    "pid": 42,
    "code": 0,
    "signal": -1,
    "timed_out": False,
    "duration_ms": 12,
    "resources": {
        "user_time_us": 1000,
        "sys_time_us": 500,
        "max_rss_kb": 1024,
        "shared_memory": 0,
        "unshared_memory": 0,
        "swaps": 0,
        "minor_faults": 0,
        "major_faults": 0,
        "voluntary_ctx_switches": 0,
        "involuntary_ctx_switches": 0,
        "block_input_ops": 0,
        "block_output_ops": 0,
        "ipc_msgs_sent": 0,
        "ipc_msgs_received": 0,
        "signals_received": 0,
    },
}

EVENT_PAYLOADS = (
    {
        "id": 0,
        "ts": "2026-06-08T20:00:00Z",
        "spid": 0,
        "type": "init",
        "data": {
            "started_at": "2026-06-08T20:00:00.000000000Z",
            "runtime_path": "/run/contreeinitd",
            "verbose": False,
            "init_pid": 1,
        },
    },
    {
        "id": 1,
        "ts": "2026-06-08T20:00:00.10Z",
        "spid": 1,
        "type": "spawn",
        "data": {
            "pid": 42,
            "command": "/bin/sh",
            "args": ["-c", "echo hello world"],
            "shell": True,
            "cwd": "/",
            "uid": 0,
            "gid": 0,
            "timeout": 60,
            "truncate_at": 1048576,
            "env": {"PATH": "/usr/bin:/bin"},
        },
    },
    {
        "id": 2,
        "ts": "2026-06-08T20:00:00.50Z",
        "spid": 1,
        "type": "stdout",
        "data": {"value": "hello world\n", "encoding": "ascii"},
    },
    {
        "id": 3,
        "ts": "2026-06-08T20:00:00.60Z",
        "spid": 1,
        "type": "truncated",
        "data": {
            "stream": "stdout",
            "bytes_emitted": 1048576,
            "bytes_dropped": 4096,
        },
    },
    {
        "id": 4,
        "ts": "2026-06-08T20:00:01Z",
        "spid": 1,
        "type": "exit",
        "data": EXIT_DATA,
    },
    {
        "id": 5,
        "ts": "2026-06-08T20:00:02Z",
        "spid": 0,
        "type": "completion",
        "data": {"status": "SUCCESS", "error": None, "duration_ms": 1500},
    },
)

OPERATION_PAYLOAD = {
    "uuid": OPERATION_ID,
    "kind": "instance",
    "status": "SUCCESS",
    "metadata": {
        "command": "echo hello world",
        "image": "tag:ubuntu:latest",
        "result": {
            "state": {"exit_code": 0, "pid": 42},
            "stdout": {"value": "hello world\n", "encoding": "ascii"},
            "stderr": {"value": "", "encoding": "ascii"},
        },
    },
    "result_image_uuid": IMAGE_UUID,
}


def build_tar() -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        body = b"127.0.0.1 localhost\n"
        info = tarfile.TarInfo("etc/hosts")
        info.size = len(body)
        tar.addfile(info, io.BytesIO(body))
    return buffer.getvalue()


def arm(double: Any) -> Any:
    """Pre-arm a testing double with canned happy-path responses."""
    models = importlib.import_module("contree_client.models")

    double.mock(
        "whoami",
        models.WhoAmIResponse.from_dict(
            {
                "token_uuid": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
                "token_expiration": None,
                "permissions": {"spawn": True, "import": True},
                "limits": {"instance_max_timeout": 3600},
                "operations_stat": {},
            }
        ),
    )
    double.mock(
        "spawn_instance",
        models.InstanceSpawnResponse.from_dict({"uuid": OPERATION_ID}),
    )
    double.mock(
        "get_operation_status", models.OperationResponse.from_dict(OPERATION_PAYLOAD)
    )
    double.mock(
        "iter_operation_events",
        [models.OperationEvent.from_dict(event) for event in EVENT_PAYLOADS],
    )
    double.mock("cancel_operation", None)
    double.mock(
        "list_operations",
        [
            models.OperationSummary.from_dict(
                {"uuid": OPERATION_ID, "kind": "instance", "status": "SUCCESS"}
            )
        ],
    )
    double.mock(
        "upload_file",
        models.FileResponse.from_dict(
            {
                "uuid": FILE_UUID,
                "sha256": FILE_INFO["sha256"],
                "size": len(PAYLOAD),
            }
        ),
    )
    double.mock("get_file", models.File.from_dict(FILE_INFO))
    double.mock("check_file_exists", True)
    double.mock(
        "list_files", models.FilesListResponse.from_dict({"files": [FILE_INFO]})
    )
    double.mock(
        "list_images",
        models.ImageListResponse.from_dict(
            {"images": [{"uuid": IMAGE_UUID, "tag": "busybox:latest"}]}
        ),
    )
    double.mock("import_image", OPERATION_ID)
    double.mock(
        "update_image_tag",
        models.Image.from_dict({"uuid": IMAGE_UUID, "tag": "my/base:latest"}),
    )
    double.mock("delete_image_tag", None)
    double.mock("inspect_find_image_by_tag", IMAGE_UUID)
    double.mock(
        "inspect_image",
        models.Image.from_dict({"uuid": IMAGE_UUID, "tag": "busybox:latest"}),
    )
    double.mock(
        "inspect_image_list",
        models.DirectoryList.from_dict({"path": "/etc", "files": []}),
    )
    double.mock(
        "inspect_image_grep",
        models.GrepResult.from_dict(
            {
                "path": "/etc/hosts",
                "patterns": ["^root:", "localhost"],
                "matches": [
                    {
                        "path": "/etc/hosts",
                        "line_number": 1,
                        "absolute_offset": 0,
                        "line_text": "127.0.0.1 localhost\n",
                        "line_bytes": 20,
                        "submatches": [{"text": "localhost", "start": 10, "end": 19}],
                        "type": "match",
                    }
                ],
                "truncated": False,
            }
        ),
    )
    double.mock("inspect_image_download", b"127.0.0.1 localhost\n")
    double.mock("inspect_image_download_stream", [b"127.0.0.1 ", b"localhost\n"])
    double.mock("check_image_file", True)
    double.mock("check_image_archive", True)
    double.mock("inspect_image_archive", [build_tar()])
    return double


@pytest.fixture
def client() -> Any:
    testing = importlib.import_module("contree_client.testing")
    return arm(testing.ContreeClient())


@pytest.fixture
def async_client() -> Any:
    testing = importlib.import_module("contree_client.testing")
    return arm(testing.ContreeAsyncClient())


@pytest.fixture
def subprocess_events() -> list[Any]:
    """Main-process readiness, child output and exit, then parent completion."""
    models = importlib.import_module("contree_client.models")
    payloads = [
        EVENT_PAYLOADS[0],
        EVENT_PAYLOADS[1],
        {
            **EVENT_PAYLOADS[1],
            "spid": 2,
            "data": {**EVENT_PAYLOADS[1]["data"], "command": "id", "args": []},
        },
        {**EVENT_PAYLOADS[2], "spid": 2},
        {**EVENT_PAYLOADS[4], "spid": 2},
        EVENT_PAYLOADS[4],
        {
            **EVENT_PAYLOADS[5],
            "data": {"status": "CANCELLED", "duration_ms": 1500},
        },
    ]
    return [
        models.OperationEvent.from_dict({**payload, "id": event_id})
        for event_id, payload in enumerate(payloads)
    ]


@pytest.fixture
def operation_id() -> str:
    return OPERATION_ID


@pytest.fixture
def image_uuid() -> str:
    return IMAGE_UUID


@pytest.fixture
def file_uuid() -> str:
    return FILE_UUID


@pytest.fixture
def payload() -> bytes:
    return PAYLOAD


@pytest.fixture
def tar_archive() -> bytes:
    return build_tar()


@pytest.fixture
def isolated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run the example in a scratch directory it may write into."""
    monkeypatch.chdir(tmp_path)
    return tmp_path
