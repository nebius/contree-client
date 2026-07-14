# Tutorial

A walk through the whole API from JavaScript. The npm package is
generated from the same OpenAPI specification as the Python client and
speaks the same wire protocol; the one transport is the platform
`fetch` (Node ≥ 18.17, browsers). Working in Python? The same walk
lives in the [Python tutorial](../python/tutorial.md).

Naming translates mechanically from Python: verbs are camelCase,
everything that travels on the wire — model fields and option keys —
keeps its snake_case wire spelling. Required arguments are positional,
every optional knob rides in a trailing options object:

```text
client.spawn_instance(cmd, image, shell=True)      # Python
client.spawnInstance(cmd, image, { shell: true }); // JavaScript
```

The snippets below are exercised by the `node --test` suite shipped in
`client-js/`, not by this page's doc tests.

## Getting a client

Credentials come in two ways, exactly like in Python:

- **Production** — pass the token (and the project for IAM tokens)
  explicitly; a service pulls them from its own configuration or a
  secret manager.
- **Local development (Node)** — *profiles*: the INI files under
  `$CONTREE_HOME` (`~/.config/contree` by default), shared with the
  Python client and the rest of the Contree tooling.
  `fromProfile()` resolves the explicit argument, then the
  `CONTREE_PROFILE` environment variable, then the active profile from
  `[DEFAULT]`.

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

The package ships TypeScript declarations: annotate your own code
against `ContreeClient` and inject the [testing double](testing.md) in
tests — it has the exact same generated surface:

```js
/** @param {import("contree-client").ContreeClient} client */
async function greet(client) {
  const me = await client.whoami();
  console.log(me.permissions, me.limits);
}
```

`whoami()` resolves to a `WhoAmIResponse` describing the token: the
granted `permissions` map and the resource `limits` the server will
enforce.

## Run a command

`spawnInstance()` creates a sandboxed instance from an image and runs
a command in it. Only `command` and `image` are required; every option
you do not pass is omitted from the request so the server defaults
apply. Shell expressions need `shell: true`:

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

Standard input is a `StreamRepr` (build one with
`StreamRepr.fromText()` / `StreamRepr.fromBytes()`), files are staged
with `FileSpec` (mode accepts a number or the octal wire string), and
cgroup limits ride in `InstanceResourcesLimits`:

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
  resources_limits: new InstanceResourcesLimits({
    max_layer_bytes: 1 << 30,
  }),
  truncate_output_at: 1 << 20, // cap captured stdout/stderr
  uid: 1000,
  gid: 1000,
});
```

## Wait for the result

A spawn resolves immediately with an operation id. The push-based
`waitOperation()` follows the event stream to the `completion` frame
and fetches the terminal `OperationResponse` — no polling; `timeout`
bounds the whole wait (an idle stream past the deadline rejects with a
`TimeoutError`):

```js
import { OperationStatus } from "contree-client";

const operation = await client.waitOperation(operationId, {
  timeout: 300,
});
console.assert(operation.status === OperationStatus.SUCCESS);

const result = operation.metadata.result;
console.log(result.stdout.asText()); // decoded, whatever the encoding
console.log("exit code:", result.state.exit_code);
console.log("result image:", operation.result_image_uuid);
```

`operation.metadata` is discriminated by the operation kind:
`OperationInstanceMetadata` for sandbox runs (with an
`InstanceResult` inside), `ImageImportMetadata` for imports. Statuses
are plain wire strings; the `OperationStatus` constants,
`TERMINAL_STATUSES` / `ACTIVE_STATUSES` sets and
`isTerminalStatus()` answer membership:

```js
import { isTerminalStatus, TERMINAL_STATUSES } from "contree-client";

console.assert(isTerminalStatus(operation.status));
console.assert(!TERMINAL_STATUSES.has("EXECUTING"));

const status = await client.getOperationStatus(operationId); // one poll
```

## Stream the event log

`iterOperationEvents()` yields typed `OperationEvent`s as the server
flushes them (`follow: true` keeps the connection open while the
operation runs). The higher-level `followOperationEvents()` adds
transparent reconnection (`Last-Event-Id` resume) and stops after the
`completion` frame; `decodeChunk()` extracts raw bytes from a
stdout/stderr payload no matter the encoding:

```js
import { decodeChunk } from "contree-client";

for await (const event of client.followOperationEvents(operationId)) {
  if (event.type === "stdout") {
    process.stdout.write(decodeChunk(event.data));
  }
}

// the raw one-connection iterator: replay a finished log (no follow)
for await (const event of client.iterOperationEvents(operationId)) {
  console.log(event.id, event.type);
}
```

## Handle events

`event.type` and the payload class discriminate together. Process
events carry a Spawned Process ID: `spid === 0` is the sandbox daemon,
`spid === 1` is the main process — its `EventDataExit` drives your
exit code. A payload the client does not recognize (an unknown event
type, a partial body) degrades to a plain object instead of killing
the stream:

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
    console.warn(`${data.stream}: ${data.bytes_dropped} bytes dropped`);
  } else if (data instanceof EventDataCompletion) {
    console.log("operation:", data.status);
  } else {
    console.log("unrecognized event:", event.type, data); // plain object
  }
}

console.assert(exitCode === 0);
```

## Manage operations

Listings mirror the wire API one page at a time; the `iter*` twins
paginate lazily — break out of the loop and nothing else is fetched:

```js
// the raw wire call: exactly one request, one page
const page = await client.listOperations({ limit: 50, status: "SUCCESS" });

// lazy pagination across pages
for await (const summary of client.iterOperations({
  kind: "instance",
  page_size: 100,
  limit: 500, // stop after 500 records in total
})) {
  console.log(summary.uuid, summary.status);
}

await client.cancelOperation(operationId);
```

## Files

Uploads are content-addressed by sha256. `ensureFile()` hashes
locally, probes the server and only uploads on a miss — pass bytes, a
string or a `Blob` (Node hashes Blobs chunk by chunk; a
`ReadableStream` cannot be re-read, so it skips deduplication and
uploads directly):

```js
import { textToBytes } from "contree-client";

const stored = await client.ensureFile(textToBytes("hello world\n"));
console.log(stored.uuid, stored.sha256);

// the raw pieces underneath
const uploaded = await client.uploadFile(payload);
const exists = await client.checkFileExists(uploaded.sha256); // HEAD
const info = await client.getFile(uploaded.sha256);

for await (const file of client.iterFiles({ page_size: 100 })) {
  console.log(file.sha256, file.size);
}
```

## Images

`resolveImage()` accepts a UUID, a `tag:NAME` reference or a bare tag
and returns the UUID; imports are long-running operations like spawns:

```js
const importId = await client.importImage(
  new ImageImportRegistry({ url: "docker://docker.io/ubuntu:latest" }),
  { tag: "ubuntu:latest" },
);
await client.waitOperation(importId, { timeout: 600 });

const uuid = await client.resolveImage("tag:ubuntu:latest");
const image = await client.inspectImage(uuid);

await client.updateImageTag(uuid, "my/base:latest");
await client.deleteImageTag(uuid, { tag: "my/base:latest" });

for await (const item of client.iterImages({ tagged: true })) {
  console.log(item.uuid, item.tag);
}
```

Inspection reads image contents without starting anything:

```js
const listing = await client.inspectImageList(uuid, "/etc");
const hosts = await client.inspectImageDownload(uuid, "/etc/hosts");

// large files chunk by chunk instead of one buffered body
for await (const chunk of client.inspectImageDownloadStream(
  uuid,
  "/var/log/big.log",
)) {
  sink.write(chunk);
}

// a directory as a POSIX PAX tar stream
for await (const chunk of client.inspectImageArchive(uuid, "/etc")) {
  tarSink.write(chunk);
}

await client.checkImageFile(uuid, "/etc/hosts"); // HEAD -> boolean
```

## Handle errors

HTTP errors map to a typed hierarchy: `ContreeAPIError` carries
`status`, the parsed server `error` and `retryAfter`; subclasses match
the status (`BadRequestError`, `NotFoundError`, `GoneError`,
`TooEarlyError`, `ServerError`, ...). A broken SSE stream surfaces as
`SSEStreamError` with the `lastEventId` to resume from — or is
reconnected transparently by `followOperationEvents()`. Timeouts
reject with a platform `DOMException` named `TimeoutError`:

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

Transient failures retry automatically once you opt in — pass
`retry: new RetryPolicy()` to the constructor. The policy retries
transient network errors and 410/425/5xx responses (honoring
`Retry-After`) with finite backoff, and never replays a
non-idempotent POST unless you set `retryUnsafe: true`.
