"""Identifier conversion helpers."""

from __future__ import annotations

import keyword
import re

FIRST_CAP_RE = re.compile(r"(.)([A-Z][a-z]+)")
ALL_CAP_RE = re.compile(r"([a-z0-9])([A-Z])")
NON_IDENT_RE = re.compile(r"[^0-9a-zA-Z]+")


def snake_case(name: str) -> str:
    name = NON_IDENT_RE.sub("_", name)
    name = FIRST_CAP_RE.sub(r"\1_\2", name)
    name = ALL_CAP_RE.sub(r"\1_\2", name)
    name = re.sub(r"__+", "_", name)
    return safe_ident(name.strip("_").lower())


def pascal_case(name: str) -> str:
    parts = [p for p in NON_IDENT_RE.sub("_", name).split("_") if p]
    return "".join(p[0].upper() + p[1:] for p in parts)


def safe_ident(name: str) -> str:
    if keyword.iskeyword(name):
        return name + "_"
    if name and name[0].isdigit():
        return "n" + name
    return name
