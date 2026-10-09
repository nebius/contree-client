"""Contract checks for operation metadata used by the Go test transport."""

from __future__ import annotations

from api_generator.golang.emitter import GoEmitter
from api_generator.golang.testing import render_testing
from api_generator.ir import (
    OperationDef,
    RequestDef,
    ResponseDef,
    ResponseMode,
    SpecIR,
    SuccessPolicy,
    TypeKind,
    TypeRef,
)


def test_mock_metadata_preserves_response_contracts() -> None:
    responses = [
        ("whoami", ResponseDef(ResponseMode.JSON, type=TypeRef(TypeKind.STRING))),
        (
            "spawn_instance",
            ResponseDef(ResponseMode.JSON, type=TypeRef(TypeKind.STRING)),
        ),
        (
            "create_item",
            ResponseDef(
                ResponseMode.JSON,
                type=TypeRef(TypeKind.STRING),
                success_statuses=(201,),
                json_path=("result", "uuid"),
            ),
        ),
        (
            "check_item",
            ResponseDef(
                ResponseMode.STATUS_BOOL,
                success=SuccessPolicy.EXACT,
                success_statuses=(204,),
                false_statuses=(404, 410),
            ),
        ),
        (
            "resolve_item",
            ResponseDef(
                ResponseMode.LOCATION,
                success=SuccessPolicy.EXACT,
                success_statuses=(303,),
                header_name="X-Item-Location",
            ),
        ),
        ("download_item", ResponseDef(ResponseMode.BYTES)),
        ("archive_item", ResponseDef(ResponseMode.BYTE_STREAM)),
        ("stream_events", ResponseDef(ResponseMode.SSE)),
        ("delete_item", ResponseDef(ResponseMode.EMPTY)),
    ]
    ir = SpecIR(
        default_base_url="https://example.invalid",
        spec_text="",
        spec_sha256="",
        models=[],
        operations=[
            OperationDef(
                name=name,
                http_method="GET",
                path=f"/custom/{name}/{{itemID}}",
                summary="",
                arguments=[],
                request=RequestDef(),
                response=response,
            )
            for name, response in responses
        ],
        status_values=[],
        terminal_status_values=[],
        event_type_values=[],
        event_data_variants=(),
    )
    source = render_testing(ir, "custom")
    assert "package custom" in source
    assert 'name: "WhoAmI"' in source
    assert 'name: "SpawnInstance"' in source
    assert 'aliases: []string{"SpawnInstanceWithResponse"}' in source
    assert 'path: "/custom/whoami/{itemID}"' in source
    assert 'jsonPath: []string{"result", "uuid"}' in source
    assert "status: 201" in source
    assert "status: 204" in source
    assert "falseStatus: 404" in source
    assert "status: 303" in source
    assert 'header: "X-Item-Location"' in source
    assert 'aliases: []string{"DownloadItemStream"}' in source
    assert 'aliases: []string{"ArchiveItemRaw"}' in source
    assert 'mode: "SSE"' in source
    assert 'mode: "EMPTY"' in source
    assert "testing.gen.go" in GoEmitter.files

    # Renamed paths must update the mock transport from the same IR.
    ir.operations[0].path = "/new/whoami"
    assert 'path: "/new/whoami"' in render_testing(ir)
