"""Render operation metadata for the offline Go HTTP transport."""

from __future__ import annotations

import json

from ..ir import ResponseMode, SpecIR, SuccessPolicy
from .naming import HEADER, go_name, go_type
from .operations import go_operation_name


def render_testing(ir: SpecIR, package_name: str = "contree") -> str:
    """Keep mock routing and response encoding aligned with generated methods."""
    lines = [
        HEADER.rstrip(),
        f"package {package_name}",
        "",
        "// Operation links a generated endpoint to its mock result type.",
        "// Use the generated Op values. The zero value is invalid.",
        "type Operation[T any] struct { name string; _ [0]T }",
        "",
        "// Name returns the canonical name used by MockHTTP, MockError, and CallsFor.",
        "func (op Operation[T]) Name() string { return op.name }",
        "",
        "type mockOperation struct {",
        "\tname string",
        "\tmethod string",
        "\tpath string",
        "\tmode string",
        "\tstatus int",
        "\tfalseStatus int",
        "\theader string",
        "\tjsonPath []string",
        "\taliases []string",
        "}",
        "",
        "var mockOperations = []mockOperation{",
    ]
    for operation in ir.operations:
        response = operation.response
        name = go_name(operation.name)
        status = response.success_statuses[0] if response.success_statuses else 200
        if response.success is SuccessPolicy.ANY_2XX and not 200 <= status < 300:
            status = 200
        lines.extend(
            [
                "\t{",
                f"\t\tname: {json.dumps(name)},",
                f"\t\tmethod: {json.dumps(operation.http_method)},",
                f"\t\tpath: {json.dumps(operation.path)},",
                f"\t\tmode: {json.dumps(response.mode.name)},",
                f"\t\tstatus: {status},",
            ]
        )
        if response.false_statuses:
            lines.append(f"\t\tfalseStatus: {response.false_statuses[0]},")
        if response.header_name:
            lines.append(f"\t\theader: {json.dumps(response.header_name)},")
        if response.json_path:
            values = ", ".join(map(json.dumps, response.json_path))
            lines.append(f"\t\tjsonPath: []string{{{values}}},")
        aliases = []
        method_name = go_operation_name(operation.name)
        if method_name != name:
            aliases.append(method_name)
        if response.mode in (ResponseMode.BYTES, ResponseMode.BYTE_STREAM):
            suffix = "Stream" if response.mode is ResponseMode.BYTES else "Raw"
            aliases.append(method_name + suffix)
        if aliases:
            values = ", ".join(map(json.dumps, aliases))
            lines.append(f"\t\taliases: []string{{{values}}},")
        lines.append("\t},")
    lines.extend(["}", ""])
    for operation in ir.operations:
        response = operation.response
        name = go_name(operation.name)
        if response.mode is ResponseMode.JSON:
            if response.type is None:
                raise ValueError(
                    f"operation {operation.name!r} has no JSON response type"
                )
            result_type = go_type(response.type)
        else:
            result_type = {
                ResponseMode.EMPTY: "struct{}",
                ResponseMode.STATUS_BOOL: "bool",
                ResponseMode.LOCATION: "string",
                ResponseMode.BYTES: "[]byte",
                ResponseMode.BYTE_STREAM: "[]byte",
                ResponseMode.SSE: "[]OperationEvent",
            }[response.mode]
        lines.extend(
            [
                f"// Op{name} configures the mock result for {name}.",
                f"var Op{name} = Operation[{result_type}]{{name: {json.dumps(name)}}}",
                "",
            ]
        )
    imports = sorted(
        package
        for package in ("time", "encoding/json", "io")
        if any(
            "Operation[" in line and package.split("/")[-1] + "." in line
            for line in lines
        )
    )
    if imports:
        lines[3:3] = ["import (", *(f"\t{json.dumps(p)}" for p in imports), ")", ""]
    return "\n".join(lines)
