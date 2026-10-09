package contree

import (
	"context"
	"errors"
	"time"
)

// SpawnInstanceOptions configures SpawnInstance. Zero scalar values and nil maps, slices,
// and pointers omit their fields so the server defaults apply. Non-nil empty
// maps and slices are sent as empty JSON objects and arrays.
//
// Use SpawnInstanceWithResponseOptions with SpawnInstanceWithResponse to send
// explicit zero or null values for fields that SpawnInstanceOptions would omit.
type SpawnInstanceOptions struct {
	Disposable  bool
	Hostname    string
	Args        []string
	Shell       bool
	Env         map[string]string
	PreserveEnv bool
	Cwd         string
	UID         int64
	GID         int64

	ResourcesLimits *InstanceResourcesLimits
	// Networking defaults to enabled. Set Enabled to Some(false) to disable it.
	Networking *InstanceNetworking
	Stdin      *ClosableStreamRepr

	// Timeout limits server execution, independently of the request context.
	// Zero uses the server default. Positive durations round up to whole seconds.
	// Negative durations return an error before sending a request.
	Timeout time.Duration
	// TruncateOutputAt limits captured bytes per stream. Nil uses the server
	// default (1 MiB); a pointer to zero sends an explicit zero.
	TruncateOutputAt *int64
	Files            map[string]FileSpec
}

// SpawnInstance starts an instance and returns its operation ID without waiting for
// execution to finish. Image accepts a tag:NAME reference or a bare image UUID.
// Pass SpawnInstanceOptions{} to use the server defaults.
//
// A successful HTTP response without a non-empty operation ID returns an error.
// The instance can already exist in that case; SpawnInstance does not repeat the call.
// Use SpawnInstanceWithResponse for the full response or exact JSON field states.
func (c *Client) SpawnInstance(
	ctx context.Context,
	command string,
	image string,
	options SpawnInstanceOptions,
) (string, error) {
	if options.Timeout < 0 {
		return "", errors.New("contree: spawn timeout must not be negative")
	}

	wire := SpawnInstanceWithResponseOptions{}
	if options.Disposable {
		wire.Disposable = Some(true)
	}
	if options.Hostname != "" {
		wire.Hostname = Some(options.Hostname)
	}
	if options.Args != nil {
		wire.Args = Some(options.Args)
	}
	if options.Shell {
		wire.Shell = Some(true)
	}
	if options.Env != nil {
		wire.Env = Some(options.Env)
	}
	if options.PreserveEnv {
		wire.PreserveEnv = Some(true)
	}
	if options.Cwd != "" {
		wire.Cwd = Some(options.Cwd)
	}
	if options.UID != 0 {
		wire.UID = Some(options.UID)
	}
	if options.GID != 0 {
		wire.GID = Some(options.GID)
	}
	if options.ResourcesLimits != nil {
		wire.ResourcesLimits = Some(*options.ResourcesLimits)
	}
	if options.Networking != nil {
		wire.Networking = Some(*options.Networking)
	}
	if options.Stdin != nil {
		wire.Stdin = Some(*options.Stdin)
	}
	if options.Timeout > 0 {
		seconds := int64(options.Timeout / time.Second)
		if options.Timeout%time.Second != 0 {
			seconds++
		}
		wire.Timeout = Some(seconds)
	}
	if options.TruncateOutputAt != nil {
		wire.TruncateOutputAt = Some(*options.TruncateOutputAt)
	}
	if options.Files != nil {
		wire.Files = Some(options.Files)
	}

	response, err := c.SpawnInstanceWithResponse(ctx, command, image, &wire)
	if err != nil {
		return "", err
	}
	operationID, ok := response.UUID.Value()
	if !ok || operationID == "" {
		return "", errors.New("contree: spawn response has no operation ID")
	}
	return operationID, nil
}
