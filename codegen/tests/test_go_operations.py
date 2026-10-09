"""Offline contract tests for generated Go operations."""

from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from api_generator.golang.operations import _local_name, render_operations
from api_generator.ir import (
    ArgumentDef,
    ArgumentPresence,
    BodyBinding,
    BodyDef,
    BodyKind,
    FieldDef,
    ModelDef,
    OperationDef,
    ParameterDef,
    ParameterEncoding,
    ParameterLocation,
    RequestDef,
    ResponseDef,
    ResponseMode,
    SpecIR,
    SuccessPolicy,
    TypeKind,
    TypeRef,
)

STR = TypeRef(TypeKind.STRING)
INT = TypeRef(TypeKind.INTEGER)
BOOL = TypeRef(TypeKind.BOOLEAN)
BYTES = TypeRef(TypeKind.BYTES)
REPEATABLE_STRING = TypeRef(
    TypeKind.UNION,
    arguments=(STR, TypeRef(TypeKind.SEQUENCE, arguments=(STR,))),
)
TIME_PARAMETER = TypeRef(
    TypeKind.UNION,
    arguments=(STR, INT, TypeRef(TypeKind.DATETIME)),
)
STATUS_FILTER = TypeRef(
    TypeKind.UNION,
    arguments=(TypeRef(TypeKind.ENUM, name="OperationStatus"), STR),
)
BINARY_INPUT = TypeRef(
    TypeKind.UNION,
    arguments=(BYTES, TypeRef(TypeKind.BINARY_STREAM)),
)


def test_local_name_lowercases_initialisms_with_digits() -> None:
    assert _local_name("sha256") == "sha256"
    assert _local_name("api_response") == "apiResponse"


def make_ir() -> SpecIR:
    payload = ModelDef(
        name="Payload",
        description="",
        fields=[
            FieldDef("as_bytes", "payload-value", STR, True, False),
            FieldDef("note", "note", STR, False, True),
        ],
    )
    response_model = ModelDef(
        name="APIResponse",
        description="",
        fields=[FieldDef("operation_id", "operationId", STR, True, False)],
    )
    operations = [
        OperationDef(
            name="create_api_item",
            http_method="POST",
            path="/items/{resourceID}",
            summary="",
            arguments=[
                ArgumentDef("resource_id", STR, ArgumentPresence.REQUIRED),
                ArgumentDef("payload_value", STR, ArgumentPresence.REQUIRED),
                ArgumentDef("pattern", REPEATABLE_STRING, ArgumentPresence.REQUIRED),
                ArgumentDef("note", STR, ArgumentPresence.OMIT_IF_UNSET, True),
                ArgumentDef("tagged", BOOL, ArgumentPresence.OMIT_IF_FALSE),
                ArgumentDef(
                    "since",
                    TIME_PARAMETER,
                    ArgumentPresence.OMIT_IF_NULL,
                    True,
                ),
                ArgumentDef("retry_after", INT, ArgumentPresence.OMIT_IF_NULL, True),
                ArgumentDef("status", STATUS_FILTER, ArgumentPresence.OMIT_IF_NULL),
            ],
            request=RequestDef(
                parameters=[
                    ParameterDef(
                        "resourceID",
                        "resource_id",
                        ParameterLocation.PATH,
                    ),
                    ParameterDef(
                        "filter[]",
                        "pattern",
                        ParameterLocation.QUERY,
                        repeatable=True,
                    ),
                    ParameterDef(
                        "tagged",
                        "tagged",
                        ParameterLocation.QUERY,
                        ParameterEncoding.ONE_IF_TRUE,
                    ),
                    ParameterDef(
                        "since.time",
                        "since",
                        ParameterLocation.QUERY,
                        ParameterEncoding.TIME,
                    ),
                    ParameterDef(
                        "X-Retry-After",
                        "retry_after",
                        ParameterLocation.HEADER,
                        ParameterEncoding.STRING,
                    ),
                    ParameterDef(
                        "status",
                        "status",
                        ParameterLocation.QUERY,
                        ParameterEncoding.STRING,
                    ),
                ],
                body=BodyDef(
                    BodyKind.JSON_MODEL,
                    bindings=[
                        BodyBinding("payload-value", "payload_value"),
                        BodyBinding("note", "note"),
                    ],
                    model=TypeRef(TypeKind.MODEL, name="Payload"),
                ),
                idempotent=True,
                accept="application/vnd.example+json",
            ),
            response=ResponseDef(
                ResponseMode.JSON,
                type=TypeRef(TypeKind.MODEL, name="APIResponse"),
                success=SuccessPolicy.ANY_2XX,
                success_statuses=(201,),
            ),
        ),
        OperationDef(
            name="patch_item",
            http_method="PATCH",
            path="/items/{item-id}",
            summary="",
            arguments=[
                ArgumentDef("item_id", STR, ArgumentPresence.REQUIRED),
                ArgumentDef("display_name", STR, ArgumentPresence.REQUIRED),
                ArgumentDef("wire_name", STR, ArgumentPresence.OMIT_IF_UNSET),
                ArgumentDef("tagged", BOOL, ArgumentPresence.OMIT_IF_FALSE),
                ArgumentDef("retry_after", INT, ArgumentPresence.OMIT_IF_NULL, True),
            ],
            request=RequestDef(
                parameters=[
                    ParameterDef("item-id", "item_id", ParameterLocation.PATH),
                    ParameterDef(
                        "wire.query",
                        "wire_name",
                        ParameterLocation.QUERY,
                    ),
                    ParameterDef(
                        "tagged",
                        "tagged",
                        ParameterLocation.QUERY,
                        ParameterEncoding.ONE_IF_TRUE,
                    ),
                    ParameterDef(
                        "X-Retry-After",
                        "retry_after",
                        ParameterLocation.HEADER,
                        ParameterEncoding.STRING,
                    ),
                ],
                body=BodyDef(
                    BodyKind.JSON_INLINE,
                    bindings=[
                        BodyBinding("display-name", "display_name"),
                        BodyBinding("wire-name", "wire_name"),
                    ],
                ),
            ),
            response=ResponseDef(
                ResponseMode.EMPTY,
                success=SuccessPolicy.EXACT,
                success_statuses=(204,),
            ),
        ),
        OperationDef(
            name="upload_blob",
            http_method="PUT",
            path="/blobs",
            summary="",
            arguments=[ArgumentDef("content", BINARY_INPUT, ArgumentPresence.REQUIRED)],
            request=RequestDef(
                body=BodyDef(
                    BodyKind.BINARY,
                    bindings=[BodyBinding("content", "content")],
                ),
                idempotent=True,
            ),
            response=ResponseDef(ResponseMode.EMPTY),
        ),
        _response_operation(
            "head_item",
            ResponseDef(
                ResponseMode.STATUS_BOOL,
                type=BOOL,
                false_statuses=(404, 410),
            ),
        ),
        _response_operation(
            "find_redirect",
            ResponseDef(
                ResponseMode.LOCATION,
                type=STR,
                success=SuccessPolicy.EXACT,
                success_statuses=(302,),
                header_name="location",
            ),
        ),
        _response_operation(
            "download_archive",
            ResponseDef(ResponseMode.BYTES, type=BYTES, success_statuses=(200,)),
        ),
        _response_operation(
            "stream_archive",
            ResponseDef(
                ResponseMode.BYTE_STREAM,
                type=BYTES,
                success=SuccessPolicy.EXACT,
                success_statuses=(200,),
            ),
        ),
        _response_operation(
            "watch_events",
            ResponseDef(
                ResponseMode.SSE,
                type=TypeRef(TypeKind.MODEL, name="OperationEvent"),
                success_statuses=(200,),
            ),
            accept="text/event-stream",
        ),
        _response_operation(
            "read_scalar",
            ResponseDef(
                ResponseMode.JSON,
                type=INT,
                success=SuccessPolicy.EXACT,
                success_statuses=(200,),
                json_path=("result", "value"),
            ),
        ),
    ]
    return SpecIR(
        default_base_url="https://api.example.invalid/v1",
        spec_text="synthetic",
        spec_sha256="0123456789abcdef",
        models=[payload, response_model],
        operations=operations,
        event_type_values=[],
        status_values=[],
        terminal_status_values=[],
        event_data_variants=(),
    )


def _response_operation(
    name: str,
    response: ResponseDef,
    *,
    accept: str | None = None,
) -> OperationDef:
    return OperationDef(
        name=name,
        http_method="GET",
        path=f"/{name}",
        summary="",
        request=RequestDef(idempotent=True, accept=accept),
        response=response,
    )


def test_rendered_surface_preserves_names_bindings_and_presence() -> None:
    source = render_operations(make_ir())

    assert "type CreateAPIItemOptions struct" in source
    assert "Note Optional[string]" in source
    assert "Tagged bool" in source
    assert "Since *any" in source
    assert "RetryAfter *int64" in source
    assert "Status *OperationStatus" in source
    assert "ctx context.Context" in source
    assert "resourceID string" in source
    assert "pattern []string" in source
    assert "options *CreateAPIItemOptions" in source
    assert "quotePath(resourceID)" in source
    assert 'addQueryValue(query, "filter[]", pattern, true)' in source
    assert 'addQueryValue(query, "tagged", "1", false)' in source
    assert "formatTimeParam(*options.Since)" in source
    assert 'headers.Set("X-Retry-After", fmt.Sprint(*options.RetryAfter))' in source
    assert "payload := Payload{" in source
    assert "AsBytesField: payloadValue," in source
    assert "payload.Note = options.Note" in source
    assert 'method: "POST"' in source
    assert 'contentType: "application/json"' in source
    assert 'accept: "application/vnd.example+json"' in source
    assert "idempotent: true" in source


def test_rendered_surface_uses_each_response_contract() -> None:
    source = render_operations(make_ir())

    assert "decodeJSONResponse[APIResponse](response, acceptAny2xx)" in source
    assert (
        'decodeJSONPathResponse[int64](response, acceptStatuses(200), "result", '
        '"value")' in source
    )
    assert "emptyResponse(response, acceptStatuses(204))" in source
    assert "booleanResponse(response, acceptAny2xx, 404, 410)" in source
    assert 'locationResponse(response, acceptStatuses(302), "location")' in source
    assert "byteResponse(response, acceptAny2xx)" in source
    assert "func (c *Client) DownloadArchiveStream(" in source
    assert "streamResponse(response, acceptAny2xx)" in source
    assert "streamResponse(response, acceptStatuses(200))" in source
    assert "func (c *Client) StreamArchiveRaw(" in source
    assert "rawStreamResponse(response, acceptStatuses(200))" in source
    assert "response, err := c.doRawStream(ctx, spec)" in source
    assert "newEventStream(response, acceptAny2xx)" in source
    assert "func (c *Client) WatchEvents(" in source
    assert ") iter.Seq2[OperationEvent, error] {" in source
    assert "func (c *Client) openWatchEvents(" in source
    assert "return eventSequence(func() (*eventStream, error) {" in source
    assert source.count("c.doStream(ctx, spec)") == 3


def test_spawn_instance_reserves_the_plain_options_method_name() -> None:
    ir = make_ir()
    ir.operations[0].name = "spawn_instance"
    source = render_operations(ir)

    assert "func (c *Client) SpawnInstanceWithResponse(" in source
    assert "type SpawnInstanceWithResponseOptions struct" in source
    assert "options *SpawnInstanceWithResponseOptions" in source
    assert "func buildSpawnInstanceWithResponse(" in source
    assert "func (c *Client) SpawnInstance(" not in source
    assert "type SpawnInstanceOptions struct" not in source
    assert "decodeJSONResponse[APIResponse](response, acceptAny2xx)" in source


def test_binary_body_accepts_reader_and_is_one_shot() -> None:
    source = render_operations(make_ir())

    assert "content io.Reader" in source
    assert "body := oneShotBody(content)" in source
    assert 'contentType: "application/octet-stream"' in source


def test_generated_operations_compile_and_preserve_wire_behavior(
    tmp_path: Path,
) -> None:
    go = shutil.which("go")
    gofmt = shutil.which("gofmt")
    if go is None or gofmt is None:
        pytest.skip("Go and gofmt are required for generated-source tests")

    generated = tmp_path / "operations.gen.go"
    generated.write_text(render_operations(make_ir()), encoding="utf-8")
    runtime = tmp_path / "runtime_stub.go"
    runtime.write_text(textwrap.dedent(_RUNTIME_STUB), encoding="utf-8")
    behavior = tmp_path / "operations_test.go"
    behavior.write_text(textwrap.dedent(_GO_BEHAVIOR_TEST), encoding="utf-8")
    (tmp_path / "go.mod").write_text(
        "module generated.invalid/contree\n\ngo 1.23\n",
        encoding="utf-8",
    )
    subprocess.run([gofmt, "-w", generated, runtime, behavior], check=True)
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


_RUNTIME_STUB = r"""
    package contree

    import (
        "bytes"
        "context"
        "encoding/json"
        "errors"
        "fmt"
        "io"
        "iter"
        "net/http"
        "net/url"
    )

    type Optional[T any] struct {
        value T
        set bool
        null bool
    }

    func Some[T any](value T) Optional[T] {
        return Optional[T]{value: value, set: true}
    }

    func Null[T any]() Optional[T] {
        return Optional[T]{set: true, null: true}
    }

    func (o Optional[T]) IsSet() bool { return o.set }

    func (o Optional[T]) Value() (T, bool) {
        return o.value, o.set && !o.null
    }

    func (o Optional[T]) MarshalJSON() ([]byte, error) {
        if !o.set || o.null {
            return []byte("null"), nil
        }
        return json.Marshal(o.value)
    }

    type Payload struct {
        AsBytesField string
        Note Optional[string]
    }

    type APIResponse struct{ OperationID string }
    type OperationStatus string
    type OperationEvent struct{}
    type eventStream struct{}
    type Client struct{}

    type bodyFactory func() (io.ReadCloser, error)

    type requestSpec struct {
        method string
        path string
        query url.Values
        headers http.Header
        body bodyFactory
        contentType string
        accept string
        idempotent bool
    }

    type statusPolicy struct{}
    var acceptAny2xx statusPolicy

    func acceptStatuses(...int) statusPolicy { return statusPolicy{} }

    func bytesBody(value []byte) bodyFactory {
        return func() (io.ReadCloser, error) {
            return io.NopCloser(bytes.NewReader(value)), nil
        }
    }

    func jsonBody(value any) (bodyFactory, error) {
        encoded, err := json.Marshal(value)
        if err != nil {
            return nil, err
        }
        return bytesBody(encoded), nil
    }

    func oneShotBody(value io.Reader) bodyFactory {
        used := false
        return func() (io.ReadCloser, error) {
            if used {
                return nil, errors.New("used")
            }
            used = true
            return io.NopCloser(value), nil
        }
    }

    func quotePath(value any) string { return url.PathEscape(fmt.Sprint(value)) }

    func addQueryValue(
        query url.Values,
        name string,
        value any,
        repeatable bool,
    ) error {
        if repeatable {
            switch items := value.(type) {
            case string:
                query.Add(name, items)
            case []string:
                for _, item := range items {
                    query.Add(name, item)
                }
            default:
                return fmt.Errorf("bad repeatable value %T", value)
            }
            return nil
        }
        query.Add(name, fmt.Sprint(value))
        return nil
    }

    func formatTimeParam(value any) (string, error) {
        return fmt.Sprint(value), nil
    }

    func (c *Client) do(
        context.Context,
        requestSpec,
    ) (*http.Response, error) {
        return nil, errors.New("not called")
    }

    func (c *Client) doStream(
        context.Context,
        requestSpec,
    ) (*http.Response, error) {
        return nil, errors.New("not called")
    }

    func (c *Client) doRawStream(
        context.Context,
        requestSpec,
    ) (*http.Response, error) {
        return nil, errors.New("not called")
    }

    func decodeJSONResponse[T any](*http.Response, statusPolicy) (T, error) {
        var zero T
        return zero, nil
    }

    func decodeJSONPathResponse[T any](
        *http.Response,
        statusPolicy,
        ...string,
    ) (T, error) {
        var zero T
        return zero, nil
    }

    func emptyResponse(*http.Response, statusPolicy) error { return nil }
    func booleanResponse(*http.Response, statusPolicy, ...int) (bool, error) {
        return false, nil
    }
    func byteResponse(*http.Response, statusPolicy) ([]byte, error) {
        return nil, nil
    }
    func streamResponse(*http.Response, statusPolicy) (io.ReadCloser, error) {
        return nil, nil
    }
    func rawStreamResponse(*http.Response, statusPolicy) (io.ReadCloser, error) {
        return nil, nil
    }
    func locationResponse(*http.Response, statusPolicy, string) (string, error) {
        return "", nil
    }
    func newEventStream(*http.Response, statusPolicy) (*eventStream, error) {
        return nil, nil
    }
    func eventSequence(
        open func() (*eventStream, error),
    ) iter.Seq2[OperationEvent, error] {
        return func(yield func(OperationEvent, error) bool) {
            if _, err := open(); err != nil { yield(OperationEvent{}, err) }
        }
    }
"""


_GO_BEHAVIOR_TEST = r"""
    package contree

    import (
        "encoding/json"
        "io"
        "testing"
    )

    func TestPatchItemWirePresence(t *testing.T) {
        retryAfter := int64(7)
        options := &PatchItemOptions{
            WireName: Null[string](),
            Tagged: true,
            RetryAfter: &retryAfter,
        }
        spec, err := buildPatchItem("a/b", "visible", options)
        if err != nil {
            t.Fatal(err)
        }
        if spec.method != "PATCH" || spec.path != "/items/a%2Fb" {
            t.Fatalf("wrong target: %s %s", spec.method, spec.path)
        }
        if spec.query.Get("tagged") != "1" {
            t.Fatalf("tagged query: %#v", spec.query)
        }
        if _, exists := spec.query["wire.query"]; exists {
            t.Fatalf("null query value was not omitted: %#v", spec.query)
        }
        if spec.headers.Get("X-Retry-After") != "7" {
            t.Fatalf("header value was not encoded: %#v", spec.headers)
        }
        body, err := spec.body()
        if err != nil {
            t.Fatal(err)
        }
        encoded, err := io.ReadAll(body)
        if err != nil {
            t.Fatal(err)
        }
        var object map[string]json.RawMessage
        if err := json.Unmarshal(encoded, &object); err != nil {
            t.Fatal(err)
        }
        if string(object["display-name"]) != `"visible"` {
            t.Fatalf("required body field: %s", encoded)
        }
        if string(object["wire-name"]) != "null" {
            t.Fatalf("explicit body null: %s", encoded)
        }

        withoutOptions, err := buildPatchItem("plain", "visible", nil)
        if err != nil {
            t.Fatal(err)
        }
        if withoutOptions.headers.Get("X-Retry-After") != "" {
            t.Fatalf("nil header value was not omitted: %#v", withoutOptions.headers)
        }
        nextBody, err := withoutOptions.body()
        if err != nil {
            t.Fatal(err)
        }
        encoded, err = io.ReadAll(nextBody)
        if err != nil {
            t.Fatal(err)
        }
        object = nil
        if err := json.Unmarshal(encoded, &object); err != nil {
            t.Fatal(err)
        }
        if _, exists := object["wire-name"]; exists {
            t.Fatalf("unset body field was not omitted: %s", encoded)
        }
    }
"""
