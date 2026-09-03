# Testing your code

{mod}`contree_client.testing` ships in-memory test doubles for user
test suites. `ContreeClient` and `ContreeAsyncClient` implement the
same interface as every real backend but perform no I/O: each API
method is mocked by name, results are returned exactly as given, and
every call is recorded for assertions. `mock()` itself is a plain
synchronous call on both flavours.

Because all adapters are interchangeable (see
[Transport adapters](adapters.md)), any code annotated against the
base classes from {mod}`contree_client.types` accepts the test double
unchanged.

## Mocking operations

`mock(operation, result)` prepares the next return value of an API
method; calling anything unmocked raises `NotMockedError`:

::::{tab-set}

:::{tab-item} Sync
:sync: sync

<!-- name: test_mocking_operations -->
```python
from contree_client.models import WhoAmIResponse
from contree_client.testing import ContreeClient

client = ContreeClient()
client.mock(
    "whoami",
    WhoAmIResponse(
        token_uuid="a1b2c3d4-e5f6-7890-abcd-ef1234567890",
        token_expiration=None,
        permissions={"spawn": True},
        operations_stat={},
    ),
)

assert client.whoami().permissions["spawn"] is True
```
:::

:::{tab-item} Async
:sync: async

<!-- name: async test_mocking_operations_async -->
```python
from contree_client.models import WhoAmIResponse
from contree_client.testing import ContreeAsyncClient

client = ContreeAsyncClient()
client.mock(
    "whoami",
    WhoAmIResponse(
        token_uuid="a1b2c3d4-e5f6-7890-abcd-ef1234567890",
        token_expiration=None,
        permissions={"spawn": True},
        operations_stat={},
    ),
)

assert (await client.whoami()).permissions["spawn"] is True
```
:::

::::

## Sequential results

Repeated `mock()` calls for the same operation queue results; the last
one is sticky. This models polling loops naturally:

::::{tab-set}

:::{tab-item} Sync
:sync: sync

<!--
name: test_sequential_results
```python
from contree_client.models import OperationResponse
from contree_client.testing import ContreeClient

UUID = "87654321-9abc-baba-deda-0123456789ab"
client = ContreeClient()
running = OperationResponse.from_dict({"uuid": UUID, "status": "EXECUTING"})
success = OperationResponse.from_dict({"uuid": UUID, "status": "SUCCESS"})
```
-->
```python
client.mock("get_operation_status", running)   # first call
client.mock("get_operation_status", success)   # every call after that

assert not client.get_operation_status(UUID).status.is_terminal()
assert client.get_operation_status(UUID).status.is_terminal()
```
:::

:::{tab-item} Async
:sync: async

<!--
name: async test_sequential_results_async
```python
from contree_client.models import OperationResponse
from contree_client.testing import ContreeAsyncClient

UUID = "87654321-9abc-baba-deda-0123456789ab"
client = ContreeAsyncClient()
running = OperationResponse.from_dict({"uuid": UUID, "status": "EXECUTING"})
success = OperationResponse.from_dict({"uuid": UUID, "status": "SUCCESS"})
```
-->
```python
client.mock("get_operation_status", running)   # first call
client.mock("get_operation_status", success)   # every call after that

assert not (await client.get_operation_status(UUID)).status.is_terminal()
assert (await client.get_operation_status(UUID)).status.is_terminal()
```
:::

::::

## Errors

`error=` raises instead of returning — API errors are ordinary
exception instances:

::::{tab-set}

:::{tab-item} Sync
:sync: sync

<!--
name: test_error_outcome
```python
from contree_client.testing import ContreeClient

client = ContreeClient()
```
-->
```python
from contree_client.exceptions import NotFoundError

client.mock("get_operation_status", error=NotFoundError(404, "no such"))
```
:::

:::{tab-item} Async
:sync: async

<!--
name: async test_error_outcome_async
```python
from contree_client.testing import ContreeAsyncClient

client = ContreeAsyncClient()
```
-->
```python
from contree_client.exceptions import NotFoundError

client.mock("get_operation_status", error=NotFoundError(404, "no such"))
```
:::

::::

## Streaming operations

Iterator-returning operations (`iter_operation_events`,
`inspect_image_archive`, ...) take an iterable of items and yield them
one by one; a queued `error` is raised *after* the items, which models
a stream broken mid-flight:

::::{tab-set}

:::{tab-item} Sync
:sync: sync

<!--
name: test_streaming_operations
```python
from contree_client.models import OperationEvent
from contree_client.testing import ContreeClient

client = ContreeClient()
stdout_event = OperationEvent.from_dict(
    {
        "id": 1,
        "ts": "2026-06-08T20:00:00Z",
        "spid": 1,
        "type": "stdout",
        "data": {"value": "hi\n", "encoding": "ascii"},
    }
)
exit_event = stdout_event
```
-->
```python
client.mock(
    "iter_operation_events",
    [stdout_event, exit_event],
    error=ConnectionError("broken"),
)
```
:::

:::{tab-item} Async
:sync: async

<!--
name: async test_streaming_operations_async
```python
from contree_client.models import OperationEvent
from contree_client.testing import ContreeAsyncClient

client = ContreeAsyncClient()
stdout_event = OperationEvent.from_dict(
    {
        "id": 1,
        "ts": "2026-06-08T20:00:00Z",
        "spid": 1,
        "type": "stdout",
        "data": {"value": "hi\n", "encoding": "ascii"},
    }
)
exit_event = stdout_event
```
-->
```python
client.mock(
    "iter_operation_events",
    [stdout_event, exit_event],
    error=ConnectionError("broken"),
)
```
:::

::::

## Asserting on calls

Every invocation is recorded as a `Call` with positional and keyword
arguments:

::::{tab-set}

:::{tab-item} Sync
:sync: sync

<!--
name: test_asserting_on_calls
```python
from contree_client.models import InstanceSpawnResponse
from contree_client.testing import ContreeClient

client = ContreeClient()
client.mock(
    "spawn_instance",
    InstanceSpawnResponse.from_dict({"uuid": "87654321-9abc-baba-deda-0123456789ab"}),
)
```
-->
```python
client.spawn_instance("uname -a", "tag:ubuntu:latest", shell=True)

(call,) = client.calls_for("spawn_instance")
assert call.args == ("uname -a", "tag:ubuntu:latest")
assert call.kwargs == {"shell": True}
```
:::

:::{tab-item} Async
:sync: async

<!--
name: async test_asserting_on_calls_async
```python
from contree_client.models import InstanceSpawnResponse
from contree_client.testing import ContreeAsyncClient

client = ContreeAsyncClient()
client.mock(
    "spawn_instance",
    InstanceSpawnResponse.from_dict({"uuid": "87654321-9abc-baba-deda-0123456789ab"}),
)
```
-->
```python
await client.spawn_instance("uname -a", "tag:ubuntu:latest", shell=True)

(call,) = client.calls_for("spawn_instance")
assert call.args == ("uname -a", "tag:ubuntu:latest")
assert call.kwargs == {"shell": True}
```
:::

::::

## Asserting on construction

The double accepts the same constructor kwargs as every real backend
(`base_url`, `project`, `timeout`, `retry`, `identity`, ...) and
records them in `constructed_with` — convenient for testing code that
builds clients itself (factories, `from_profile` wiring):

::::{tab-set}

:::{tab-item} Sync
:sync: sync

<!--
name: test_asserting_on_construction
```python
from contree_client import testing


def build_my_client(client_class):
    return client_class(
        "IAM_TOKEN",
        project="my-project",
        identity="my-cli/1.2.3",
    )
```
-->
```python
client = build_my_client(testing.ContreeClient)  # code under test

assert client.constructed_with["project"] == "my-project"
assert client.constructed_with["identity"] == "my-cli/1.2.3"
```
:::

:::{tab-item} Async
:sync: async

<!--
name: test_asserting_on_construction_async
```python
from contree_client import testing


def build_my_client(client_class):
    return client_class(
        "IAM_TOKEN",
        project="my-project",
        identity="my-cli/1.2.3",
    )
```
-->
```python
client = build_my_client(testing.ContreeAsyncClient)  # code under test

assert client.constructed_with["project"] == "my-project"
assert client.constructed_with["identity"] == "my-cli/1.2.3"
```
:::

::::

## Reference

```{eval-rst}
.. automodule:: contree_client.testing
```
