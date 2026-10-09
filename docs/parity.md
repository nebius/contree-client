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
| Typed errors | Connection and HTTP status subclasses | Equivalent error hierarchy | Typed errors with `errors.As` and unwrapping | Complete |
| Offline test client | Method-level result queues and call log | Method-level result queues and call log | `NewTestClient` returns `*Client` with `MockTransport`, outcome queues, and HTTP call log | Idiomatic; Go exercises the real request builders, helpers, and decoders |
| Retry and deadlines | Retry policy and numeric timeouts | Retry policy and numeric timeouts | Retry policy, client timeout, and contexts | Idiomatic |
| Gzip and large responses | Decoded and raw streams | Fetch-managed decoded streams | Decoded and raw streams | Transport-specific; Fetch does not expose compressed bytes |
| Operation events | SSE iterator, reconnecting follower, terminal probe, wait | Equivalent async helpers | Native `iter.Seq2[OperationEvent, error]`, reconnecting iterator, terminal probe, wait | Idiomatic; iteration closes the event stream automatically |
| Pagination | Lazy image, file, and operation iterators | Async iterators | Native `iter.Seq2[T, error]` for images, files, and operations | Idiomatic; errors are yielded once and `break` stops fetching |
| File and image helpers | Deduplicated upload and image resolution | Equivalent helpers | Reader-aware upload and image resolution | Complete |
| Subprocess lifecycle | Create, inspect, stdin, kill | Same four operations | Same four operations | Complete |
| Tutorial coverage | Executed examples | Subprocess, grep, and download examples executed | Examples in every language tab set; subprocess, grep, and download snippets compiled and executed | Full Go tutorial compile gate remains pending |

## Remaining work

Runtime parity still has open audit findings in retries, cancellation, and mock
error handling. These API and documentation improvements also remain:

| Priority | Item | Impact | Current workaround |
|---|---|---|---|
| Before stable v1 | Generate named Go types or constants for inline string literals | Compile-time validation for encoding, stream, reason, kind, and case fields | Use the documented string values |
| Before stable v1 | Compile all Go tutorial snippets in CI | Detect documentation drift after generated API changes | Subprocess, grep, and download snippets run from Markdown; other examples rely on package tests |
| Optional | Add `DecodeChunk` and `DecodeStream` for unknown event payloads | Matches the Python and JavaScript fallback helpers | Use typed stream data methods or decode `json.RawMessage` |
| Optional | Add a common SDK error marker | Lets callers identify every normalized client error with one `errors.As` check | Check `APIConnectionError` and `APIStatusError` separately |
| On demand | Support unbounded retries | Matches the nullable Python and JavaScript attempt limit | Set a large finite `MaxAttempts`; contexts still bound execution |

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
