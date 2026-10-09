# Testing Go code

`NewTestClient` returns a normal `*contree.Client` and its offline
`*contree.MockTransport`. Application functions can accept `*contree.Client`
directly. The transport never opens a network connection.

## Mock a response

Application code adds context when it returns an error:

```go
func permissions(ctx context.Context, client *contree.Client) (map[string]bool, error) {
    me, err := client.WhoAmI(ctx)
    if err != nil {
        return nil, fmt.Errorf("get token permissions: %w", err)
    }
    return me.Permissions, nil
}
```

Use the same concrete client type in a test:

```go
func TestPermissions(t *testing.T) {
    client, mock, err := contree.NewTestClient()
    if err != nil {
        t.Fatalf("create test client: %v", err)
    }
    err = mock.Mock("WhoAmI", contree.WhoAmIResponse{
        Permissions:    map[string]bool{"spawn": true},
        OperationsStat: map[string]int64{},
    })
    if err != nil {
        t.Fatalf("mock token permissions: %v", err)
    }

    allowed, err := permissions(context.Background(), client)
    if err != nil {
        t.Fatalf("load permissions: %v", err)
    }
    if !allowed["spawn"] {
        t.Fatal("expected spawn permission")
    }
    if calls := mock.CallsFor("WhoAmI"); len(calls) != 1 {
        t.Fatalf("WhoAmI calls = %d, want 1", len(calls))
    }
}
```

Import `context`, `fmt`, `testing`, and the `contree` package in the test file.
`%w` preserves the underlying error for `errors.Is` and `errors.As`.

`Mock` serializes model values when configured. The real client then decodes
the response, including required-field checks. Include required maps and slices
even when empty. Use `MockHTTP` to supply malformed responses deliberately.

## Queues and errors

Each operation has an independent response queue. Repeated `Mock` calls append
responses; the last response repeats indefinitely. This supports polling:

```go
for _, status := range []contree.OperationStatus{
    contree.OperationStatusExecuting,
    contree.OperationStatusSuccess,
} {
    err := mock.Mock("GetOperationStatus", contree.OperationResponse{
        UUID:   contree.Some(operationID),
        Status: contree.Some(status),
    })
    if err != nil {
        return fmt.Errorf("mock operation status %s: %w", status, err)
    }
}
```

`MockError` converts typed HTTP errors into responses. Other errors become
transport failures wrapped in `APIConnectionError`:

```go
err := mock.MockError("GetFile", &contree.NotFoundError{
    APIStatusError: &contree.APIStatusError{
        StatusCode: http.StatusNotFound,
        Details:    "file not found",
    },
})
if err != nil {
    return fmt.Errorf("mock missing file: %w", err)
}
```

`NewTestClient` disables retries by default, like the normal client. Pass
`WithRetry` to test retries against queued statuses or transport errors.
`CallsFor` records each HTTP attempt, including retries and unmocked requests.

Unknown operation names fail during mock configuration. Calling an unconfigured
operation returns `NotMockedError`, wrapped in `APIConnectionError`.

## Helpers and recorded requests

Configure operation names, such as `SpawnInstance` or `ListFiles`.
`SpawnInstanceWithResponse` shares the `SpawnInstance` queue and call log.
Helpers execute normally: `SpawnInstance` uses `SpawnInstanceWithResponse`, `IterFiles` uses
`ListFiles`, and `EnsureFile` can use both `GetFile` and `UploadFile`.

```go
err := mock.Mock("SpawnInstance", contree.InstanceSpawnResponse{
    UUID: contree.Some("operation-1"),
})
if err != nil {
    return fmt.Errorf("mock instance creation: %w", err)
}
operationID, err := client.SpawnInstance(ctx, "echo hello", "tag:ubuntu:latest",
    contree.SpawnInstanceOptions{Shell: true})
if err != nil {
    return fmt.Errorf("spawn greeting command: %w", err)
}
fmt.Println(operationID)

for _, call := range mock.CallsFor("SpawnInstance") {
    fmt.Println(call.Method, call.Path, string(call.Body))
}
```

`Calls` and `CallsFor` return independent snapshots. Each `MockCall` includes
the operation name, method, URL, decoded path parameters, query, headers, and
serialized request body. Queues and snapshots support concurrent use.

## Streams and raw HTTP responses

Use `[]OperationEvent` for SSE and `[]byte`, `string`, or `[][]byte` for byte
streams. `MockStream` can return a read error after the prepared data:

```go
err := mock.MockStream("IterOperationEvents", []contree.OperationEvent{
    {
        ID:   1,
        Ts:   time.Now(),
        Type: contree.OperationEventTypeStdout,
        Data: contree.NewEventDataStreamFromText("hello\n"),
    },
}, io.ErrUnexpectedEOF)
if err != nil {
    return fmt.Errorf("mock interrupted event stream: %w", err)
}
```

The client decodes the event before it receives the read error. To test
reconnection, queue another event response and configure `GetOperationStatus`
for the follower's terminal-state probe.

`MockHTTP` accepts an explicit status, headers, raw bytes, and an optional body
read error:

```go
err := mock.MockHTTP("InspectImageArchive", contree.MockResponse{
    StatusCode: http.StatusOK,
    Header:     http.Header{"Content-Type": {"application/x-tar"}},
    Body:       tarBytes,
})
if err != nil {
    return fmt.Errorf("mock image archive: %w", err)
}
```

Each call receives a fresh body. `InspectImageArchiveRaw` shares the
`InspectImageArchive` queue; `InspectImageDownloadStream` shares the
`InspectImageDownload` queue. Raw and decoded variants exercise their normal
gzip behavior when `Content-Encoding: gzip` is set.

For factories that construct their own clients, create a zero-value
`MockTransport` and pass it through `WithHTTPClient`. The test-client constructor
accepts ordinary client options, but always installs its offline transport last.
