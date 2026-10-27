package contree

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"iter"
	"net/http"
	"strconv"
	"strings"
	"time"
)

const maxSSEFrameBytes = 4 << 20

// SSEError reports an explicit sse_error frame from the server.
type SSEError struct {
	Message     string
	LastEventID int64
	HasEventID  bool
}

func (*SSEError) apiError() {}

func (e *SSEError) Error() string {
	if e.HasEventID {
		return fmt.Sprintf(
			"contree: event stream error after event %d: %s",
			e.LastEventID,
			e.Message,
		)
	}
	return "contree: event stream error: " + e.Message
}

// eventStream incrementally decodes operation events from an HTTP SSE body.
// Call Close when iteration stops before io.EOF.
type eventStream struct {
	scanner *bufio.Scanner
	body    io.ReadCloser
	ctx     context.Context
	method  string
	url     string

	event       string
	eventID     int64
	hasEventID  bool
	data        []string
	pendingSize int
	dirty       bool
	closed      bool
	closeErr    error
	lastID      int64
	hasLastID   bool
}

// eventSequence opens and closes a new response for each traversal.
func eventSequence(open func() (*eventStream, error)) iter.Seq2[OperationEvent, error] {
	return func(yield func(OperationEvent, error) bool) {
		stream, err := open()
		if err != nil {
			yield(OperationEvent{}, err)
			return
		}
		defer stream.Close()
		for {
			event, err := stream.Next()
			if errors.Is(err, io.EOF) {
				if err := stream.Close(); err != nil {
					yield(OperationEvent{}, err)
				}
				return
			}
			if err != nil {
				yield(OperationEvent{}, err)
				return
			}
			if !yield(*event, nil) {
				return
			}
		}
	}
}

func newEventStream(
	response *http.Response,
	policy statusPolicy,
) (*eventStream, error) {
	body, err := streamResponse(response, policy)
	if err != nil {
		return nil, err
	}
	scanner := bufio.NewScanner(body)
	scanner.Buffer(make([]byte, 64<<10), maxSSEFrameBytes+1)
	return &eventStream{
		scanner: scanner,
		body:    body,
		ctx:     response.Request.Context(),
		method:  response.Request.Method,
		url:     response.Request.URL.String(),
	}, nil
}

// Next returns the next payload event. Keepalive and empty frames are skipped.
func (s *eventStream) Next() (*OperationEvent, error) {
	if s == nil || s.closed {
		return nil, io.EOF
	}
	select {
	case <-s.ctx.Done():
		_ = s.Close()
		return nil, connectionError(s.method, s.url, s.ctx.Err())
	default:
	}
	for s.scanner.Scan() {
		line := strings.TrimSuffix(s.scanner.Text(), "\r")
		if line == "" {
			event, err := s.flush()
			if err != nil {
				_ = s.Close()
				return nil, err
			}
			if event != nil {
				return event, err
			}
			continue
		}
		if strings.HasPrefix(line, ":") {
			continue
		}
		s.pendingSize += len(line) + 1
		if s.pendingSize > maxSSEFrameBytes {
			_ = s.Close()
			return nil, fmt.Errorf(
				"contree: SSE frame exceeds %d bytes",
				maxSSEFrameBytes,
			)
		}
		name, value, found := strings.Cut(line, ":")
		if !found {
			value = ""
		} else {
			value = strings.TrimPrefix(value, " ")
		}
		s.dirty = true
		switch name {
		case "id":
			parsed, err := strconv.ParseInt(value, 10, 64)
			if err != nil {
				s.hasEventID = false
			} else {
				s.eventID = parsed
				s.hasEventID = true
			}
		case "event":
			s.event = value
		case "data":
			s.data = append(s.data, value)
		}
	}
	if err := s.scanner.Err(); err != nil {
		_ = s.Close()
		if errors.Is(err, bufio.ErrTooLong) {
			return nil, fmt.Errorf(
				"contree: SSE line exceeds %d bytes",
				maxSSEFrameBytes,
			)
		}
		return nil, normalizeResponseReadError(s.method, s.url, err)
	}
	_ = s.Close()
	return nil, io.EOF
}

// LastEventID returns the most recent parsed SSE or payload event ID.
func (s *eventStream) LastEventID() (int64, bool) {
	if s == nil {
		return 0, false
	}
	return s.lastID, s.hasLastID
}

// Close releases the response body. It is safe to call more than once.
func (s *eventStream) Close() error {
	if s == nil {
		return nil
	}
	if !s.closed {
		s.closed = true
		s.closeErr = s.body.Close()
	}
	return s.closeErr
}

func (s *eventStream) flush() (*OperationEvent, error) {
	if !s.dirty {
		return nil, nil
	}
	eventName := s.event
	eventID := s.eventID
	hasEventID := s.hasEventID
	data := strings.Join(s.data, "\n")
	s.resetFrame()
	if hasEventID {
		s.lastID = eventID
		s.hasLastID = true
	}
	if eventName == "sse_error" {
		return nil, &SSEError{
			Message:     data,
			LastEventID: s.lastID,
			HasEventID:  s.hasLastID,
		}
	}
	if data == "" {
		return nil, nil
	}
	encoded := bytes.TrimSpace([]byte(data))
	if !json.Valid(encoded) {
		return nil, fmt.Errorf("contree: decode SSE JSON payload: invalid JSON")
	}
	if len(encoded) == 0 || encoded[0] != '{' {
		return nil, nil
	}
	var event OperationEvent
	if err := json.Unmarshal(encoded, &event); err != nil {
		return nil, fmt.Errorf("contree: decode SSE JSON payload: %w", err)
	}
	if !hasEventID && (event.ID != 0 || !s.hasLastID) {
		s.lastID = event.ID
		s.hasLastID = true
	}
	return &event, nil
}

func (s *eventStream) resetFrame() {
	s.event = ""
	s.eventID = 0
	s.hasEventID = false
	s.data = nil
	s.pendingSize = 0
	s.dirty = false
}

var _ error = (*SSEError)(nil)
var _ io.Closer = (*eventStream)(nil)

// streamIdleError distinguishes a stalled connection from caller cancellation.
type streamIdleError struct{}

func (*streamIdleError) Error() string   { return "SSE stream inactivity timeout" }
func (*streamIdleError) Timeout() bool   { return true }
func (*streamIdleError) Temporary() bool { return true }

var errStreamIdle = &streamIdleError{}

// idleStreamBody times each transport read before gzip and SSE decoding.
// A ping or any other bytes complete a read and renew the inactivity budget.
// No timer runs while the consumer processes a yielded event.
type idleStreamBody struct {
	io.ReadCloser
	ctx     context.Context
	cancel  context.CancelCauseFunc
	timeout time.Duration
}

func (b *idleStreamBody) Read(data []byte) (int, error) {
	timer := time.AfterFunc(b.timeout, func() { b.cancel(errStreamIdle) })
	defer timer.Stop()
	n, err := b.ReadCloser.Read(data)
	if errors.Is(context.Cause(b.ctx), errStreamIdle) {
		return n, errStreamIdle
	}
	return n, err
}

func (b *idleStreamBody) Close() error {
	b.cancel(nil)
	return b.ReadCloser.Close()
}

func (c *Client) openEventStream(ctx context.Context, spec requestSpec, policy statusPolicy, idleTimeout time.Duration) (*eventStream, error) {
	if idleTimeout == 0 {
		response, err := c.doStream(ctx, spec)
		if err != nil {
			return nil, err
		}
		return newEventStream(response, policy)
	}
	streamContext, cancel := context.WithCancelCause(ctx)
	timer := time.AfterFunc(idleTimeout, func() { cancel(errStreamIdle) })
	response, err := c.doStream(streamContext, spec)
	timer.Stop()
	if err != nil {
		if errors.Is(context.Cause(streamContext), errStreamIdle) {
			err = &APIConnectionError{Err: errStreamIdle, TimedOut: true}
		}
		cancel(nil)
		return nil, err
	}
	if response != nil && response.Body != nil {
		response.Body = &idleStreamBody{ReadCloser: response.Body, ctx: streamContext, cancel: cancel, timeout: idleTimeout}
	}
	stream, err := newEventStream(response, policy)
	if err != nil {
		cancel(nil)
	}
	return stream, err
}
