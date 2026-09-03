"""Target-facing documentation formatting shared by language emitters."""

from __future__ import annotations

import json
import re
import textwrap
from typing import Any

INLINE_CODE_RE = re.compile(r"(?<!`)`([^`\n]+)`(?!`)")
LITERAL_SPAN_RE = re.compile(r"``[^`\n]+``")
BULLET_RE = re.compile(r"^(\s*)([-*+]\s+|\d+[.)]\s+)?")
NBSP = "\x00"


def render_docstring(text: str, indent: str) -> list[str]:
    summary = summary_line(text)
    wrapped = textwrap.wrap(summary, width=72)
    if not wrapped:
        return []
    if len(wrapped) == 1:
        return [f'{indent}"""{wrapped[0]}"""']
    lines = [f'{indent}"""{wrapped[0]}']
    lines.extend(f"{indent}{line}" for line in wrapped[1:])
    lines.append(f'{indent}"""')
    return lines


def summary_line(text: str) -> str:
    for line in text.strip().splitlines():
        line = line.strip()
        if line:
            return line.rstrip(".") + "."
    return ""


def description_body(text: str) -> str:
    """Return everything after the line used as the docstring summary."""
    lines = text.strip().splitlines()
    if len(lines) <= 1:
        return ""
    return "\n".join(lines[1:]).strip("\n")


def sanitize_doc(text: str, escape: bool = True) -> str:
    """Prepare a spec description for a docstring or reST document."""
    if escape:
        text = text.replace("\\", "\\\\").replace('"""', '\\"""')
    text = text.replace("\u2011", "-")
    return INLINE_CODE_RE.sub(r"``\1``", text)


def protect_literals(text: str) -> str:
    """Make ``...`` spans unbreakable for textwrap."""
    return LITERAL_SPAN_RE.sub(lambda match: match.group(0).replace(" ", NBSP), text)


def restore_literals(lines: list[str]) -> list[str]:
    return [line.replace(NBSP, " ") for line in lines]


def format_example(value: Any) -> str:
    """Render a spec example compactly for an Example note."""
    if value is None:
        return ""
    if isinstance(value, str):
        rendered = value
    else:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            default=str,
            separators=(",", ":"),
        )
    rendered = " ".join(rendered.split())
    if not rendered:
        return ""
    if len(rendered) > 60:
        rendered = rendered[:57] + "..."
    return rendered


def documentation_text(
    description: str,
    example: Any,
    has_example: bool,
) -> str:
    """Combine raw documentation fields for generated API entries."""
    parts = [description] if description else []
    rendered_example = format_example(example) if has_example else ""
    if rendered_example:
        parts.append(f"Example: ``{rendered_example}``.")
    return " ".join(parts)


def wrap_doc_line(raw: str, width: int = 72) -> list[str]:
    raw = raw.rstrip()
    if len(raw) <= width:
        return [raw]
    match = BULLET_RE.match(raw)
    assert match is not None
    pad = len(match.group(1))
    hang = " " * (pad + len(match.group(2) or ""))
    first, *rest = textwrap.wrap(
        protect_literals(raw.strip()),
        width=max(20, width - pad),
        break_long_words=False,
        break_on_hyphens=False,
    )
    lines = [" " * pad + first]
    lines.extend(hang + piece for piece in rest)
    return restore_literals(lines)


def doc_block_lines(text: str, escape: bool = True) -> list[str]:
    """Wrap a description while preserving its original line structure."""
    lines: list[str] = []
    raw_lines = sanitize_doc(text, escape=escape).strip().splitlines()

    def literal_block(body: list[str]) -> None:
        if lines and lines[-1]:
            lines.append("")
        lines.append("::")
        lines.append("")
        lines.extend(f"    {item}".rstrip() for item in body)
        lines.append("")

    index = 0
    last_pad = 0
    while index < len(raw_lines):
        raw = raw_lines[index].rstrip()
        pad = len(raw) - len(raw.lstrip())
        if raw and lines and lines[-1] and pad < last_pad:
            lines.append("")
        if raw:
            last_pad = pad
        if raw.lstrip().startswith("|"):
            table: list[str] = []
            while index < len(raw_lines) and raw_lines[index].lstrip().startswith("|"):
                table.append(raw_lines[index].strip())
                index += 1
            literal_block(table)
            continue
        if raw.lstrip().startswith("```"):
            fence: list[str] = []
            index += 1
            while index < len(raw_lines) and not raw_lines[index].lstrip().startswith(
                "```"
            ):
                fence.append(raw_lines[index].rstrip())
                index += 1
            index += 1
            literal_block(fence)
            continue
        lines.extend(wrap_doc_line(raw))
        index += 1
    return lines


def doc_entry_lines(name: str, text: str) -> list[str]:
    """Render one entry in an Args or Attributes napoleon section."""
    body = " ".join(sanitize_doc(text).split())
    return restore_literals(
        textwrap.wrap(
            protect_literals(f"{name}: {body}"),
            width=68,
            initial_indent="    ",
            subsequent_indent="        ",
            break_long_words=False,
            break_on_hyphens=False,
        )
    )


def build_docstring(
    indent: str,
    summary: str,
    description: str = "",
    section_title: str = "",
    entries: list[tuple[str, str]] | None = None,
) -> list[str]:
    """Render a docstring with a summary, description, and section."""
    relative = textwrap.wrap(sanitize_doc(summary_line(summary)), width=72)
    if not relative:
        relative = [""]
    if description:
        relative.append("")
        relative.extend(doc_block_lines(description))
    if entries:
        relative.append("")
        relative.append(f"{section_title}:")
        for name, text in entries:
            relative.extend(doc_entry_lines(name, text))
    if len(relative) == 1:
        return [f'{indent}"""{relative[0]}"""']
    lines = [f'{indent}"""{relative[0]}']
    lines.extend(f"{indent}{line}" if line else "" for line in relative[1:])
    lines.append(f'{indent}"""')
    return lines
