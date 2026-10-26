package contree

import (
	"context"
	"errors"
	"io"
	"net/http"
	"strings"
	"testing"
)

func TestEventStreamDecodesFramesAndSkipsComments(t *testing.T) {
	t.Parallel()

	source := strings.Join([]string{
		": keepalive\r",
		"data: []\r",
		"\r",
		"id: 4\r",
		"event: operation\r",
		`data: {"id":4,"ts":"2026-09-03T12:00:00Z","type":"completion",`,
		`data: "data":{"status":"SUCCESS","duration_ms":5}}`,
		"\r",
		"",
	}, "\n")
	body := &trackingBody{Reader: strings.NewReader(source)}
	stream, err := newEventStream(streamResponseForTest(body), acceptAny2xx)
	if err != nil {
		t.Fatal(err)
	}
	event, err := stream.Next()
	if err != nil {
		t.Fatal(err)
	}
	if event.ID != 4 || event.Type != OperationEventTypeCompletion {
		t.Fatalf("event = %#v", event)
	}
	completion, ok := event.Data.(EventDataCompletion)
	if !ok || completion.Status != OperationStatusSuccess || completion.DurationMs != 5 {
		t.Fatalf("event data = %#v", event.Data)
	}
	if id, ok := stream.LastEventID(); !ok || id != 4 {
		t.Fatalf("last event ID = %d, %v", id, ok)
	}
	if _, err := stream.Next(); !errors.Is(err, io.EOF) {
		t.Fatalf("second Next error = %v", err)
	}
	if !body.closed {
		t.Fatal("stream body was not closed at EOF")
	}
}

func TestEventStreamTracksIDOnlyFrame(t *testing.T) {
	t.Parallel()

	stream, err := newEventStream(
		streamResponseForTest(io.NopCloser(strings.NewReader("id: 17\n\n"))),
		acceptAny2xx,
	)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := stream.Next(); !errors.Is(err, io.EOF) {
		t.Fatalf("Next error = %v", err)
	}
	if id, ok := stream.LastEventID(); !ok || id != 17 {
		t.Fatalf("last event ID = %d, %v", id, ok)
	}
}

func TestEventStreamReturnsSSEErrorWithCursor(t *testing.T) {
	t.Parallel()

	stream, err := newEventStream(
		streamResponseForTest(io.NopCloser(strings.NewReader(
			"id: 9\nevent: sse_error\ndata: backend stopped\n\n",
		))),
		acceptAny2xx,
	)
	if err != nil {
		t.Fatal(err)
	}
	_, err = stream.Next()
	var streamError *SSEError
	if !errors.As(err, &streamError) {
		t.Fatalf("Next error = %T, want *SSEError", err)
	}
	if !streamError.HasEventID || streamError.LastEventID != 9 {
		t.Fatalf("SSE error = %#v", streamError)
	}
}

func TestEventStreamDiscardsUnterminatedFrame(t *testing.T) {
	t.Parallel()

	stream, err := newEventStream(
		streamResponseForTest(io.NopCloser(strings.NewReader(
			`data: {"id":1,"ts":"2026-09-03T12:00:00Z","type":"completion","data":{"status":"SUCCESS","duration_ms":1}}`,
		))),
		acceptAny2xx,
	)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := stream.Next(); !errors.Is(err, io.EOF) {
		t.Fatalf("Next error = %v", err)
	}
	if _, ok := stream.LastEventID(); ok {
		t.Fatal("unterminated frame updated the cursor")
	}
}

func TestEventStreamClosesAfterDecodeFailure(t *testing.T) {
	t.Parallel()

	body := &trackingBody{Reader: strings.NewReader("data: {invalid}\n\n")}
	stream, err := newEventStream(streamResponseForTest(body), acceptAny2xx)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := stream.Next(); err == nil {
		t.Fatal("malformed event succeeded")
	}
	if !body.closed {
		t.Fatal("stream body was not closed after a decode failure")
	}
}

func TestEventStreamRejectsOversizedAccumulatedFrame(t *testing.T) {
	t.Parallel()

	line := "data: " + strings.Repeat("x", 1024) + "\n"
	body := &trackingBody{Reader: strings.NewReader(strings.Repeat(line, 4097))}
	stream, err := newEventStream(streamResponseForTest(body), acceptAny2xx)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := stream.Next(); err == nil || !strings.Contains(err.Error(), "frame exceeds") {
		t.Fatalf("Next error = %v", err)
	}
	if !body.closed {
		t.Fatal("oversized frame did not close the body")
	}
}

func TestEventStreamChecksRequestContext(t *testing.T) {
	t.Parallel()

	ctx, cancel := context.WithCancel(context.Background())
	request, err := http.NewRequestWithContext(
		ctx,
		http.MethodGet,
		"https://example.test/v1/events",
		nil,
	)
	if err != nil {
		t.Fatal(err)
	}
	response := &http.Response{
		StatusCode: http.StatusOK,
		Header:     make(http.Header),
		Body:       io.NopCloser(strings.NewReader("")),
		Request:    request,
	}
	stream, err := newEventStream(response, acceptAny2xx)
	if err != nil {
		t.Fatal(err)
	}
	cancel()
	_, err = stream.Next()
	var connection *APIConnectionError
	if !errors.As(err, &connection) || !errors.Is(err, context.Canceled) {
		t.Fatalf("Next error = %#v", err)
	}
}

func streamResponseForTest(body io.ReadCloser) *http.Response {
	request, _ := http.NewRequest(
		http.MethodGet,
		"https://example.test/v1/events",
		nil,
	)
	return &http.Response{
		StatusCode: http.StatusOK,
		Header:     make(http.Header),
		Body:       body,
		Request:    request,
	}
}
