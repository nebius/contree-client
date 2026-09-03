"""Every backend - the testing double included - exposes one interface.

Guards the Liskov contract: the whole API surface lives on the base
classes and adapters only implement the transport primitives, so any
backend of the matching flavour is substitutable. A failure here means
an adapter drifted: it grew an incompatible constructor, overrode an
API method, or the sync/async surfaces diverged.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
from types import ModuleType

SYNC_BACKENDS = ("http", "urllib3", "requests", "httpx", "testing")
ASYNC_BACKENDS = ("httpx", "aiohttp", "testing")

# adapters implement these; everything else must come from the base
TRANSPORT_CONTRACT = frozenset({"request", "stream", "close", "open"})

COMMON_PARAMS = (
    "token",
    "base_url",
    "project",
    "timeout",
    "retry",
    "identity",
    "ssl_context",
)


def load_class(module_name: str, class_name: str) -> type:
    module = importlib.import_module(f"contree_client.{module_name}")
    return getattr(module, class_name)


def all_backends(generated_package: ModuleType) -> list[tuple[str, type, type]]:
    base = importlib.import_module("contree_client.base")
    backends = [
        (
            f"{name}.ContreeClient",
            load_class(name, "ContreeClient"),
            base.ContreeSyncClient,
        )
        for name in SYNC_BACKENDS
    ]
    backends.extend(
        (
            f"{name}.ContreeAsyncClient",
            load_class(name, "ContreeAsyncClient"),
            base.ContreeAsyncClient,
        )
        for name in ASYNC_BACKENDS
    )
    return backends


def api_names(base_class: type) -> set[str]:
    """Public API callables of a base class, transport contract aside."""
    base_module = importlib.import_module("contree_client.base")
    names: set[str] = set()
    for klass in (base_class, base_module.ContreeClientBase):
        for name, value in vars(klass).items():
            if name.startswith("_") or name in TRANSPORT_CONTRACT:
                continue
            if callable(value) or isinstance(value, (classmethod, staticmethod)):
                names.add(name)
    return names


def test_constructors_share_the_common_signature(
    generated_package: ModuleType,
) -> None:
    for label, klass, _ in all_backends(generated_package):
        parameters = inspect.signature(klass.__init__).parameters
        names = list(parameters)
        assert names[0] == "self", label
        assert names[1] == "token", label
        for common in COMMON_PARAMS[1:]:
            assert common in parameters, f"{label} lost the {common!r} kwarg"
            assert parameters[common].kind is inspect.Parameter.KEYWORD_ONLY, (
                f"{label}: {common!r} must be keyword-only"
            )
        adapter = klass.__module__.rsplit(".", 1)[-1]
        for name, parameter in parameters.items():
            if name in ("self", *COMMON_PARAMS):
                continue
            # transport-specific extras must not break substitution
            assert parameter.kind is inspect.Parameter.KEYWORD_ONLY, (
                f"{label}: extra {name!r} must be keyword-only"
            )
            assert parameter.default is not inspect.Parameter.empty, (
                f"{label}: extra {name!r} must have a default"
            )
            # ...and must be recognizable as adapter-specific by name
            assert name.startswith(f"{adapter}_"), (
                f"{label}: adapter-specific {name!r} must carry the {adapter}_ prefix"
            )


def test_api_methods_are_inherited_unchanged(
    generated_package: ModuleType,
) -> None:
    for label, klass, base_class in all_backends(generated_package):
        for name in sorted(api_names(base_class)):
            ours = inspect.getattr_static(klass, name)
            inherited = inspect.getattr_static(base_class, name)
            assert ours is inherited, (
                f"{label} overrides API method {name!r};"
                " backends may only implement the transport contract"
            )


def test_sync_and_async_surfaces_match(generated_package: ModuleType) -> None:
    base = importlib.import_module("contree_client.base")
    sync_names = api_names(base.ContreeSyncClient)
    async_names = api_names(base.ContreeAsyncClient)
    assert sync_names == async_names


def test_transport_contract_is_implemented_everywhere(
    generated_package: ModuleType,
) -> None:
    for label, klass, _ in all_backends(generated_package):
        abstract: frozenset[str] = getattr(klass, "__abstractmethods__", frozenset())
        assert not abstract, f"{label} leaves {set(abstract)} unimplemented"


def test_adapters_expose_no_public_internals(generated_package: ModuleType) -> None:
    """Implementation-specific state stays underscore-prefixed: nothing
    a caller writes against one adapter (``client.session``,
    ``client.pool``, ...) may accidentally exist on it - the public
    surface is exactly the base one, on every backend."""
    for label, klass, base_class in all_backends(generated_package):
        if klass.__module__ == "contree_client.testing":
            # the double's mocks/calls/constructed_with are its own API
            continue
        client = klass("token", base_url="http://localhost:1")
        try:
            extra_attrs = {
                name for name in vars(client) if not name.startswith("_")
            } - set(COMMON_PARAMS)
            assert not extra_attrs, (
                f"{label} leaks public instance attributes {sorted(extra_attrs)};"
                " adapter internals must be _-prefixed"
            )
            extra_names = (
                {name for name in vars(klass) if not name.startswith("_")}
                - TRANSPORT_CONTRACT
                - set(dir(base_class))
            )
            assert not extra_names, (
                f"{label} adds public class attributes {sorted(extra_names)}"
                " beyond the base surface and the transport contract"
            )
        finally:
            if inspect.iscoroutinefunction(klass.close):
                asyncio.run(client.close())
            else:
                client.close()
