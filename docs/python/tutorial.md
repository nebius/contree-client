# Tutorial

A transport-agnostic walk through the whole API. Every example below
works with **any** backend: the code only relies on the interface of
{class}`~contree_client.base.ContreeSyncClient` /
{class}`~contree_client.base.ContreeAsyncClient`, never on a concrete
adapter. Working in JavaScript? The same walk lives in the
[JavaScript tutorial](../js/tutorial.md).

## Getting a client

The autodetect modules pick the first installed backend for you (the
concrete backends, extras and constructor knobs — retries, timeouts,
the User-Agent identity — are covered on the
[Transport adapters](adapters.md) page).

Credentials come in two ways:

- **Production** — pass the token (and the project for IAM tokens)
  explicitly; a service pulls them from its own configuration or a
  secret manager, there are no profiles on a production host.
- **Local development** — *profiles*: the INI files under
  `$CONTREE_HOME` (`~/.config/contree` by default), shared by all
  Contree tooling. `from_profile()` resolves
  the explicit argument, then the `CONTREE_PROFILE` environment
  variable, then the active profile from the config — so one machine
  can hold several environments (`default`, `staging`, ...). The
  same mechanism also works on a server if you deliberately set
  `CONTREE_HOME` and ship an `auth.ini` there, but a plain token is
  usually simpler.

Every client is a context manager that releases its transport on
exit:

::::{tab-set}

:::{tab-item} Sync
:sync: sync

<!--
name: test_tutorial_init;
fixtures: tmp_path, monkeypatch
```python
monkeypatch.setenv("CONTREE_TOKEN", "IAM_TOKEN")
monkeypatch.setenv("CONTREE_PROJECT", "my-project-id")
(tmp_path / "auth.ini").write_text(
    "[DEFAULT]\nprofile = default\n"
    "[profile:default]\ntoken = SECRET\nurl = https://contree.example.com\n"
)
monkeypatch.setenv("CONTREE_HOME", str(tmp_path))
monkeypatch.delenv("CONTREE_PROFILE", raising=False)
```
-->
```python
import os

from contree_client.sync import ContreeClient  # the first installed backend

# production: explicit credentials from your configuration
client = ContreeClient(
    os.environ["CONTREE_TOKEN"],
    project=os.environ["CONTREE_PROJECT"],
)

# local development: a saved Contree profile
client = ContreeClient.from_profile()

with client:
    ...
```
:::

:::{tab-item} Async
:sync: async

<!--
name: async test_tutorial_init_async;
fixtures: tmp_path, monkeypatch
```python
monkeypatch.setenv("CONTREE_TOKEN", "IAM_TOKEN")
monkeypatch.setenv("CONTREE_PROJECT", "my-project-id")
(tmp_path / "auth.ini").write_text(
    "[DEFAULT]\nprofile = default\n"
    "[profile:default]\ntoken = SECRET\nurl = https://contree.example.com\n"
)
monkeypatch.setenv("CONTREE_HOME", str(tmp_path))
monkeypatch.delenv("CONTREE_PROFILE", raising=False)
```
-->
```python
import os

from contree_client.asyncio import ContreeAsyncClient  # the first installed backend

# production: explicit credentials from your configuration
client = ContreeAsyncClient(
    os.environ["CONTREE_TOKEN"],
    project=os.environ["CONTREE_PROJECT"],
)

# local development: a saved Contree profile
client = ContreeAsyncClient.from_profile()

async with client:
    ...
```
:::

::::

Annotate your own code against the base classes from
{mod}`contree_client.types` and let the caller pick the transport —
every example on this page runs in CI against the
[testing double](testing.md), which is just another backend:

::::{tab-set}

:::{tab-item} Sync
:sync: sync

<!--
name: test_tutorial;
fixtures: client, file_uuid, payload, isolated_cwd
-->
```python
from contree_client.types import ContreeSyncClient


def greet(client: ContreeSyncClient) -> None:
    me = client.whoami()
    print(me.permissions, me.limits)


greet(client)  # works with any transport, including the test double
```
:::

:::{tab-item} Async
:sync: async

<!--
name: async test_tutorial_async;
fixtures: async_client, file_uuid, payload, isolated_cwd
```python
client = async_client
```
-->
```python
from contree_client.types import ContreeAsyncClient


async def greet(client: ContreeAsyncClient) -> None:
    me = await client.whoami()
    print(me.permissions, me.limits)


await greet(client)  # works with any transport, including the test double
```
:::

::::

{meth}`~contree_client.base.ContreeSyncClient.whoami` returns a
{class}`~contree_client.WhoAmIResponse` describing the token: the
granted `permissions` map and the resource `limits` the server will
enforce.

## Run a command

{meth}`~contree_client.base.ContreeSyncClient.spawn_instance` creates
a sandboxed instance from an image and runs a command in it. Only
`command` and `image` are required; everything you do not pass is
omitted from the request so the server defaults apply (see
{class}`~contree_client.InstanceSpawnRequest` for every knob and its
default). Shell expressions need `shell=True`:

::::{tab-set}

:::{tab-item} Sync
:sync: sync

<!-- name: test_tutorial; case: spawn -->
```python
response = client.spawn_instance(
    "wc -l < /work/data.txt",
    "tag:ubuntu:latest",         # an image tag, or a bare image UUID
    shell=True,                  # the command is a shell expression
    env={"LC_ALL": "C"},         # explicit None sends a JSON null
    cwd="/work",
    timeout=60,                  # seconds; server-side hard cap
    disposable=True,             # do not persist a result image
)
operation_id = response.uuid
```
:::

:::{tab-item} Async
:sync: async

<!-- name: test_tutorial_async; case: spawn -->
```python
response = await client.spawn_instance(
    "wc -l < /work/data.txt",
    "tag:ubuntu:latest",         # an image tag, or a bare image UUID
    shell=True,                  # the command is a shell expression
    env={"LC_ALL": "C"},         # explicit None sends a JSON null
    cwd="/work",
    timeout=60,                  # seconds; server-side hard cap
    disposable=True,             # do not persist a result image
)
operation_id = response.uuid
```
:::

::::

Standard input is a {class}`~contree_client.StreamRepr` (build one
with {meth}`~contree_client.StreamRepr.from_text` /
{meth}`~contree_client.StreamRepr.from_bytes`), files are staged with
{class}`~contree_client.FileSpec` (mode accepts an int or the octal
wire string), and cgroup limits ride in
{class}`~contree_client.InstanceResourcesLimits`:

::::{tab-set}

:::{tab-item} Sync
:sync: sync

<!-- name: test_tutorial; case: spawn_files -->
```python
from contree_client import FileSpec, InstanceResourcesLimits, StreamRepr

client.spawn_instance(
    "/bin/sh",
    "tag:ubuntu:latest",
    args=["-c", "cat - >> /work/data.txt"],
    stdin=StreamRepr.from_text("appended line\n"),
    files={"/work/data.txt": FileSpec(uuid=file_uuid, mode=0o644)},
    resources_limits=InstanceResourcesLimits(max_layer_bytes=1 << 30),
    truncate_output_at=1 << 20,  # cap captured stdout/stderr
    uid=1000,
    gid=1000,
)
```
:::

:::{tab-item} Async
:sync: async

<!-- name: test_tutorial_async; case: spawn_files -->
```python
from contree_client import FileSpec, InstanceResourcesLimits, StreamRepr

await client.spawn_instance(
    "/bin/sh",
    "tag:ubuntu:latest",
    args=["-c", "cat - >> /work/data.txt"],
    stdin=StreamRepr.from_text("appended line\n"),
    files={"/work/data.txt": FileSpec(uuid=file_uuid, mode=0o644)},
    resources_limits=InstanceResourcesLimits(max_layer_bytes=1 << 30),
    truncate_output_at=1 << 20,  # cap captured stdout/stderr
    uid=1000,
    gid=1000,
)
```
:::

::::

## Wait for the result

A spawn returns immediately with an operation id. The push-based
{meth}`~contree_client.base.ContreeSyncClient.wait_operation` follows
the event stream to the ``completion`` frame and fetches the terminal
{class}`~contree_client.OperationResponse` — no polling; *timeout*
bounds the whole wait:

::::{tab-set}

:::{tab-item} Sync
:sync: sync

<!-- name: test_tutorial; case: wait -->
```python
from contree_client import OperationStatus

operation = client.wait_operation(operation_id, timeout=300)
assert operation.status is OperationStatus.SUCCESS

result = operation.metadata.result
print(result.stdout.as_text())        # decoded, whatever the encoding
print("exit code:", result.state.exit_code)
print("result image:", operation.result_image_uuid)
```
:::

:::{tab-item} Async
:sync: async

<!-- name: test_tutorial_async; case: wait -->
```python
from contree_client import OperationStatus

operation = await client.wait_operation(operation_id, timeout=300)
assert operation.status is OperationStatus.SUCCESS

result = operation.metadata.result
print(result.stdout.as_text())        # decoded, whatever the encoding
print("exit code:", result.state.exit_code)
print("result image:", operation.result_image_uuid)
```
:::

::::

`operation.metadata` is discriminated by the operation kind:
{class}`~contree_client.OperationInstanceMetadata` for sandbox runs
(with an {class}`~contree_client.InstanceResult` inside),
{class}`~contree_client.ImageImportMetadata` for imports. Statuses
are the {class}`~contree_client.OperationStatus` enum;
{data}`~contree_client.TERMINAL_STATUSES` /
{data}`~contree_client.ACTIVE_STATUSES` answer membership even for
plain wire strings:

::::{tab-set}

:::{tab-item} Sync
:sync: sync

<!-- name: test_tutorial; case: statuses -->
```python
from contree_client import TERMINAL_STATUSES

assert operation.status in TERMINAL_STATUSES
assert "EXECUTING" not in TERMINAL_STATUSES

status = client.get_operation_status(operation_id)  # a single poll
```
:::

:::{tab-item} Async
:sync: async

<!-- name: test_tutorial_async; case: statuses -->
```python
from contree_client import TERMINAL_STATUSES

assert operation.status in TERMINAL_STATUSES
assert "EXECUTING" not in TERMINAL_STATUSES

status = await client.get_operation_status(operation_id)  # a single poll
```
:::

::::

## Stream the event log

{meth}`~contree_client.base.ContreeSyncClient.iter_operation_events`
yields typed {class}`~contree_client.OperationEvent` frames as the
server flushes them (`follow=True` keeps the connection open while
the operation runs). Payloads are discriminated by `event.type` —
{class}`~contree_client.EventDataStream` for stdout/stderr,
{class}`~contree_client.EventDataExit`,
{class}`~contree_client.EventDataCompletion` and friends;
{func}`~contree_client.decode_chunk` extracts raw bytes from a
stdout/stderr payload no matter the encoding. The higher-level
{meth}`~contree_client.base.ContreeSyncClient.follow_operation_events`
adds transparent reconnection (``Last-Event-Id`` resume) and stops
after the ``completion`` frame:

::::{tab-set}

:::{tab-item} Sync
:sync: sync

<!-- name: test_tutorial; case: stream -->
```python
import sys

from contree_client import decode_chunk

for event in client.follow_operation_events(operation_id):
    if event.type in ("stdout", "stderr"):
        sys.stdout.buffer.write(decode_chunk(event.data))

# the raw one-connection iterator: replay a finished log (no follow)
events = list(client.iter_operation_events(operation_id))
```
:::

:::{tab-item} Async
:sync: async

<!-- name: test_tutorial_async; case: stream -->
```python
import sys

from contree_client import decode_chunk

async for event in client.follow_operation_events(operation_id):
    if event.type in ("stdout", "stderr"):
        sys.stdout.buffer.write(decode_chunk(event.data))

# the raw one-connection iterator: replay a finished log (no follow)
events = [event async for event in client.iter_operation_events(operation_id)]
```
:::

::::

Resuming a broken raw stream by hand (``Last-Event-Id``) is covered
in [Stream operation events](api.md#stream-operation-events-sse).

## Handle events

`event.type` and the payload class discriminate together, so
structural `match` gives a natural typed dispatcher. Process events
carry a Spawned Process ID: `spid == 0` is the sandbox daemon,
`spid == 1` is the main process — its {class}`~contree_client.EventDataExit`
drives your exit code. Service frames report stream truncation
({class}`~contree_client.EventDataTruncated`), filesystem caps
({class}`~contree_client.EventDataSizeCap`) and networking; a payload
the client does not recognize (an unknown event type, a partial body)
degrades to a plain `dict` instead of killing the stream:

::::{tab-set}

:::{tab-item} Sync
:sync: sync

<!-- name: test_tutorial; case: handle_events -->
```python
from contree_client import (
    EventDataCompletion,
    EventDataExit,
    EventDataStream,
    EventDataTruncated,
)

output = bytearray()
exit_code = None

for event in client.follow_operation_events(operation_id):
    match event.data:
        case EventDataStream() as chunk if event.type == "stdout":
            output.extend(chunk.as_bytes())
        case EventDataExit() as fin if event.spid == 1:
            exit_code = fin.code          # the main process finished
        case EventDataTruncated() as cut:
            print(f"{cut.stream}: {cut.bytes_dropped} bytes dropped")
        case EventDataCompletion() as done:
            print("operation:", done.status)
        case dict() as raw:
            print("unrecognized event:", event.type, raw)

assert output.decode() == "hello world\n"
assert exit_code == 0
```
:::

:::{tab-item} Async
:sync: async

<!-- name: test_tutorial_async; case: handle_events -->
```python
from contree_client import (
    EventDataCompletion,
    EventDataExit,
    EventDataStream,
    EventDataTruncated,
)

output = bytearray()
exit_code = None

async for event in client.follow_operation_events(operation_id):
    match event.data:
        case EventDataStream() as chunk if event.type == "stdout":
            output.extend(chunk.as_bytes())
        case EventDataExit() as fin if event.spid == 1:
            exit_code = fin.code          # the main process finished
        case EventDataTruncated() as cut:
            print(f"{cut.stream}: {cut.bytes_dropped} bytes dropped")
        case EventDataCompletion() as done:
            print("operation:", done.status)
        case dict() as raw:
            print("unrecognized event:", event.type, raw)

assert output.decode() == "hello world\n"
assert exit_code == 0
```
:::

::::

## Manage operations

Listings mirror the wire API one page at a time; the `iter_*` twins
paginate transparently (see [Pagination](api.md#pagination)).
{meth}`~contree_client.base.ContreeSyncClient.cancel_operation` stops
an active operation:

::::{tab-set}

:::{tab-item} Sync
:sync: sync

<!-- name: test_tutorial; case: operations -->
```python
for summary in client.iter_operations(kind="instance"):
    print(summary.uuid, summary.status)

# the raw wire call: exactly one request, one page
page = client.list_operations(limit=50, status="EXECUTING")

client.cancel_operation(operation_id)
```
:::

:::{tab-item} Async
:sync: async

<!-- name: test_tutorial_async; case: operations -->
```python
async for summary in client.iter_operations(kind="instance"):
    print(summary.uuid, summary.status)

# the raw wire call: exactly one request, one page
page = await client.list_operations(limit=50, status="EXECUTING")

await client.cancel_operation(operation_id)
```
:::

::::

## Files

Uploads are content-addressed by sha256.
{meth}`~contree_client.base.ContreeSyncClient.ensure_file` is the
deduplicating flavour: it hashes locally, probes the server with
{meth}`~contree_client.base.ContreeSyncClient.get_file` and uploads
only on a miss (pass `sha256=...` when you already know the digest):

::::{tab-set}

:::{tab-item} Sync
:sync: sync

<!-- name: test_tutorial; case: files -->
```python
uploaded = client.upload_file(payload)        # the unconditional upload
stored = client.ensure_file(payload)          # upload only if unknown
assert client.check_file_exists(stored.sha256)
info = client.get_file(stored.sha256)         # uuid, size, timestamps

for item in client.iter_files(since="1w"):
    print(item.uuid, item.size)

page = client.list_files(limit=10)            # one request, one page
```
:::

:::{tab-item} Async
:sync: async

<!-- name: test_tutorial_async; case: files -->
```python
uploaded = await client.upload_file(payload)  # the unconditional upload
stored = await client.ensure_file(payload)    # upload only if unknown
assert await client.check_file_exists(stored.sha256)
info = await client.get_file(stored.sha256)   # uuid, size, timestamps

async for item in client.iter_files(since="1w"):
    print(item.uuid, item.size)

page = await client.list_files(limit=10)      # one request, one page
```
:::

::::

## Images

{meth}`~contree_client.base.ContreeSyncClient.import_image` pulls an
image from a registry ({class}`~contree_client.ImageImportRegistry`,
credentials ride in
{class}`~contree_client.ImageImportRegistryCredentials`) and returns
an operation id — wait for it like any other operation.
{meth}`~contree_client.base.ContreeSyncClient.resolve_image` turns
any reference (UUID, `tag:NAME`, bare tag) into a UUID:

::::{tab-set}

:::{tab-item} Sync
:sync: sync

<!-- name: test_tutorial; case: images -->
```python
from contree_client import ImageImportRegistry

import_id = client.import_image(
    ImageImportRegistry(url="docker://docker.io/library/busybox:latest"),
    tag="busybox:latest",
)
client.wait_operation(import_id, timeout=600)

image_uuid = client.resolve_image("tag:busybox:latest")
# the raw lookup underneath resolve_image (tag name only, no prefix)
image_uuid = client.inspect_find_image_by_tag("busybox:latest")

image = client.update_image_tag(image_uuid, tag="my/base:latest")
client.delete_image_tag(image_uuid, tag="my/base:latest")

for image in client.iter_images(tagged=True):
    print(image.uuid, image.tag)

page = client.list_images(tagged=True, limit=100)  # one request, one page
```
:::

:::{tab-item} Async
:sync: async

<!-- name: test_tutorial_async; case: images -->
```python
from contree_client import ImageImportRegistry

import_id = await client.import_image(
    ImageImportRegistry(url="docker://docker.io/library/busybox:latest"),
    tag="busybox:latest",
)
await client.wait_operation(import_id, timeout=600)

image_uuid = await client.resolve_image("tag:busybox:latest")
# the raw lookup underneath resolve_image (tag name only, no prefix)
image_uuid = await client.inspect_find_image_by_tag("busybox:latest")

image = await client.update_image_tag(image_uuid, tag="my/base:latest")
await client.delete_image_tag(image_uuid, tag="my/base:latest")

async for image in client.iter_images(tagged=True):
    print(image.uuid, image.tag)

page = await client.list_images(tagged=True, limit=100)  # one request, one page
```
:::

::::

The inspect family reads an image without spawning anything:
{meth}`~contree_client.base.ContreeSyncClient.inspect_image` (the
record), {meth}`~contree_client.base.ContreeSyncClient.inspect_image_list`
(a directory as {class}`~contree_client.DirectoryList` of
{class}`~contree_client.FileItem`),
{meth}`~contree_client.base.ContreeSyncClient.check_image_file`,
{meth}`~contree_client.base.ContreeSyncClient.inspect_image_download`
(plus a chunked `_stream` twin) and the stream-only
{meth}`~contree_client.base.ContreeSyncClient.inspect_image_archive`
(a POSIX tar; `compressed=True` yields the body exactly as served):

::::{tab-set}

:::{tab-item} Sync
:sync: sync

<!-- name: test_tutorial; case: inspect -->
```python
record = client.inspect_image(image_uuid)      # uuid, tag, created_at
listing = client.inspect_image_list(image_uuid, "/etc")
if client.check_image_file(image_uuid, "/etc/hosts"):
    content = client.inspect_image_download(image_uuid, "/etc/hosts")

# large files chunk by chunk instead of one buffered body
with open("hosts", "wb") as hosts:
    for chunk in client.inspect_image_download_stream(image_uuid, "/etc/hosts"):
        hosts.write(chunk)

if client.check_image_archive(image_uuid, "/etc"):
    with open("etc.tar", "wb") as archive:
        for chunk in client.inspect_image_archive(image_uuid, "/etc"):
            archive.write(chunk)
```
:::

:::{tab-item} Async
:sync: async

<!-- name: test_tutorial_async; case: inspect -->
```python
record = await client.inspect_image(image_uuid)  # uuid, tag, created_at
listing = await client.inspect_image_list(image_uuid, "/etc")
if await client.check_image_file(image_uuid, "/etc/hosts"):
    content = await client.inspect_image_download(image_uuid, "/etc/hosts")

# large files chunk by chunk instead of one buffered body
with open("hosts", "wb") as hosts:
    async for chunk in client.inspect_image_download_stream(
        image_uuid, "/etc/hosts"
    ):
        hosts.write(chunk)

if await client.check_image_archive(image_uuid, "/etc"):
    with open("etc.tar", "wb") as archive:
        async for chunk in client.inspect_image_archive(image_uuid, "/etc"):
            archive.write(chunk)
```
:::

::::

## Handle errors

Every non-success HTTP status maps to a subclass of
{class}`~contree_client.ContreeAPIError` — the full table lives in
[Error handling](api.md#error-handling); retryable statuses carry
`retry_after`. Transient failures can also be retried automatically
by the transport with a
{class}`~contree_client.RetryPolicy` (see
[Retries](adapters.md#retries)):

::::{tab-set}

:::{tab-item} Sync
:sync: sync

<!--
name: test_tutorial; case: errors
```python
from contree_client.exceptions import NotFoundError as ArmedNotFound

# queue the error behind the canned success and consume the success,
# so the visible call below hits the error
client.mock("inspect_image", error=ArmedNotFound(404, "image not found"))
client.inspect_image(image_uuid)
```
-->
```python
from contree_client import ContreeAPIError, NotFoundError

try:
    client.inspect_image(image_uuid)
except NotFoundError:
    print("no such image")
except ContreeAPIError as error:
    print(error.status, error.error, error.retry_after)
```
:::

:::{tab-item} Async
:sync: async

<!--
name: test_tutorial_async; case: errors
```python
from contree_client.exceptions import NotFoundError as ArmedNotFound

# queue the error behind the canned success and consume the success,
# so the visible call below hits the error
client.mock("inspect_image", error=ArmedNotFound(404, "image not found"))
await client.inspect_image(image_uuid)
```
-->
```python
from contree_client import ContreeAPIError, NotFoundError

try:
    await client.inspect_image(image_uuid)
except NotFoundError:
    print("no such image")
except ContreeAPIError as error:
    print(error.status, error.error, error.retry_after)
```
:::

::::

## Where next

- [Transport adapters](adapters.md) — picking a backend, profiles,
  retries, keepalive, logging, the User-Agent identity.
- [Models and API](api.md) — the generated reference: every model,
  every method, the wire conventions.
- [Testing your code](testing.md) — the in-memory double these very
  examples run against.
