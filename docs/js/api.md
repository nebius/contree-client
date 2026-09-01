# Models and API

The `contree-client` npm package is pure ESM with zero runtime
dependencies and full TypeScript declarations. There is no adapter
zoo: the platform `fetch` is the transport, and connection pooling,
TLS and gzip are the platform's job.

```shell
npm install contree-client
```

## Naming convention

Everything that travels on the wire keeps its wire spelling; only the
verbs are JavaScript-flavoured:

| Surface | Style | Example |
|---|---|---|
| methods and functions | camelCase | `spawnInstance`, `waitOperation`, `ensureFile` |
| model fields | snake_case (wire) | `operation.result_image_uuid` |
| option-object keys | snake_case (wire) | `{ preserve_env: true, page_size: 100 }` |

Required arguments are positional, every optional knob rides in a
trailing options object.

## The client

<!-- name: api_client; fixtures: token, customFetch -->
```js
import { ContreeClient, RetryPolicy } from "contree-client";

const client = new ContreeClient(token, {
  baseUrl: "https://contree.example.com",
  project: "my-project",
  timeout: 300, // seconds, like the Python default
  retry: new RetryPolicy(), // opt-in, identical semantics
  identity: "my-app/1.2.3", // leads the User-Agent (Node only)
  fetch: customFetch, // BYO transport: proxies, TLS, mocks
});
```

The token may be `null`, just like the project: the client then sends
no `Authorization` header, which is what the endpoints that need no
authentication expect.

`fetch` is the single customization point — wrap the platform fetch
to add an undici dispatcher (TLS options, proxies) in Node or a mock
in tests:

```js
import { Agent } from "undici";

const dispatcher = new Agent({ connect: { ca: internalCa } });
const client = new ContreeClient(token, {
  fetch: (url, options) => fetch(url, { ...options, dispatcher }),
});
```

`ContreeClient.fromProfile()` (Node only) reads the Contree profile
files (`$CONTREE_HOME/{cli,auth}.ini`, shared with the Python client),
including the active profile from `[DEFAULT]` and `CONTREE_PROFILE`. For config-less environments (CI, containers)
`profiles.fromEnv()` builds a profile from `CONTREE_TOKEN` /
`NEBIUS_API_KEY`, `CONTREE_URL` and `CONTREE_PROJECT` — a non-null
result means the environment fully described one.

## Return shapes

The mapping from the Python client is one-to-one:

- buffered calls resolve to typed models (`whoami()`,
  `getOperationStatus()`, `listImages()`, ...);
- `checkFileExists()` / `checkImageFile()` / `checkImageArchive()`
  resolve to booleans (`HEAD`, 404 means `false`);
- `inspectImageDownload()` resolves to a `Uint8Array`;
  `inspectImageDownloadStream()` and `inspectImageArchive()` are
  `AsyncGenerator<Uint8Array>`;
- `iterOperationEvents()` / `followOperationEvents()` yield typed
  `OperationEvent`s — reconnection with `Last-Event-Id`, deadlines and
  in-band `sse_error` handling match the Python
  `follow_operation_events`;
- `iterImages()` / `iterOperations()` / `iterFiles()` paginate lazily
  with the same `page_size` / `limit` knobs.

Helpers carry over unchanged: `waitOperation(id, { timeout })`,
`resolveImage(ref)`, `ensureFile(content)` (sha256 dedup; Blobs hash
chunk-by-chunk in Node).

## Models

Generated classes expose `fromWire()` / `toWire()` instead of
`from_dict()` / `to_dict()`. The tri-state field semantics are the
Python ones: an unset optional field is `undefined` and omitted on the
wire, an explicit `null` is sent as JSON null. Timestamps become
`Date` where the spec says so.

- `StreamRepr` / `EventDataStream` keep their codec helpers:
  `asBytes()`, `asText()`, `StreamRepr.fromBytes()`,
  `StreamRepr.fromText()`; module-level `decodeChunk()` /
  `decodeStream()` decode payloads that may be typed or raw.
- `OperationResponse.metadata` is discriminated by `kind`:
  `OperationInstanceMetadata` or `ImageImportMetadata`.
- `OperationEvent.data` is discriminated by `event.type`
  (`EventDataStream`, `EventDataExit`, `EventDataCompletion`, ...);
  unknown payloads stay plain objects.
- `OperationStatus` constants, `TERMINAL_STATUSES` /
  `ACTIVE_STATUSES` sets and `isTerminalStatus()` mirror the Python
  enum and frozen sets.

## Errors

The hierarchy is the Python one with camelCase fields:

```text
ContreeError
└── ContreeTransportError
    ├── ContreeProtocolError
    │   └── ContreeStreamError        (compatibility base)
    │       └── SSEStreamError        (lastEventId)
    └── ContreeHTTPError
        └── ContreeAPIError           (status, error, traceback, retryAfter)
            ├── BadRequestError       400
            ├── UnauthorizedError     401
            ├── ForbiddenError        403
            ├── NotFoundError         404
            ├── ConflictError         409
            ├── GoneError             410
            ├── UnprocessableEntityError 422
            ├── TooEarlyError         425
            └── ServerError           5xx
```

Use `ContreeProtocolError` instead of `ContreeStreamError` in new code.

The JavaScript classes use the same top-level transport, protocol, and
HTTP grouping. Python provides additional backend-specific connection,
timeout, TLS, closed-connection, and decompression categories. The Fetch
API does not expose portable network subtypes, so connection failures
reject with `TypeError`. Timeouts reject with a platform `DOMException`
whose `name` is `"TimeoutError"`.

## Platform notes

- Browsers forbid the `User-Agent` header — it is only attached in
  Node; `client.userAgent()` reports the composed value everywhere.
- `fromProfile()` and `ReadableStream` request bodies are Node-only.
- A truncated compressed download ends the stream short instead of
  raising (fetch hides the raw bytes); the Python client raises
  `DecompressionError` there.

The complete generated surface — every method with its options and
every model with its fields — lives on the [API reference](reference.rst)
page, rendered from the same OpenAPI IR as the code; the package's
`.d.ts` files carry the same information for your editor.
