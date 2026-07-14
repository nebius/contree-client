"""In-memory test doubles for user test suites.

``ContreeClient`` and ``ContreeAsyncClient`` here implement the same
interface as every real backend, but perform no I/O: each API method
must be mocked first with :meth:`MockMixin.mock`, and calling anything
unmocked raises :class:`NotMockedError`. Results are returned exactly
as given, calls are recorded for assertions::

    from contree_client.models import WhoAmIResponse
    from contree_client.testing import ContreeClient

    client = ContreeClient()
    client.mock("whoami", WhoAmIResponse(permissions={"spawn": True}))

    assert client.whoami().permissions["spawn"] is True
    assert client.calls_for("whoami")

Repeated ``mock()`` calls for the same operation queue sequential
results; the last one is sticky (convenient for polling loops)::

    client.mock("get_operation_status", running)
    client.mock("get_operation_status", success)

Iterator-returning operations (``iter_operation_events``,
``inspect_image_archive``, ...) take an iterable of items and yield
them one by one; a queued *error* is raised after the items, which
models a stream broken mid-flight.
"""

from __future__ import annotations

import inspect
import ssl
from collections import deque
from collections.abc import AsyncGenerator, AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any

from . import base
from .runtime import RequestSpec, ResponseData, RetryPolicy, logger
from .spec_info import DEFAULT_BASE_URL

RESERVED = frozenset({"request", "stream", "open", "close", "mock", "calls_for"})


class NotMockedError(AssertionError):
    """An API method was called without a prepared mock."""


@dataclass
class Call:
    """One recorded API method invocation."""

    operation: str
    args: tuple[Any, ...]
    kwargs: dict[str, Any]


@dataclass
class Outcome:
    result: Any
    error: BaseException | None


def unmocked(spec: RequestSpec) -> NotMockedError:
    return NotMockedError(
        f"unmocked request {spec.method} {spec.path}: the API method"
        " that builds it is not mocked; call client.mock(...) first"
    )


class MockMixin:
    """Mock bookkeeping shared by the sync and async test clients."""

    mocks: dict[str, deque[Outcome]]
    calls: list[Call]

    def mock(
        self,
        operation: str,
        result: Any = None,
        *,
        error: BaseException | None = None,
    ) -> None:
        """Prepare the next outcome of *operation*.

        ``result`` is returned as-is (an iterable of items for
        iterator operations); ``error`` is raised instead (after the
        items for iterator operations).
        """
        method = self.api_method(operation)
        queue = self.mocks.setdefault(operation, deque())
        queue.append(Outcome(result, error))
        setattr(self, operation, self.build_wrapper(operation, method))

    def calls_for(self, operation: str) -> list[Call]:
        """Recorded calls of a single operation, in order."""
        return [call for call in self.calls if call.operation == operation]

    def api_method(self, operation: str) -> Any:
        if (
            operation.startswith("__")
            or operation in RESERVED
            or hasattr(MockMixin, operation)
            or operation in vars(base.ContreeClientBase)
        ):
            raise ValueError(f"{operation!r} is not a mockable API method")
        method = getattr(type(self), operation, None)
        if not callable(method):
            raise ValueError(f"{type(self).__name__} has no API method {operation!r}")
        return method

    def take(self, operation: str) -> Outcome:
        queue = self.mocks[operation]
        if len(queue) > 1:
            return queue.popleft()
        return queue[0]

    def record(
        self, operation: str, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> Outcome:
        self.calls.append(Call(operation, args, dict(kwargs)))
        return self.take(operation)

    def build_wrapper(self, operation: str, method: Any) -> Any:
        if inspect.isasyncgenfunction(method):

            def async_iter_wrapper(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
                outcome = self.record(operation, args, kwargs)

                async def generator() -> AsyncIterator[Any]:
                    for item in outcome.result or ():
                        yield item
                    if outcome.error is not None:
                        raise outcome.error

                return generator()

            return async_iter_wrapper

        if inspect.iscoroutinefunction(method):

            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                outcome = self.record(operation, args, kwargs)
                if outcome.error is not None:
                    raise outcome.error
                return outcome.result

            return async_wrapper

        if inspect.isgeneratorfunction(method):

            def iter_wrapper(*args: Any, **kwargs: Any) -> Iterator[Any]:
                outcome = self.record(operation, args, kwargs)

                def generator() -> Iterator[Any]:
                    yield from outcome.result or ()
                    if outcome.error is not None:
                        raise outcome.error

                return generator()

            return iter_wrapper

        def wrapper(*args: Any, **kwargs: Any) -> Any:
            outcome = self.record(operation, args, kwargs)
            if outcome.error is not None:
                raise outcome.error
            return outcome.result

        return wrapper


class ContreeClient(MockMixin, base.ContreeSyncClient):
    """Synchronous test double; see the module docstring."""

    log = logger.getChild("testing")

    def __init__(
        self,
        token: str = "test-token",
        *,
        base_url: str = DEFAULT_BASE_URL,
        project: str | None = None,
        timeout: float | None = None,
        retry: RetryPolicy | None = None,
        identity: str | None = None,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        super().__init__(
            token,
            base_url=base_url,
            project=project,
            timeout=timeout,
            retry=retry,
            identity=identity,
        )
        self.mocks = {}
        self.calls = []
        # how the double was constructed, for assertions in tests of
        # code that builds clients itself (factories, from_profile)
        self.constructed_with = {
            "token": token,
            "base_url": base_url,
            "project": project,
            "timeout": timeout,
            "retry": retry,
            "identity": identity,
            "ssl_context": ssl_context,
        }

    def request(self, spec: RequestSpec) -> ResponseData:
        raise unmocked(spec)

    def stream(
        self,
        spec: RequestSpec,
        auto_decompress: bool = True,
    ) -> Iterator[bytes]:
        raise unmocked(spec)

    def close(self) -> None:
        pass


class ContreeAsyncClient(MockMixin, base.ContreeAsyncClient):
    """Asynchronous test double; see the module docstring."""

    log = logger.getChild("testing")

    def __init__(
        self,
        token: str = "test-token",
        *,
        base_url: str = DEFAULT_BASE_URL,
        project: str | None = None,
        timeout: float | None = None,
        retry: RetryPolicy | None = None,
        identity: str | None = None,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        super().__init__(
            token,
            base_url=base_url,
            project=project,
            timeout=timeout,
            retry=retry,
            identity=identity,
        )
        self.mocks = {}
        self.calls = []
        # how the double was constructed, for assertions in tests of
        # code that builds clients itself (factories, from_profile)
        self.constructed_with = {
            "token": token,
            "base_url": base_url,
            "project": project,
            "timeout": timeout,
            "retry": retry,
            "identity": identity,
            "ssl_context": ssl_context,
        }

    async def request(self, spec: RequestSpec) -> ResponseData:
        raise unmocked(spec)

    def stream(
        self,
        spec: RequestSpec,
        auto_decompress: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        raise unmocked(spec)

    async def close(self) -> None:
        pass
