# Tutorial

A walk through the whole API. Every Python example works with **any**
backend: the code only relies on the interface of
{class}`~contree_client.base.ContreeSyncClient` /
{class}`~contree_client.base.ContreeAsyncClient`, never on a concrete
adapter (see [Transport adapters](python/adapters.md)). The JavaScript and Go
packages are generated from the same OpenAPI specification and speak the same
wire protocol. JavaScript uses the platform `fetch` transport (Node ≥ 18.17,
browsers). Go uses `net/http`.

Go snippets use `fmt.Errorf` with `%w` to add context and preserve the original
error for `errors.Is` and `errors.As`. Import `fmt` in the enclosing Go file.

Naming translates mechanically across languages. Python uses snake_case,
JavaScript uses camelCase, and Go uses PascalCase. Every client maps public
names to exact wire names. Optional arguments use kwargs in Python and a trailing
options object in JavaScript. Go's `SpawnInstance` helper accepts an options struct and
returns the operation ID:

```text
client.spawn_instance(cmd, image, shell=True)      # Python
client.spawnInstance(cmd, image, { shell: true }); // JavaScript
client.SpawnInstance(ctx, cmd, image, contree.SpawnInstanceOptions{
    Shell: true,
}) // Go
```

Generated Go methods, including `SpawnInstanceWithResponse`, accept pointers to options
structs and preserve exact JSON field states with `Optional[T]`.

## Getting a client

Python's autodetect modules pick the first installed backend for you
(the concrete backends, extras and constructor knobs — retries,
timeouts, the User-Agent identity — are covered on
[Transport adapters](python/adapters.md)); JavaScript has only one transport.
Go accepts functional options around its `net/http` transport.

All clients accept credentials in two ways:

- **Production** — services usually pass the token and IAM project
  explicitly from their configuration or secret manager.
- **Local development** — *profiles*: the INI files under
  `$CONTREE_HOME` (`~/.config/contree` by default), shared by all
  Contree tooling and every client. `from_profile()`, `fromProfile()`, and
  `ResolveProfile()` select an explicit name, then `CONTREE_PROFILE`, then the
  active profile from the configuration. One machine can hold several
  environments such as `default` and `staging`. This mechanism also works on
  a server when you set `CONTREE_HOME` and provide `auth.ini`.

Python clients are context managers. JavaScript exposes `close()` to release
its transport. Go does not own its `http.Client`, so it has no close method:

::::{tab-set}

:::{tab-item} Python · sync
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

:::{tab-item} Python · async
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

:::{tab-item} JavaScript
:sync: js

```js
import { ContreeClient } from "contree-client";

// production: explicit credentials from your configuration
let client = new ContreeClient(process.env.CONTREE_TOKEN, {
  project: process.env.CONTREE_PROJECT,
});

// local development (Node): a saved Contree profile
client = await ContreeClient.fromProfile();

try {
  // ...
} finally {
  await client.close();
}
```
:::

:::{tab-item} Go
:sync: go

```go
import (
    "fmt"
    "os"

    contree "github.com/nebius/contree-client/go/v1"
)

client, err := contree.NewClient(
    os.Getenv("CONTREE_TOKEN"),
    contree.WithProject(os.Getenv("CONTREE_PROJECT")),
)
if err != nil {
    return fmt.Errorf("create client: %w", err)
}

// local development: a saved Contree profile
profile, err := contree.ResolveProfile("")
if err != nil {
    return fmt.Errorf("resolve active profile: %w", err)
}
client, err = contree.NewClientFromProfile(profile)
if err != nil {
    return fmt.Errorf("create client from profile: %w", err)
}
```
:::

::::

Annotate Python code against the base classes in
{mod}`contree_client.types`. JavaScript uses `ContreeClient`, which has the
same generated surface as its testing double. Go code accepts `*contree.Client`;
`NewTestClient` returns that same type with an offline transport:

::::{tab-set}

:::{tab-item} Python · sync
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

:::{tab-item} Python · async
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

:::{tab-item} JavaScript
:sync: js

<!-- name: tutorial_greet; fixtures: client -->
```js
/** @param {import("contree-client").ContreeClient} client */
async function greet(client) {
  const me = await client.whoami();
  console.log(me.permissions, me.limits);
}

await greet(client); // works with any transport, including the test double
```
:::

:::{tab-item} Go
:sync: go

```go
import (
    "context"
    "fmt"

    contree "github.com/nebius/contree-client/go/v1"
)

func greet(ctx context.Context, client *contree.Client) error {
    me, err := client.WhoAmI(ctx)
    if err != nil {
        return fmt.Errorf("get token permissions: %w", err)
    }
    limits, _ := me.Limits.Value()
    fmt.Println(me.Permissions, limits)
    return nil
}

if err := greet(ctx, client); err != nil {
    return fmt.Errorf("greet user: %w", err)
}
```
:::

::::

`whoami()` / `WhoAmI()` resolves to a `WhoAmIResponse` describing the token: the
granted `permissions` map and the resource `limits` the server will
enforce.

For offline Go tests, use `NewTestClient` and configure responses with `Mock`.
The application still receives `*contree.Client`. See
[Testing Go code](go/testing.md) for response queues, errors, streams, and call
assertions.

## Run a command

`spawn_instance()` / `spawnInstance()` / `SpawnInstance()` creates a sandboxed
instance from an image and runs a command in it. Only `command` and `image` are
required. Omitted options use the server defaults (see
{class}`~contree_client.InstanceSpawnRequest` for every knob and its
default). Shell expressions need `shell=True`, `shell: true`, or
`Shell: true`:

::::{tab-set}

:::{tab-item} Python · sync
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

:::{tab-item} Python · async
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

:::{tab-item} JavaScript
:sync: js

<!-- name: tutorial_spawn; fixtures: client -->
```js
const response = await client.spawnInstance(
  "wc -l < /work/data.txt",
  "tag:ubuntu:latest", // an image tag, or a bare image UUID
  {
    shell: true, // the command is a shell expression
    env: { LC_ALL: "C" }, // explicit null sends a JSON null
    cwd: "/work",
    timeout: 60, // seconds; server-side hard cap
    disposable: true, // do not persist a result image
  },
);
const operationId = response.uuid;
```
:::

:::{tab-item} Go
:sync: go

```go
operationID, err := client.SpawnInstance(
    ctx,
    "wc -l < /work/data.txt",
    "tag:ubuntu:latest", // an image tag, or a bare image UUID
    contree.SpawnInstanceOptions{
        Shell:      true,
        Env:        map[string]string{"LC_ALL": "C"},
        Cwd:        "/work",
        Timeout:    time.Minute,
        Disposable: true,
    },
)
if err != nil {
    return fmt.Errorf("spawn word-count command: %w", err)
}
```
:::

::::

Go's `SpawnInstance` returns the operation ID without waiting for completion. Pass
`contree.SpawnInstanceOptions{}` for server defaults. Zero scalar values and nil
collections or pointers omit their fields; non-nil empty collections are sent
explicitly. `Timeout` uses `time.Duration` and rounds positive values up to whole
seconds. Use `SpawnInstanceWithResponse` for the full response or explicit zero and null
values that `SpawnInstance` would omit.

Standard input is a {class}`~contree_client.StreamRepr` (build one with
`from_text()` / `fromText()` or `from_bytes()` / `fromBytes()`). Go uses a
`ClosableStreamRepr` value and `Some` for each present field. Files
are staged with {class}`~contree_client.FileSpec` (mode accepts an int
/ number or the octal wire string), and cgroup limits ride in
{class}`~contree_client.InstanceResourcesLimits`:

::::{tab-set}

:::{tab-item} Python · sync
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

:::{tab-item} Python · async
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

:::{tab-item} JavaScript
:sync: js

<!-- name: tutorial_spawn_files; fixtures: client, fileUuid -->
```js
import {
  FileSpec,
  InstanceResourcesLimits,
  StreamRepr,
} from "contree-client";

await client.spawnInstance("/bin/sh", "tag:ubuntu:latest", {
  args: ["-c", "cat - >> /work/data.txt"],
  stdin: StreamRepr.fromText("appended line\n"),
  files: { "/work/data.txt": new FileSpec({ uuid: fileUuid, mode: 0o644 }) },
  resourcesLimits: new InstanceResourcesLimits({
    maxLayerBytes: 1 << 30,
  }),
  truncateOutputAt: 1 << 20, // cap captured stdout/stderr
  uid: 1000,
  gid: 1000,
});
```
:::

:::{tab-item} Go
:sync: go

```go
outputLimit := int64(1 << 20)
_, err := client.SpawnInstance(
    ctx,
    "/bin/sh",
    "tag:ubuntu:latest",
    contree.SpawnInstanceOptions{
        Args: []string{"-c", "cat - >> /work/data.txt"},
        Stdin: &contree.ClosableStreamRepr{
            Value:    "appended line\n",
            Encoding: contree.Some("ascii"),
        },
        Files: map[string]contree.FileSpec{
            "/work/data.txt": {
                UUID: contree.Some(fileUUID),
                Mode: contree.Some(contree.StringOrInt64FromInt64(0o644)),
            },
        },
        ResourcesLimits: &contree.InstanceResourcesLimits{
            MaxLayerBytes: contree.Some(int64(1 << 30)),
        },
        TruncateOutputAt: &outputLimit,
        UID:              1000,
        GID:              1000,
    },
)
if err != nil {
    return fmt.Errorf("spawn command with staged files: %w", err)
}
```
:::

::::

## Wait for the result

A spawn returns immediately with an operation id. `wait_operation()` /
`waitOperation()` / `WaitOperation()` follows the event stream and probes
status between reconnects. It then fetches the terminal
{class}`~contree_client.OperationResponse`. *timeout* sets an absolute deadline
for stream reads, reconnects, and status probes. The client checks it whenever
iteration resumes. It does not interrupt caller code between events. A
synchronous native read started before expiry can delay `TimeoutError` until it
returns. An idle JavaScript stream past the deadline rejects with `TimeoutError`.
Go receives the deadline through `context.Context`.

::::{tab-set}

:::{tab-item} Python · sync
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

:::{tab-item} Python · async
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

:::{tab-item} JavaScript
:sync: js

<!-- name: tutorial_wait; fixtures: client, operationId -->
```js
import { OperationStatus } from "contree-client";

const operation = await client.waitOperation(operationId, {
  timeout: 300,
});
console.assert(operation.status === OperationStatus.SUCCESS);

const result = operation.metadata.result;
console.log(result.stdout.asText()); // decoded, whatever the encoding
console.log("exit code:", result.state.exitCode);
console.log("result image:", operation.resultImageUuid);
```
:::

:::{tab-item} Go
:sync: go

```go
waitCtx, cancel := context.WithTimeout(ctx, 5*time.Minute)
defer cancel()

operation, err := client.WaitOperation(waitCtx, operationID)
if err != nil {
    return fmt.Errorf("wait for operation %s: %w", operationID, err)
}
status, ok := operation.Status.Value()
if !ok || status != contree.OperationStatusSuccess {
    return fmt.Errorf("operation did not succeed: %q", status)
}

metadataValue, ok := operation.Metadata.Value()
if !ok {
    return errors.New("operation has no metadata")
}
metadata, ok := metadataValue.(contree.OperationInstanceMetadata)
if !ok {
    return fmt.Errorf("unexpected metadata type %T", metadataValue)
}
result, ok := metadata.Result.Value()
if !ok {
    return errors.New("operation has no instance result")
}
stdout, _ := result.Stdout.Value()
state, _ := result.State.Value()
exitCode, _ := state.ExitCode.Value()
resultImageID, _ := operation.ResultImageUUID.Value()
fmt.Print(stdout.AsText())
fmt.Println("exit code:", exitCode)
fmt.Println("result image:", resultImageID)
```
:::

::::

`operation.metadata` is discriminated by the operation kind:
{class}`~contree_client.OperationInstanceMetadata` for sandbox runs
(with an {class}`~contree_client.InstanceResult` inside),
{class}`~contree_client.ImageImportMetadata` for imports (the same
class names across languages). Go returns this metadata through
`Optional[any]`; use a type assertion before reading the result. Statuses are the
{class}`~contree_client.OperationStatus` enum;
{data}`~contree_client.TERMINAL_STATUSES` /
{data}`~contree_client.ACTIVE_STATUSES` answer membership even for
plain wire strings:

::::{tab-set}

:::{tab-item} Python · sync
:sync: sync

<!-- name: test_tutorial; case: statuses -->
```python
from contree_client import TERMINAL_STATUSES

assert operation.status in TERMINAL_STATUSES
assert "EXECUTING" not in TERMINAL_STATUSES

status = client.get_operation_status(operation_id)  # a single poll
```
:::

:::{tab-item} Python · async
:sync: async

<!-- name: test_tutorial_async; case: statuses -->
```python
from contree_client import TERMINAL_STATUSES

assert operation.status in TERMINAL_STATUSES
assert "EXECUTING" not in TERMINAL_STATUSES

status = await client.get_operation_status(operation_id)  # a single poll
```
:::

:::{tab-item} JavaScript
:sync: js

<!-- name: tutorial_wait; fixtures: client, operationId -->
```js
import { isTerminalStatus, TERMINAL_STATUSES } from "contree-client";

console.assert(isTerminalStatus(operation.status));
console.assert(!TERMINAL_STATUSES.has("EXECUTING"));

const status = await client.getOperationStatus(operationId); // one poll
```
:::

:::{tab-item} Go
:sync: go

```go
status, ok := operation.Status.Value()
if !ok {
    return errors.New("operation response has no status")
}
if !status.IsTerminal() {
    return fmt.Errorf("operation is still %s", status)
}

polled, err := client.GetOperationStatus(ctx, operationID, nil) // one poll
if err != nil {
    return fmt.Errorf("poll operation %s: %w", operationID, err)
}
_ = polled
```
:::

::::

## Run subprocesses

A subprocess runs inside an existing instance operation. Start the operation,
follow its event stream, and wait for the main process's `spawn` event before
creating a subprocess. The main process has `spid=1`; creation returns the new
subprocess's ID.

These examples log every event, including the subprocess's output and `exit`
event. After the subprocess exits, they cancel the parent `sleep` command and
keep reading until the operation's `completion` event. An `exit` event ends one
process; `completion` ends the operation. This example deliberately cancels
the parent, so completion does not mean success. The server timeout bounds the
parent's lifetime if the example stops early.

::::{tab-set}

:::{tab-item} Python · sync
:sync: sync

<!--
name: test_tutorial_subprocesses;
fixtures: client, subprocess_events
```python
client.mocks["iter_operation_events"].clear()
client.mock("iter_operation_events", subprocess_events)
client.mock("operation_subprocess_create", 2)
```
-->
```python
from contextlib import closing

response = client.spawn_instance(
    "sleep 60", "tag:ubuntu:latest", shell=True, timeout=60, disposable=True
)
operation_id = response.uuid
spid = None
subprocess_exited = False
operation_completed = False

with closing(client.follow_operation_events(operation_id)) as events:
    for event in events:
        print("event:", event.to_dict())
        if event.type == "spawn" and event.spid == 1 and spid is None:
            spid = client.operation_subprocess_create(operation_id, "id")
            print("started subprocess:", spid)
        elif event.type == "exit" and spid is not None and event.spid == spid:
            subprocess_exited = True
            print("subprocess exited:", spid)
            client.cancel_operation(operation_id)
        elif event.type == "completion":
            operation_completed = True
            print("operation completed:", operation_id, event.data)
            break

if not subprocess_exited or not operation_completed:
    raise RuntimeError("event stream ended before subprocess exit and operation completion")
```
:::

:::{tab-item} Python · async
:sync: async

<!--
name: async test_tutorial_subprocesses_async;
fixtures: async_client, subprocess_events
```python
client = async_client
client.mocks["iter_operation_events"].clear()
client.mock("iter_operation_events", subprocess_events)
client.mock("operation_subprocess_create", 2)
```
-->
```python
from contextlib import aclosing

response = await client.spawn_instance(
    "sleep 60", "tag:ubuntu:latest", shell=True, timeout=60, disposable=True
)
operation_id = response.uuid
spid = None
subprocess_exited = False
operation_completed = False

async with aclosing(client.follow_operation_events(operation_id)) as events:
    async for event in events:
        print("event:", event.to_dict())
        if event.type == "spawn" and event.spid == 1 and spid is None:
            spid = await client.operation_subprocess_create(operation_id, "id")
            print("started subprocess:", spid)
        elif event.type == "exit" and spid is not None and event.spid == spid:
            subprocess_exited = True
            print("subprocess exited:", spid)
            await client.cancel_operation(operation_id)
        elif event.type == "completion":
            operation_completed = True
            print("operation completed:", operation_id, event.data)
            break

if not subprocess_exited or not operation_completed:
    raise RuntimeError("event stream ended before subprocess exit and operation completion")
```
:::

:::{tab-item} JavaScript
:sync: js

<!-- name: tutorial_subprocesses; fixtures: client -->
```js
const response = await client.spawnInstance("sleep 60", "tag:ubuntu:latest", {
  shell: true,
  timeout: 60,
  disposable: true,
});
const operationId = response.uuid;
let spid;
let subprocessExited = false;
let operationCompleted = false;

for await (const event of client.followOperationEvents(operationId)) {
  console.log("event:", event);
  if (event.type === "spawn" && event.spid === 1 && spid === undefined) {
    spid = await client.operationSubprocessCreate(operationId, "id");
    console.log("started subprocess:", spid);
  } else if (event.type === "exit" && spid !== undefined && event.spid === spid) {
    subprocessExited = true;
    console.log("subprocess exited:", spid);
    await client.cancelOperation(operationId);
  } else if (event.type === "completion") {
    operationCompleted = true;
    console.log("operation completed:", operationId, event.data);
    break;
  }
}
if (!subprocessExited || !operationCompleted) {
  throw new Error("event stream ended before subprocess exit and operation completion");
}
```
:::

:::{tab-item} Go
:sync: go

Import `context`, `encoding/json`, `fmt`, `log`, and `time` in the enclosing file.

<!-- go-example: subprocesses -->
```go
runCtx, cancel := context.WithTimeout(ctx, 2*time.Minute)
defer cancel()
operationID, err := client.SpawnInstance(
    runCtx, "sleep 60", "tag:ubuntu:latest",
    contree.SpawnInstanceOptions{Shell: true, Timeout: time.Minute, Disposable: true},
)
if err != nil {
    return fmt.Errorf("start parent operation: %w", err)
}
var spid int64
subprocessExited, operationCompleted := false, false
for event, err := range client.FollowOperationEvents(runCtx, operationID, nil) {
    if err != nil {
        return fmt.Errorf("read operation %s events: %w", operationID, err)
    }
    payload, err := json.Marshal(event)
    if err != nil {
        return fmt.Errorf("encode operation %s event: %w", operationID, err)
    }
    log.Printf("event: %s", payload)
    eventSPID, _ := event.Spid.Value()
    switch {
    case event.Type == contree.OperationEventTypeSpawn && eventSPID == 1 && spid == 0:
        spid, err = client.OperationSubprocessCreate(runCtx, operationID, "id", nil)
        if err != nil {
            return fmt.Errorf("start subprocess in operation %s: %w", operationID, err)
        }
        log.Printf("started subprocess: %d", spid)
    case event.Type == contree.OperationEventTypeExit && spid != 0 && eventSPID == spid:
        subprocessExited = true
        log.Printf("subprocess exited: %d", spid)
        if err := client.CancelOperation(runCtx, operationID); err != nil {
            return fmt.Errorf("cancel parent operation %s: %w", operationID, err)
        }
    case event.Type == contree.OperationEventTypeCompletion:
        operationCompleted = true
        log.Printf("operation completed: %s", operationID)
    }
}
if !subprocessExited || !operationCompleted {
    return fmt.Errorf("operation %s stream ended before subprocess exit and operation completion", operationID)
}
```
:::

::::

`OperationSubprocess` / `operation_subprocess` / `operationSubprocess` can fetch
the accumulated result later. Use the stdin and kill methods when a subprocess
needs more input or must be stopped; neither is required to receive its events.

## Stream the event log

`iter_operation_events()` / `iterOperationEvents()` / `IterOperationEvents()`
yields typed {class}`~contree_client.OperationEvent` frames as the server
flushes them. Follow mode keeps the connection open while the operation runs.
Payloads are discriminated by `event.type` —
{class}`~contree_client.EventDataStream` for stdout/stderr,
{class}`~contree_client.EventDataExit`,
{class}`~contree_client.EventDataCompletion` and friends;
{func}`~contree_client.decode_chunk` / `decodeChunk()` extracts raw bytes from
a stream payload. The higher-level `follow_operation_events()` /
`followOperationEvents()` / `FollowOperationEvents()` adds transparent
reconnection (`Last-Event-Id` resume) and stops after the `completion` frame.
Go returns `iter.Seq2[OperationEvent, error]`. Use `for event, err := range ...`
and check each error. Ending the loop closes its response body automatically.
Go decodes stream payloads with `AsBytes()`:

::::{tab-set}

:::{tab-item} Python · sync
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

:::{tab-item} Python · async
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

:::{tab-item} JavaScript
:sync: js

<!-- name: tutorial_stream; fixtures: client, operationId -->
```js
import { decodeChunk } from "contree-client";

for await (const event of client.followOperationEvents(operationId)) {
  if (event.type === "stdout" || event.type === "stderr") {
    process.stdout.write(decodeChunk(event.data));
  }
}

// the raw one-connection iterator: replay a finished log (no follow)
for await (const event of client.iterOperationEvents(operationId)) {
  console.log(event.id, event.type);
}
```
:::

:::{tab-item} Go
:sync: go

```go
for event, err := range client.FollowOperationEvents(ctx, operationID, nil) {
    if err != nil {
        return fmt.Errorf("read operation %s events: %w", operationID, err)
    }
    if data, ok := event.Data.(contree.EventDataStream); ok &&
        (event.Type == contree.OperationEventTypeStdout ||
            event.Type == contree.OperationEventTypeStderr) {
        _, _ = os.Stdout.Write(data.AsBytes())
    }
}
// The raw one-connection stream replays a finished log without follow mode.
for event, err := range client.IterOperationEvents(ctx, operationID, nil) {
    if err != nil {
        return fmt.Errorf("read operation %s event: %w", operationID, err)
    }
    fmt.Println(event.ID, event.Type)
}
```
:::

::::

Resuming a broken raw stream by hand (`Last-Event-Id`) is covered in
[Stream operation events](python/api.md#stream-operation-events-sse) for
Python. JavaScript `followOperationEvents()` and Go `FollowOperationEvents()`
handle it transparently.

## Handle events

`event.type` and the payload class discriminate together. Python uses
structural `match`, JavaScript checks `instanceof`, and Go uses a type switch.
Process events carry a Spawned Process ID. Zero identifies the sandbox daemon,
and one identifies the main process. Its
{class}`~contree_client.EventDataExit` drives your exit code. Service frames
report stream truncation
({class}`~contree_client.EventDataTruncated`), filesystem caps
({class}`~contree_client.EventDataSizeCap`) and networking; a payload
the client does not recognize (an unknown event type, a partial body)
degrades to a plain `dict`, object, or `any` value instead of killing the
stream:

::::{tab-set}

:::{tab-item} Python · sync
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

:::{tab-item} Python · async
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

:::{tab-item} JavaScript
:sync: js

<!-- name: tutorial_handle_events; fixtures: client, operationId -->
```js
import {
  EventDataCompletion,
  EventDataExit,
  EventDataStream,
  EventDataTruncated,
} from "contree-client";

const output = [];
let exitCode = null;

for await (const event of client.followOperationEvents(operationId)) {
  const data = event.data;
  if (data instanceof EventDataStream && event.type === "stdout") {
    output.push(data.asBytes());
  } else if (data instanceof EventDataExit && event.spid === 1) {
    exitCode = data.code; // the main process finished
  } else if (data instanceof EventDataTruncated) {
    console.warn(`${data.stream}: ${data.bytesDropped} bytes dropped`);
  } else if (data instanceof EventDataCompletion) {
    console.log("operation:", data.status);
  } else {
    console.log("unrecognized event:", event.type, data); // plain object
  }
}

console.assert(exitCode === 0);
```
:::

:::{tab-item} Go
:sync: go

```go
var output bytes.Buffer
var exitCode int64 = -1

for event, err := range client.FollowOperationEvents(ctx, operationID, nil) {
    if err != nil {
        return fmt.Errorf("collect operation %s output: %w", operationID, err)
    }
    spid, hasSPID := event.Spid.Value()
    switch data := event.Data.(type) {
    case contree.EventDataStream:
        if event.Type == contree.OperationEventTypeStdout {
            output.Write(data.AsBytes())
        }
    case contree.EventDataExit:
        if hasSPID && spid == 1 {
            exitCode = data.Code // the main process finished
        }
    case contree.EventDataTruncated:
        fmt.Printf("%s: %d bytes dropped\n", data.Stream, data.BytesDropped)
    case contree.EventDataCompletion:
        fmt.Println("operation:", data.Status)
    default:
        fmt.Printf("unrecognized event: %s %#v\n", event.Type, data)
    }
}
if output.String() != "hello world\n" || exitCode != 0 {
    return errors.New("unexpected operation output")
}
```
:::

::::

## Manage operations

Listings mirror the wire API one page at a time; the `iter_*` / `iter*` /
`Iter*` variants paginate transparently (see
[Pagination](python/api.md#pagination)). `cancel_operation()` /
`cancelOperation()` / `CancelOperation()` stops an active operation:

::::{tab-set}

:::{tab-item} Python · sync
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

:::{tab-item} Python · async
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

:::{tab-item} JavaScript
:sync: js

<!-- name: tutorial_operations; fixtures: client, operationId -->
```js
// the raw wire call: exactly one request, one page
const page = await client.listOperations({ limit: 50, status: "SUCCESS" });

// lazy pagination across pages
for await (const summary of client.iterOperations({
  kind: "instance",
  pageSize: 100,
  limit: 500, // stop after 500 records in total
})) {
  console.log(summary.uuid, summary.status);
}

await client.cancelOperation(operationId);
```
:::

:::{tab-item} Go
:sync: go

```go
kind := "instance"
totalLimit := int64(500)
for summary, err := range client.IterOperations(ctx, &contree.IterOperationsOptions{
    Kind:     &kind,
    PageSize: 100,
    Limit:    &totalLimit,
}) {
    if err != nil {
        return fmt.Errorf("iterate operations: %w", err)
    }
    id, _ := summary.UUID.Value()
    status, _ := summary.Status.Value()
    fmt.Println(id, status)
}

// The raw wire call makes exactly one request.
pageLimit := int64(50)
executing := contree.OperationStatusExecuting
page, err := client.ListOperations(ctx, &contree.ListOperationsOptions{
    Limit:  &pageLimit,
    Status: &executing,
})
if err != nil {
    return fmt.Errorf("list executing operations: %w", err)
}
_ = page

if err := client.CancelOperation(ctx, operationID); err != nil {
    return fmt.Errorf("cancel operation %s: %w", operationID, err)
}
```
:::

::::

## Files

Uploads are content-addressed by sha256. `ensure_file()` / `ensureFile()` /
`EnsureFile()` hashes seekable content locally, probes with `get_file()` /
`getFile()` / `GetFile()`, and uploads only on a miss. Pass a known digest to
skip hashing. Without a digest, non-seekable readers upload directly:

::::{tab-set}

:::{tab-item} Python · sync
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

:::{tab-item} Python · async
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

:::{tab-item} JavaScript
:sync: js

<!-- name: tutorial_files; fixtures: client, payload -->
```js
import { textToBytes } from "contree-client";

const stored = await client.ensureFile(textToBytes("hello world\n"));
console.log(stored.uuid, stored.sha256);

// the raw pieces underneath
const uploaded = await client.uploadFile(payload);
const exists = await client.checkFileExists(uploaded.sha256); // HEAD
const info = await client.getFile(uploaded.sha256);

for await (const file of client.iterFiles({ pageSize: 100 })) {
  console.log(file.sha256, file.size);
}

const page = await client.listFiles({ limit: 10 }); // one request, one page
```
:::

:::{tab-item} Go
:sync: go

```go
file, err := os.Open("artifact.bin")
if err != nil {
    return fmt.Errorf("open artifact.bin: %w", err)
}
defer file.Close()

stored, err := client.EnsureFile(ctx, file, nil)
if err != nil {
    return fmt.Errorf("ensure artifact.bin is uploaded: %w", err)
}
fmt.Println(stored.UUID, stored.SHA256)

// The raw pieces underneath EnsureFile.
payload := []byte("hello world\n")
uploaded, err := client.UploadFile(ctx, bytes.NewReader(payload))
if err != nil {
    return fmt.Errorf("upload payload: %w", err)
}
exists, err := client.CheckFileExists(ctx, uploaded.SHA256)
if err != nil {
    return fmt.Errorf("check uploaded file: %w", err)
}
info, err := client.GetFile(ctx, uploaded.SHA256)
if err != nil {
    return fmt.Errorf("get uploaded file: %w", err)
}
fmt.Println(exists, info.Size)

for item, err := range client.IterFiles(ctx, &contree.IterFilesOptions{PageSize: 100}) {
    if err != nil {
        return fmt.Errorf("iterate files: %w", err)
    }
    fmt.Println(item.SHA256, item.Size)
}

limit := int64(10)
page, err := client.ListFiles(ctx, &contree.ListFilesOptions{Limit: &limit})
if err != nil {
    return fmt.Errorf("list files: %w", err)
}
_ = page
```
:::

::::

## Images

`import_image()` / `importImage()` / `ImportImage()` pulls an image from a registry
({class}`~contree_client.ImageImportRegistry`, credentials ride in
{class}`~contree_client.ImageImportRegistryCredentials`) and returns an
operation id — wait for it like any other operation. `resolve_image()` /
`resolveImage()` / `ResolveImage()` turns any reference (UUID, `tag:NAME`, bare tag)
into a UUID:

::::{tab-set}

:::{tab-item} Python · sync
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

:::{tab-item} Python · async
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

:::{tab-item} JavaScript
:sync: js

<!-- name: tutorial_images; fixtures: client -->
```js
import { ImageImportRegistry } from "contree-client";

const importId = await client.importImage(
  new ImageImportRegistry({ url: "docker://docker.io/ubuntu:latest" }),
  { tag: "ubuntu:latest" },
);
await client.waitOperation(importId, { timeout: 600 });

const uuid = await client.resolveImage("tag:ubuntu:latest");
// the raw lookup underneath resolveImage (tag name only, no prefix)
const rawUuid = await client.inspectFindImageByTag("ubuntu:latest");
const image = await client.inspectImage(uuid);

await client.updateImageTag(uuid, "my/base:latest");
await client.deleteImageTag(uuid, { tag: "my/base:latest" });

for await (const item of client.iterImages({ tagged: true })) {
  console.log(item.uuid, item.tag);
}

const page = await client.listImages({ tagged: true, limit: 100 });
```
:::

:::{tab-item} Go
:sync: go

```go
importID, err := client.ImportImage(
    ctx,
    contree.ImageImportRegistry{
        URL: "docker://docker.io/library/busybox:latest",
    },
    &contree.ImportImageOptions{Tag: contree.Some("busybox:latest")},
)
if err != nil {
    return fmt.Errorf("import busybox image: %w", err)
}
waitCtx, cancel := context.WithTimeout(ctx, 10*time.Minute)
defer cancel()
if _, err := client.WaitOperation(waitCtx, importID); err != nil {
    return fmt.Errorf("wait for image import %s: %w", importID, err)
}

imageUUID, err := client.ResolveImage(ctx, "tag:busybox:latest")
if err != nil {
    return fmt.Errorf("resolve busybox image: %w", err)
}
// The raw lookup underneath ResolveImage accepts a tag without its prefix.
imageUUID, err = client.InspectFindImageByTag(ctx, "busybox:latest")
if err != nil {
    return fmt.Errorf("find busybox image by tag: %w", err)
}

image, err := client.UpdateImageTag(ctx, imageUUID, "my/base:latest")
if err != nil {
    return fmt.Errorf("update image tag: %w", err)
}
tag := "my/base:latest"
if err := client.DeleteImageTag(
    ctx,
    imageUUID,
    &contree.DeleteImageTagOptions{Tag: &tag},
); err != nil {
    return fmt.Errorf("delete image tag: %w", err)
}
_ = image

for item, err := range client.IterImages(ctx, &contree.IterImagesOptions{Tagged: true}) {
    if err != nil {
        return fmt.Errorf("iterate images: %w", err)
    }
    id, _ := item.UUID.Value()
    tag, _ := item.Tag.Value()
    fmt.Println(id, tag)
}

limit := int64(100)
page, err := client.ListImages(ctx, &contree.ListImagesOptions{
    Tagged: true,
    Limit:  &limit,
})
if err != nil {
    return fmt.Errorf("list tagged images: %w", err)
}
_ = page
```
:::

::::

## Inspect images without starting a sandbox

The inspect family reads an image without spawning anything:
`inspect_image()` / `inspectImage()` / `InspectImage()` (the record),
`inspect_image_list()` / `inspectImageList()` / `InspectImageList()` (a directory as
{class}`~contree_client.DirectoryList` of
{class}`~contree_client.FileItem`), `check_image_file()` /
`checkImageFile()` / `CheckImageFile()`, `inspect_image_download()` /
`inspectImageDownload()` / `InspectImageDownload()` (plus a streaming twin),
and the stream-only `inspect_image_archive()` / `inspectImageArchive()` /
`InspectImageArchive()` (a POSIX tar). In Go, `InspectImageArchiveRaw()`
preserves transport gzip exactly as served.

### Search file contents with grep

`inspect_image_grep()` / `inspectImageGrep()` / `InspectImageGrep()` searches
inside the image with ripgrep. Patterns use Rust regex syntax; multiple
patterns are OR-ed. Paths can name files or directories. Repeated paths share
the same total-match limit. Every requested path must exist.

This example searches the hosts and passwd files. It includes one context line
on each side and limits the result to 20 matches:

::::{tab-set}

:::{tab-item} Python · sync
:sync: sync

<!-- name: test_tutorial; case: grep -->
```python
matches = client.inspect_image_grep(
    image_uuid,
    ["^root:", "localhost"],
    path=["/etc/hosts", "/etc/passwd"],
    glob=["!*.bak"],
    case="sensitive",
    before=1,
    after=1,
    max_total=20,
)
for line in matches.matches:
    print(f"{line.path}:{line.line_number} [{line.type}] {line.line_text}", end="")
if matches.truncated:
    print("Search stopped early; narrow the paths or patterns.")
```
:::

:::{tab-item} Python · async
:sync: async

<!-- name: test_tutorial_async; case: grep -->
```python
matches = await client.inspect_image_grep(
    image_uuid,
    ["^root:", "localhost"],
    path=["/etc/hosts", "/etc/passwd"],
    glob=["!*.bak"],
    case="sensitive",
    before=1,
    after=1,
    max_total=20,
)
for line in matches.matches:
    print(f"{line.path}:{line.line_number} [{line.type}] {line.line_text}", end="")
if matches.truncated:
    print("Search stopped early; narrow the paths or patterns.")
```
:::

:::{tab-item} JavaScript
:sync: js

<!-- name: tutorial_grep; fixtures: client, uuid -->
```js
const matches = await client.inspectImageGrep(uuid, ["^root:", "localhost"], {
  path: ["/etc/hosts", "/etc/passwd"],
  glob: ["!*.bak"],
  case: "sensitive",
  before: 1,
  after: 1,
  maxTotal: 20,
});
for (const line of matches.matches) {
  console.log(`${line.path}:${line.lineNumber} [${line.type}] ${line.lineText.trimEnd()}`);
}
if (matches.truncated) {
  console.log("Search stopped early; narrow the paths or patterns.");
}
```
:::

:::{tab-item} Go
:sync: go

<!-- go-example: grep -->
```go
paths := []string{"/etc/hosts", "/etc/passwd"}
globs := []string{"!*.bak"}
caseMode := "sensitive"
contextLines, maxTotal := int64(1), int64(20)
matches, err := client.InspectImageGrep(ctx, imageUUID,
    []string{"^root:", "localhost"},
    &contree.InspectImageGrepOptions{
        Path: &paths, Glob: &globs, Case: &caseMode,
        Before: &contextLines, After: &contextLines, MaxTotal: &maxTotal,
    },
)
if err != nil {
    return fmt.Errorf("search image %s: %w", imageUUID, err)
}
for _, line := range matches.Matches {
    fmt.Printf("%s:%d [%s] %s", line.Path, line.LineNumber, line.Type, line.LineText)
}
if matches.Truncated {
    fmt.Println("Search stopped early; narrow the paths or patterns.")
}
```
:::

::::

Rows have type `match` or `context`. An empty result is a successful search with
no matches. `truncated` means the total-match limit or search deadline stopped
the search early. Hidden files are searched, binary files are skipped, and
symlinks are not followed. `glob` accepts exclusions such as `!*.bak`.

### Download individual files and tar archives

Use the buffered download method for small files. Use its streaming variant for
large files; Go limits buffered responses to 64 MiB. Archive downloads are always
streamed and contain POSIX tar data. Save decoded archive bytes as `.tar`.

The following examples inspect the image, download `/etc/hosts`, and save `/etc`
as `etc.tar`. Optional HEAD checks report whether the requested file or archive
exists. They do not replace error handling on the later download.

::::{tab-set}

:::{tab-item} Python · sync
:sync: sync

<!-- name: test_tutorial; case: inspect -->
```python
from contextlib import closing

record = client.inspect_image(image_uuid)      # uuid, tag, created_at
listing = client.inspect_image_list(image_uuid, "/etc")
if client.check_image_file(image_uuid, "/etc/hosts"):
    content = client.inspect_image_download(image_uuid, "/etc/hosts")

# large files chunk by chunk instead of one buffered body
with closing(client.inspect_image_download_stream(image_uuid, "/etc/hosts")) as stream:
    with open("hosts", "wb") as hosts:
        for chunk in stream:
            hosts.write(chunk)

if client.check_image_archive(image_uuid, "/etc"):
    with closing(client.inspect_image_archive(image_uuid, "/etc")) as stream:
        with open("etc.tar", "wb") as archive:
            for chunk in stream:
                archive.write(chunk)
```
:::

:::{tab-item} Python · async
:sync: async

<!-- name: test_tutorial_async; case: inspect -->
```python
from contextlib import aclosing

record = await client.inspect_image(image_uuid)  # uuid, tag, created_at
listing = await client.inspect_image_list(image_uuid, "/etc")
if await client.check_image_file(image_uuid, "/etc/hosts"):
    content = await client.inspect_image_download(image_uuid, "/etc/hosts")

# large files chunk by chunk instead of one buffered body
async with aclosing(client.inspect_image_download_stream(image_uuid, "/etc/hosts")) as stream:
    with open("hosts", "wb") as hosts:
        async for chunk in stream:
            hosts.write(chunk)

if await client.check_image_archive(image_uuid, "/etc"):
    async with aclosing(client.inspect_image_archive(image_uuid, "/etc")) as stream:
        with open("etc.tar", "wb") as archive:
            async for chunk in stream:
                archive.write(chunk)
```
:::

:::{tab-item} JavaScript
:sync: js

<!-- name: tutorial_downloads; fixtures: client, uuid -->
```js
import { createWriteStream } from "node:fs";
import { pipeline } from "node:stream/promises";

const record = await client.inspectImage(uuid);
const listing = await client.inspectImageList(uuid, "/etc");
const hosts = await client.inspectImageDownload(uuid, "/etc/hosts");
console.log(record.uuid, listing.path, new TextDecoder().decode(hosts));

// Node.js: pipeline handles backpressure, errors, and stream closure.
await pipeline(
  client.inspectImageDownloadStream(uuid, "/etc/hosts"),
  createWriteStream("hosts"),
);

if (await client.checkImageArchive(uuid, "/etc")) {
  await pipeline(
    client.inspectImageArchive(uuid, "/etc"),
    createWriteStream("etc.tar"),
  );
}

await client.checkImageFile(uuid, "/etc/hosts"); // HEAD -> boolean
```
:::

:::{tab-item} Go
:sync: go

<!-- go-example: downloads -->
```go
record, err := client.InspectImage(ctx, imageUUID)
if err != nil {
    return fmt.Errorf("inspect image %s: %w", imageUUID, err)
}
listing, err := client.InspectImageList(ctx, imageUUID, "/etc")
if err != nil {
    return fmt.Errorf("list image directory /etc: %w", err)
}
fmt.Println(record, listing.Path)

exists, err := client.CheckImageFile(ctx, imageUUID, "/etc/hosts")
if err != nil {
    return fmt.Errorf("check image file /etc/hosts: %w", err)
}
if exists {
    content, err := client.InspectImageDownload(ctx, imageUUID, "/etc/hosts")
    if err != nil {
        return fmt.Errorf("download image file /etc/hosts: %w", err)
    }
    fmt.Println(string(content))
}

download, err := client.InspectImageDownloadStream(ctx, imageUUID, "/etc/hosts")
if err != nil {
    return fmt.Errorf("open image file /etc/hosts stream: %w", err)
}
defer download.Close()
hosts, err := os.Create("hosts")
if err != nil {
    return fmt.Errorf("create hosts file: %w", err)
}
defer hosts.Close()
if _, err := io.Copy(hosts, download); err != nil {
    return fmt.Errorf("save hosts file: %w", err)
}
if err := hosts.Close(); err != nil {
    return fmt.Errorf("close hosts file: %w", err)
}
if err := download.Close(); err != nil {
    return fmt.Errorf("close hosts download: %w", err)
}

archiveExists, err := client.CheckImageArchive(ctx, imageUUID, "/etc")
if err != nil {
    return fmt.Errorf("check image archive /etc: %w", err)
}
if archiveExists {
    archive, err := client.InspectImageArchive(ctx, imageUUID, "/etc")
    if err != nil {
        return fmt.Errorf("open image archive /etc: %w", err)
    }
    defer archive.Close()
    archiveFile, err := os.Create("etc.tar")
    if err != nil {
        return fmt.Errorf("create etc.tar: %w", err)
    }
    defer archiveFile.Close()
    if _, err := io.Copy(archiveFile, archive); err != nil {
        return fmt.Errorf("save etc.tar: %w", err)
    }
    if err := archiveFile.Close(); err != nil {
        return fmt.Errorf("close etc.tar: %w", err)
    }
    if err := archive.Close(); err != nil {
        return fmt.Errorf("close image archive: %w", err)
    }
}
```
:::

::::

Inspect the saved archive without extracting it:

```shell
tar -tf etc.tar
```

These examples use decoded streams. `InspectImageArchiveRaw` in Go and
`compressed=True` in Python preserve bytes as served; those bytes are gzip only
when the response has `Content-Encoding: gzip`. JavaScript Fetch decodes that
transport compression automatically. A failed download can leave a partial
local file; check the error before using it.

## Handle errors

Every buffered request status of 400 or greater maps to a typed form of
{class}`~contree_client.APIStatusError` — the full table lives in
[Error handling](python/api.md#error-handling) (Python) and
[Errors](js/api.md#errors) (JavaScript); retryable statuses carry
`retry_after` / `retryAfter` / `RetryAfter`. Transient failures can also be retried
automatically by the transport with a
{class}`~contree_client.RetryPolicy` (see
[Retries](python/adapters.md#retries)):

::::{tab-set}

:::{tab-item} Python · sync
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
from contree_client import APIStatusError, NotFoundError

try:
    client.inspect_image(image_uuid)
except NotFoundError:
    print("no such image")
except APIStatusError as error:
    print(error.status, error.error, error.retry_after)
```
:::

:::{tab-item} Python · async
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
from contree_client import APIStatusError, NotFoundError

try:
    await client.inspect_image(image_uuid)
except NotFoundError:
    print("no such image")
except APIStatusError as error:
    print(error.status, error.error, error.retry_after)
```
:::

:::{tab-item} JavaScript
:sync: js

<!-- name: tutorial_errors; fixtures: notFoundClient as client -->
```js
import { NotFoundError, RetryPolicy } from "contree-client";

try {
  await client.inspectImage("00000000-0000-0000-0000-000000000000");
} catch (error) {
  if (error instanceof NotFoundError) {
    console.log("no such image:", error.error);
  } else {
    throw error;
  }
}
```
:::

:::{tab-item} Go
:sync: go

```go
_, err := client.InspectImage(
    ctx,
    "00000000-0000-0000-0000-000000000000",
)
var notFound *contree.NotFoundError
var statusError *contree.APIStatusError
switch {
case errors.As(err, &notFound):
    fmt.Println("no such image:", notFound)
case errors.As(err, &statusError):
    fmt.Println(statusError.StatusCode, statusError.Details, statusError.RetryAfter)
case err != nil:
    return fmt.Errorf("inspect image: %w", err)
}
```
:::

::::

JavaScript retries opt in with `retry: new RetryPolicy()`. Go uses
`contree.WithRetry(contree.DefaultRetryPolicy())`. The policy retries
connection errors and 410/425/429/5xx responses (honoring
`Retry-After`) with finite backoff. Replayable non-idempotent requests require
`retryUnsafe: true`, `retry_unsafe=True`, or `RetryUnsafe: true` after
transport failures and most retryable statuses. HTTP 425 and 429 can retry
without that option because the service rejected the request before processing.

## Where next

- **Python** — [Transport adapters](python/adapters.md) (picking a
  backend, profiles, retries, keepalive, logging, the User-Agent
  identity), [Models and API](python/api.md) (the generated reference:
  every model, every method, the wire conventions),
  [Testing your code](python/testing.md) (the in-memory double these
  very examples run against).
- **JavaScript** — [Models and API](js/api.md) (the generated
  reference, platform notes), [API reference](js/reference.rst) (every
  method and model), [Testing your code](js/testing.md) (the in-memory
  double these very examples run against).
- **Go** — [Go client guide](go/api.md). Pass a context to every operation,
  inspect `Optional[T]` with `Value()`, and close streams when iteration stops
  before EOF.
