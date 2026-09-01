# contree-client (JavaScript)

JavaScript/TypeScript client for the [Contree](https://contree.dev/) API,
generated from the same OpenAPI specification as the Python package.
Pure ESM, zero runtime dependencies, TypeScript types included. Works
in Node ≥ 18.17 and in browsers — the only transport is the platform
`fetch`, which pools keepalive connections and decodes gzip on its own.

Naming convention: methods are camelCase (`spawnInstance`,
`waitOperation`); model fields and option-object keys stay snake_case —
exactly the names that travel on the wire (`preserve_env`, `page_size`).

## Quick start

```js
import { ContreeClient } from "contree-client";

const client = new ContreeClient(process.env.CONTREE_TOKEN, {
  baseUrl: "https://contree.example.com",
  project: "my-project",
});

const spawned = await client.spawnInstance("echo hello", "tag:ubuntu:latest", {
  shell: true,
});
const operation = await client.waitOperation(spawned.uuid);
console.log(operation.status, operation.metadata.result.stdout.asText());
```

In Node a saved Contree profile works too:
`await ContreeClient.fromProfile()` reads
`$CONTREE_HOME/{cli,auth}.ini` (shared with the Python client).

## Streaming

Every streaming surface is an async iterator:

```js
// typed Server-Sent Events with transparent reconnection
for await (const event of client.followOperationEvents(spawned.uuid)) {
  console.log(event.id, event.type, event.data);
}

// tar archive of a path inside an image
for await (const chunk of client.inspectImageArchive(imageUuid, "/etc")) {
  sink.write(chunk);
}

// lazy pagination
for await (const image of client.iterImages({ page_size: 100 })) {
  console.log(image.uuid, image.tag);
}
```

## Retries and deduplication

```js
import { ContreeClient, RetryPolicy } from "contree-client";

const client = new ContreeClient(token, { retry: new RetryPolicy() });

// uploads only when the server does not already store the payload
const stored = await client.ensureFile(bytes);
```

Idempotent requests retry after transient network errors, request
timeouts, and retryable responses (410/425/429/5xx). The client honors
`Retry-After`. Pass `new RetryPolicy({ retryUnsafe: true })` to retry
POSTs. Responses 425 and 429 are always safe to retry because they mean
the backend did not process the request.

## Bring your own fetch

The constructor accepts a `fetch` implementation — the single
customization point for proxies, TLS options (an undici dispatcher
wrapper in Node), or request mocking:

```js
import { Agent } from "undici";

const dispatcher = new Agent({ connect: { ca: internalCa } });
const client = new ContreeClient(token, {
  fetch: (url, options) => fetch(url, { ...options, dispatcher }),
});
```

## Browser notes

- The `User-Agent` header is a forbidden header name in browsers: it
  is only attached in Node; `client.userAgent()` reports the composed
  string everywhere.
- `inspectFindImageByTag()` follows the server redirect and derives
  the UUID from the final URL (browsers cannot read a 302 `Location`).
- `ContreeClient.fromProfile()` and streaming request bodies
  (`ReadableStream` uploads) are Node-only.
- A truncated compressed download ends the stream short instead of
  throwing: `fetch` is lenient about a missing gzip trailer (the
  Python client raises `DecompressionError` there).

## Testing

`contree-client/testing` ships an offline double with the exact
generated surface:

```js
import { ContreeClient } from "contree-client/testing";

const client = new ContreeClient();
client.mock("whoami", { permissions: { spawn: true } });
await myCode(client);
assert.equal(client.callsFor("whoami").length, 1);
```

Queued mocks step through outcomes (the last one is sticky); pass
`{ error }` to make an operation throw.

## Development

The package is generated from the OpenAPI spec — `lib/models.js`,
`lib/operations.js`, `lib/client.js`, `lib/specInfo.js` and
`lib/index.js` (plus their `.d.ts`) are build artifacts, never edited
by hand. From the repository root:

```sh
export CONTREE_SPEC=...   # the spec location is never stored in the repo
make generate-js          # regenerate into client-js/lib
make lint-js              # prettier --check + tsc --noEmit
make test-js              # node --test against the shared python stub
```

## Copyright

Nebius B.V. 2026, Licensed under the Apache License, Version 2.0 (see "LICENSE" file).
