"""Sphinx configuration for contree-client."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import tomllib

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "client"))
sys.path.insert(0, str(ROOT / "codegen"))

with (ROOT / "client" / "pyproject.toml").open("rb") as fp:
    release = tomllib.load(fp)["project"]["version"]
version = ".".join(release.split(".")[:2])

project = "contree-client"
author = "Dmitry Orlov"
copyright = f"{datetime.now(timezone.utc).year}, Nebius B.V."  # noqa: A001

extensions = [
    "myst_parser",
    "sphinx.ext.autodoc",
    "sphinx.ext.intersphinx",
    "sphinx.ext.napoleon",
    "sphinx.ext.viewcode",
    "sphinx_design",
    "sphinx_mintlify_output",
]

mintlify_docs_json = {
    "name": "contree-client",
    "theme": "mint",
    "colors": {"primary": "#0d9373"},
}

source_suffix = {
    ".rst": "restructuredtext",
    ".md": "markdown",
}

autodoc_member_order = "bysource"
autodoc_typehints = "description"
autodoc_default_options = {
    "members": True,
    "undoc-members": True,
    "show-inheritance": True,
    # don't follow __all__: the package page would otherwise document
    # every re-exported model a second time (imported members are
    # skipped by default, each module documents only what it defines)
    "ignore-module-all": True,
}

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
}

myst_enable_extensions = [
    "colon_fence",
    "deflist",
]

# section headings get predictable slugs, so pages can deep-link each
# other (tutorial -> api.md#pagination and friends)
myst_heading_anchors = 3

html_title = "contree-client"

exclude_patterns = ["_build"]

# `type[...]` annotations produce an ambiguous `type` cross-reference
# (it collides with attributes named "type", e.g. OperationEvent.type)
suppress_warnings = ["ref.python"]


def inject_field_docs(
    app: object,
    what: str,
    name: str,
    obj: object,
    options: object,
    lines: list[str],
) -> None:
    """Document dataclass fields from their `field(metadata=...)`.

    The generated models carry the spec descriptions, examples and
    defaults as machine-readable field metadata; autodoc never emits
    docstring events for undocumented attributes, so a Fields section
    is appended to the class docstring instead (as a bullet list -
    definition lists are not portable across builders).
    """
    import dataclasses
    import importlib
    import textwrap

    from api_generator.documentation import (
        format_example,
        protect_literals,
        restore_literals,
        sanitize_doc,
    )

    if what != "class":
        return
    module_name, _, class_name = name.rpartition(".")
    try:
        cls = getattr(importlib.import_module(module_name), class_name)
    except Exception:
        return
    if not (isinstance(cls, type) and dataclasses.is_dataclass(cls)):
        return
    entries: list[str] = []
    for spec_field in dataclasses.fields(cls):
        metadata = spec_field.metadata
        if not metadata:
            continue
        body = " ".join(
            sanitize_doc(str(metadata.get("description", "")), escape=False).split()
        )
        if "default" in metadata:
            body = f"{body} Default: ``{metadata['default']!r}``.".strip()
        if "example" in metadata:
            example = format_example(metadata["example"])
            if example:
                body = f"{body} Example: ``{example}``.".strip()
        if not body:
            continue
        entries.extend(
            restore_literals(
                textwrap.wrap(
                    protect_literals(f"``{spec_field.name}`` - {body}"),
                    width=70,
                    initial_indent="- ",
                    subsequent_indent="  ",
                    break_long_words=False,
                    break_on_hyphens=False,
                )
            )
        )
    if not entries:
        return
    if lines and lines[-1]:
        lines.append("")
    lines.append("**Fields:**")
    lines.append("")
    lines.extend(entries)
    lines.append("")


def setup(app):  # type: ignore[no-untyped-def]
    app.connect("autodoc-process-docstring", inject_field_docs)
