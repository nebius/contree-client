package contree

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"reflect"
	"testing"
	"time"
)

const spawnOperationID = "bbb4f479-77c8-47a2-a460-0d729cf70baa"

func TestSpawnInstanceRequest(t *testing.T) {
	zero := int64(0)
	tests := []struct {
		name    string
		image   string
		options SpawnInstanceOptions
		body    string
	}{
		{
			name:  "server defaults",
			image: "tag:ubuntu:latest",
			body:  `{"command":"/bin/sh","image":"tag:ubuntu:latest"}`,
		},
		{
			name:  "common options",
			image: "tag:ubuntu:latest",
			options: SpawnInstanceOptions{
				Shell:      true,
				Env:        map[string]string{"LC_ALL": "C"},
				Cwd:        "/work",
				Timeout:    time.Minute,
				Disposable: true,
			},
			body: `{
				"command":"/bin/sh","image":"tag:ubuntu:latest",
				"shell":true,"env":{"LC_ALL":"C"},"cwd":"/work",
				"timeout":60,"disposable":true
			}`,
		},
		{
			name:  "advanced options and explicit false",
			image: convenienceImageUUID,
			options: SpawnInstanceOptions{
				Hostname:    "worker",
				Args:        []string{"-c", "cat"},
				PreserveEnv: true,
				UID:         1000,
				GID:         1001,
				ResourcesLimits: &InstanceResourcesLimits{
					MaxLayerBytes: Some(int64(1 << 30)),
				},
				Networking: &InstanceNetworking{Enabled: Some(false)},
				Stdin: &ClosableStreamRepr{
					Value: "input\n", Encoding: Some("ascii"), Close: Some(false),
				},
				TruncateOutputAt: &zero,
				Files: map[string]FileSpec{
					"/work/data.txt": {
						UUID: Some(convenienceFileUUID),
						Mode: Some(StringOrInt64FromInt64(0o644)),
					},
				},
			},
			body: `{
				"command":"/bin/sh","image":"f85d16e5-43ef-42ad-86bc-06f56518c443",
				"hostname":"worker","args":["-c","cat"],"preserve_env":true,
				"uid":1000,"gid":1001,"resources_limits":{"max_layer_bytes":1073741824},
				"networking":{"enabled":false},
				"stdin":{"value":"input\n","encoding":"ascii","close":false},
				"truncate_output_at":0,
				"files":{"/work/data.txt":{"uuid":"a9165a5d-5c86-4bd8-8ee4-ae46c19cf45d","mode":"0644"}}
			}`,
		},
		{
			name:  "non-nil empty collections and objects",
			image: "tag:ubuntu:latest",
			options: SpawnInstanceOptions{
				Args:            []string{},
				Env:             map[string]string{},
				Files:           map[string]FileSpec{},
				ResourcesLimits: &InstanceResourcesLimits{},
				Networking:      &InstanceNetworking{},
				Stdin:           &ClosableStreamRepr{},
			},
			body: `{
				"command":"/bin/sh","image":"tag:ubuntu:latest",
				"args":[],"env":{},"files":{},"resources_limits":{},
				"networking":{},"stdin":{"value":""}
			}`,
		},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			calls := 0
			client := newConvenienceClient(t, func(request *http.Request) (*http.Response, error) {
				calls++
				if request.Method != http.MethodPost || request.URL.Path != "/v1/instances" {
					t.Fatalf("request = %s %s", request.Method, request.URL.Path)
				}
				var got, want any
				if err := json.NewDecoder(request.Body).Decode(&got); err != nil {
					t.Fatal(err)
				}
				if err := json.Unmarshal([]byte(test.body), &want); err != nil {
					t.Fatal(err)
				}
				if !reflect.DeepEqual(got, want) {
					t.Errorf("request body = %#v, want %#v", got, want)
				}
				return convenienceResponse(request, http.StatusCreated, `{"uuid":"`+spawnOperationID+`"}`), nil
			})
			id, err := client.SpawnInstance(context.Background(), "/bin/sh", test.image, test.options)
			if err != nil || id != spawnOperationID {
				t.Fatalf("SpawnInstance = %q, %v", id, err)
			}
			if calls != 1 {
				t.Fatalf("requests = %d, want 1", calls)
			}
		})
	}
}

func TestSpawnInstanceTimeout(t *testing.T) {
	tests := []struct {
		timeout time.Duration
		seconds int64
	}{
		{time.Nanosecond, 1},
		{999 * time.Millisecond, 1},
		{time.Second, 1},
		{time.Second + time.Nanosecond, 2},
		{time.Minute, 60},
		{time.Duration(1<<63 - 1), 9223372037},
	}
	for _, test := range tests {
		t.Run(test.timeout.String(), func(t *testing.T) {
			client := newConvenienceClient(t, func(request *http.Request) (*http.Response, error) {
				var body struct {
					Timeout int64 `json:"timeout"`
				}
				if err := json.NewDecoder(request.Body).Decode(&body); err != nil {
					t.Fatal(err)
				}
				if body.Timeout != test.seconds {
					t.Errorf("timeout = %d, want %d", body.Timeout, test.seconds)
				}
				return convenienceResponse(request, http.StatusOK, `{"uuid":"`+spawnOperationID+`"}`), nil
			})
			if _, err := client.SpawnInstance(context.Background(), "true", convenienceImageUUID, SpawnInstanceOptions{
				Timeout: test.timeout,
			}); err != nil {
				t.Fatal(err)
			}
		})
	}

	t.Run("negative duration does not send a request", func(t *testing.T) {
		client := newConvenienceClient(t, func(*http.Request) (*http.Response, error) {
			t.Fatal("unexpected request")
			return nil, nil
		})
		id, err := client.SpawnInstance(context.Background(), "true", convenienceImageUUID, SpawnInstanceOptions{
			Timeout: -time.Nanosecond,
		})
		if err == nil || id != "" {
			t.Fatalf("SpawnInstance = %q, %v; want empty ID and error", id, err)
		}
	})
}

func TestSpawnInstanceRejectsInvalidResponses(t *testing.T) {
	for _, body := range []string{`{}`, `{"uuid":null}`, `{"uuid":""}`, `{"uuid":123}`, `invalid json`} {
		t.Run(body, func(t *testing.T) {
			calls := 0
			client := newConvenienceClient(t, func(request *http.Request) (*http.Response, error) {
				calls++
				return convenienceResponse(request, http.StatusCreated, body), nil
			})
			id, err := client.SpawnInstance(context.Background(), "true", convenienceImageUUID, SpawnInstanceOptions{})
			if err == nil || id != "" {
				t.Fatalf("SpawnInstance = %q, %v; want empty ID and error", id, err)
			}
			if calls != 1 {
				t.Fatalf("requests = %d, want 1", calls)
			}
		})
	}
}

func TestSpawnInstancePreservesErrors(t *testing.T) {
	t.Run("HTTP error", func(t *testing.T) {
		client := newConvenienceClient(t, func(request *http.Request) (*http.Response, error) {
			return convenienceResponse(request, http.StatusForbidden, `{"error":"denied"}`), nil
		})
		id, err := client.SpawnInstance(context.Background(), "true", convenienceImageUUID, SpawnInstanceOptions{})
		var denied *PermissionDeniedError
		if id != "" || !errors.As(err, &denied) || denied.StatusCode != http.StatusForbidden {
			t.Fatalf("SpawnInstance = %q, %v; want PermissionDeniedError", id, err)
		}
	})
	t.Run("transport error", func(t *testing.T) {
		client := newConvenienceClient(t, func(*http.Request) (*http.Response, error) {
			return nil, io.ErrUnexpectedEOF
		})
		id, err := client.SpawnInstance(context.Background(), "true", convenienceImageUUID, SpawnInstanceOptions{})
		var connection *APIConnectionError
		if id != "" || !errors.As(err, &connection) || !errors.Is(err, io.ErrUnexpectedEOF) {
			t.Fatalf("SpawnInstance = %q, %v; want wrapped transport error", id, err)
		}
	})
	t.Run("caller context", func(t *testing.T) {
		ctx, cancel := context.WithCancel(context.Background())
		defer cancel()
		client := newConvenienceClient(t, func(request *http.Request) (*http.Response, error) {
			cancel()
			return nil, request.Context().Err()
		})
		id, err := client.SpawnInstance(ctx, "true", convenienceImageUUID, SpawnInstanceOptions{})
		if id != "" || !errors.Is(err, context.Canceled) {
			t.Fatalf("SpawnInstance = %q, %v; want context cancellation", id, err)
		}
	})
}
