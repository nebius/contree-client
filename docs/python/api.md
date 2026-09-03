# Models and API

The `models`, `operations`, `base` and `spec_info` modules are
produced from the OpenAPI specification by the code generator. The
whole API surface lives on
{class}`~contree_client.base.ContreeSyncClient` /
{class}`~contree_client.base.ContreeAsyncClient` and is identical for
every [transport adapter](adapters.md); the async interface mirrors
the sync one exactly — methods are awaitable, iterators are
asynchronous.

## Run a command in a sandbox

::::{tab-set}

:::{tab-item} Sync
:sync: sync

<!-- name: test_api_spawn; fixtures: client -->
```python
response = client.spawn_instance(
    "echo hello world",
    "tag:ubuntu:latest",   # image tag, or a bare image UUID
    shell=True,
    env={"GREETING": "hi"},
    timeout=60,
)
operation_id = response.uuid
```
:::

:::{tab-item} Async
:sync: async

<!--
name: async test_api_spawn_async;
fixtures: async_client
```python
client = async_client
```
-->
```python
response = await client.spawn_instance(
    "echo hello world",
    "tag:ubuntu:latest",   # image tag, or a bare image UUID
    shell=True,
    env={"GREETING": "hi"},
    timeout=60,
)
operation_id = response.uuid
```
:::

::::

Parameters you do not pass are **omitted from the request entirely**,
so the server-side defaults apply. Passing an explicit `None` sends a
JSON `null` (models are tri-state: unset `...` / `None` / value):

::::{tab-set}

:::{tab-item} Sync
:sync: sync

<!-- name: test_api_tristate; fixtures: client -->
```python
client.spawn_instance("/bin/true", "tag:ubuntu:latest")
# body: {"command": "/bin/true", "image": "tag:ubuntu:latest"}

client.spawn_instance("/bin/true", "tag:ubuntu:latest", env=None)
# body: {"command": "/bin/true", "image": "tag:ubuntu:latest", "env": null}
```
:::

:::{tab-item} Async
:sync: async

<!--
name: async test_api_tristate_async;
fixtures: async_client
```python
client = async_client
```
-->
```python
await client.spawn_instance("/bin/true", "tag:ubuntu:latest")
# body: {"command": "/bin/true", "image": "tag:ubuntu:latest"}

await client.spawn_instance("/bin/true", "tag:ubuntu:latest", env=None)
# body: {"command": "/bin/true", "image": "tag:ubuntu:latest", "env": null}
```
:::

::::

Files staged via {ref}`upload <api-files>` can be injected into the
sandbox, stdin is a `StreamRepr`:

::::{tab-set}

:::{tab-item} Sync
:sync: sync

<!-- name: test_api_spawn_files; fixtures: client, file_uuid -->
```python
from contree_client import FileSpec, StreamRepr

client.spawn_instance(
    "/bin/sh",
    "tag:ubuntu:latest",
    args=["-c", "wc -l < /work/data.txt"],
    # mode accepts the octal wire string ("0644") or a plain int
    files={"/work/data.txt": FileSpec(uuid=file_uuid, mode=0o644)},
    # StreamRepr.from_bytes()/from_text() pick the encoding for you
    stdin=StreamRepr.from_text("hello\n"),
)
```
:::

:::{tab-item} Async
:sync: async

<!--
name: async test_api_spawn_files_async;
fixtures: async_client, file_uuid
```python
client = async_client
```
-->
```python
from contree_client import FileSpec, StreamRepr

await client.spawn_instance(
    "/bin/sh",
    "tag:ubuntu:latest",
    args=["-c", "wc -l < /work/data.txt"],
    # mode accepts the octal wire string ("0644") or a plain int
    files={"/work/data.txt": FileSpec(uuid=file_uuid, mode=0o644)},
    # StreamRepr.from_bytes()/from_text() pick the encoding for you
    stdin=StreamRepr.from_text("hello\n"),
)
```
:::

::::

## Wait for an operation

::::{tab-set}

:::{tab-item} Sync
:sync: sync

<!-- name: test_api_wait; fixtures: client, operation_id -->
```python
import time

from contree_client import OperationStatus

while True:
    operation = client.get_operation_status(operation_id)
    if operation.status.is_terminal():   # SUCCESS / FAILED / CANCELLED
        break
    time.sleep(1)

if operation.status is OperationStatus.SUCCESS:
    result = operation.metadata.result
    print(result.stdout.value, result.state.exit_code)
    print("result image:", operation.result_image_uuid)
```
:::

:::{tab-item} Async
:sync: async

<!--
name: async test_api_wait_async;
fixtures: async_client, operation_id
```python
client = async_client
```
-->
```python
import asyncio

from contree_client import OperationStatus

while True:
    operation = await client.get_operation_status(operation_id)
    if operation.status.is_terminal():   # SUCCESS / FAILED / CANCELLED
        break
    await asyncio.sleep(1)

if operation.status is OperationStatus.SUCCESS:
    result = operation.metadata.result
    print(result.stdout.value, result.state.exit_code)
    print("result image:", operation.result_image_uuid)
```
:::

::::

## Stream operation events (SSE)

`iter_operation_events` yields typed
{class}`~contree_client.OperationEvent` objects as the server flushes
them:

::::{tab-set}

:::{tab-item} Sync
:sync: sync

<!-- name: test_api_events; fixtures: client, operation_id -->
```python
for event in client.iter_operation_events(operation_id, follow=True):
    match event.type:
        case "stdout" | "stderr":
            print(event.type, event.data.value)
        case "exit":
            print("exit code:", event.data.code)
        case "completion":
            print("finished:", event.data.status)
```
:::

:::{tab-item} Async
:sync: async

<!--
name: async test_api_events_async;
fixtures: async_client, operation_id
```python
client = async_client
```
-->
```python
async for event in client.iter_operation_events(operation_id, follow=True):
    match event.type:
        case "stdout" | "stderr":
            print(event.type, event.data.value)
        case "exit":
            print("exit code:", event.data.code)
        case "completion":
            print("finished:", event.data.status)
```
:::

::::

Use `follow_operation_events` when the client must reconnect a broken
event stream automatically.

(api-files)=
## Files

::::{tab-set}

:::{tab-item} Sync
:sync: sync

<!-- name: test_api_file_operations; fixtures: client -->
```python
import hashlib

payload = b"hello world\n"
uploaded = client.upload_file(payload)           # bytes or a binary file
assert uploaded.sha256 == hashlib.sha256(payload).hexdigest()

# deduplicating upload: hashes locally, probes the server and only
# uploads on a miss (pass sha256=... when you already know the digest)
stored = client.ensure_file(payload)

client.check_file_exists(uploaded.sha256)        # True / False
info = client.get_file(uploaded.sha256)          # uuid, size, timestamps
listing = client.list_files(limit=10, since="1w")
```
:::

:::{tab-item} Async
:sync: async

<!--
name: async test_api_file_operations_async;
fixtures: async_client
```python
client = async_client
```
-->
```python
import hashlib

payload = b"hello world\n"
uploaded = await client.upload_file(payload)     # bytes or a binary file
assert uploaded.sha256 == hashlib.sha256(payload).hexdigest()

# deduplicating upload: hashes locally (in a worker thread), probes
# the server and only uploads on a miss
stored = await client.ensure_file(payload)

await client.check_file_exists(uploaded.sha256)  # True / False
info = await client.get_file(uploaded.sha256)    # uuid, size, timestamps
listing = await client.list_files(limit=10, since="1w")
```
:::

::::

## Pagination

Every listing (`list_images`, `list_operations`, `list_files`) mirrors
the wire API: one call, one page (`limit`/`offset`). The `iter_*`
twins paginate transparently as items are consumed — breaking out of
the loop stops fetching. `page_size` tunes the per-request batch and
`limit` caps the total:

::::{tab-set}

:::{tab-item} Sync
:sync: sync

<!-- name: test_api_pagination; fixtures: client -->
```python
for image in client.iter_images(tagged=True, page_size=500, limit=2000):
    print(image.uuid, image.tag)
```
:::

:::{tab-item} Async
:sync: async

<!--
name: async test_api_pagination_async;
fixtures: async_client
```python
client = async_client
```
-->
```python
async for image in client.iter_images(tagged=True, page_size=500, limit=2000):
    print(image.uuid, image.tag)
```
:::

::::

## Images

::::{tab-set}

:::{tab-item} Sync
:sync: sync

<!-- name: test_api_image_operations; fixtures: client, image_uuid -->
```python
from contree_client import ImageImportRegistry

# import from a registry; returns the operation id to poll/stream
operation_id = client.import_image(
    ImageImportRegistry(url="docker://docker.io/busybox:latest"),
    tag="busybox:latest",
)

# tags
image = client.update_image_tag(image_uuid, tag="my/base:latest")
client.delete_image_tag(image_uuid, tag="my/base:latest")

# inspection without spawning anything
image = client.inspect_image(image_uuid)
listing = client.inspect_image_list(image_uuid, "/etc")
content = client.inspect_image_download(image_uuid, "/etc/hosts")
exists = client.check_image_file(image_uuid, "/etc/hosts")
```
:::

:::{tab-item} Async
:sync: async

<!--
name: async test_api_image_operations_async;
fixtures: async_client, image_uuid
```python
client = async_client
```
-->
```python
from contree_client import ImageImportRegistry

# import from a registry; returns the operation id to poll/stream
operation_id = await client.import_image(
    ImageImportRegistry(url="docker://docker.io/busybox:latest"),
    tag="busybox:latest",
)

# tags
image = await client.update_image_tag(image_uuid, tag="my/base:latest")
await client.delete_image_tag(image_uuid, tag="my/base:latest")

# inspection without spawning anything
image = await client.inspect_image(image_uuid)
listing = await client.inspect_image_list(image_uuid, "/etc")
content = await client.inspect_image_download(image_uuid, "/etc/hosts")
exists = await client.check_image_file(image_uuid, "/etc/hosts")
```
:::

::::

Archives can be arbitrarily large, so `inspect_image_archive` is
stream-only. By default the chunks are a plain tar; `compressed=True`
disables transparent decompression, yielding the body exactly as
served — a ready-to-save `.tar.gz` when the server compresses the
response:

::::{tab-set}

:::{tab-item} Sync
:sync: sync

<!--
name: test_api_archive;
fixtures: client, image_uuid, tar_archive, isolated_cwd
```python
import gzip

# the second stream is served compressed
client.mock("inspect_image_archive", [gzip.compress(tar_archive)])
```
-->
```python
with open("etc.tar", "wb") as archive:
    for chunk in client.inspect_image_archive(image_uuid, "/etc"):
        archive.write(chunk)

with open("etc.tar.gz", "wb") as archive:
    for chunk in client.inspect_image_archive(image_uuid, "/etc", compressed=True):
        archive.write(chunk)
```
:::

:::{tab-item} Async
:sync: async

<!--
name: async test_api_archive_async;
fixtures: async_client, image_uuid, tar_archive, isolated_cwd
```python
import gzip

client = async_client
# the second stream is served compressed
client.mock("inspect_image_archive", [gzip.compress(tar_archive)])
```
-->
```python
with open("etc.tar", "wb") as archive:
    async for chunk in client.inspect_image_archive(image_uuid, "/etc"):
        archive.write(chunk)

with open("etc.tar.gz", "wb") as archive:
    async for chunk in client.inspect_image_archive(
        image_uuid, "/etc", compressed=True
    ):
        archive.write(chunk)
```
:::

::::

File downloads have a streaming variant too
(`inspect_image_download_stream`).

## Error handling

Every buffered response status of 400 or greater maps to
{class}`~contree_client.APIStatusError`. Known statuses use a specific
subclass. The exception contains `status`, `error`, `traceback`, and
`retry_after`.

::::{tab-set}

:::{tab-item} Sync
:sync: sync

<!--
name: test_api_error_handling;
fixtures: client, image_uuid
```python
import time

from contree_client.exceptions import NotFoundError as ArmedNotFound

# queue the error behind the canned success and consume the success,
# so the visible call below hits the error
client.mock("inspect_image", error=ArmedNotFound(404, "image not found"))
client.inspect_image(image_uuid)
```
-->
```python
from contree_client import (
    APIStatusError,
    AuthenticationError,  # 401
    GoneError,        # 410 - retry after error.retry_after seconds
    NotFoundError,    # 404
    TooEarlyError,    # 425 - not ready yet, retry after error.retry_after
)

try:
    client.inspect_image(image_uuid)
except NotFoundError:
    ...
except TooEarlyError as error:
    time.sleep(error.retry_after or 1)
except APIStatusError as error:
    print(error.status, error.error)
```
:::

:::{tab-item} Async
:sync: async

<!--
name: async test_api_error_handling_async;
fixtures: async_client, image_uuid
```python
import asyncio

from contree_client.exceptions import NotFoundError as ArmedNotFound

client = async_client
# queue the error behind the canned success and consume the success,
# so the visible call below hits the error
client.mock("inspect_image", error=ArmedNotFound(404, "image not found"))
await client.inspect_image(image_uuid)
```
-->
```python
from contree_client import (
    APIStatusError,
    AuthenticationError,  # 401
    GoneError,        # 410 - retry after error.retry_after seconds
    NotFoundError,    # 404
    TooEarlyError,    # 425 - not ready yet, retry after error.retry_after
)

try:
    await client.inspect_image(image_uuid)
except NotFoundError:
    ...
except TooEarlyError as error:
    await asyncio.sleep(error.retry_after or 1)
except APIStatusError as error:
    print(error.status, error.error)
```
:::

::::

`400 → BadRequestError`, `401 → AuthenticationError`,
`403 → PermissionDeniedError`, `404 → NotFoundError`,
`409 → ConflictError`, `410 → GoneError`,
`422 → UnprocessableEntityError`, `425 → TooEarlyError`,
`429 → RateLimitError`, `>=500 → ServerError`.
Other error statuses use `APIStatusError` directly.

### Exception hierarchy

Normalized request failures inherit
{class}`~contree_client.ContreeError`:

```
ContreeError
├── APIConnectionError
└── APIStatusError
    ├── BadRequestError              # 400
    ├── AuthenticationError          # 401
    ├── PermissionDeniedError        # 403
    ├── NotFoundError                # 404
    ├── ConflictError                # 409
    ├── GoneError                    # 410
    ├── UnprocessableEntityError     # 422
    ├── TooEarlyError                # 425
    ├── RateLimitError               # 429
    └── ServerError                  # >=500
```

{class}`~contree_client.APIConnectionError` covers backend failures in an
adapter's buffered `request()` method. The backend exception remains the
standard Python `__cause__` for diagnostics. Streaming methods preserve
their backend's errors. `timed_out` is true for backend timeouts.

<!--
name: test_connection_error_handling;
fixtures: client
```python
from contree_client import APIConnectionError

client.mock("whoami", error=APIConnectionError("connection refused"))
```
-->
```python
from contree_client import APIConnectionError

try:
    client.whoami()
except APIConnectionError as error:
    print(error)
    print(error.__cause__)
```

Client-side argument errors remain `ValueError` or `TypeError`.
Operation helper deadlines raise `TimeoutError`. Cancellation,
`KeyboardInterrupt`, and `SystemExit` are not wrapped.

## Models

Models are plain dataclasses generated from the spec. `from_dict`
parses wire payloads (missing key → unset `...`, explicit `null` →
`None`), `to_dict` serializes back omitting unset fields; the spec
descriptions, examples and defaults are attached as
`dataclasses.field(metadata=...)`.

## Reference

### contree_client.base

```{eval-rst}
.. automodule:: contree_client.base
```

### contree_client.models

```{eval-rst}
.. automodule:: contree_client.models
```

### contree_client.exceptions

```{eval-rst}
.. automodule:: contree_client.exceptions
```

### contree_client.operations

```{eval-rst}
.. automodule:: contree_client.operations
```

### contree_client.spec_info

```{eval-rst}
.. automodule:: contree_client.spec_info
```
