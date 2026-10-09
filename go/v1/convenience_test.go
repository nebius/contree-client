package contree

import (
	"bytes"
	"context"
	"crypto/sha256"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"testing"
	"time"
)

const (
	convenienceFileUUID  = "a9165a5d-5c86-4bd8-8ee4-ae46c19cf45d"
	convenienceImageUUID = "f85d16e5-43ef-42ad-86bc-06f56518c443"
)

type convenienceRoundTripFunc func(*http.Request) (*http.Response, error)

func (function convenienceRoundTripFunc) RoundTrip(
	request *http.Request,
) (*http.Response, error) {
	return function(request)
}

func newConvenienceClient(
	t *testing.T,
	transport convenienceRoundTripFunc,
) *Client {
	t.Helper()
	client, err := NewClient(
		"",
		WithBaseURL("https://contree.test"),
		WithHTTPClient(&http.Client{Transport: transport}),
	)
	if err != nil {
		t.Fatal(err)
	}
	return client
}

func convenienceResponse(
	request *http.Request,
	status int,
	body string,
) *http.Response {
	return &http.Response{
		StatusCode: status,
		Header:     make(http.Header),
		Body:       io.NopCloser(strings.NewReader(body)),
		Request:    request,
	}
}

func storedFileJSON(digest string, size int) string {
	return fmt.Sprintf(
		`{"uuid":%q,"sha256":%q,"size":%d,`+
			`"created_at":"2024-01-01T12:00:00Z",`+
			`"updated_at":"2024-01-01T12:00:00Z"}`,
		convenienceFileUUID,
		digest,
		size,
	)
}

func uploadedFileJSON(digest string, size int) string {
	return fmt.Sprintf(
		`{"uuid":%q,"sha256":%q,"size":%d}`,
		convenienceFileUUID,
		digest,
		size,
	)
}

func TestEnsureFileReturnsExistingFileAndRestoresOSFile(t *testing.T) {
	payload := []byte("hello world\n")
	digest := fmt.Sprintf("%x", sha256.Sum256(payload))
	file, err := os.CreateTemp(t.TempDir(), "contree-upload-*")
	if err != nil {
		t.Fatal(err)
	}
	defer file.Close()
	if _, err := file.Write(append([]byte("skip"), payload...)); err != nil {
		t.Fatal(err)
	}
	if _, err := file.Seek(4, io.SeekStart); err != nil {
		t.Fatal(err)
	}

	calls := 0
	client := newConvenienceClient(t, func(request *http.Request) (*http.Response, error) {
		calls++
		if request.Method != http.MethodGet {
			t.Errorf("method = %q", request.Method)
		}
		if request.URL.Path != "/v1/files/"+digest {
			t.Errorf("path = %q", request.URL.Path)
		}
		return convenienceResponse(
			request,
			http.StatusOK,
			storedFileJSON(digest, len(payload)),
		), nil
	})

	result, err := client.EnsureFile(context.Background(), file, nil)
	if err != nil {
		t.Fatal(err)
	}
	if result.UUID != convenienceFileUUID ||
		result.SHA256 != digest ||
		result.Size != int64(len(payload)) {
		t.Fatalf("result = %#v", result)
	}
	createdAt, hasCreatedAt := result.CreatedAt.Value()
	updatedAt, hasUpdatedAt := result.UpdatedAt.Value()
	wantTimestamp := time.Date(2024, time.January, 1, 12, 0, 0, 0, time.UTC)
	if !hasCreatedAt || !createdAt.Equal(wantTimestamp) ||
		!hasUpdatedAt || !updatedAt.Equal(wantTimestamp) {
		t.Fatalf("timestamps = %#v, %#v", result.CreatedAt, result.UpdatedAt)
	}
	position, err := file.Seek(0, io.SeekCurrent)
	if err != nil {
		t.Fatal(err)
	}
	if position != 4 {
		t.Fatalf("position = %d, want 4", position)
	}
	if calls != 1 {
		t.Fatalf("calls = %d, want 1", calls)
	}
}

func TestEnsureFileUploadsOnlyAfterNotFound(t *testing.T) {
	payload := []byte("new content")
	digest := fmt.Sprintf("%x", sha256.Sum256(payload))
	calls := 0
	client := newConvenienceClient(t, func(request *http.Request) (*http.Response, error) {
		calls++
		switch calls {
		case 1:
			if request.Method != http.MethodGet {
				t.Errorf("first method = %q", request.Method)
			}
			return convenienceResponse(request, http.StatusNotFound, `{"error":"missing"}`), nil
		case 2:
			if request.Method != http.MethodPost {
				t.Errorf("second method = %q", request.Method)
			}
			body, err := io.ReadAll(request.Body)
			if err != nil {
				t.Error(err)
			}
			if !bytes.Equal(body, payload) {
				t.Errorf("upload body = %q", body)
			}
			return convenienceResponse(
				request,
				http.StatusCreated,
				uploadedFileJSON(digest, len(payload)),
			), nil
		default:
			t.Errorf("unexpected request %d", calls)
			return convenienceResponse(request, http.StatusInternalServerError, ""), nil
		}
	})

	result, err := client.EnsureFile(
		context.Background(),
		bytes.NewReader(payload),
		nil,
	)
	if err != nil {
		t.Fatal(err)
	}
	if result.SHA256 != digest || result.Size != int64(len(payload)) {
		t.Fatalf("result = %#v", result)
	}
	if result.CreatedAt.IsSet() || result.UpdatedAt.IsSet() {
		t.Fatalf("upload result has timestamps: %#v", result)
	}
	if calls != 2 {
		t.Fatalf("calls = %d, want 2", calls)
	}
}

func TestEnsureFileUsesExplicitDigestWithoutReading(t *testing.T) {
	digest := strings.Repeat("f", 64)
	readerError := errors.New("reader must not be used")
	content := &failingReadSeeker{readErr: readerError, seekErr: readerError}
	client := newConvenienceClient(t, func(request *http.Request) (*http.Response, error) {
		if request.URL.Path != "/v1/files/"+digest {
			t.Errorf("path = %q", request.URL.Path)
		}
		return convenienceResponse(request, http.StatusOK, storedFileJSON(digest, 8)), nil
	})

	result, err := client.EnsureFile(
		context.Background(),
		content,
		&EnsureFileOptions{SHA256: digest},
	)
	if err != nil {
		t.Fatal(err)
	}
	if result.SHA256 != digest {
		t.Fatalf("SHA256 = %q", result.SHA256)
	}
}

func TestEnsureFileUploadsNonSeekableReaderDirectly(t *testing.T) {
	payload := []byte("streamed content")
	digest := fmt.Sprintf("%x", sha256.Sum256(payload))
	content := struct{ io.Reader }{Reader: bytes.NewReader(payload)}
	calls := 0
	client := newConvenienceClient(t, func(request *http.Request) (*http.Response, error) {
		calls++
		if request.Method != http.MethodPost || request.URL.Path != "/v1/files" {
			t.Errorf("request = %s %s", request.Method, request.URL.Path)
		}
		body, err := io.ReadAll(request.Body)
		if err != nil {
			t.Error(err)
		}
		if !bytes.Equal(body, payload) {
			t.Errorf("upload body = %q", body)
		}
		return convenienceResponse(
			request,
			http.StatusCreated,
			uploadedFileJSON(digest, len(payload)),
		), nil
	})

	result, err := client.EnsureFile(context.Background(), content, nil)
	if err != nil {
		t.Fatal(err)
	}
	if result.SHA256 != digest || calls != 1 {
		t.Fatalf("result = %#v, calls = %d", result, calls)
	}
}

func TestEnsureFilePropagatesNonNotFoundError(t *testing.T) {
	client := newConvenienceClient(t, func(request *http.Request) (*http.Response, error) {
		if request.Method != http.MethodGet {
			t.Errorf("unexpected method %q", request.Method)
		}
		return convenienceResponse(request, http.StatusForbidden, `{"error":"denied"}`), nil
	})

	_, err := client.EnsureFile(
		context.Background(),
		bytes.NewReader([]byte("content")),
		nil,
	)
	var denied *PermissionDeniedError
	if !errors.As(err, &denied) {
		t.Fatalf("error = %T %v", err, err)
	}
}

func TestEnsureFileReportsHashAndSeekErrors(t *testing.T) {
	readErr := errors.New("read failed")
	positionErr := errors.New("position failed")
	restoreErr := errors.New("restore failed")
	tests := []struct {
		name    string
		content io.Reader
		want    error
	}{
		{
			name:    "hash",
			content: &failingReadSeeker{readErr: readErr},
			want:    readErr,
		},
		{
			name:    "position",
			content: &failingReadSeeker{seekErr: positionErr},
			want:    positionErr,
		},
		{
			name:    "restore",
			content: &restoreFailingReader{err: restoreErr},
			want:    restoreErr,
		},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			client := newConvenienceClient(t, func(request *http.Request) (*http.Response, error) {
				t.Fatalf("unexpected request %s", request.URL)
				return nil, nil
			})
			_, err := client.EnsureFile(context.Background(), test.content, nil)
			if !errors.Is(err, test.want) {
				t.Fatalf("error = %v, want %v", err, test.want)
			}
		})
	}
}

func TestResolveImageAcceptsUUIDAndTags(t *testing.T) {
	queries := make([]string, 0, 2)
	client := newConvenienceClient(t, func(request *http.Request) (*http.Response, error) {
		queries = append(queries, request.URL.Query().Get("tag"))
		response := convenienceResponse(request, http.StatusFound, "")
		response.Header.Set("Location", "/v1/inspect/"+convenienceImageUUID+"/")
		return response, nil
	})

	resolved, err := client.ResolveImage(context.Background(), convenienceImageUUID)
	if err != nil || resolved != convenienceImageUUID {
		t.Fatalf("UUID: resolved = %q, error = %v", resolved, err)
	}
	resolved, err = client.ResolveImage(context.Background(), "tag:busybox:latest")
	if err != nil || resolved != convenienceImageUUID {
		t.Fatalf("prefixed tag: resolved = %q, error = %v", resolved, err)
	}
	resolved, err = client.ResolveImage(context.Background(), "busybox:latest")
	if err != nil || resolved != convenienceImageUUID {
		t.Fatalf("bare tag: resolved = %q, error = %v", resolved, err)
	}
	if len(queries) != 2 || queries[0] != "busybox:latest" || queries[1] != "busybox:latest" {
		t.Fatalf("tag queries = %#v", queries)
	}
}

type failingReadSeeker struct {
	readErr error
	seekErr error
}

func (reader *failingReadSeeker) Read([]byte) (int, error) {
	return 0, reader.readErr
}

func (reader *failingReadSeeker) Seek(int64, int) (int64, error) {
	if reader.seekErr != nil {
		return 0, reader.seekErr
	}
	return 0, nil
}

type restoreFailingReader struct {
	seekCalls int
	err       error
}

func (*restoreFailingReader) Read([]byte) (int, error) {
	return 0, io.EOF
}

func (reader *restoreFailingReader) Seek(int64, int) (int64, error) {
	reader.seekCalls++
	if reader.seekCalls == 1 {
		return 3, nil
	}
	return 0, reader.err
}
