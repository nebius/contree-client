package contree

import (
	"context"
	"errors"
	"fmt"
	"io"
	"iter"
	"time"
)

const operationEventNoProgressDelay = 500 * time.Millisecond

// FollowOperationEventsOptions controls a reconnecting operation event stream.
// Live following switches to a finite retained-event read after terminal status.
type FollowOperationEventsOptions struct {
	SPID        *int64
	Since       *int64
	LastEventID *int64
}

// operationEventFollower pulls operation events and reconnects interrupted
// streams from the last received event ID. It is not safe for concurrent use.
type operationEventFollower struct {
	client      *Client
	ctx         context.Context
	operationID string

	spid       int64
	hasSPID    bool
	since      int64
	hasSince   bool
	lastID     int64
	hasLastID  bool
	attemptID  int64
	attemptHad bool

	draining      bool
	drainAttempts int
	allowDrainEOF bool

	stream   *eventStream
	event    *OperationEvent
	err      error
	finished bool
	closed   bool
}

// FollowOperationEvents lazily yields events and reconnects interrupted streams.
// Each traversal starts from the supplied options. A terminal error is yielded
// once with a zero event. Ending iteration closes the active response body.
func (c *Client) FollowOperationEvents(
	ctx context.Context,
	operationID string,
	options *FollowOperationEventsOptions,
) iter.Seq2[OperationEvent, error] {
	return func(yield func(OperationEvent, error) bool) {
		follower, err := c.newOperationEventFollower(ctx, operationID, options)
		if err != nil {
			yield(OperationEvent{}, err)
			return
		}
		defer follower.Close()
		for follower.Next() {
			if !yield(*follower.Event(), nil) {
				return
			}
		}
		if err := follower.Err(); err != nil {
			yield(OperationEvent{}, err)
		}
	}
}

func (c *Client) newOperationEventFollower(
	ctx context.Context,
	operationID string,
	options *FollowOperationEventsOptions,
) (*operationEventFollower, error) {
	if c == nil {
		return nil, errors.New("contree: client must not be nil")
	}
	if ctx == nil {
		return nil, errors.New("contree: context must not be nil")
	}
	follower := &operationEventFollower{
		client:      c,
		ctx:         ctx,
		operationID: operationID,
	}
	if options == nil {
		return follower, nil
	}
	follower.allowDrainEOF = options.SPID != nil || options.Since != nil || options.LastEventID != nil
	if options.SPID != nil {
		follower.spid = *options.SPID
		follower.hasSPID = true
	}
	if options.Since != nil {
		follower.since = *options.Since
		follower.hasSince = true
	}
	if options.LastEventID != nil {
		follower.lastID = *options.LastEventID
		follower.hasLastID = true
	}
	return follower, nil
}

// Next advances to the next operation event. It returns false after completion,
// a retained-event drain, cancellation, or an unrecoverable error.
func (f *operationEventFollower) Next() bool {
	if f == nil {
		return false
	}
	f.event = nil
	if f.finished || f.closed || f.err != nil {
		return false
	}
	for {
		if err := f.ctx.Err(); err != nil {
			f.finish(err)
			return false
		}
		if f.stream == nil {
			if err := f.openStream(); err != nil {
				if !transientOperationStreamError(err) {
					f.finish(err)
					return false
				}
				if !f.recoverStream(err) {
					return false
				}
				continue
			}
		}

		// Preserve the cursor before probing status or retrying a retained read.
		event, streamErr := f.stream.Next()
		f.captureLastEventID()
		if event != nil {
			f.event = event
			if event.Type == OperationEventTypeCompletion {
				f.finish(nil)
			}
			return true
		}
		_ = f.closeStream()
		if streamErr != nil && !transientOperationStreamError(streamErr) {
			f.finish(streamErr)
			return false
		}
		if !f.recoverStream(streamErr) {
			return false
		}
	}
}

// Event returns the event produced by the most recent successful Next call.
func (f *operationEventFollower) Event() *OperationEvent {
	if f == nil {
		return nil
	}
	return f.event
}

// Err returns the error that stopped iteration. Normal completion returns nil.
func (f *operationEventFollower) Err() error {
	if f == nil {
		return nil
	}
	return f.err
}

// LastEventID returns the latest SSE or payload event ID accepted by the
// follower.
func (f *operationEventFollower) LastEventID() (int64, bool) {
	if f == nil {
		return 0, false
	}
	return f.lastID, f.hasLastID
}

// Close stops iteration and releases the active response body.
func (f *operationEventFollower) Close() error {
	if f == nil || f.closed {
		return nil
	}
	f.closed = true
	f.finished = true
	return f.closeStream()
}

func (f *operationEventFollower) openStream() error {
	f.attemptID = f.lastID
	f.attemptHad = f.hasLastID
	if f.draining {
		f.drainAttempts++
	}
	options := &IterOperationEventsOptions{Follow: !f.draining}
	if f.hasSPID {
		value := f.spid
		options.Spid = &value
	}
	if f.hasSince {
		value := f.since
		options.Since = &value
	}
	if f.hasLastID {
		value := f.lastID
		options.LastEventID = &value
	}
	stream, err := f.client.openIterOperationEvents(f.ctx, f.operationID, options)
	if err != nil {
		return err
	}
	if stream == nil {
		return errors.New("contree: event stream is nil")
	}
	f.stream = stream
	return nil
}

func (f *operationEventFollower) captureLastEventID() {
	if f.stream == nil {
		return
	}
	if eventID, ok := f.stream.LastEventID(); ok {
		f.lastID = eventID
		f.hasLastID = true
	}
}

func (f *operationEventFollower) madeProgress() bool {
	if f.hasLastID != f.attemptHad {
		return true
	}
	return f.hasLastID && f.lastID != f.attemptID
}

func (f *operationEventFollower) recoverStream(streamErr error) bool {
	if err := f.ctx.Err(); err != nil {
		f.finish(err)
		return false
	}
	if f.draining {
		if errors.Is(streamErr, io.EOF) && f.allowDrainEOF {
			f.finish(nil)
			return false
		}
		if streamErr == nil || errors.Is(streamErr, io.EOF) {
			streamErr = &APIConnectionError{Err: fmt.Errorf("retained operation events ended before completion: %w", io.ErrUnexpectedEOF)}
		}
		attempts := 3
		delay := operationEventNoProgressDelay
		if f.client.retry != nil {
			attempts = f.client.retry.MaxAttempts
			delay = retryDelay(f.client.retry, f.drainAttempts, 0, false)
		}
		if f.drainAttempts >= attempts {
			f.finish(streamErr)
			return false
		}
		if err := waitForRetry(f.ctx, delay); err != nil {
			f.finish(err)
			return false
		}
		return true
	}
	terminal, err := f.operationTerminal()
	if err != nil {
		f.finish(err)
		return false
	}
	if terminal {
		f.draining = true
		return true
	}
	if !f.madeProgress() {
		if err := waitForRetry(f.ctx, operationEventNoProgressDelay); err != nil {
			f.finish(err)
			return false
		}
	}
	return true
}

func (f *operationEventFollower) operationTerminal() (bool, error) {
	return f.client.OperationTerminal(f.ctx, f.operationID)
}

// OperationTerminal reports whether an operation has reached a terminal
// status. Transient transport and server failures return false without an
// error, so callers can retry the probe.
func (c *Client) OperationTerminal(
	ctx context.Context,
	operationID string,
) (bool, error) {
	if ctx == nil {
		return false, errors.New("contree: context must not be nil")
	}
	operation, err := c.GetOperationStatus(ctx, operationID, nil)
	if err != nil {
		if contextErr := ctx.Err(); contextErr != nil {
			return false, contextErr
		}
		if transientOperationProbeError(err) {
			return false, nil
		}
		return false, err
	}
	status, ok := operation.Status.Value()
	if !ok {
		return false, nil
	}
	return status.IsTerminal(), nil
}

func transientOperationStreamError(err error) bool {
	if errors.Is(err, io.EOF) {
		return true
	}
	var streamError *SSEError
	if errors.As(err, &streamError) {
		return true
	}
	return transientOperationProbeError(err)
}

func transientOperationProbeError(err error) bool {
	var connectionError *APIConnectionError
	if errors.As(err, &connectionError) {
		return true
	}
	var statusError *APIStatusError
	if !errors.As(err, &statusError) {
		return false
	}
	return statusError.StatusCode == 410 ||
		statusError.StatusCode == 425 ||
		statusError.StatusCode == 429 ||
		(statusError.StatusCode >= 500 && statusError.StatusCode < 600)
}

func (f *operationEventFollower) closeStream() error {
	if f.stream == nil {
		return nil
	}
	stream := f.stream
	f.stream = nil
	return stream.Close()
}

func (f *operationEventFollower) finish(err error) {
	if f.finished {
		return
	}
	f.finished = true
	closeErr := f.closeStream()
	if err != nil {
		f.err = err
	} else if closeErr != nil {
		f.err = closeErr
	}
}

// WaitOperation waits for operation completion and returns its final status.
// The context bounds event following, status probes, and the final request.
func (c *Client) WaitOperation(
	ctx context.Context,
	operationID string,
) (OperationResponse, error) {
	var zero OperationResponse
	for _, err := range c.FollowOperationEvents(ctx, operationID, nil) {
		if err != nil {
			return zero, err
		}
	}
	return c.GetOperationStatus(ctx, operationID, nil)
}

var _ io.Closer = (*operationEventFollower)(nil)
