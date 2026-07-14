"""OpenAPI document loading and local $ref resolution."""

from __future__ import annotations

import hashlib
import os
import urllib.request
from pathlib import Path
from typing import Any

import yaml

REF_PREFIX = "#/"

# an unresponsive endpoint must not hold the build forever, and a
# misbehaving one must not stream gigabytes into memory
FETCH_TIMEOUT = 60.0
MAX_SPEC_BYTES = 32 * 1024 * 1024


class Spec:
    def __init__(self, raw: dict[str, Any], text: str = "", sha256: str = "") -> None:
        self.raw = raw
        self.text = text
        self.sha256 = sha256

    @property
    def paths(self) -> dict[str, Any]:
        result: dict[str, Any] = self.raw["paths"]
        return result

    @property
    def schemas(self) -> dict[str, Any]:
        result: dict[str, Any] = self.raw["components"]["schemas"]
        return result

    @property
    def default_base_url(self) -> str:
        server = self.raw["servers"][0]
        result: str = server["variables"]["baseUrl"]["default"]
        return result

    def resolve_pointer(self, ref: str) -> Any:
        if not ref.startswith(REF_PREFIX):
            raise ValueError(f"only local refs are supported, got {ref!r}")
        node: Any = self.raw
        for part in ref[len(REF_PREFIX) :].split("/"):
            node = node[part.replace("~1", "/").replace("~0", "~")]
        return node

    def deref(self, node: dict[str, Any]) -> dict[str, Any]:
        """Follow $ref chains, returning the final node."""
        seen: set[str] = set()
        while "$ref" in node:
            ref = node["$ref"]
            if ref in seen:
                raise ValueError(f"circular reference: {ref!r}")
            seen.add(ref)
            node = self.resolve_pointer(ref)
        return node


def ref_name(node: dict[str, Any]) -> str | None:
    ref = node.get("$ref")
    if ref is None:
        return None
    name: str = ref.rsplit("/", 1)[-1]
    return name


def fetch_url(location: str) -> bytes:
    try:
        with urllib.request.urlopen(location, timeout=FETCH_TIMEOUT) as response:
            payload: bytes = response.read(MAX_SPEC_BYTES + 1)
    except OSError as exc:
        raise OSError(
            f"failed to fetch the OpenAPI spec from {location}: {exc}"
        ) from exc
    if len(payload) > MAX_SPEC_BYTES:
        raise ValueError(
            f"the OpenAPI spec at {location} exceeds {MAX_SPEC_BYTES} bytes"
        )
    return payload


def load_spec(source: str | Path) -> Spec:
    """Load the spec from a local path or an http(s) URL.

    The document digest is recorded on the returned :class:`Spec`;
    when the ``CONTREE_SPEC_SHA256`` environment variable is set (a
    release pin), a mismatching document is rejected outright.
    """
    location = str(source)
    if location.startswith(("http://", "https://")):
        payload = fetch_url(location)
    else:
        payload = Path(source).read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    expected = os.environ.get("CONTREE_SPEC_SHA256", "").strip().lower()
    if expected and digest != expected:
        raise ValueError(
            f"OpenAPI spec digest mismatch for {location}:"
            f" expected sha256 {expected}, got {digest}"
        )
    text = payload.decode("utf-8")
    return Spec(yaml.safe_load(text), text, sha256=digest)
