package contree

import (
	"compress/gzip"
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

const operationHelperID = "12345678-9abc-baba-deda-0123456789ab"

type operationHelperRoundTripFunc func(*http.Request) (*http.Response, error)

func (f operationHelperRoundTripFunc) RoundTrip(
	request *http.Request,
) (*http.Response, error) {
	return f(request)
}

type operationHelperBody struct {
	io.Reader
	closed atomic.Bool
}

func (b *operationHelperBody) Close() error {
	b.closed.Store(true)
	return nil
}

func newOperationHelperClient(
	t *testing.T,
	roundTrip operationHelperRoundTripFunc,
) *Client {
	t.Helper()
	client, err := NewClient(
		"",
		WithBaseURL("https://example.test"),
		WithHTTPClient(&http.Client{Transport: roundTrip}),
		WithoutRetry(),
		WithTimeout(0),
	)
	if err != nil {
		t.Fatal(err)
	}
	return client
}

func operationHelperResponse(
	request *http.Request,
	status int,
	body io.ReadCloser,
) *http.Response {
	return &http.Response{
		StatusCode: status,
		Header:     make(http.Header),
		Body:       body,
		Request:    request,
	}
}

func operationHelperJSONResponse(
	request *http.Request,
	status int,
	body string,
) *http.Response {
	return operationHelperResponse(
		request,
		status,
		io.NopCloser(strings.NewReader(body)),
	)
}

func operationHelperEvent(id int64, eventType, data string) string {
	return fmt.Sprintf(
		"id: %d\ndata: {\"id\":%d,\"ts\":\"2026-09-03T12:00:00Z\",\"type\":%q,\"data\":%s}\n\n",
		id,
		id,
		eventType,
		data,
	)
}

func TestOperationEventFollowerReconnectsFromLastEventID(t *testing.T) {
	t.Parallel()

	var eventCalls atomic.Int64
	var statusCalls atomic.Int64
	var eventBodies []*operationHelperBody
	client := newOperationHelperClient(t, func(request *http.Request) (*http.Response, error) {
		switch request.URL.Path {
		case "/v1/operations/" + operationHelperID + "/events":
			call := eventCalls.Add(1)
			if request.URL.Query().Get("follow") != "1" {
				t.Errorf("follow query = %q", request.URL.Query().Get("follow"))
			}
			bodyText := operationHelperEvent(
				1,
				"stdout",
				`{"value":"first","encoding":"ascii"}`,
			)
			if call == 1 {
				if value := request.Header.Get("Last-Event-Id"); value != "" {
					t.Errorf("initial Last-Event-Id = %q", value)
				}
			} else {
				if value := request.Header.Get("Last-Event-Id"); value != "1" {
					t.Errorf("resumed Last-Event-Id = %q", value)
				}
				bodyText = operationHelperEvent(
					2,
					"completion",
					`{"status":"SUCCESS","duration_ms":1}`,
				)
			}
			body := &operationHelperBody{Reader: strings.NewReader(bodyText)}
			eventBodies = append(eventBodies, body)
			return operationHelperResponse(request, http.StatusOK, body), nil
		case "/v1/operations/" + operationHelperID:
			statusCalls.Add(1)
			return operationHelperJSONResponse(
				request,
				http.StatusOK,
				`{"status":"EXECUTING"}`,
			), nil
		default:
			return nil, fmt.Errorf("unexpected request path %s", request.URL.Path)
		}
	})

	follower, err := client.newOperationEventFollower(context.Background(), operationHelperID, nil)
	if err != nil {
		t.Fatal(err)
	}
	if !follower.Next() || follower.Event().ID != 1 {
		t.Fatalf("first event = %#v, error = %v", follower.Event(), follower.Err())
	}
	if !follower.Next() || follower.Event().Type != OperationEventTypeCompletion {
		t.Fatalf("completion event = %#v, error = %v", follower.Event(), follower.Err())
	}
	if follower.Next() {
		t.Fatal("follower produced an event after completion")
	}
	if err := follower.Err(); err != nil {
		t.Fatalf("follower error = %v", err)
	}
	if eventCalls.Load() != 2 || statusCalls.Load() != 1 {
		t.Fatalf("event calls = %d, status calls = %d", eventCalls.Load(), statusCalls.Load())
	}
	for index, body := range eventBodies {
		if !body.closed.Load() {
			t.Errorf("event body %d was not closed", index)
		}
	}
}

func TestOperationEventFollowerResumesAfterIDOnlyFrame(t *testing.T) {
	t.Parallel()

	var eventCalls atomic.Int64
	client := newOperationHelperClient(t, func(request *http.Request) (*http.Response, error) {
		switch request.URL.Path {
		case "/v1/operations/" + operationHelperID + "/events":
			call := eventCalls.Add(1)
			if call == 1 {
				body := "id: 5\n\nevent: sse_error\ndata: interrupted\n\n"
				return operationHelperJSONResponse(request, http.StatusOK, body), nil
			}
			if value := request.Header.Get("Last-Event-Id"); value != "5" {
				t.Errorf("resumed Last-Event-Id = %q", value)
			}
			body := operationHelperEvent(
				6,
				"completion",
				`{"status":"SUCCESS","duration_ms":1}`,
			)
			return operationHelperJSONResponse(request, http.StatusOK, body), nil
		case "/v1/operations/" + operationHelperID:
			return operationHelperJSONResponse(
				request,
				http.StatusOK,
				`{"status":"EXECUTING"}`,
			), nil
		default:
			return nil, fmt.Errorf("unexpected request path %s", request.URL.Path)
		}
	})

	started := time.Now()
	follower, err := client.newOperationEventFollower(context.Background(), operationHelperID, nil)
	if err != nil {
		t.Fatal(err)
	}
	if !follower.Next() || follower.Event().ID != 6 {
		t.Fatalf("event = %#v, error = %v", follower.Event(), follower.Err())
	}
	if elapsed := time.Since(started); elapsed >= operationEventNoProgressDelay {
		t.Fatalf("resume after cursor progress took %s", elapsed)
	}
	if eventID, ok := follower.LastEventID(); !ok || eventID != 6 {
		t.Fatalf("last event ID = %d, %v", eventID, ok)
	}
}

func TestOperationEventFollowerDrainsAfterTerminalProbe(t *testing.T) {
	t.Parallel()
	var eventCalls, statusCalls int
	client := newOperationHelperClient(t, func(request *http.Request) (*http.Response, error) {
		if strings.HasSuffix(request.URL.Path, "/events") {
			eventCalls++
			query := request.URL.Query()
			if eventCalls == 1 {
				if query.Get("follow") != "1" {
					t.Fatalf("initial query = %v", query)
				}
				return operationHelperJSONResponse(request, 200, operationHelperEvent(1, "stdout", `{"value":"head","encoding":"ascii"}`)), nil
			}
			if query.Get("follow") != "" || request.Header.Get("Last-Event-Id") != "1" {
				t.Fatalf("drain request = %s %v", request.URL, request.Header)
			}
			body := operationHelperEvent(2, "stdout", `{"value":"tail","encoding":"ascii"}`) + operationHelperEvent(3, "completion", `{"status":"SUCCESS","duration_ms":1}`)
			return operationHelperJSONResponse(request, 200, body), nil
		}
		statusCalls++
		return operationHelperJSONResponse(request, 200, `{"status":"SUCCESS"}`), nil
	})
	var ids []int64
	for event, err := range client.FollowOperationEvents(context.Background(), operationHelperID, nil) {
		if err != nil {
			t.Fatal(err)
		}
		ids = append(ids, event.ID)
	}
	if fmt.Sprint(ids) != "[1 2 3]" || eventCalls != 2 || statusCalls != 1 {
		t.Fatalf("events = %v; requests = %d/%d", ids, eventCalls, statusCalls)
	}
}

func TestOperationEventFollowerBoundsIncompleteDrain(t *testing.T) {
	t.Parallel()
	for _, filtered := range []bool{false, true} {
		t.Run(fmt.Sprint(filtered), func(t *testing.T) {
			calls := 0
			client := newOperationHelperClient(t, func(request *http.Request) (*http.Response, error) {
				if strings.HasSuffix(request.URL.Path, "/events") {
					calls++
					if filtered && request.URL.Query().Get("spid") != "7" {
						t.Fatalf("lost filter: %s", request.URL)
					}
					return operationHelperJSONResponse(request, 200, ""), nil
				}
				return operationHelperJSONResponse(request, 200, `{"status":"SUCCESS"}`), nil
			})
			client.retry = &RetryPolicy{MaxAttempts: 2, Delays: []time.Duration{0}}
			var options *FollowOperationEventsOptions
			if filtered {
				spid := int64(7)
				options = &FollowOperationEventsOptions{SPID: &spid}
			}
			var lastErr error
			for _, err := range client.FollowOperationEvents(context.Background(), operationHelperID, options) {
				lastErr = err
			}
			if filtered {
				if lastErr != nil || calls != 2 {
					t.Fatalf("filtered: calls=%d error=%v", calls, lastErr)
				}
			} else if !errors.Is(lastErr, io.ErrUnexpectedEOF) || calls != 3 {
				t.Fatalf("incomplete: calls=%d error=%v", calls, lastErr)
			}
		})
	}
}

func TestOperationEventFollowerContinuesAfterProbeWithoutStatus(t *testing.T) {
	t.Parallel()

	for name, statusBody := range map[string]string{
		"absent": `{}`,
		"null":   `{"status":null}`,
	} {
		name, statusBody := name, statusBody
		t.Run(name, func(t *testing.T) {
			t.Parallel()

			var eventCalls atomic.Int64
			client := newOperationHelperClient(t, func(request *http.Request) (*http.Response, error) {
				switch request.URL.Path {
				case "/v1/operations/" + operationHelperID + "/events":
					if eventCalls.Add(1) == 1 {
						return operationHelperJSONResponse(request, http.StatusOK, "id: 8\n\n"), nil
					}
					if value := request.Header.Get("Last-Event-Id"); value != "8" {
						t.Errorf("resumed Last-Event-Id = %q", value)
					}
					body := operationHelperEvent(
						9,
						"completion",
						`{"status":"SUCCESS","duration_ms":1}`,
					)
					return operationHelperJSONResponse(request, http.StatusOK, body), nil
				case "/v1/operations/" + operationHelperID:
					return operationHelperJSONResponse(request, http.StatusOK, statusBody), nil
				default:
					return nil, fmt.Errorf("unexpected request path %s", request.URL.Path)
				}
			})

			follower, err := client.newOperationEventFollower(
				context.Background(),
				operationHelperID,
				nil,
			)
			if err != nil {
				t.Fatal(err)
			}
			if !follower.Next() || follower.Event().Type != OperationEventTypeCompletion {
				t.Fatalf("event = %#v, error = %v", follower.Event(), follower.Err())
			}
			if follower.Next() {
				t.Fatal("follower produced an event after completion")
			}
			if err := follower.Err(); err != nil {
				t.Fatal(err)
			}
			if eventCalls.Load() != 2 {
				t.Fatalf("event calls = %d", eventCalls.Load())
			}
		})
	}
}

func TestOperationTerminal(t *testing.T) {
	t.Parallel()

	tests := map[string]struct {
		status int
		body   string
		want   bool
		isErr  bool
	}{
		"terminal":  {status: http.StatusOK, body: `{"status":"SUCCESS"}`, want: true},
		"active":    {status: http.StatusOK, body: `{"status":"EXECUTING"}`},
		"no status": {status: http.StatusOK, body: `{}`},
		"transient": {status: http.StatusServiceUnavailable, body: `{"error":"busy"}`},
		"permanent": {status: http.StatusNotFound, body: `{"error":"missing"}`, isErr: true},
	}
	for name, test := range tests {
		name, test := name, test
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			client := newOperationHelperClient(t, func(request *http.Request) (*http.Response, error) {
				return operationHelperJSONResponse(request, test.status, test.body), nil
			})
			got, err := client.OperationTerminal(context.Background(), operationHelperID)
			if (err != nil) != test.isErr {
				t.Fatalf("error = %v", err)
			}
			if got != test.want {
				t.Fatalf("terminal = %t, want %t", got, test.want)
			}
		})
	}
}

func TestOperationTerminalRejectsNilContext(t *testing.T) {
	t.Parallel()

	client := newOperationHelperClient(t, func(request *http.Request) (*http.Response, error) {
		t.Fatalf("unexpected request %s", request.URL)
		return nil, nil
	})
	if _, err := client.OperationTerminal(nil, operationHelperID); err == nil {
		t.Fatal("OperationTerminal accepted a nil context")
	}
}

func TestOperationEventFollowerStopsOnMalformedEvent(t *testing.T) {
	t.Parallel()

	var statusCalls atomic.Int64
	client := newOperationHelperClient(t, func(request *http.Request) (*http.Response, error) {
		if strings.HasSuffix(request.URL.Path, "/events") {
			return operationHelperJSONResponse(
				request,
				http.StatusOK,
				"data: {invalid}\n\n",
			), nil
		}
		statusCalls.Add(1)
		return operationHelperJSONResponse(request, http.StatusOK, `{"status":"EXECUTING"}`), nil
	})
	follower, err := client.newOperationEventFollower(context.Background(), operationHelperID, nil)
	if err != nil {
		t.Fatal(err)
	}
	if follower.Next() || follower.Err() == nil {
		t.Fatalf("malformed event result = %v, %v", follower.Event(), follower.Err())
	}
	if statusCalls.Load() != 0 {
		t.Fatalf("malformed event triggered %d status probes", statusCalls.Load())
	}
}

func TestOperationEventFollowerStopsOnPermanentOpenError(t *testing.T) {
	t.Parallel()

	var statusCalls atomic.Int64
	client := newOperationHelperClient(t, func(request *http.Request) (*http.Response, error) {
		if strings.HasSuffix(request.URL.Path, "/events") {
			return operationHelperJSONResponse(
				request,
				http.StatusUnauthorized,
				`{"error":"invalid token"}`,
			), nil
		}
		statusCalls.Add(1)
		return operationHelperJSONResponse(request, http.StatusOK, `{"status":"EXECUTING"}`), nil
	})
	follower, err := client.newOperationEventFollower(context.Background(), operationHelperID, nil)
	if err != nil {
		t.Fatal(err)
	}
	if follower.Next() {
		t.Fatal("unauthorized stream produced an event")
	}
	var authentication *AuthenticationError
	if !errors.As(follower.Err(), &authentication) {
		t.Fatalf("follower error = %T, want *AuthenticationError", follower.Err())
	}
	if statusCalls.Load() != 0 {
		t.Fatalf("unauthorized stream triggered %d status probes", statusCalls.Load())
	}
}

func TestOperationEventFollowerNoProgressWaitHonorsContext(t *testing.T) {
	t.Parallel()

	var eventCalls atomic.Int64
	client := newOperationHelperClient(t, func(request *http.Request) (*http.Response, error) {
		switch request.URL.Path {
		case "/v1/operations/" + operationHelperID + "/events":
			eventCalls.Add(1)
			return operationHelperJSONResponse(request, http.StatusOK, ""), nil
		case "/v1/operations/" + operationHelperID:
			return operationHelperJSONResponse(
				request,
				http.StatusOK,
				`{"status":"EXECUTING"}`,
			), nil
		default:
			return nil, fmt.Errorf("unexpected request path %s", request.URL.Path)
		}
	})

	ctx, cancel := context.WithTimeout(context.Background(), 40*time.Millisecond)
	defer cancel()
	follower, err := client.newOperationEventFollower(ctx, operationHelperID, nil)
	if err != nil {
		t.Fatal(err)
	}
	started := time.Now()
	if follower.Next() {
		t.Fatal("empty streams produced an event")
	}
	if !errors.Is(follower.Err(), context.DeadlineExceeded) {
		t.Fatalf("follower error = %v", follower.Err())
	}
	if elapsed := time.Since(started); elapsed >= operationEventNoProgressDelay {
		t.Fatalf("context cancellation took %s", elapsed)
	}
	if eventCalls.Load() != 1 {
		t.Fatalf("event calls = %d, want one before backoff", eventCalls.Load())
	}
}

func TestOperationEventFollowerCloseReleasesActiveStream(t *testing.T) {
	t.Parallel()

	body := &operationHelperBody{
		Reader: strings.NewReader(operationHelperEvent(
			1,
			"stdout",
			`{"value":"first","encoding":"ascii"}`,
		)),
	}
	client := newOperationHelperClient(t, func(request *http.Request) (*http.Response, error) {
		return operationHelperResponse(request, http.StatusOK, body), nil
	})
	follower, err := client.newOperationEventFollower(context.Background(), operationHelperID, nil)
	if err != nil {
		t.Fatal(err)
	}
	if !follower.Next() {
		t.Fatalf("Next failed: %v", follower.Err())
	}
	if body.closed.Load() {
		t.Fatal("body closed while an active stream event was being consumed")
	}
	if err := follower.Close(); err != nil {
		t.Fatal(err)
	}
	if !body.closed.Load() {
		t.Fatal("Close did not release the active event body")
	}
	if follower.Next() {
		t.Fatal("closed follower produced an event")
	}
}

func TestWaitOperationReturnsFinalStatus(t *testing.T) {
	t.Parallel()

	var statusCalls atomic.Int64
	eventBody := &operationHelperBody{Reader: strings.NewReader(operationHelperEvent(
		1,
		"completion",
		`{"status":"SUCCESS","duration_ms":1}`,
	))}
	client := newOperationHelperClient(t, func(request *http.Request) (*http.Response, error) {
		switch request.URL.Path {
		case "/v1/operations/" + operationHelperID + "/events":
			return operationHelperResponse(request, http.StatusOK, eventBody), nil
		case "/v1/operations/" + operationHelperID:
			statusCalls.Add(1)
			return operationHelperJSONResponse(
				request,
				http.StatusOK,
				`{"uuid":"`+operationHelperID+`","status":"SUCCESS"}`,
			), nil
		default:
			return nil, fmt.Errorf("unexpected request path %s", request.URL.Path)
		}
	})

	operation, err := client.WaitOperation(context.Background(), operationHelperID)
	if err != nil {
		t.Fatal(err)
	}
	status, ok := operation.Status.Value()
	if !ok || status != OperationStatusSuccess {
		t.Fatalf("operation status = %q, %v", status, ok)
	}
	if statusCalls.Load() != 1 {
		t.Fatalf("status calls = %d", statusCalls.Load())
	}
	if !eventBody.closed.Load() {
		t.Fatal("WaitOperation did not close its event stream")
	}
}

func TestOperationEventFollowerDoesNotRetryCorruptStream(t *testing.T) {
	t.Parallel()
	calls := 0
	body := &operationHelperBody{Reader: corruptEventReader{}}
	client := newOperationHelperClient(t, func(request *http.Request) (*http.Response, error) {
		calls++
		return operationHelperResponse(request, 200, body), nil
	})
	var lastErr error
	for _, err := range client.FollowOperationEvents(context.Background(), operationHelperID, nil) {
		lastErr = err
	}
	if !errors.Is(lastErr, gzip.ErrChecksum) || calls != 1 || !body.closed.Load() {
		t.Fatalf("calls=%d closed=%t error=%v", calls, body.closed.Load(), lastErr)
	}
}

type corruptEventReader struct{}

func (corruptEventReader) Read([]byte) (int, error) { return 0, gzip.ErrChecksum }

func TestOperationEventFollowerResumesInterruptedDrain(t *testing.T) {
	t.Parallel()
	calls := 0
	client := newOperationHelperClient(t, func(request *http.Request) (*http.Response, error) {
		if !strings.HasSuffix(request.URL.Path, "/events") {
			return operationHelperJSONResponse(request, 200, `{"status":"SUCCESS"}`), nil
		}
		calls++
		if calls == 1 {
			return operationHelperJSONResponse(request, 200, ""), nil
		}
		if request.URL.Query().Has("follow") {
			t.Fatal("drain enabled follow mode")
		}
		if calls == 2 {
			body := operationHelperEvent(8, "stdout", `{"value":"tail","encoding":"ascii"}`) + "event: sse_error\ndata: retry\n\n"
			return operationHelperJSONResponse(request, 200, body), nil
		}
		if request.Header.Get("Last-Event-Id") != "8" {
			t.Fatal("drain lost cursor")
		}
		return operationHelperJSONResponse(request, 200, operationHelperEvent(9, "completion", `{"status":"SUCCESS","duration_ms":1}`)), nil
	})
	client.retry = &RetryPolicy{MaxAttempts: 2, Delays: []time.Duration{0}}
	var ids []int64
	for event, err := range client.FollowOperationEvents(context.Background(), operationHelperID, nil) {
		if err != nil {
			t.Fatal(err)
		}
		ids = append(ids, event.ID)
	}
	if fmt.Sprint(ids) != "[8 9]" || calls != 3 {
		t.Fatalf("ids=%v calls=%d", ids, calls)
	}
}

func TestOperationEventFollowerCancellationStopsDrainRetry(t *testing.T) {
	t.Parallel()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	calls := 0
	client := newOperationHelperClient(t, func(request *http.Request) (*http.Response, error) {
		if !strings.HasSuffix(request.URL.Path, "/events") {
			return operationHelperJSONResponse(request, 200, `{"status":"SUCCESS"}`), nil
		}
		calls++
		if calls == 2 {
			cancel()
		}
		return operationHelperJSONResponse(request, 200, ""), nil
	})
	var lastErr error
	for _, err := range client.FollowOperationEvents(ctx, operationHelperID, nil) {
		lastErr = err
	}
	if !errors.Is(lastErr, context.Canceled) || calls != 2 {
		t.Fatalf("calls=%d error=%v", calls, lastErr)
	}
}
