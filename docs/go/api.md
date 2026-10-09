# Go client guide

The `contree` package implements the Contree v1 API with `net/http`. It requires
Go 1.23 or later. CI tests Go 1.23 and Go 1.27.

Examples wrap errors with `fmt.Errorf("context: %w", err)`. Import `fmt` in the
enclosing Go file. Wrapped errors remain available through `errors.Is` and
`errors.As`.

## Install

```shell
go get github.com/nebius/contree-client/go/v1
```

## Construct a client

Pass production credentials explicitly:

```go
client, err := contree.NewClient(
    token,
    contree.WithProject(project),
)
if err != nil {
    return fmt.Errorf("create client: %w", err)
}
```

For local development, resolve the active saved profile and construct the
client:

```go
profile, err := contree.ResolveProfile("")
if err != nil {
    return fmt.Errorf("resolve active profile: %w", err)
}
client, err := contree.NewClientFromProfile(profile)
if err != nil {
    return fmt.Errorf("create client from profile: %w", err)
}
```

Pass an explicit profile name and `auth.ini` path when required:

```go
profile, err := contree.ResolveProfile("staging", "/run/secrets/contree/auth.ini")
if err != nil {
    return fmt.Errorf("resolve staging profile: %w", err)
}
```

`LoadProfiles` reads sibling `cli.ini` first and `auth.ini` second. Values from
`auth.ini` override conflicts. Every `[profile:NAME]` section inherits
`[DEFAULT]` values.

The default configuration directory uses this order:

1. `$CONTREE_HOME`
2. `$XDG_CONFIG_HOME/contree`
3. `~/.config/contree`

Profile selection uses an explicit name, then `$CONTREE_PROFILE`, then the
active `[DEFAULT] profile`. IAM profiles use the generated service URL when
they omit `url`. JWT profiles require `url`.

`ProfileFromEnvironment` reads `CONTREE_TOKEN` or `NEBIUS_API_KEY`,
`CONTREE_URL`, and the optional project aliases. It returns `false` unless the
token and URL are both present.

Client options applied to `NewClientFromProfile` override the saved URL and
project. Other options configure timeouts, retries, the HTTP client, and the
User-Agent identity.

## Run a command

`SpawnInstance` returns the operation ID directly. Common options use ordinary Go values:

```go
operationID, err := client.SpawnInstance(
    ctx,
    "wc -l < /work/data.txt",
    "tag:ubuntu:latest",
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

Use `contree.SpawnInstanceOptions{}` for server defaults. `SpawnInstance` starts the instance
without waiting for execution to finish. Pass its ID to `WaitOperation` or
`FollowOperationEvents` when needed.

Zero scalar values and nil collections or pointers omit their fields. Non-nil
empty maps and slices are sent as empty objects and arrays. `Shell`,
`Disposable`, and `PreserveEnv` default to `false` on the server.

`Timeout` is a `time.Duration` for server execution. Zero uses the server default;
positive values round up to whole seconds. Negative values fail before sending
a request. The request context and client timeout still control the HTTP call.

Some options must distinguish omission from an explicit zero value:

```go
outputLimit := int64(0)
options := contree.SpawnInstanceOptions{
    Networking: &contree.InstanceNetworking{
        Enabled: contree.Some(false), // networking defaults to enabled
    },
    TruncateOutputAt: &outputLimit, // nil uses the default 1 MiB
}
```

`SpawnInstance` returns an error if the response omits the operation ID or contains a
null or empty ID. The instance can already exist in that case. The helper does
not retry that response.

## Optional values

Generated `Optional[T]` values preserve absent, explicit `null`, and present
JSON states. Use the generated `SpawnInstanceWithResponse` method when you need the full
response or explicit values that `SpawnInstance` would omit:

```go
options := &contree.SpawnInstanceWithResponseOptions{
    Shell:   contree.Some(false),
    Env:     contree.Null[map[string]string](),
    Timeout: contree.Some(int64(0)),
}
```

Use `contree.Null[T]()` only when the API distinguishes explicit JSON `null`
from omission.

## Pagination and streams

`IterImages`, `IterFiles`, and `IterOperations` return `iter.Seq2[T, error]`.
Check the error inside the range loop:

```go
for image, err := range client.IterImages(ctx, &contree.IterImagesOptions{PageSize: 100}) {
    if err != nil {
        return fmt.Errorf("iterate images: %w", err)
    }
    id, _ := image.UUID.Value()
    fmt.Println(id)
}
```

Each traversal starts from the first page. Requests start only when iteration
begins. Invalid options and request failures yield a zero item and an error,
then stop. `break` stops fetching more pages.

`IterOperationEvents` and `FollowOperationEvents` return
`iter.Seq2[OperationEvent, error]`. Their response bodies close automatically
on completion, error, `break`, `return`, or panic. Each traversal opens a new
stream. The supplied context bounds its lifetime.

If you use `iter.Pull2` instead of a range loop, defer its `stop` function.

Byte downloads still return `io.ReadCloser`; callers must close those streams.
The client timeout applies only to buffered operations.

`FollowOperationEvents` reconnects from the last event ID. `WaitOperation`
drains that follower and returns the final operation response. Use a context
deadline to bound either helper.

## Files and images

`EnsureFile` accepts any `io.Reader`. It hashes and rewinds seekable readers,
including `*os.File`, then uploads only when the digest is absent remotely.
Non-seekable readers upload directly unless you pass a known SHA-256 digest.

`ResolveImage` accepts an image UUID, `tag:NAME`, or a bare tag. UUID values do
not cause a request.

## Errors

Use `errors.As` with typed transport and status errors:

```go
var notFound *contree.NotFoundError
var statusError *contree.APIStatusError
switch {
case errors.As(err, &notFound):
    // Handle a missing resource.
case errors.As(err, &statusError):
    fmt.Println(statusError.StatusCode, statusError.RequestID)
case err != nil:
    return fmt.Errorf("call Contree API: %w", err)
}
```

## Test your code

`NewTestClient` returns a normal `*Client` and its offline `*MockTransport`.
Code that accepts `*Client` needs no test-specific interface. Configure each
generated operation with `Mock`, `MockError`, `MockStream`, or `MockHTTP`.
`CallsFor` records actual HTTP attempts, including helper calls and retries.

See [Testing Go code](testing.md) for complete examples.

The package API is also available through
[pkg.go.dev](https://pkg.go.dev/github.com/nebius/contree-client/go/v1) after
the first Go module release.
