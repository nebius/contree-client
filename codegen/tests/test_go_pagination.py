"""Offline contract tests for generated Go pagination constructors."""

from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from api_generator.golang.emitter import GoEmitter
from api_generator.golang.pagination import _local_name, render_pagination
from api_generator.ir import (
    ArgumentDef,
    ArgumentPresence,
    FieldDef,
    ModelDef,
    OperationDef,
    PaginationDef,
    ParameterDef,
    ParameterEncoding,
    ParameterLocation,
    RequestDef,
    ResponseDef,
    ResponseMode,
    SpecIR,
    TypeKind,
    TypeRef,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
STR = TypeRef(TypeKind.STRING)
INT = TypeRef(TypeKind.INTEGER)
BOOL = TypeRef(TypeKind.BOOLEAN)
WIDGET = TypeRef(TypeKind.MODEL, name="Widget")
WIDGETS = TypeRef(TypeKind.LIST, arguments=(WIDGET,))
STATUS_FILTER = TypeRef(
    TypeKind.UNION,
    arguments=(TypeRef(TypeKind.ENUM, name="OperationStatus"), STR),
)
REPEATABLE_STRING = TypeRef(
    TypeKind.UNION,
    arguments=(STR, TypeRef(TypeKind.SEQUENCE, arguments=(STR,))),
)
TIME_FILTER = TypeRef(
    TypeKind.UNION,
    arguments=(STR, INT, TypeRef(TypeKind.DATETIME)),
)


def test_local_name_lowercases_initialisms_with_digits() -> None:
    assert _local_name("sha256") == "sha256"
    assert _local_name("api_response") == "apiResponse"


def make_ir() -> SpecIR:
    widget = ModelDef(
        name="Widget",
        description="",
        fields=[FieldDef("id", "id", STR, True, False)],
    )
    page = ModelDef(
        name="WidgetPage",
        description="",
        fields=[FieldDef("as_bytes", "items-wire", WIDGETS, False, True)],
    )
    operation = OperationDef(
        name="list_api_widgets",
        http_method="GET",
        path="/tenants/{tenantID}/widgets",
        summary="",
        arguments=[
            ArgumentDef("tenant_id", STR, ArgumentPresence.REQUIRED),
            ArgumentDef("limit", INT, ArgumentPresence.OMIT_IF_NULL, True),
            ArgumentDef("offset", INT, ArgumentPresence.OMIT_IF_NULL, True),
            ArgumentDef("active", BOOL, ArgumentPresence.OMIT_IF_FALSE),
            ArgumentDef("status", STATUS_FILTER, ArgumentPresence.OMIT_IF_NULL),
            ArgumentDef(
                "patterns",
                REPEATABLE_STRING,
                ArgumentPresence.OMIT_IF_NULL,
                True,
            ),
            ArgumentDef("since", TIME_FILTER, ArgumentPresence.OMIT_IF_NULL, True),
        ],
        request=RequestDef(
            parameters=[
                ParameterDef("tenantID", "tenant_id", ParameterLocation.PATH),
                ParameterDef(
                    "limit",
                    "limit",
                    ParameterLocation.QUERY,
                    ParameterEncoding.STRING,
                ),
                ParameterDef(
                    "offset",
                    "offset",
                    ParameterLocation.QUERY,
                    ParameterEncoding.STRING,
                ),
                ParameterDef(
                    "active",
                    "active",
                    ParameterLocation.QUERY,
                    ParameterEncoding.ONE_IF_TRUE,
                ),
                ParameterDef(
                    "status",
                    "status",
                    ParameterLocation.QUERY,
                    ParameterEncoding.STRING,
                ),
                ParameterDef(
                    "pattern",
                    "patterns",
                    ParameterLocation.QUERY,
                    repeatable=True,
                ),
                ParameterDef(
                    "since",
                    "since",
                    ParameterLocation.QUERY,
                    ParameterEncoding.TIME,
                ),
            ],
            idempotent=True,
        ),
        response=ResponseDef(
            ResponseMode.JSON,
            type=TypeRef(TypeKind.MODEL, name="WidgetPage"),
        ),
        pagination=PaginationDef(
            iterator_name="iter_api_widgets",
            item_type=WIDGET,
            items_path=("items-wire",),
            limit_argument="limit",
            offset_argument="offset",
            max_page_size=3,
        ),
    )
    return SpecIR(
        default_base_url="https://api.example.invalid/v1",
        spec_text="synthetic",
        spec_sha256="0123456789abcdef",
        models=[widget, page],
        operations=[operation],
        event_type_values=[],
        status_values=[],
        terminal_status_values=[],
        event_data_variants=(),
    )


def test_rendered_pagination_preserves_filters_and_exact_item_path() -> None:
    source = render_pagination(make_ir())

    assert "type IterAPIWidgetsOptions struct" in source
    assert "PageSize int64" in source
    assert "Limit *int64" in source
    assert "Active bool" in source
    assert "Status *OperationStatus" in source
    assert "Patterns *[]string" in source
    assert "Since *any" in source
    assert "tenantID string" in source
    assert "options *IterAPIWidgetsOptions" in source
    assert "iter.Seq2[Widget, error]" in source
    assert "filters := ListAPIWidgetsOptions{}" in source
    assert "filters.Active = options.Active" in source
    assert "requestOptions.Limit = &pageSize" in source
    assert "requestOptions.Offset = &offset" in source
    assert "response, err := c.ListAPIWidgets(ctx, tenantID, &requestOptions)" in source
    assert "pageValue1, ok := response.AsBytesField.Value()" in source
    assert "return pageValue1, nil" in source
    assert "return iteratePages[Widget](" in source
    assert "\t\t3," in source


def test_render_pagination_returns_empty_source_without_paginated_operations() -> None:
    ir = make_ir()
    ir.operations[0].pagination = None

    assert render_pagination(ir) == ""


def test_go_emitter_appends_pagination_to_operations_file() -> None:
    source = GoEmitter().render(make_ir())["operations.gen.go"]

    assert "func (c *Client) IterAPIWidgets(" in source
    assert '"iter"' in source


def test_generated_pagination_compiles_and_runs(tmp_path: Path) -> None:
    go = shutil.which("go")
    gofmt = shutil.which("gofmt")
    if go is None or gofmt is None:
        pytest.skip("Go and gofmt are required for generated-source tests")

    generated = tmp_path / "pagination.gen.go"
    generated.write_text(
        'package contree\n\nimport ("context"; "iter")\n\n'
        + render_pagination(make_ir()),
        encoding="utf-8",
    )
    pager = tmp_path / "pagination.go"
    pager.write_text(
        (REPO_ROOT / "go" / "v1" / "pagination.go").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    stub = tmp_path / "client_stub.go"
    stub.write_text(textwrap.dedent(_CLIENT_STUB), encoding="utf-8")
    behavior = tmp_path / "pagination_test.go"
    behavior.write_text(textwrap.dedent(_GO_BEHAVIOR_TEST), encoding="utf-8")
    (tmp_path / "go.mod").write_text(
        "module generated.invalid/contree\n\ngo 1.23\n",
        encoding="utf-8",
    )
    subprocess.run([gofmt, "-w", generated, pager, stub, behavior], check=True)
    environment = dict(os.environ)
    environment.update({"GOTOOLCHAIN": "local", "GOWORK": "off"})
    completed = subprocess.run(
        [go, "test", "./..."],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


_CLIENT_STUB = r"""
    package contree

    import "context"

    type Optional[T any] struct {
        value T
        set bool
        null bool
    }

    func Some[T any](value T) Optional[T] {
        return Optional[T]{value: value, set: true}
    }

    func (o Optional[T]) Value() (T, bool) {
        return o.value, o.set && !o.null
    }

    type OperationStatus string
    type Widget struct{ ID string }
    type WidgetPage struct{ AsBytesField Optional[[]Widget] }

    type ListAPIWidgetsOptions struct {
        Limit *int64
        Offset *int64
        Active bool
        Status *OperationStatus
        Patterns *[]string
        Since *any
    }

    type pageCall struct {
        size int64
        offset int64
        active bool
        status OperationStatus
        patterns []string
    }

    type Client struct {
        calls []pageCall
        pages [][]Widget
    }

    func (c *Client) ListAPIWidgets(
        _ context.Context,
        tenantID string,
        options *ListAPIWidgetsOptions,
    ) (WidgetPage, error) {
        if tenantID != "tenant" {
            panic("filter changed")
        }
        call := pageCall{
            size: *options.Limit,
            offset: *options.Offset,
            active: options.Active,
        }
        if options.Status != nil {
            call.status = *options.Status
        }
        if options.Patterns != nil {
            call.patterns = append([]string(nil), (*options.Patterns)...)
        }
        c.calls = append(c.calls, call)
        page := c.pages[0]
        c.pages = c.pages[1:]
        return WidgetPage{AsBytesField: Some(page)}, nil
    }
"""


_GO_BEHAVIOR_TEST = r"""
    package contree

    import (
        "context"
        "reflect"
        "testing"
    )

    func TestGeneratedIterator(t *testing.T) {
        status := OperationStatus("READY")
        patterns := []string{"one", "two"}
        limit := int64(3)
        client := &Client{
            pages: [][]Widget{
                {{ID: "1"}, {ID: "2"}},
                {{ID: "3"}},
            },
        }
        items := client.IterAPIWidgets(
            context.Background(),
            "tenant",
            &IterAPIWidgetsOptions{
                PageSize: 2,
                Limit: &limit,
                Active: true,
                Status: &status,
                Patterns: &patterns,
            },
        )
        if len(client.calls) != 0 {
            t.Fatal("constructor made a request")
        }
        var ids []string
        for item, err := range items {
            if err != nil { t.Fatal(err) }
            ids = append(ids, item.ID)
        }
        if !reflect.DeepEqual(ids, []string{"1", "2", "3"}) {
            t.Fatalf("ids = %#v", ids)
        }
        wantCalls := []pageCall{
            {
                size: 2, offset: 0, active: true, status: "READY",
                patterns: []string{"one", "two"},
            },
            {
                size: 1, offset: 2, active: true, status: "READY",
                patterns: []string{"one", "two"},
            },
        }
        if !reflect.DeepEqual(client.calls, wantCalls) {
            t.Fatalf("calls = %#v, want %#v", client.calls, wantCalls)
        }
    }

    func TestGeneratedIteratorValidatesWithoutRequests(t *testing.T) {
        client := &Client{}
        negative := int64(-1)
        cases := []*IterAPIWidgetsOptions{{PageSize: 4}, {Limit: &negative}}
        for _, options := range cases {
            count := 0
            items := client.IterAPIWidgets(context.Background(), "tenant", options)
            for _, err := range items {
                count++
                if err == nil { t.Fatal("invalid iterator did not fail") }
            }
            if count != 1 { t.Fatalf("validation error count = %d", count) }
        }
        zero := int64(0)
        for range client.IterAPIWidgets(
            context.Background(),
            "tenant",
            &IterAPIWidgetsOptions{Limit: &zero},
        ) {
            t.Fatal("zero limit returned an item")
        }
        if len(client.calls) != 0 {
            t.Fatalf("validation made %d requests", len(client.calls))
        }
    }
"""
