package contree

import (
	"bytes"
	"context"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func TestNewClientRejectsUnsafeBaseURLs(t *testing.T) {
	t.Parallel()

	for _, value := range []string{
		"",
		"example.com",
		"ftp://example.com",
		"https://example.com:65536",
		"https://user:secret@example.com",
		"https://example.com?token=secret",
	} {
		value := value
		t.Run(value, func(t *testing.T) {
			t.Parallel()
			if _, err := NewClient("token", WithBaseURL(value)); err == nil {
				t.Fatalf("NewClient(%q) succeeded", value)
			}
		})
	}
}

func TestClientBuildsAuthenticatedRequest(t *testing.T) {
	t.Parallel()

	requests := make(chan *http.Request, 1)
	server := httptest.NewServer(http.HandlerFunc(func(
		writer http.ResponseWriter,
		request *http.Request,
	) {
		requests <- request.Clone(request.Context())
		writer.WriteHeader(http.StatusNoContent)
	}))
	defer server.Close()

	client, err := NewClient(
		"secret",
		WithBaseURL(server.URL+"/sandboxes/"),
		WithProject("project-id"),
		WithIdentity("test-app/1.0"),
	)
	if err != nil {
		t.Fatal(err)
	}
	response, err := client.do(context.Background(), requestSpec{
		method: http.MethodGet,
		path:   "/images",
	})
	if err != nil {
		t.Fatal(err)
	}
	drainAndClose(response.Body)

	request := <-requests
	if request.URL.Path != "/sandboxes/v1/images" {
		t.Fatalf("path = %q", request.URL.Path)
	}
	if got := request.Header.Get("Authorization"); got != "Bearer secret" {
		t.Fatalf("Authorization = %q", got)
	}
	if got := request.Header.Get("Project"); got != "project-id" {
		t.Fatalf("Project = %q", got)
	}
	if got := request.Header.Get("User-Agent"); !strings.HasPrefix(
		got,
		"test-app/1.0 contree-client-go/",
	) {
		t.Fatalf("User-Agent = %q", got)
	}
}

func TestClientRetriesOnlyIdempotentRequests(t *testing.T) {
	t.Parallel()

	var calls atomic.Int64
	server := httptest.NewServer(http.HandlerFunc(func(
		writer http.ResponseWriter,
		_ *http.Request,
	) {
		if calls.Add(1) < 3 {
			writer.WriteHeader(http.StatusServiceUnavailable)
			return
		}
		writer.WriteHeader(http.StatusNoContent)
	}))
	defer server.Close()

	policy := DefaultRetryPolicy()
	policy.MaxAttempts = 3
	policy.Delays = []time.Duration{0}
	client, err := NewClient(
		"",
		WithBaseURL(server.URL),
		WithRetry(policy),
	)
	if err != nil {
		t.Fatal(err)
	}
	response, err := client.do(context.Background(), requestSpec{
		method:     http.MethodGet,
		path:       "/retry",
		idempotent: true,
	})
	if err != nil {
		t.Fatal(err)
	}
	drainAndClose(response.Body)
	if response.StatusCode != http.StatusNoContent || calls.Load() != 3 {
		t.Fatalf("status=%d calls=%d", response.StatusCode, calls.Load())
	}

	calls.Store(0)
	response, err = client.do(context.Background(), requestSpec{
		method: http.MethodPost,
		path:   "/no-retry",
	})
	if err != nil {
		t.Fatal(err)
	}
	drainAndClose(response.Body)
	if response.StatusCode != http.StatusServiceUnavailable || calls.Load() != 1 {
		t.Fatalf("status=%d calls=%d", response.StatusCode, calls.Load())
	}
}

func TestClientRetriesRejectedPostWithReplayableBody(t *testing.T) {
	t.Parallel()

	var calls atomic.Int64
	server := httptest.NewServer(http.HandlerFunc(func(
		writer http.ResponseWriter,
		request *http.Request,
	) {
		body, err := io.ReadAll(request.Body)
		if err != nil {
			t.Error(err)
			return
		}
		if string(body) != "payload" {
			t.Errorf("body = %q", body)
		}
		if calls.Add(1) == 1 {
			writer.WriteHeader(http.StatusTooEarly)
			return
		}
		writer.WriteHeader(http.StatusNoContent)
	}))
	defer server.Close()

	policy := DefaultRetryPolicy()
	policy.MaxAttempts = 2
	policy.Delays = []time.Duration{0}
	client, err := NewClient("", WithBaseURL(server.URL), WithRetry(policy))
	if err != nil {
		t.Fatal(err)
	}
	response, err := client.do(context.Background(), requestSpec{
		method: http.MethodPost,
		path:   "/retry-post",
		body:   bytesBody([]byte("payload")),
	})
	if err != nil {
		t.Fatal(err)
	}
	drainAndClose(response.Body)
	if response.StatusCode != http.StatusNoContent || calls.Load() != 2 {
		t.Fatalf("status=%d calls=%d", response.StatusCode, calls.Load())
	}
}

func TestClientReplaysSeekableBodyFromInitialOffset(t *testing.T) {
	t.Parallel()

	var received []string
	server := httptest.NewServer(http.HandlerFunc(func(
		writer http.ResponseWriter,
		request *http.Request,
	) {
		body, err := io.ReadAll(request.Body)
		if err != nil {
			t.Error(err)
			return
		}
		received = append(received, string(body))
		if len(received) == 1 {
			writer.WriteHeader(http.StatusServiceUnavailable)
			return
		}
		writer.WriteHeader(http.StatusNoContent)
	}))
	defer server.Close()

	policy := DefaultRetryPolicy()
	policy.MaxAttempts = 2
	policy.Delays = []time.Duration{0}
	client, err := NewClient("", WithBaseURL(server.URL), WithRetry(policy))
	if err != nil {
		t.Fatal(err)
	}
	payload := strings.NewReader("prefix-payload")
	if _, err := payload.Seek(int64(len("prefix-")), io.SeekStart); err != nil {
		t.Fatal(err)
	}
	response, err := client.do(context.Background(), requestSpec{
		method:     http.MethodPut,
		path:       "/retry-reader",
		body:       oneShotBody(payload),
		idempotent: true,
	})
	if err != nil {
		t.Fatal(err)
	}
	drainAndClose(response.Body)
	if len(received) != 2 || received[0] != "payload" || received[1] != "payload" {
		t.Fatalf("received bodies = %#v", received)
	}
}

func TestClientDoesNotRetryNonReplayableBody(t *testing.T) {
	t.Parallel()

	var calls atomic.Int64
	server := httptest.NewServer(http.HandlerFunc(func(
		writer http.ResponseWriter,
		request *http.Request,
	) {
		calls.Add(1)
		_, _ = io.Copy(io.Discard, request.Body)
		writer.WriteHeader(http.StatusServiceUnavailable)
	}))
	defer server.Close()

	policy := DefaultRetryPolicy()
	policy.MaxAttempts = 2
	policy.Delays = []time.Duration{0}
	client, err := NewClient("", WithBaseURL(server.URL), WithRetry(policy))
	if err != nil {
		t.Fatal(err)
	}
	response, err := client.do(context.Background(), requestSpec{
		method:     http.MethodPut,
		path:       "/no-replay",
		body:       oneShotBody(&readerOnly{Reader: strings.NewReader("payload")}),
		idempotent: true,
	})
	if err != nil {
		t.Fatal(err)
	}
	drainAndClose(response.Body)
	if calls.Load() != 1 {
		t.Fatalf("calls = %d", calls.Load())
	}
}

func TestUploadFileRejectsNilReadersBeforeTransport(t *testing.T) {
	t.Parallel()

	var calls atomic.Int64
	server := httptest.NewServer(http.HandlerFunc(func(
		writer http.ResponseWriter,
		_ *http.Request,
	) {
		calls.Add(1)
		writer.WriteHeader(http.StatusNoContent)
	}))
	defer server.Close()
	client, err := NewClient("", WithBaseURL(server.URL))
	if err != nil {
		t.Fatal(err)
	}

	var typedNil *bytes.Reader
	for _, reader := range []io.Reader{nil, typedNil} {
		if _, err := client.UploadFile(context.Background(), reader); err == nil {
			t.Fatal("nil upload reader succeeded")
		}
	}
	if calls.Load() != 0 {
		t.Fatalf("nil readers made %d requests", calls.Load())
	}
}

func TestClientDoesNotFollowRedirects(t *testing.T) {
	t.Parallel()

	server := httptest.NewServer(http.HandlerFunc(func(
		writer http.ResponseWriter,
		request *http.Request,
	) {
		if request.URL.Path == "/v1/redirect" {
			http.Redirect(writer, request, "/v1/final", http.StatusFound)
			return
		}
		t.Fatal("redirect was followed")
	}))
	defer server.Close()

	client, err := NewClient("", WithBaseURL(server.URL))
	if err != nil {
		t.Fatal(err)
	}
	response, err := client.do(context.Background(), requestSpec{
		method: http.MethodGet,
		path:   "/redirect",
	})
	if err != nil {
		t.Fatal(err)
	}
	drainAndClose(response.Body)
	if response.StatusCode != http.StatusFound {
		t.Fatalf("status = %d", response.StatusCode)
	}
}

func TestParseRetryAfterPreservesZeroAndRejectsInvalidValues(t *testing.T) {
	t.Parallel()

	if delay, present := parseRetryAfter("0", time.Now()); !present || delay != 0 {
		t.Fatalf("Retry-After zero = %v, %v", delay, present)
	}
	if delay, present := parseRetryAfter("-2", time.Now()); !present || delay != 0 {
		t.Fatalf("negative Retry-After = %v, %v", delay, present)
	}
	if _, present := parseRetryAfter("not-a-delay", time.Now()); present {
		t.Fatal("invalid Retry-After was accepted")
	}
}

func TestClientTimeoutWrapsContextError(t *testing.T) {
	t.Parallel()

	server := httptest.NewServer(http.HandlerFunc(func(
		writer http.ResponseWriter,
		_ *http.Request,
	) {
		time.Sleep(100 * time.Millisecond)
		writer.WriteHeader(http.StatusNoContent)
	}))
	defer server.Close()

	client, err := NewClient(
		"",
		WithBaseURL(server.URL),
		WithTimeout(10*time.Millisecond),
	)
	if err != nil {
		t.Fatal(err)
	}
	_, err = client.do(context.Background(), requestSpec{
		method: http.MethodGet,
		path:   "/slow",
	})
	var connection *APIConnectionError
	if !errors.As(err, &connection) || !connection.TimedOut {
		t.Fatalf("error = %#v", err)
	}
	if !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("error does not wrap context deadline: %v", err)
	}
}

func TestResponseBodyKeepsRequestContextUntilClose(t *testing.T) {
	t.Parallel()

	server := httptest.NewServer(http.HandlerFunc(func(
		writer http.ResponseWriter,
		_ *http.Request,
	) {
		flusher := writer.(http.Flusher)
		writer.WriteHeader(http.StatusOK)
		_, _ = io.WriteString(writer, "first")
		flusher.Flush()
		time.Sleep(20 * time.Millisecond)
		_, _ = io.WriteString(writer, "second")
	}))
	defer server.Close()

	client, err := NewClient(
		"",
		WithBaseURL(server.URL),
		WithTimeout(time.Second),
	)
	if err != nil {
		t.Fatal(err)
	}
	response, err := client.do(context.Background(), requestSpec{
		method: http.MethodGet,
		path:   "/stream",
	})
	if err != nil {
		t.Fatal(err)
	}
	body, err := io.ReadAll(response.Body)
	if err != nil {
		t.Fatal(err)
	}
	if err := response.Body.Close(); err != nil {
		t.Fatal(err)
	}
	if string(body) != "firstsecond" {
		t.Fatalf("body = %q", body)
	}
}

type readerOnly struct{ io.Reader }
