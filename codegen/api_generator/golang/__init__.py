"""Go source generation from the shared semantic IR."""

from .emitter import GoEmitter, generate
from .naming import go_model_field_names, go_name, go_type
from .pagination import render_pagination

__all__ = [
    "GoEmitter",
    "generate",
    "go_model_field_names",
    "go_name",
    "go_type",
    "render_pagination",
]
