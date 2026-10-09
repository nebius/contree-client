package contree

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"sync"
	"time"
)

// MockCall records an HTTP attempt, including attempts made by helpers and
// retries. Operation is the canonical generated method name. PathParams uses
// the wire parameter names. Body contains the serialized request body.
type MockCall struct {
	Operation  string
	Method     string
	URL        string
	Path       string
	PathParams map[string]string
	Query      url.Values
	Header     http.Header
	Body       []byte
}

// MockResponse supplies a wire response for MockHTTP. BodyError, when set,
// is returned after all Body bytes have been read. StatusCode must be explicit.
type MockResponse struct {
	StatusCode int
	Header     http.Header
	Body       []byte
	BodyError  error
}

// NotMockedError reports a request without a configured outcome. Client methods
// wrap it in APIConnectionError; errors.As can recover it.
type NotMockedError struct {
	Operation string
	Method    string
	Path      string
}

func (e *NotMockedError) Error() string {
	return fmt.Sprintf("contree: no mock configured for %s (%s %s)", e.Operation, e.Method, e.Path)
}

type mockOutcome struct {
	response MockResponse
	err      error
}

// MockTransport is an offline HTTP transport with outcome queues and a call
// log. Its zero value is ready to use. Configuration, requests, and log access
// are safe for concurrent use. Each queue repeats its last outcome.
//
// Mock operation names, such as SpawnInstance or ListImages. Helpers such as
// SpawnInstance, EnsureFile, and IterImages execute normally through those mocks.
// Full-response and byte-stream aliases share the operation's queue and call log.
type MockTransport struct {
	mu    sync.Mutex
	mocks map[string][]mockOutcome
	calls []MockCall
}

var _ http.RoundTripper = (*MockTransport)(nil)

// NewTestClient constructs a normal Client and its offline MockTransport.
// It uses "test-token" and https://contree.test by default. Options can change
// normal client settings; the mock HTTP transport is always installed last.
// No request can fall back to a network transport.
//
// For code that constructs its own client, supply a zero-value MockTransport
// through WithHTTPClient instead.
func NewTestClient(options ...ClientOption) (*Client, *MockTransport, error) {
	mock := &MockTransport{}
	configured := make([]ClientOption, 0, len(options)+2)
	configured = append(configured, WithBaseURL("https://contree.test"))
	configured = append(configured, options...)
	configured = append(configured, WithHTTPClient(&http.Client{Transport: mock}))
	client, err := NewClient("test-token", configured...)
	if err != nil {
		return nil, nil, err
	}
	return client, mock, nil
}

// Mock queues a result for a generated operation. JSON operations accept model
// values; boolean operations accept bool; location operations accept the final
// path segment as a string. Empty responses accept nil. Byte responses accept
// []byte, string, or [][]byte. SSE responses accept []OperationEvent.
//
// Results are serialized when configured, then decoded by the real client when
// called. Unknown names and values that cannot be encoded return an error.
func (m *MockTransport) Mock(operation string, result any) error {
	op, err := lookupMockOperation(operation)
	if err != nil {
		return err
	}
	response, err := encodeMockResult(op, result)
	if err != nil {
		return fmt.Errorf("contree: mock %s: %w", operation, err)
	}
	m.enqueue(op.name, mockOutcome{response: response})
	return nil
}

// MockError queues an error. APIStatusError and its typed wrappers become HTTP
// responses, so status handling and retries run normally. Other errors become
// transport failures wrapped in APIConnectionError and remain accessible through
// errors.Is and errors.As. Nil errors are rejected.
func (m *MockTransport) MockError(operation string, err error) error {
	op, lookupErr := lookupMockOperation(operation)
	if lookupErr != nil {
		return lookupErr
	}
	if err == nil {
		return errors.New("contree: mock error must not be nil")
	}
	var status *APIStatusError
	if errors.As(err, &status) && status != nil {
		response := MockResponse{StatusCode: status.StatusCode, Header: make(http.Header), Body: status.Body}
		if response.Body == nil && status.Details != nil {
			body, marshalErr := json.Marshal(map[string]any{"error": status.Details})
			if marshalErr != nil {
				return fmt.Errorf("contree: encode mock error: %w", marshalErr)
			}
			response.Body = body
		}
		if status.RequestID != "" {
			response.Header.Set("X-Request-ID", status.RequestID)
		}
		if status.RetryAfter != nil {
			seconds := int64(*status.RetryAfter / time.Second)
			if *status.RetryAfter%time.Second > 0 {
				seconds++
			}
			response.Header.Set("Retry-After", strconv.FormatInt(seconds, 10))
		}
		return m.MockHTTP(operation, response)
	}
	m.enqueue(op.name, mockOutcome{err: err})
	return nil
}

// MockStream queues byte or SSE results followed by a read error. Result uses
// the same representation as Mock. A nil error produces normal end-of-stream.
func (m *MockTransport) MockStream(operation string, result any, err error) error {
	op, lookupErr := lookupMockOperation(operation)
	if lookupErr != nil {
		return lookupErr
	}
	if op.mode != "SSE" && op.mode != "BYTES" && op.mode != "BYTE_STREAM" {
		return fmt.Errorf("contree: %s is not a stream operation", operation)
	}
	response, encodeErr := encodeMockResult(op, result)
	if encodeErr != nil {
		return fmt.Errorf("contree: mock %s: %w", operation, encodeErr)
	}
	response.BodyError = err
	m.enqueue(op.name, mockOutcome{response: response})
	return nil
}

// MockHTTP queues an exact HTTP response. Use it for malformed JSON, specific
// status codes or headers, and encoded bodies such as gzip. Input bytes and
// headers are copied, and each call receives a fresh response body.
func (m *MockTransport) MockHTTP(operation string, response MockResponse) error {
	op, err := lookupMockOperation(operation)
	if err != nil {
		return err
	}
	if response.StatusCode < 100 || response.StatusCode > 599 {
		return fmt.Errorf("contree: invalid mock HTTP status %d", response.StatusCode)
	}
	response.Body = bytes.Clone(response.Body)
	response.Header = response.Header.Clone()
	m.enqueue(op.name, mockOutcome{response: response})
	return nil
}

func (m *MockTransport) enqueue(operation string, outcome mockOutcome) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.mocks == nil {
		m.mocks = make(map[string][]mockOutcome)
	}
	m.mocks[operation] = append(m.mocks[operation], outcome)
}

// Calls returns an independent snapshot of all recorded HTTP attempts.
func (m *MockTransport) Calls() []MockCall {
	return m.callsFor("")
}

// CallsFor returns a snapshot for one generated operation, in request order.
// Unknown names return an empty snapshot. Stream aliases use the canonical name.
func (m *MockTransport) CallsFor(operation string) []MockCall {
	op, err := lookupMockOperation(operation)
	if err != nil {
		return nil
	}
	return m.callsFor(op.name)
}

func (m *MockTransport) callsFor(operation string) []MockCall {
	m.mu.Lock()
	defer m.mu.Unlock()
	var calls []MockCall
	for _, call := range m.calls {
		if operation != "" && call.Operation != operation {
			continue
		}
		call.Header = call.Header.Clone()
		call.Query = url.Values(http.Header(call.Query).Clone())
		call.Body = bytes.Clone(call.Body)
		params := make(map[string]string, len(call.PathParams))
		for key, value := range call.PathParams {
			params[key] = value
		}
		call.PathParams = params
		calls = append(calls, call)
	}
	return calls
}

// RoundTrip records the request and returns its next configured outcome.
// It never opens a connection. Unconfigured requests return NotMockedError.
func (m *MockTransport) RoundTrip(request *http.Request) (*http.Response, error) {
	if request == nil || request.URL == nil {
		return nil, errors.New("contree: mock request and URL must not be nil")
	}
	if request.Body != nil {
		defer request.Body.Close()
	}
	if err := request.Context().Err(); err != nil {
		return nil, err
	}
	op, params := matchMockOperation(request.Method, request.URL.EscapedPath())
	call := MockCall{
		Method: request.Method, URL: request.URL.String(), Path: request.URL.Path,
		PathParams: params, Query: request.URL.Query(), Header: request.Header.Clone(),
	}
	if op != nil {
		call.Operation = op.name
	}
	var readErr error
	if request.Body != nil {
		call.Body, readErr = io.ReadAll(request.Body)
	}
	m.mu.Lock()
	m.calls = append(m.calls, call)
	queue := m.mocks[call.Operation]
	var outcome mockOutcome
	if readErr == nil && len(queue) > 0 {
		outcome = queue[0]
		if len(queue) > 1 {
			m.mocks[call.Operation] = queue[1:]
		}
	}
	m.mu.Unlock()
	if readErr != nil {
		return nil, readErr
	}
	if len(queue) == 0 {
		return nil, &NotMockedError{Operation: call.Operation, Method: call.Method, Path: call.Path}
	}
	if outcome.err != nil {
		return nil, outcome.err
	}
	header := outcome.response.Header.Clone()
	if header == nil {
		header = make(http.Header)
	}
	return &http.Response{
		StatusCode: outcome.response.StatusCode,
		Status:     fmt.Sprintf("%d %s", outcome.response.StatusCode, http.StatusText(outcome.response.StatusCode)),
		Header:     header, Request: request,
		Body:          &mockBody{ctx: request.Context(), reader: bytes.NewReader(outcome.response.Body), err: outcome.response.BodyError},
		ContentLength: -1,
	}, nil
}

func lookupMockOperation(name string) (*mockOperation, error) {
	for i := range mockOperations {
		op := &mockOperations[i]
		if op.name == name {
			return op, nil
		}
		for _, alias := range op.aliases {
			if alias == name {
				return op, nil
			}
		}
	}
	return nil, fmt.Errorf("contree: unknown mock operation %q; use a generated operation name", name)
}

func matchMockOperation(method, escapedPath string) (*mockOperation, map[string]string) {
	parts := strings.Split(escapedPath, "/")
	var matched *mockOperation
	var matchedParams map[string]string
	bestScore := -1
	for i := range mockOperations {
		op := &mockOperations[i]
		if op.method != method {
			continue
		}
		template := strings.Split(strings.TrimPrefix(op.path, "/"), "/")
		start := len(parts) - len(template)
		if start < 1 || parts[start-1] != "v1" {
			continue
		}
		params := make(map[string]string)
		score := 0
		matches := true
		for j, segment := range template {
			value, err := url.PathUnescape(parts[start+j])
			if err != nil {
				matches = false
				break
			}
			if strings.HasPrefix(segment, "{") && strings.HasSuffix(segment, "}") {
				params[segment[1:len(segment)-1]] = value
			} else if segment == value {
				score++
			} else {
				matches = false
				break
			}
		}
		if matches && score > bestScore {
			matched, matchedParams, bestScore = op, params, score
		}
	}
	return matched, matchedParams
}

func encodeMockResult(op *mockOperation, result any) (MockResponse, error) {
	response := MockResponse{StatusCode: op.status, Header: make(http.Header)}
	switch op.mode {
	case "JSON":
		for i := len(op.jsonPath) - 1; i >= 0; i-- {
			result = map[string]any{op.jsonPath[i]: result}
		}
		body, err := json.Marshal(result)
		if err != nil {
			return MockResponse{}, err
		}
		response.Body = body
		response.Header.Set("Content-Type", "application/json")
	case "EMPTY":
		if result != nil {
			return MockResponse{}, errors.New("empty response requires nil")
		}
	case "STATUS_BOOL":
		value, ok := result.(bool)
		if !ok {
			return MockResponse{}, errors.New("boolean response requires bool")
		}
		if !value {
			if op.falseStatus == 0 {
				return MockResponse{}, errors.New("operation has no false status")
			}
			response.StatusCode = op.falseStatus
		}
	case "LOCATION":
		value, ok := result.(string)
		if !ok || value == "" || value == "." || value == ".." {
			return MockResponse{}, errors.New("location response requires a non-empty path segment")
		}
		response.Header.Set(op.header, "/"+url.PathEscape(value))
	case "BYTES", "BYTE_STREAM":
		switch value := result.(type) {
		case nil:
		case []byte:
			response.Body = bytes.Clone(value)
		case string:
			response.Body = []byte(value)
		case [][]byte:
			response.Body = bytes.Join(value, nil)
		default:
			return MockResponse{}, errors.New("byte response requires []byte, string, or [][]byte")
		}
		response.Header.Set("Content-Type", "application/octet-stream")
	case "SSE":
		events, ok := result.([]OperationEvent)
		if result != nil && !ok {
			return MockResponse{}, errors.New("SSE response requires []OperationEvent")
		}
		var body bytes.Buffer
		for _, event := range events {
			payload, err := json.Marshal(event)
			if err != nil {
				return MockResponse{}, err
			}
			fmt.Fprintf(&body, "id: %d\ndata: %s\n\n", event.ID, payload)
		}
		response.Body = body.Bytes()
		response.Header.Set("Content-Type", "text/event-stream")
	default:
		return MockResponse{}, fmt.Errorf("unsupported mock response mode %s", op.mode)
	}
	return response, nil
}

type mockBody struct {
	mu     sync.Mutex
	ctx    context.Context
	reader *bytes.Reader
	err    error
	closed bool
}

func (b *mockBody) Read(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.closed {
		return 0, io.ErrClosedPipe
	}
	if err := b.ctx.Err(); err != nil {
		return 0, err
	}
	n, err := b.reader.Read(p)
	if err == io.EOF && b.err != nil {
		err = b.err
	}
	return n, err
}

func (b *mockBody) Close() error {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.closed = true
	return nil
}
