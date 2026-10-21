# Client parity

This page compares the public behavior of the Python, JavaScript, and Go
clients. **Complete** means the client preserves the same API semantics.
**Idiomatic** means the behavior is equivalent through language-native APIs.

## Current status

| Capability | Python | JavaScript | Go | Status |
|---|---|---|---|---|
| Generated API | Sync and async methods | Promise-based methods | Context-aware methods | Complete: all 26 wire operations and all schemas |
| Simple instance creation | `spawn_instance` with keyword arguments; response UUID | `spawnInstance` with options; response UUID | `SpawnInstance` with plain options, `time.Duration`, and a direct operation ID | Idiomatic; `SpawnInstanceWithResponse` retains full response and JSON control |
| Optional JSON fields | `...`, `None`, value | `undefined`, `null`, value | `Optional[T]`: unset, null, value | Complete |
| Client construction | Five transport adapters | Injectable `fetch` | Functional options and injectable `http.Client` | Idiomatic |
| Saved profiles | `load_profiles()` and `from_profile()` | Node-only `loadProfiles()` and `fromProfile()` | `LoadProfiles()`, `ResolveProfile()`, and `NewClientFromProfile()` | Complete; Go resolves and constructs in two calls |
| Environment profile | `from_env()` | `fromEnv()` | `ProfileFromEnvironment()` | Complete |
| Typed errors | Connection and HTTP status subclasses | Equivalent error hierarchy | Typed errors, the common `APIError` interface, and cause unwrapping | Idiomatic |
| Offline test client | Method-level result queues and call log | Method-level result queues and call log | `NewTestClient` returns `*Client` with `MockTransport`, typed operation descriptors, outcome queues, and HTTP call log | Idiomatic; Go exercises the real request builders, helpers, and decoders |
| Retry and deadlines | Retry policy and numeric timeouts | Retry policy and numeric timeouts | Finite or unbounded retries, per-attempt timeout, and context deadline | Idiomatic; interrupted buffered reads participate in request retries |
| Gzip and large responses | Decoded and raw streams | Fetch-managed decoded streams | Decoded and raw streams | Transport-specific; Fetch does not expose compressed bytes |
| Operation events | SSE iterator, reconnecting follower, terminal probe, wait | Equivalent async helpers | Native `iter.Seq2[OperationEvent, error]`, reconnecting iterator, terminal probe, wait | Idiomatic; iteration closes the event stream automatically |
| Operation waiting | Automatic events/polling selection and explicit override | Same selection | `WaitOperation` and `WaitOperationOptions.FollowEvents` | Complete: instance events, import polling, unchanged failed/cancelled responses |
| Stream payload decoding | Typed methods and raw-payload helpers | Same helpers | Typed methods, `DecodeChunk`, and `DecodeStream` | Idiomatic |
| Literal values | Literal type annotations | Literal type annotations | Generated constants for model fields and operation arguments | Idiomatic; Go string fields retain future server values |
| Pagination | Lazy image, file, and operation iterators | Async iterators | Native `iter.Seq2[T, error]` for images, files, and operations | Idiomatic; errors are yielded once and `break` stops fetching |
| File and image helpers | Deduplicated upload and image resolution | Equivalent helpers | Reader-aware upload and image resolution | Complete |
| Subprocess lifecycle | Create, inspect, stdin, kill | Same four operations | Same four operations | Complete |
| Tutorial coverage | Executed examples | Subprocess, grep, and download examples executed | All Go blocks compile; subprocess, grep, and download snippets also execute | Compile gate runs in the Go test suite on every CI platform |

## Intentional differences

- Go uses `context.Context` for stream and operation deadlines.
- Go has no client `Close` method because callers own the supplied `http.Client`.
- Go stops event followers on permanent HTTP and malformed-event errors.
- Go iterators yield a zero item with the first error, then stop. Each traversal
  starts again; event streams close automatically when iteration ends.
- Go limits buffered success bodies to 64 MiB and error bodies to 1 MiB. Use
  `InspectImageDownloadStream` for larger downloads.
- Go keeps request specifications and the SSE parser private. Public generated
  methods own request and response resources.
- Go tests configure generated wire operations through `MockTransport`. Helpers
  execute normally, results pass through JSON or stream decoding, and call logs
  record HTTP attempts. Python and JavaScript replace public methods directly
  and record method arguments. Go constructor settings can be asserted through
  recorded request headers and URLs instead of a `constructedWith` snapshot.
- Python alone can save profiles. JavaScript and Go only read profiles because
  the Contree CLI owns credential-file updates.
