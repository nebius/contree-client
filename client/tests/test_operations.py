from __future__ import annotations

import json
from types import ModuleType

import pytest


def response(runtime: ModuleType, status: int, payload: object = None, **headers: str):
    body = b"" if payload is None else json.dumps(payload).encode()
    return runtime.ResponseData(
        status=status,
        headers={k.lower(): v for k, v in headers.items()},
        body=body,
    )


def test_build_list_images_full_query(operations: ModuleType) -> None:
    spec = operations.build_list_images(
        limit=10,
        offset=5,
        tagged=True,
        tag="busy",
        uuid="u",
        since=3600,
        until="1h",
    )
    assert spec.query == {
        "limit": "10",
        "offset": "5",
        "tagged": "1",
        "tag": "busy",
        "uuid": "u",
        "since": "3600",
        "until": "1h",
    }


def test_build_list_operations_filters(operations: ModuleType) -> None:
    spec = operations.build_list_operations(status="FAILED", kind="image_import")
    assert spec.query == {"status": "FAILED", "kind": "image_import"}


def test_build_iter_operation_events_full(operations: ModuleType) -> None:
    spec = operations.build_iter_operation_events(
        "op-id",
        follow=True,
        spid=1,
        since=41,
        last_event_id=42,
    )
    assert spec.query == {"follow": "1", "spid": "1", "since": "41"}
    assert spec.headers == {"Last-Event-Id": "42"}
    assert spec.accept == "text/event-stream"


def test_build_paths_are_quoted(operations: ModuleType) -> None:
    spec = operations.build_inspect_image_download("a/b", "/etc/hosts")
    assert spec.path == "/inspect/a%2Fb/download"
    assert spec.query == {"path": "/etc/hosts"}


def test_parse_error_responses(
    operations: ModuleType,
    runtime: ModuleType,
    exceptions: ModuleType,
) -> None:
    cases = [
        ("parse_list_images", 500, exceptions.ServerError),
        ("parse_whoami", 401, exceptions.UnauthorizedError),
        ("parse_import_image", 403, exceptions.ForbiddenError),
        ("parse_spawn_instance", 400, exceptions.BadRequestError),
        ("parse_update_image_tag", 404, exceptions.NotFoundError),
        ("parse_delete_image_tag", 409, exceptions.ConflictError),
        ("parse_cancel_operation", 409, exceptions.ConflictError),
        ("parse_get_operation_status", 404, exceptions.NotFoundError),
        ("parse_list_operations", 400, exceptions.BadRequestError),
        ("parse_inspect_find_image_by_tag", 404, exceptions.NotFoundError),
        ("parse_inspect_image", 404, exceptions.NotFoundError),
        ("parse_inspect_image_download", 422, exceptions.UnprocessableEntityError),
        # inspect_image_archive is stream-only: errors surface from the
        # transport stream, there is no parse function
        ("parse_inspect_image_list", 422, exceptions.UnprocessableEntityError),
        ("parse_list_files", 403, exceptions.ForbiddenError),
        ("parse_get_file", 404, exceptions.NotFoundError),
        ("parse_upload_file", 400, exceptions.BadRequestError),
    ]
    for parser_name, status, expected in cases:
        parser = getattr(operations, parser_name)
        with pytest.raises(expected):
            parser(response(runtime, status, {"error": "boom", "status": status}))


def test_parse_check_raises_on_unexpected_status(
    operations: ModuleType,
    runtime: ModuleType,
    exceptions: ModuleType,
) -> None:
    with pytest.raises(exceptions.UnprocessableEntityError):
        operations.parse_check_image_file(response(runtime, 422))
    with pytest.raises(exceptions.UnprocessableEntityError):
        operations.parse_check_image_archive(response(runtime, 422))
    with pytest.raises(exceptions.UnauthorizedError):
        operations.parse_check_file_exists(response(runtime, 401))


def test_parse_accepts_sibling_success_statuses(
    operations: ModuleType,
    runtime: ModuleType,
) -> None:
    """The server may answer with an undocumented 2xx sibling: a
    200-with-body where 204 is documented, a 200 where 201 is."""
    # documented 204 -> a 200 must not raise
    assert operations.parse_delete_image_tag(response(runtime, 200)) is None
    # documented 202
    assert operations.parse_cancel_operation(response(runtime, 200)) is None
    # documented 201 -> 200 with the same body shape parses
    uploaded = operations.parse_upload_file(
        response(runtime, 200, {"uuid": "u", "sha256": "s", "size": 1})
    )
    assert uploaded.uuid == "u"
    # HEAD checks: any 2xx is True, 404 stays False
    assert operations.parse_check_file_exists(response(runtime, 204)) is True
    assert operations.parse_check_file_exists(response(runtime, 404)) is False
