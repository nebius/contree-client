"""Render lazy Go pagination constructors from the shared IR."""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..ir import (
    ArgumentDef,
    ArgumentPresence,
    ModelDef,
    OperationDef,
    ParameterEncoding,
    ResponseMode,
    SpecIR,
    TypeKind,
    TypeRef,
)
from .naming import GO_KEYWORDS, go_model_field_names, go_name, go_type

_RESERVED_LOCALS = GO_KEYWORDS | {
    "ctx",
    "err",
    "filters",
    "limit",
    "offset",
    "options",
    "pageSize",
    "requestOptions",
    "response",
}


def _lower_exported_name(exported: str) -> str:
    if len(exported) == 1:
        return exported.lower()
    if not any(character.islower() for character in exported):
        return exported.lower()
    if exported[1].islower():
        return exported[0].lower() + exported[1:]
    end = 1
    while end < len(exported) and exported[end].isupper():
        end += 1
    if end < len(exported):
        end -= 1
    return exported[:end].lower() + exported[end:]


def _local_name(name: str) -> str:
    parts = [part for part in re.split(r"[^A-Za-z0-9]+", name) if part]
    if len(parts) > 1:
        first, *rest = parts
        return _lower_exported_name(go_name(first)) + "".join(
            go_name(part) for part in rest
        )
    return _lower_exported_name(go_name(name))


def _status_union_type(type_ref: TypeRef) -> str | None:
    if type_ref.kind is not TypeKind.UNION:
        return None
    variants = type_ref.arguments
    if len(variants) != 2 or not any(
        variant.kind is TypeKind.STRING for variant in variants
    ):
        return None
    named = next(
        (
            variant
            for variant in variants
            if variant.kind is TypeKind.ENUM and variant.name is not None
        ),
        None,
    )
    return go_type(named) if named is not None else None


def _argument_type(operation: OperationDef, argument: ArgumentDef) -> str:
    parameters = [
        parameter
        for parameter in operation.request.parameters
        if parameter.argument == argument.name
    ]
    if any(parameter.repeatable for parameter in parameters):
        return "[]string"
    if any(parameter.encoding is ParameterEncoding.TIME for parameter in parameters):
        return "any"
    status_type = _status_union_type(argument.type)
    if status_type is not None and parameters:
        return status_type
    return go_type(
        argument.type,
        nullable=argument.required and argument.nullable,
    )


def _option_type(operation: OperationDef, argument: ArgumentDef) -> str:
    rendered = _argument_type(operation, argument)
    if argument.presence is ArgumentPresence.OMIT_IF_FALSE:
        if argument.type.kind is not TypeKind.BOOLEAN:
            raise ValueError(
                f"OMIT_IF_FALSE argument {argument.name!r} must be boolean"
            )
        return "bool"
    if argument.presence is ArgumentPresence.OMIT_IF_NULL:
        return f"*{rendered}"
    if argument.presence is ArgumentPresence.OMIT_IF_UNSET:
        return f"Optional[{rendered}]"
    raise ValueError(f"required argument {argument.name!r} is not an option")


@dataclass(frozen=True)
class _Names:
    local: str
    field: str


class _PaginationRenderer:
    def __init__(self, operation: OperationDef, models: dict[str, ModelDef]) -> None:
        pagination = operation.pagination
        if pagination is None:
            raise ValueError(f"operation {operation.name!r} is not paginated")
        self.operation = operation
        self.pagination = pagination
        self.models = models
        self.arguments: dict[str, ArgumentDef] = {}
        self.names: dict[str, _Names] = {}
        used_locals = set(_RESERVED_LOCALS)
        used_fields = {"PageSize", "Limit"}
        for argument in operation.arguments:
            if argument.name in self.arguments:
                raise ValueError(
                    f"operation {operation.name!r} has duplicate argument "
                    f"{argument.name!r}"
                )
            local_base = _local_name(argument.name)
            local = local_base
            suffix = 2
            while local in used_locals:
                local = (
                    f"{local_base}Arg" if suffix == 2 else f"{local_base}Arg{suffix}"
                )
                suffix += 1
            used_locals.add(local)
            field = go_name(argument.name)
            if (
                argument.name not in self.control_arguments
                and not argument.required
                and field in used_fields
            ):
                raise ValueError(
                    f"pagination filter {operation.name}.{argument.name} "
                    f"collides with {field}"
                )
            used_fields.add(field)
            self.arguments[argument.name] = argument
            self.names[argument.name] = _Names(local=local, field=field)
        self._validate()

    @property
    def control_arguments(self) -> frozenset[str]:
        return frozenset(
            (
                self.pagination.limit_argument,
                self.pagination.offset_argument,
            )
        )

    @property
    def method_name(self) -> str:
        return go_name(self.pagination.iterator_name)

    @property
    def options_name(self) -> str:
        return self.method_name + "Options"

    @property
    def list_method_name(self) -> str:
        return go_name(self.operation.name)

    @property
    def list_options_name(self) -> str:
        return self.list_method_name + "Options"

    @property
    def item_type(self) -> str:
        return go_type(self.pagination.item_type)

    @property
    def required_filters(self) -> list[ArgumentDef]:
        return [
            argument
            for argument in self.operation.arguments
            if argument.required and argument.name not in self.control_arguments
        ]

    @property
    def optional_filters(self) -> list[ArgumentDef]:
        return [
            argument
            for argument in self.operation.arguments
            if not argument.required and argument.name not in self.control_arguments
        ]

    @property
    def has_list_options(self) -> bool:
        return any(not argument.required for argument in self.operation.arguments)

    def _validate(self) -> None:
        for name in self.control_arguments:
            try:
                argument = self.arguments[name]
            except KeyError as exc:
                raise ValueError(
                    f"paginated operation {self.operation.name!r} has no "
                    f"{name!r} argument"
                ) from exc
            if argument.type.kind is not TypeKind.INTEGER:
                raise ValueError(
                    f"pagination control {self.operation.name}.{name} "
                    "must be an integer"
                )
        if self.pagination.max_page_size < 1:
            raise ValueError(
                f"pagination maximum for {self.operation.name!r} must be positive"
            )
        if self.operation.response.mode is not ResponseMode.JSON:
            raise ValueError(
                f"paginated operation {self.operation.name!r} must return JSON"
            )

    def render_options(self) -> str:
        maximum = self.pagination.max_page_size
        lines = [
            f"// {self.options_name} configures {self.method_name}.",
            f"type {self.options_name} struct {{",
            (
                f"\t// PageSize is the number of items requested per page. "
                f"Zero uses {maximum}."
            ),
            "\tPageSize int64",
            "\t// Limit bounds the total item count. Nil requests all items.",
            "\tLimit *int64",
        ]
        for argument in self.optional_filters:
            field = self.names[argument.name].field
            lines.append(f"\t{field} {_option_type(self.operation, argument)}")
        lines.append("}")
        return "\n".join(lines)

    def _method_parameters(self) -> list[str]:
        parameters = ["ctx context.Context"]
        parameters.extend(
            f"{self.names[argument.name].local} "
            f"{_argument_type(self.operation, argument)}"
            for argument in self.required_filters
        )
        parameters.append(f"options *{self.options_name}")
        return parameters

    def _list_call_arguments(self) -> list[str]:
        arguments = ["ctx"]
        for argument in self.operation.arguments:
            if not argument.required:
                continue
            if argument.name == self.pagination.limit_argument:
                arguments.append("pageSize")
            elif argument.name == self.pagination.offset_argument:
                arguments.append("offset")
            else:
                arguments.append(self.names[argument.name].local)
        if self.has_list_options:
            arguments.append("&requestOptions")
        return arguments

    def _render_filter_snapshot(self) -> list[str]:
        if not self.has_list_options:
            return []
        lines = [f"filters := {self.list_options_name}{{}}"]
        if not self.optional_filters:
            return lines
        lines.append("if options != nil {")
        lines.extend(
            (
                f"\tfilters.{self.names[argument.name].field} = "
                f"options.{self.names[argument.name].field}"
            )
            for argument in self.optional_filters
        )
        lines.append("}")
        return lines

    def _render_request_options(self) -> list[str]:
        if not self.has_list_options:
            return []
        lines = ["requestOptions := filters"]
        limit = self.arguments[self.pagination.limit_argument]
        if not limit.required:
            lines.append(f"requestOptions.{self.names[limit.name].field} = &pageSize")
        offset = self.arguments[self.pagination.offset_argument]
        if not offset.required:
            lines.append(f"requestOptions.{self.names[offset.name].field} = &offset")
        return lines

    def _field_for_path(self, model: ModelDef, part: str):
        matches = [
            field
            for field in model.fields
            if field.name == part or field.wire_name == part
        ]
        if len(matches) != 1:
            raise ValueError(
                f"pagination path {part!r} does not identify one field "
                f"in model {model.name!r}"
            )
        return matches[0]

    def _render_page_result(self) -> list[str]:
        current = self.operation.response.type
        if current is None:
            raise ValueError(
                f"paginated operation {self.operation.name!r} has no response type"
            )
        expression = "response"
        lines: list[str] = []
        for index, part in enumerate(self.pagination.items_path):
            if current.kind is not TypeKind.MODEL or current.name is None:
                raise ValueError(
                    f"pagination path for {self.operation.name!r} crosses "
                    f"non-model type {current.kind.name}"
                )
            try:
                model = self.models[current.name]
            except KeyError as exc:
                raise ValueError(
                    f"pagination path uses unknown model {current.name!r}"
                ) from exc
            field = self._field_for_path(model, part)
            field_name = go_model_field_names(model)[field.wire_name]
            field_expression = f"{expression}.{field_name}"
            value_name = f"pageValue{index + 1}"
            if not field.required:
                lines.extend(
                    (
                        f"{value_name}, ok := {field_expression}.Value()",
                        "if !ok {",
                        "\treturn nil, nil",
                        "}",
                    )
                )
                expression = value_name
            elif field.nullable:
                lines.extend(
                    (
                        f"if {field_expression} == nil {{",
                        "\treturn nil, nil",
                        "}",
                        f"{value_name} := *{field_expression}",
                    )
                )
                expression = value_name
            else:
                expression = field_expression
            current = field.type
        if (
            current.kind not in (TypeKind.LIST, TypeKind.SEQUENCE)
            or len(current.arguments) != 1
            or current.arguments[0] != self.pagination.item_type
        ):
            raise ValueError(
                f"pagination path for {self.operation.name!r} does not produce "
                f"a sequence of {self.pagination.item_type!r}"
            )
        lines.append(f"return {expression}, nil")
        return lines

    def render_method(self) -> str:
        lines = [
            f"// {self.method_name} lazily iterates {self.list_method_name} pages.",
            "// Each traversal starts from the first page. The first error is yielded",
            "// with a zero item, then iteration stops. Break stops fetching pages.",
            f"func (c *Client) {self.method_name}(",
        ]
        lines.extend(f"\t{parameter}," for parameter in self._method_parameters())
        lines.append(f") iter.Seq2[{self.item_type}, error] {{")
        body = [
            "var pageSize int64",
            "var limit *int64",
            "if options != nil {",
            "\tpageSize = options.PageSize",
            "\tlimit = options.Limit",
            "}",
            *self._render_filter_snapshot(),
            f"return iteratePages[{self.item_type}](",
            "\tctx,",
            "\tpageSize,",
            f"\t{self.pagination.max_page_size},",
            "\tlimit,",
            (f"\tfunc(pageSize, offset int64) ([]{self.item_type}, error) {{"),
            *("\t\t" + line if line else "" for line in self._render_request_options()),
            (
                f"\t\tresponse, err := c.{self.list_method_name}("
                f"{', '.join(self._list_call_arguments())})"
            ),
            "\t\tif err != nil {",
            "\t\t\treturn nil, err",
            "\t\t}",
            *("\t\t" + line if line else "" for line in self._render_page_result()),
            "\t},",
            ")",
        ]
        lines.extend(f"\t{line}" if line else "" for line in body)
        lines.append("}")
        return "\n".join(lines)

    def render(self) -> str:
        return self.render_options() + "\n\n" + self.render_method()


def render_pagination(ir: SpecIR) -> str:
    """Render code appended to the generated Go operations source."""
    models = {model.name: model for model in ir.models}
    renderers = [
        _PaginationRenderer(operation, models)
        for operation in ir.operations
        if operation.pagination is not None
    ]
    method_names = [renderer.method_name for renderer in renderers]
    if len(set(method_names)) != len(method_names):
        raise ValueError("IR has duplicate Go pagination method names")
    if not renderers:
        return ""
    return "\n\n".join(renderer.render() for renderer in renderers) + "\n"
