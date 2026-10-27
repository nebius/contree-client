"""Descriptions render as structured reST, including inside docstrings."""

from __future__ import annotations

from io import StringIO

import pytest
from docutils import nodes
from docutils.core import publish_doctree

from api_generator.documentation import build_docstring, doc_block_lines


def parse_description(text: str) -> nodes.document:
    warnings = StringIO()
    document = publish_doctree(text, settings_overrides={"warning_stream": warnings})
    assert not warnings.getvalue(), warnings.getvalue()
    return document


@pytest.mark.parametrize("python_docstring", [False, True])
def test_markdown_table_and_heading_render_as_nodes(python_docstring: bool) -> None:
    description = r"""### Error handling

| Status | Meaning | Client action |
| :--- | ---: | :---: |
| 200 | Stream open | Read `stdout\|stderr` |
| 403 | Requires `follow=1` | Use the right scope |
| 410 | Not ready | Retry after `Retry-After` |
| 504 | | Retry |

### Resume with `Last-Event-Id`

Keep the cursor in `C:\tmp`.
"""
    if python_docstring:
        source = "def read_events():\n" + "\n".join(
            build_docstring("    ", "Read events.", description)
        )
        namespace: dict = {}
        exec(compile(source, "<generated>", "exec"), namespace)
        text = namespace["read_events"].__doc__
    else:
        text = "\n".join(doc_block_lines(description, escape=False))

    document = parse_description(text)
    tables = list(document.findall(nodes.table))
    assert len(tables) == 1
    rows = list(tables[0].findall(nodes.row))
    assert [[entry.astext() for entry in row.findall(nodes.entry)] for row in rows] == [
        ["Status", "Meaning", "Client action"],
        ["200", "Stream open", "Read stdout|stderr"],
        ["403", "Requires follow=1", "Use the right scope"],
        ["410", "Not ready", "Retry after Retry-After"],
        ["504", "", "Retry"],
    ]
    assert [node.astext() for node in document.findall(nodes.rubric)] == [
        "Error handling",
        "Resume with Last-Event-Id",
    ]
    assert r"C:\tmp" in [node.astext() for node in document.findall(nodes.literal)]
    assert not list(document.findall(nodes.literal_block))


def test_fenced_markdown_stays_literal() -> None:
    description = "```markdown\n### Heading\n| Key | Value |\n| --- | --- |\n```"
    document = parse_description("\n".join(doc_block_lines(description, escape=False)))
    assert not list(document.findall(nodes.table))
    assert not list(document.findall(nodes.rubric))
    assert next(document.findall(nodes.literal_block)).astext() == (
        "### Heading\n| Key | Value |\n| --- | --- |"
    )


@pytest.mark.parametrize(
    "description",
    ["| not a table\n| just lines", "| A | B |\n| --- | --- |\n| only one cell |"],
)
def test_non_table_pipe_blocks_stay_literal(description: str) -> None:
    document = parse_description("\n".join(doc_block_lines(description, escape=False)))
    assert not list(document.findall(nodes.table))
    assert next(document.findall(nodes.literal_block)).astext() == description
