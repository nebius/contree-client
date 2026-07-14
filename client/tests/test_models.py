from __future__ import annotations

import dataclasses
import importlib
import json
from datetime import datetime, timezone
from enum import Enum
from types import ModuleType


def test_operation_status_enum(models: ModuleType) -> None:
    status = models.OperationStatus
    assert issubclass(status, str)
    assert issubclass(status, Enum)
    assert [member.value for member in status] == [
        "PENDING",
        "ASSIGNED",
        "EXECUTING",
        "SUCCESS",
        "FAILED",
        "CANCELLED",
    ]
    assert str(status.EXECUTING) == "EXECUTING"
    assert status.SUCCESS == "SUCCESS"


def test_operation_status_is_terminal(models: ModuleType) -> None:
    status = models.OperationStatus
    terminal = {member for member in status if member.is_terminal()}
    assert terminal == {status.SUCCESS, status.FAILED, status.CANCELLED}


def test_operation_response_status_is_enum(models: ModuleType) -> None:
    operation = models.OperationResponse.from_dict(
        {"kind": "instance", "status": "SUCCESS"}
    )
    assert isinstance(operation.status, models.OperationStatus)
    assert operation.status.is_terminal()
    dumped = operation.to_dict()
    assert dumped["status"] == "SUCCESS"
    assert type(dumped["status"]) is str


def test_field_metadata_from_spec(models: ModuleType) -> None:
    image_fields = {f.name: f for f in dataclasses.fields(models.Image)}
    assert image_fields["tag"].metadata["example"] == "busybox:latest"
    assert image_fields["tag"].metadata["description"].startswith("Tag to identify")

    spawn_fields = {f.name: f for f in dataclasses.fields(models.InstanceSpawnRequest)}
    assert spawn_fields["env"].metadata["example"] == {
        "KEY1": "value1",
        "KEY2": "value2",
    }
    # spec defaults are data, not dataclass defaults (those stay ...)
    assert spawn_fields["truncate_output_at"].metadata["default"] == 1048576
    assert spawn_fields["truncate_output_at"].default is ...

    event_fields = {f.name: f for f in dataclasses.fields(models.OperationEvent)}
    assert "description" in event_fields["spid"].metadata


def test_spec_info_embeds_spec(generated_package: ModuleType) -> None:
    spec_info = importlib.import_module("contree_client.spec_info")
    assert spec_info.__doc__ is not None
    assert "openapi: 3.0.0" in spec_info.__doc__
    assert "/operations/{operationId}/events:" in spec_info.__doc__


def test_image_roundtrip(models: ModuleType) -> None:
    data = {
        "uuid": "12345678-9abc-baba-deda-0123456789ab",
        "tag": "busybox:latest",
        "created_at": "2024-01-01T12:00:00+00:00",
        "operation_uuid": None,
    }
    image = models.Image.from_dict(data)
    assert image.uuid == data["uuid"]
    assert image.tag == "busybox:latest"
    # explicit JSON null round-trips as an explicit null
    assert image.operation_uuid is None
    assert image.to_dict() == data


def test_missing_field_is_unset(models: ModuleType) -> None:
    image = models.Image.from_dict({"uuid": "u"})
    assert image.tag is ...
    assert image.operation_uuid is ...
    assert image.to_dict() == {"uuid": "u"}


def test_spawn_request_omits_unset(models: ModuleType) -> None:
    request = models.InstanceSpawnRequest(command="echo hi", image="tag:busybox")
    dumped = request.to_dict()
    assert dumped == {"command": "echo hi", "image": "tag:busybox"}


def test_spawn_request_explicit_none_is_sent(models: ModuleType) -> None:
    request = models.InstanceSpawnRequest(
        command="echo hi",
        image="tag:busybox",
        env=None,
    )
    dumped = request.to_dict()
    assert dumped == {"command": "echo hi", "image": "tag:busybox", "env": None}


def test_spawn_request_files(models: ModuleType) -> None:
    request = models.InstanceSpawnRequest(
        command="/bin/app",
        image="tag:busybox",
        files={
            "/root/hello.txt": models.FileSpec(
                uuid="a9165a5d-5c86-4bd8-8ee4-ae46c19cf45d",
                mode="0644",
            )
        },
    )
    dumped = request.to_dict()
    assert dumped["files"]["/root/hello.txt"]["mode"] == "0644"
    parsed = models.InstanceSpawnRequest.from_dict(dumped)
    assert parsed.files["/root/hello.txt"].mode == "0644"


def test_operation_metadata_discriminated_by_kind(models: ModuleType) -> None:
    instance = models.OperationResponse.from_dict(
        {
            "kind": "instance",
            "status": "SUCCESS",
            "metadata": {"command": "echo", "image": "u", "shell": True},
        }
    )
    assert isinstance(instance.metadata, models.OperationInstanceMetadata)
    assert instance.metadata.command == "echo"

    imported = models.OperationResponse.from_dict(
        {
            "kind": "image_import",
            "status": "SUCCESS",
            "metadata": {"registry": {"url": "docker://docker.io/busybox"}},
        }
    )
    assert isinstance(imported.metadata, models.ImageImportMetadata)
    assert imported.metadata.registry.url == "docker://docker.io/busybox"


def test_parse_datetime_z_suffix(models: ModuleType) -> None:
    parsed = models.parse_datetime("2026-06-08T20:00:00Z")
    assert parsed == datetime(2026, 6, 8, 20, 0, 0, tzinfo=timezone.utc)


def test_parse_datetime_nanoseconds(models: ModuleType) -> None:
    parsed = models.parse_datetime("2026-06-08T20:00:00.123456789Z")
    assert parsed.microsecond == 123456
    assert parsed.tzinfo is not None


def test_operation_event_typed_payload(models: ModuleType) -> None:
    event = models.OperationEvent.from_dict(
        {
            "id": 1,
            "ts": "2026-06-08T20:00:00.10Z",
            "spid": 1,
            "type": "stdout",
            "data": {"value": "hello\n", "encoding": "ascii"},
        }
    )
    assert isinstance(event.data, models.EventDataStream)
    assert event.data.value == "hello\n"
    assert isinstance(event.ts, datetime)


def test_operation_event_unknown_type_stays_dict(models: ModuleType) -> None:
    event = models.OperationEvent.from_dict(
        {
            "id": 1,
            "ts": "2026-06-08T20:00:00Z",
            "type": "brand_new_event",
            "data": {"anything": 1},
        }
    )
    assert event.data == {"anything": 1}


def test_operation_event_incomplete_payload_stays_dict(models: ModuleType) -> None:
    event = models.OperationEvent.from_dict(
        {
            "id": 1,
            "ts": "2026-06-08T20:00:00Z",
            "spid": 1,
            "type": "spawn",
            "data": {"pid": 4242},
        }
    )
    assert event.data == {"pid": 4242}


def test_parse_event_data_all_service_types(models: ModuleType) -> None:
    payloads = {
        "truncated": {
            "stream": "stdout",
            "bytes_emitted": 1048576,
            "bytes_dropped": 4096,
        },
        "size_cap": {"limit_bytes": 104857600, "file_size": 104800000},
        "network": {
            "interfaces": [
                {
                    "name": "eth0",
                    "rx_bytes": 1,
                    "rx_packets": 1,
                    "rx_errs": 0,
                    "rx_drop": 0,
                    "tx_bytes": 2,
                    "tx_packets": 1,
                    "tx_errs": 0,
                    "tx_drop": 0,
                }
            ],
            "configured": {"ip4": "10.0.0.5/24", "nameserver": "8.8.8.8"},
        },
        "shutdown": {
            "stopped_at": "2026-06-08T20:01:34.567890123Z",
            "reason": "job_complete",
        },
        "completion": {
            "status": "SUCCESS",
            "result_image_uuid": None,
            "error": None,
            "duration_ms": 4327,
            "image_size_bytes": 0,
        },
    }
    expected_types = {
        "truncated": models.EventDataTruncated,
        "size_cap": models.EventDataSizeCap,
        "network": models.EventDataNetwork,
        "shutdown": models.EventDataShutdown,
        "completion": models.EventDataCompletion,
    }
    for event_type, payload in payloads.items():
        parsed = models.parse_event_data(event_type, payload)
        assert isinstance(parsed, expected_types[event_type])

    network = models.parse_event_data("network", payloads["network"])
    assert network.interfaces[0].name == "eth0"
    assert network.configured.ip4 == "10.0.0.5/24"

    completion = models.parse_event_data("completion", payloads["completion"])
    assert isinstance(completion.status, models.OperationStatus)
    assert completion.status.is_terminal()
    assert completion.result_image_uuid is None


def test_image_import_metadata_roundtrip(models: ModuleType) -> None:
    metadata = models.ImageImportMetadata.from_dict(
        {
            "registry": {
                "url": "docker://docker.io/busybox:latest",
                "credentials": {"username": "user", "password": "<MASKED>"},
            },
            "tag": None,
            "timeout": 300,
        }
    )
    assert metadata.registry.credentials.username == "user"
    dumped = metadata.to_dict()
    assert dumped["registry"]["credentials"]["password"] == "<MASKED>"
    assert dumped["tag"] is None


def test_file_item_owner_union(models: ModuleType) -> None:
    base = {
        "size": 1,
        "path": "x",
        "owner": "root",
        "group": 15,
        "uid": 0,
        "gid": 15,
        "mode": 33188,
        "mtime": 0,
        "nlink": 1,
        "is_dir": False,
        "is_regular": True,
        "is_symlink": False,
        "is_socket": False,
        "is_fifo": False,
        "symlink_to": "",
    }
    item = models.FileItem.from_dict(base)
    assert item.owner == "root"
    assert item.group == 15


def test_whoami_required_nullable(models: ModuleType) -> None:
    response = models.WhoAmIResponse.from_dict(
        {
            "token_uuid": "u",
            "token_expiration": None,
            "permissions": {"spawn": True},
            "operations_stat": {},
        }
    )
    assert response.token_expiration is None
    # required nullable: the explicit null is preserved on the wire
    assert response.to_dict()["token_expiration"] is None


def test_stream_repr_as_bytes_and_text(models: ModuleType) -> None:
    encoded = models.StreamRepr(value="aGkK", encoding="base64")
    plain = models.StreamRepr(value="hello\n", encoding="ascii")

    assert encoded.as_bytes() == b"hi\n"
    assert encoded.as_text() == "hi\n"
    assert plain.as_bytes() == b"hello\n"
    assert plain.as_text() == "hello\n"


def test_stream_repr_as_bytes_broken_base64(models: ModuleType) -> None:
    broken = models.StreamRepr(value="???", encoding="base64")
    assert broken.as_bytes() == b""


def test_decode_chunk(models: ModuleType) -> None:
    stream = models.EventDataStream(value="aGkK", encoding="base64")
    assert models.decode_chunk(stream) == b"hi\n"
    assert models.decode_chunk({"value": "hi\n", "encoding": "ascii"}) == b"hi\n"
    assert models.decode_chunk({"value": "aGkK", "encoding": "base64"}) == b"hi\n"
    assert models.decode_chunk({"value": 42}) == b""
    assert models.decode_chunk(None) == b""


def test_decode_stream(models: ModuleType) -> None:
    encoded = models.StreamRepr(value="aGkK", encoding="base64")
    assert models.decode_stream(encoded) == "hi\n"
    assert models.decode_stream({"value": "hi", "encoding": "ascii"}) == "hi"
    assert models.decode_stream(None) == ""
    assert models.decode_stream({"value": ""}) == ""


def test_status_sets(models: ModuleType) -> None:
    status = models.OperationStatus
    assert status.terminal() == models.TERMINAL_STATUSES
    assert status.active() == models.ACTIVE_STATUSES
    assert status.terminal() | status.active() == frozenset(status)
    assert not status.terminal() & status.active()
    for member in status.terminal():
        assert member.is_terminal()
    # str-enum: membership answers for plain wire strings too
    assert "SUCCESS" in models.TERMINAL_STATUSES
    assert "EXECUTING" in models.ACTIVE_STATUSES


def test_stream_repr_from_bytes_and_text(models: ModuleType) -> None:
    ascii_repr = models.StreamRepr.from_bytes(b"hello\n")
    assert ascii_repr.encoding == "ascii"
    assert ascii_repr.value == "hello\n"

    binary_repr = models.StreamRepr.from_bytes(b"\x00\xff")
    assert binary_repr.encoding == "base64"
    assert binary_repr.as_bytes() == b"\x00\xff"

    unicode_repr = models.EventDataStream.from_text("привет\n")
    assert unicode_repr.encoding == "base64"
    assert unicode_repr.as_text() == "привет\n"
    assert models.StreamRepr.from_text("plain").encoding == "ascii"


def test_file_spec_accepts_int_mode(models: ModuleType) -> None:
    from_int = models.FileSpec(uuid="u", mode=0o644)
    assert from_int.mode == "0644"
    assert from_int.to_dict()["mode"] == "0644"

    from_str = models.FileSpec(uuid="u", mode="0755")
    assert from_str.mode == "0755"


def test_status_sets_are_not_character_sets(models: ModuleType) -> None:
    """P2-10: a singleton frozenset must contain the member, not its
    characters (str-enum would happily iterate the value string)."""
    for status_set in (models.TERMINAL_STATUSES, models.ACTIVE_STATUSES):
        for member in status_set:
            assert isinstance(member, models.OperationStatus)
    assert "S" not in models.TERMINAL_STATUSES
    assert "SUCCESS" in models.TERMINAL_STATUSES


def test_parse_event_data_non_mapping_payload(models: ModuleType) -> None:
    """P2-11: a malformed non-dict payload must come back raw, not
    crash with AttributeError inside a live stream."""
    assert models.parse_event_data("stdout", []) == []
    assert models.parse_event_data("stdout", None) is None
    assert models.parse_event_data("stdout", "text") == "text"


def test_decode_stream_broken_base64(models: ModuleType) -> None:
    """P3-01: malformed base64 degrades to an empty string, matching
    decode_chunk, instead of leaking a binascii error."""
    assert models.decode_stream({"value": "???", "encoding": "base64"}) == ""


def test_spec_info_publishes_digest(generated_package: ModuleType) -> None:
    """P1-10: the artifact records exactly which spec bytes built it."""
    spec_info = importlib.import_module("contree_client.spec_info")
    assert len(spec_info.SPEC_SHA256) == 64
    int(spec_info.SPEC_SHA256, 16)  # a real hex digest


def test_to_dict_encodes_containers_recursively(models: ModuleType) -> None:
    """P2-07: datetime/enum inside lists and mappings must serialize."""
    moment = datetime(2026, 1, 1, tzinfo=timezone.utc)
    encoded = models.omitted_dict(
        [
            ("times", [moment, moment]),
            ("by_name", {"first": moment}),
            ("statuses", [models.OperationStatus.SUCCESS]),
            ("nested", {"deep": [{"ts": moment}]}),
            ("unset", ...),
        ]
    )
    text = json.dumps(encoded)  # must be JSON-compatible all the way down
    assert "unset" not in encoded
    assert encoded["times"] == [moment.isoformat()] * 2
    assert encoded["statuses"] == ["SUCCESS"]
    assert "2026-01-01" in text


def test_parse_datetime_fraction_widths(models: ModuleType) -> None:
    """python 3.10 fromisoformat accepts only 3- or 6-digit fractions:
    the parser must normalize 1..9-digit ones (the server emits .10)."""
    for raw, micro in (
        ("2026-06-08T20:00:00.1+00:00", 100000),
        ("2026-06-08T20:00:00.10+00:00", 100000),
        ("2026-06-08T20:00:00.1234+00:00", 123400),
        ("2026-06-08T20:00:00.123456789Z", 123456),
        ("2026-06-08T20:00:00Z", 0),
    ):
        parsed = models.parse_datetime(raw)
        assert parsed.microsecond == micro, raw
