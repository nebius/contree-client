from __future__ import annotations

from typing import Any

import pytest

from api_generator.ir import SchemaConverter, SpecIR
from api_generator.loader import Spec
from api_generator.python.emitter import render_models


def test_wire_names_round_trip_for_scalar_and_nested_models() -> None:
    spec = Spec(
        {
            "components": {
                "schemas": {
                    "OperationSummary": {
                        "type": "object",
                        "properties": {
                            "status": {
                                "type": "string",
                                "enum": ["SUCCESS"],
                            }
                        },
                    },
                    "ScalarSample": {
                        "type": "object",
                        "required": ["wireName"],
                        "properties": {
                            "wireName": {"type": "string"},
                            "optionalValue": {
                                "type": "string",
                                "nullable": True,
                            },
                            "defaultValue": {
                                "type": "string",
                                "default": "server",
                            },
                        },
                    },
                    "Container": {
                        "type": "object",
                        "required": ["nestedValue"],
                        "properties": {
                            "nestedValue": {"$ref": "#/components/schemas/ScalarSample"}
                        },
                    },
                }
            }
        },
        "",
    )
    converter = SchemaConverter(spec)
    converter.convert_object("Container", spec.schemas["Container"])
    ir = SpecIR(
        default_base_url="",
        spec_text="",
        spec_sha256="",
        models=converter.models,
        operations=[],
        event_type_values=["init"],
        status_values=["SUCCESS"],
        terminal_status_values=["SUCCESS"],
        event_data_variants=(),
    )
    namespace: dict[str, Any] = {}
    exec(render_models(ir), namespace)

    ScalarSample = namespace["ScalarSample"]
    scalar = ScalarSample.from_dict({"wireName": "value", "optionalValue": None})
    assert scalar.wire_name == "value"
    assert scalar.optional_value is None
    assert scalar.default_value is Ellipsis
    assert scalar.to_dict() == {
        "wireName": "value",
        "optionalValue": None,
    }
    with pytest.raises(TypeError):
        ScalarSample.from_dict({"wire_name": "value"})

    Container = namespace["Container"]
    container = Container.from_dict({"nestedValue": {"wireName": "nested"}})
    assert container.nested_value.wire_name == "nested"
    assert container.to_dict() == {"nestedValue": {"wireName": "nested"}}
