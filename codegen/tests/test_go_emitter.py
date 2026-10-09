"""Offline contract tests for the Go model emitter."""

from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from api_generator.golang import emitter as go_emitter
from api_generator.golang.emitter import (
    GoEmitter,
    render_models,
    render_spec_info,
)
from api_generator.golang.naming import go_name, go_type
from api_generator.ir import (
    DiscriminatorDef,
    FieldDef,
    ModelDef,
    ModelTrait,
    SpecIR,
    TypeKind,
    TypeRef,
)


def make_ir(
    models: list[ModelDef],
    *,
    event_types: list[str] | None = None,
    statuses: list[str] | None = None,
    terminal_statuses: list[str] | None = None,
) -> SpecIR:
    return SpecIR(
        default_base_url="https://api.example.invalid/v1",
        spec_text="synthetic",
        spec_sha256="0123456789abcdef",
        models=models,
        operations=[],
        event_type_values=event_types or [],
        status_values=statuses or [],
        terminal_status_values=terminal_statuses or [],
        event_data_variants=(),
    )


def go_test(tmp_path: Path, ir: SpecIR, test_source: str) -> str:
    go = shutil.which("go")
    gofmt = shutil.which("gofmt")
    if go is None or gofmt is None:
        pytest.skip("Go and gofmt are required for generated-source tests")

    emitter = GoEmitter()
    rendered = emitter.render(ir)
    paths: list[Path] = []
    for name, source in rendered.items():
        path = tmp_path / name
        path.write_text(source, encoding="utf-8")
        paths.append(path)
    emitter.validate(paths)

    (tmp_path / "go.mod").write_text(
        "module generated.invalid/contree\n\ngo 1.23\n",
        encoding="utf-8",
    )
    test_path = tmp_path / "models_test.go"
    test_path.write_text(textwrap.dedent(test_source), encoding="utf-8")
    subprocess.run([gofmt, "-w", str(test_path)], check=True)
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
    return paths[0].read_text(encoding="utf-8")


def test_go_name_preserves_common_initialisms() -> None:
    expected = {
        "id": "ID",
        "uuid": "UUID",
        "api_url": "APIURL",
        "http_uri": "HTTPURI",
        "https_server": "HTTPSServer",
        "json_value": "JSONValue",
        "sse_event": "SSEEvent",
        "sha256": "SHA256",
        "uid_gid_ip_cpu_vm": "UIDGIDIPCPUVM",
        "operation_id": "OperationID",
        "whoami": "WhoAmI",
    }
    assert {name: go_name(name) for name in expected} == expected


def test_go_type_maps_every_type_kind() -> None:
    string = TypeRef(TypeKind.STRING)
    integer = TypeRef(TypeKind.INTEGER)
    expected = {
        TypeKind.ANY: "any",
        TypeKind.STRING: "string",
        TypeKind.INTEGER: "int64",
        TypeKind.NUMBER: "float64",
        TypeKind.BOOLEAN: "bool",
        TypeKind.DATETIME: "time.Time",
        TypeKind.BYTES: "[]byte",
        TypeKind.BINARY_STREAM: "io.Reader",
        TypeKind.MODEL: "APIResponse",
        TypeKind.ENUM: "State",
        TypeKind.ALIAS: "TokenID",
        TypeKind.LITERAL: "string",
        TypeKind.LIST: "[]string",
        TypeKind.SEQUENCE: "[]string",
        TypeKind.MAP: "map[string]int64",
        TypeKind.UNION: "StringOrInt64",
    }
    refs = {
        TypeKind.ANY: TypeRef(TypeKind.ANY),
        TypeKind.STRING: string,
        TypeKind.INTEGER: integer,
        TypeKind.NUMBER: TypeRef(TypeKind.NUMBER),
        TypeKind.BOOLEAN: TypeRef(TypeKind.BOOLEAN),
        TypeKind.DATETIME: TypeRef(TypeKind.DATETIME),
        TypeKind.BYTES: TypeRef(TypeKind.BYTES),
        TypeKind.BINARY_STREAM: TypeRef(TypeKind.BINARY_STREAM),
        TypeKind.MODEL: TypeRef(TypeKind.MODEL, name="ApiResponse"),
        TypeKind.ENUM: TypeRef(TypeKind.ENUM, name="State"),
        TypeKind.ALIAS: TypeRef(TypeKind.ALIAS, name="TokenId"),
        TypeKind.LITERAL: TypeRef(TypeKind.LITERAL, values=("one", "two")),
        TypeKind.LIST: TypeRef(TypeKind.LIST, arguments=(string,)),
        TypeKind.SEQUENCE: TypeRef(TypeKind.SEQUENCE, arguments=(string,)),
        TypeKind.MAP: TypeRef(TypeKind.MAP, arguments=(integer,)),
        TypeKind.UNION: TypeRef(
            TypeKind.UNION,
            arguments=(string, integer),
        ),
    }
    assert {kind: go_type(ref) for kind, ref in refs.items()} == expected
    assert go_type(string, nullable=True) == "*string"
    fallback = TypeRef(
        TypeKind.UNION,
        arguments=(string, TypeRef(TypeKind.BOOLEAN)),
    )
    assert go_type(fallback) == "json.RawMessage"


def test_optional_normalizes_nil_and_failed_decode_is_atomic(tmp_path: Path) -> None:
    go_test(
        tmp_path,
        make_ir([]),
        r"""
            package contree

            import (
                "encoding/json"
                "testing"
            )

            func TestOptionalNilAndDecodeFailure(t *testing.T) {
                value := Some[[]string](nil)
                if !value.IsNull() {
                    t.Fatal("Some(nil) did not become explicit null")
                }
                value.Set(nil)
                if !value.IsNull() {
                    t.Fatal("Set(nil) did not become explicit null")
                }

                var unset Optional[int64]
                if err := json.Unmarshal([]byte("invalid"), &unset); err == nil {
                    t.Fatal("invalid JSON succeeded")
                }
                if unset.IsSet() {
                    t.Fatal("failed decode changed an unset Optional")
                }

                present := Some(int64(7))
                if err := json.Unmarshal([]byte("invalid"), &present); err == nil {
                    t.Fatal("invalid JSON succeeded")
                }
                got, ok := present.Value()
                if !ok || got != 7 {
                    t.Fatalf("failed decode changed value: %d, %v", got, ok)
                }
            }
        """,
    )


def test_render_preserves_exported_and_wire_names() -> None:
    model = ModelDef(
        name="APIResponse",
        description="",
        fields=[
            FieldDef(
                "operation_id",
                "operationId",
                TypeRef(TypeKind.STRING),
                True,
                False,
            ),
            FieldDef(
                "sha256",
                "sha-256",
                TypeRef(TypeKind.STRING),
                False,
                False,
            ),
        ],
    )
    source = render_models(make_ir([model]))

    assert "type APIResponse struct" in source
    assert 'OperationID string `json:"operationId"`' in source
    assert 'SHA256 Optional[string] `json:"sha-256"`' in source
    assert 'object["operationId"]' in source
    assert 'object["sha-256"]' in source
    assert "package contree" in source


def test_optional_nullable_and_wire_names_round_trip(tmp_path: Path) -> None:
    sample = ModelDef(
        name="Sample",
        description="",
        fields=[
            FieldDef(
                "name",
                "wireName",
                TypeRef(TypeKind.STRING),
                True,
                False,
            ),
            FieldDef(
                "count",
                "count",
                TypeRef(TypeKind.INTEGER),
                True,
                True,
            ),
            FieldDef(
                "note",
                "optional_note",
                TypeRef(TypeKind.STRING),
                False,
                False,
            ),
        ],
    )
    nil_contract = ModelDef(
        name="NilContract",
        description="",
        fields=[
            FieldDef("bytes", "bytes", TypeRef(TypeKind.BYTES), True, False),
            FieldDef(
                "items",
                "items",
                TypeRef(
                    TypeKind.LIST,
                    arguments=(TypeRef(TypeKind.STRING),),
                ),
                True,
                False,
            ),
            FieldDef(
                "labels",
                "labels",
                TypeRef(
                    TypeKind.MAP,
                    arguments=(TypeRef(TypeKind.STRING),),
                ),
                True,
                False,
            ),
            FieldDef("payload", "payload", TypeRef(TypeKind.ANY), True, False),
        ],
    )
    source = go_test(
        tmp_path,
        make_ir([sample, nil_contract]),
        r"""
            package contree

            import (
                "encoding/json"
                "testing"
            )

            func decodeSample(t *testing.T, source string) Sample {
                t.Helper()
                var value Sample
                if err := json.Unmarshal([]byte(source), &value); err != nil {
                    t.Fatal(err)
                }
                return value
            }

            func objectFor(t *testing.T, value Sample) map[string]json.RawMessage {
                t.Helper()
                data, err := json.Marshal(value)
                if err != nil {
                    t.Fatal(err)
                }
                var object map[string]json.RawMessage
                if err := json.Unmarshal(data, &object); err != nil {
                    t.Fatal(err)
                }
                return object
            }

            func TestOptionalStatesRoundTrip(t *testing.T) {
                missing := decodeSample(t, `{"wireName":"x","count":null}`)
                if missing.Note.IsSet() || missing.Count != nil {
                    t.Fatal("missing and required null states were not preserved")
                }
                if _, ok := objectFor(t, missing)["optional_note"]; ok {
                    t.Fatal("unset optional field was encoded")
                }

                nullValue := decodeSample(
                    t,
                    `{"wireName":"x","count":1,"optional_note":null}`,
                )
                if !nullValue.Note.IsSet() || !nullValue.Note.IsNull() {
                    t.Fatal("explicit null was not preserved")
                }
                if string(objectFor(t, nullValue)["optional_note"]) != "null" {
                    t.Fatal("explicit null did not round-trip")
                }

                present := decodeSample(
                    t,
                    `{"wireName":"x","count":2,"optional_note":"value"}`,
                )
                note, ok := present.Note.Value()
                if !ok || note != "value" {
                    t.Fatalf("unexpected optional value: %q, %v", note, ok)
                }
                if present.Count == nil || *present.Count != 2 {
                    t.Fatal("required nullable value was not decoded")
                }
            }

            func TestRequiredContract(t *testing.T) {
                for _, source := range []string{
                    `{"count":null}`,
                    `{"wireName":null,"count":null}`,
                    `{"wire_name":"wrong","count":null}`,
                } {
                    var value Sample
                    if err := json.Unmarshal([]byte(source), &value); err == nil {
                        t.Fatalf("expected required-field error for %s", source)
                    }
                }
            }

            func TestMarshalRejectsNilRequiredValues(t *testing.T) {
                valid := NilContract{
                    Bytes: []byte{},
                    Items: []string{},
                    Labels: map[string]string{},
                    Payload: "value",
                }
                if _, err := json.Marshal(valid); err != nil {
                    t.Fatal(err)
                }

                checks := []NilContract{valid, valid, valid, valid}
                checks[0].Bytes = nil
                checks[1].Items = nil
                checks[2].Labels = nil
                var typedNil *int
                checks[3].Payload = typedNil
                for _, value := range checks {
                    if _, err := json.Marshal(value); err == nil {
                        t.Fatalf("expected nil required-field error for %#v", value)
                    }
                }
            }
        """,
    )
    assert "Count *int64" in source
    assert "Optional[string]" in source


def test_semantic_discriminators_and_event_fallback(tmp_path: Path) -> None:
    string = TypeRef(TypeKind.STRING)
    instance = TypeRef(TypeKind.MODEL, name="OperationInstanceMetadata")
    image = TypeRef(TypeKind.MODEL, name="ImageImportMetadata")
    stream = TypeRef(TypeKind.MODEL, name="EventDataStream")
    raw = TypeRef(TypeKind.MAP, arguments=(TypeRef(TypeKind.ANY),))
    models = [
        ModelDef(
            "OperationInstanceMetadata",
            "",
            [FieldDef("instance_id", "instance_id", string, True, False)],
        ),
        ModelDef(
            "ImageImportMetadata",
            "",
            [FieldDef("image_id", "image_id", string, True, False)],
        ),
        ModelDef(
            "EventDataStream",
            "",
            [FieldDef("value", "value", string, True, False)],
        ),
        ModelDef(
            "OperationResponse",
            "",
            [
                FieldDef("kind", "kind", string, True, False),
                FieldDef(
                    "metadata",
                    "metadata",
                    TypeRef(TypeKind.UNION, arguments=(instance, image)),
                    False,
                    False,
                    DiscriminatorDef(
                        parent_field="kind",
                        cases=(("instance", instance),),
                        fallback=image,
                    ),
                ),
            ],
        ),
        ModelDef(
            "OperationEvent",
            "",
            [
                FieldDef(
                    "type",
                    "type",
                    TypeRef(TypeKind.ALIAS, name="OperationEventType"),
                    True,
                    False,
                ),
                FieldDef(
                    "data",
                    "data",
                    TypeRef(
                        TypeKind.UNION,
                        arguments=(TypeRef(TypeKind.ALIAS, name="EventData"), raw),
                    ),
                    True,
                    False,
                    DiscriminatorDef(
                        parent_field="type",
                        cases=(("stdout", stream),),
                        fallback=raw,
                        name="EventData",
                    ),
                ),
            ],
        ),
    ]
    go_test(
        tmp_path,
        make_ir(models, event_types=["stdout"]),
        r"""
            package contree

            import (
                "encoding/json"
                "testing"
            )

            func TestMetadataDiscriminator(t *testing.T) {
                var instance OperationResponse
                if err := json.Unmarshal(
                    []byte(`{"kind":"instance","metadata":{"instance_id":"i"}}`),
                    &instance,
                ); err != nil {
                    t.Fatal(err)
                }
                instanceMetadata, present := instance.Metadata.Value()
                if !present {
                    t.Fatal("instance metadata is missing")
                }
                if _, ok := instanceMetadata.(OperationInstanceMetadata); !ok {
                    t.Fatalf("unexpected instance metadata: %T", instanceMetadata)
                }

                var image OperationResponse
                if err := json.Unmarshal(
                    []byte(`{"kind":"image","metadata":{"image_id":"x"}}`),
                    &image,
                ); err != nil {
                    t.Fatal(err)
                }
                imageMetadata, present := image.Metadata.Value()
                if !present {
                    t.Fatal("image metadata is missing")
                }
                if _, ok := imageMetadata.(ImageImportMetadata); !ok {
                    t.Fatalf("unexpected image metadata: %T", imageMetadata)
                }

                var nullValue OperationResponse
                if err := json.Unmarshal(
                    []byte(`{"kind":"instance","metadata":null}`),
                    &nullValue,
                ); err != nil {
                    t.Fatal(err)
                }
                if !nullValue.Metadata.IsSet() || !nullValue.Metadata.IsNull() {
                    t.Fatal("optional discriminator null state was lost")
                }
                encoded, err := json.Marshal(nullValue)
                if err != nil {
                    t.Fatal(err)
                }
                if string(encoded) != `{"kind":"instance","metadata":null}` {
                    t.Fatalf(
                        "optional discriminator null did not round-trip: %s",
                        encoded,
                    )
                }

                var missing OperationResponse
                if err := json.Unmarshal(
                    []byte(`{"kind":"instance"}`),
                    &missing,
                ); err != nil {
                    t.Fatal(err)
                }
                if missing.Metadata.IsSet() {
                    t.Fatal("missing optional discriminator became set")
                }
            }

            func TestEventDataTypedAndRawFallbacks(t *testing.T) {
                var known OperationEvent
                if err := json.Unmarshal(
                    []byte(`{"type":"stdout","data":{"value":"hello"}}`),
                    &known,
                ); err != nil {
                    t.Fatal(err)
                }
                if _, ok := known.Data.(EventDataStream); !ok {
                    t.Fatalf("unexpected event data: %T", known.Data)
                }

                var unknown OperationEvent
                if err := json.Unmarshal(
                    []byte(`{"type":"future","data":{"new":true}}`),
                    &unknown,
                ); err != nil {
                    t.Fatal(err)
                }
                raw, ok := unknown.Data.(map[string]any)
                if !ok || raw["new"] != true {
                    t.Fatalf("unexpected raw fallback: %#v", unknown.Data)
                }

                var malformed OperationEvent
                if err := json.Unmarshal(
                    []byte(`{"type":"stdout","data":{"value":null}}`),
                    &malformed,
                ); err != nil {
                    t.Fatal(err)
                }
                if _, ok := malformed.Data.(map[string]any); !ok {
                    t.Fatalf("expected raw malformed payload, got %T", malformed.Data)
                }
            }
        """,
    )


def test_model_traits_are_idiomatic_and_compile(tmp_path: Path) -> None:
    string = TypeRef(TypeKind.STRING)
    mode = TypeRef(
        TypeKind.UNION,
        arguments=(string, TypeRef(TypeKind.INTEGER)),
    )
    models = [
        ModelDef(
            "StreamRepr",
            "",
            [
                FieldDef("value", "value", string, True, False),
                FieldDef(
                    "encoding",
                    "encoding",
                    TypeRef(TypeKind.LITERAL, values=("ascii", "base64")),
                    True,
                    False,
                ),
            ],
            frozenset({ModelTrait.STREAM_VALUE}),
        ),
        ModelDef(
            "FileSpec",
            "",
            [FieldDef("mode", "mode", mode, False, False)],
            frozenset({ModelTrait.FILE_MODE}),
        ),
    ]
    go_test(
        tmp_path,
        make_ir(models),
        r"""
            package contree

            import (
                "encoding/json"
                "testing"
            )

            func TestStreamValue(t *testing.T) {
                ascii := NewStreamReprFromText("hello\n")
                if ascii.Encoding != "ascii" || ascii.AsText() != "hello\n" {
                    t.Fatalf("unexpected ASCII payload: %#v", ascii)
                }
                binary := NewStreamReprFromBytes([]byte{0, 255})
                if binary.Encoding != "base64" || len(binary.AsBytes()) != 2 {
                    t.Fatalf("unexpected binary payload: %#v", binary)
                }
                invalid := StreamRepr{Value: "!", Encoding: "base64"}
                if len(invalid.AsBytes()) != 0 {
                    t.Fatal("invalid base64 must decode to an empty slice")
                }
            }

            func TestFileMode(t *testing.T) {
                var file FileSpec
                if err := json.Unmarshal([]byte(`{"mode":420}`), &file); err != nil {
                    t.Fatal(err)
                }
                encoded, err := json.Marshal(file)
                if err != nil {
                    t.Fatal(err)
                }
                if string(encoded) != `{"mode":"0644"}` {
                    t.Fatalf("unexpected mode: %s", encoded)
                }

                file.SetMode(0755)
                value, present := file.Mode.Value()
                mode, stringVariant := value.StringValue()
                if !present || !stringVariant || mode != "0755" {
                    t.Fatalf("unexpected SetMode value: %q", mode)
                }

                absent, err := json.Marshal(FileSpec{})
                if err != nil || string(absent) != `{}` {
                    t.Fatalf("unset mode was not omitted: %s, %v", absent, err)
                }
            }
        """,
    )


def test_status_event_types_and_spec_info_compile(tmp_path: Path) -> None:
    ir = make_ir(
        [],
        event_types=["stdout", "size_cap"],
        statuses=["RUNNING", "SUCCESS", "FAILED"],
        terminal_statuses=["SUCCESS", "FAILED"],
    )
    source = go_test(
        tmp_path,
        ir,
        r"""
            package contree

            import "testing"

            func TestNamedConstants(t *testing.T) {
                if OperationStatusRunning.IsTerminal() {
                    t.Fatal("running must be active")
                }
                if !IsTerminalStatus(OperationStatusSuccess) {
                    t.Fatal("success must be terminal")
                }
                if OperationEventTypeSizeCap != "size_cap" {
                    t.Fatal("event type lost its wire value")
                }
                if DefaultBaseURL == "" || SpecSHA256 == "" {
                    t.Fatal("spec provenance is empty")
                }
            }
        """,
    )
    assert "type OperationStatus string" in source
    assert "type OperationEventType string" in source
    assert 'OperationStatusSuccess OperationStatus = "SUCCESS"' in source
    spec_info = render_spec_info(ir)
    assert 'const DefaultBaseURL = "https://api.example.invalid/v1"' in spec_info
    assert 'const SpecSHA256 = "0123456789abcdef"' in spec_info


def test_validate_requires_gofmt_and_go(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(go_emitter.shutil, "which", lambda _name: None)
    with pytest.raises(RuntimeError, match="gofmt is required"):
        GoEmitter().validate([])

    def only_gofmt(name: str) -> str | None:
        return "/usr/bin/gofmt" if name == "gofmt" else None

    monkeypatch.setattr(go_emitter.shutil, "which", only_gofmt)
    with pytest.raises(RuntimeError, match="go is required"):
        GoEmitter().validate([])


def test_custom_package_name_and_invalid_package() -> None:
    ir = make_ir([])
    assert "package generated" in GoEmitter("generated").render(ir)["models.gen.go"]
    with pytest.raises(ValueError, match="invalid Go package name"):
        GoEmitter("type")
