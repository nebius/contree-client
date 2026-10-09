package contree

import (
	"bufio"
	"bytes"
	"compress/flate"
	"compress/gzip"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"iter"
	"net/http"
	"strconv"
	"strings"
)

const maxSSEFrameBytes = 4 << 20

// SSEError reports an explicit sse_error frame from the server.
type SSEError struct {
	Message     string
	LastEventID int64
	HasEventID  bool
}

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
		var corrupt flate.CorruptInputError
		if errors.Is(err, gzip.ErrChecksum) || errors.Is(err, gzip.ErrHeader) || errors.As(err, &corrupt) {
			return nil, err
		}
		return nil, connectionError(s.method, s.url, err)
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
