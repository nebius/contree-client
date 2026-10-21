package contree

import (
	"bytes"
	"compress/gzip"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestGeneratedListImagesUsesWireQueryAndDecodesModel(t *testing.T) {
	t.Parallel()

	requests := make(chan *http.Request, 1)
	server := httptest.NewServer(http.HandlerFunc(func(
		writer http.ResponseWriter,
		request *http.Request,
	) {
		requests <- request.Clone(request.Context())
		writer.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(writer, `{"images":[{"uuid":"image-1"}]}`)
	}))
	defer server.Close()

	client, err := NewClient("", WithBaseURL(server.URL))
	if err != nil {
		t.Fatal(err)
	}
	tag := "team/base image"
	response, err := client.ListImages(context.Background(), &ListImagesOptions{
		Tagged: true,
		Tag:    &tag,
	})
	if err != nil {
		t.Fatal(err)
	}
	request := <-requests
	if request.URL.Path != "/v1/images" {
		t.Fatalf("path = %q", request.URL.Path)
	}
	if request.URL.RawQuery != "tag=team/base%20image&tagged=1" {
		t.Fatalf("query = %q", request.URL.RawQuery)
	}
	images, ok := response.Images.Value()
	if !ok || len(images) != 1 {
		t.Fatalf("images = %#v, %v", images, ok)
	}
	imageID, ok := images[0].UUID.Value()
	if !ok || imageID != "image-1" {
		t.Fatalf("image UUID = %q, %v", imageID, ok)
	}
}

func TestGeneratedSpawnInstancePreservesOptionalJSONStates(t *testing.T) {
	t.Parallel()

	bodies := make(chan map[string]json.RawMessage, 1)
	server := httptest.NewServer(http.HandlerFunc(func(
		writer http.ResponseWriter,
		request *http.Request,
	) {
		var body map[string]json.RawMessage
		if err := json.NewDecoder(request.Body).Decode(&body); err != nil {
			t.Error(err)
			return
		}
		bodies <- body
		writer.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(writer, `{}`)
	}))
	defer server.Close()

	client, err := NewClient("", WithBaseURL(server.URL))
	if err != nil {
		t.Fatal(err)
	}
	_, err = client.SpawnInstanceWithResponse(
		context.Background(),
		"printf ok",
		"ubuntu:latest",
		&SpawnInstanceWithResponseOptions{
			Hostname: Null[string](),
			Shell:    Some(false),
		},
	)
	if err != nil {
		t.Fatal(err)
	}
	body := <-bodies
	if string(body["command"]) != `"printf ok"` {
		t.Fatalf("command = %s", body["command"])
	}
	if string(body["image"]) != `"ubuntu:latest"` {
		t.Fatalf("image = %s", body["image"])
	}
	if string(body["hostname"]) != "null" {
		t.Fatalf("hostname = %s", body["hostname"])
	}
	if string(body["shell"]) != "false" {
		t.Fatalf("shell = %s", body["shell"])
	}
	if _, exists := body["timeout"]; exists {
		t.Fatal("unset timeout was encoded")
	}
}

func TestGeneratedImageIteratorIsLazyAndBoundsEachPage(t *testing.T) {
	var queries []string
	server := httptest.NewServer(http.HandlerFunc(func(
		writer http.ResponseWriter,
		request *http.Request,
	) {
		queries = append(queries, request.URL.RawQuery)
		writer.Header().Set("Content-Type", "application/json")
		if request.URL.Query().Get("offset") == "0" {
			_, _ = io.WriteString(
				writer,
				`{"images":[{"uuid":"one"},{"uuid":"two"}]}`,
			)
			return
		}
		_, _ = io.WriteString(writer, `{"images":[{"uuid":"three"}]}`)
	}))
	defer server.Close()

	client, err := NewClient("", WithBaseURL(server.URL))
	if err != nil {
		t.Fatal(err)
	}
	limit := int64(3)
	images := client.IterImages(context.Background(), &IterImagesOptions{
		PageSize: 2,
		Limit:    &limit,
	})
	if len(queries) != 0 {
		t.Fatal("iterator construction made a request")
	}
	var ids []string
	for image, err := range images {
		if err != nil {
			t.Fatal(err)
		}
		id, ok := image.UUID.Value()
		if !ok {
			t.Fatal("page item has no UUID")
		}
		ids = append(ids, id)
	}
	if strings.Join(ids, ",") != "one,two,three" {
		t.Fatalf("IDs = %#v", ids)
	}
	if strings.Join(queries, ",") != "limit=2&offset=0,limit=1&offset=2" {
		t.Fatalf("queries = %#v", queries)
	}
}

func TestGeneratedRawArchivePreservesTransportGzip(t *testing.T) {
	t.Parallel()

	var compressed bytes.Buffer
	writer := gzip.NewWriter(&compressed)
	if _, err := writer.Write([]byte("archive")); err != nil {
		t.Fatal(err)
	}
	if err := writer.Close(); err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(http.HandlerFunc(func(
		response http.ResponseWriter,
		request *http.Request,
	) {
		if request.Header.Get("Accept-Encoding") != "gzip" {
			t.Errorf("Accept-Encoding = %q", request.Header.Get("Accept-Encoding"))
		}
		response.Header().Set("Content-Encoding", "gzip")
		_, _ = response.Write(compressed.Bytes())
	}))
	defer server.Close()

	client, err := NewClient("", WithBaseURL(server.URL))
	if err != nil {
		t.Fatal(err)
	}
	body, err := client.InspectImageArchiveRaw(
		context.Background(),
		"image-id",
		"/",
	)
	if err != nil {
		t.Fatal(err)
	}
	data, err := io.ReadAll(body)
	if err != nil {
		t.Fatal(err)
	}
	if err := body.Close(); err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(data, compressed.Bytes()) {
		t.Fatalf("raw archive was transparently decoded: %q", data)
	}
}

func TestGeneratedByteStreamUsesCallerContextInsteadOfClientTimeout(t *testing.T) {
	t.Parallel()

	releaseBody := make(chan struct{})
	server := httptest.NewServer(http.HandlerFunc(func(
		response http.ResponseWriter,
		_ *http.Request,
	) {
		response.WriteHeader(http.StatusOK)
		response.(http.Flusher).Flush()
		<-releaseBody
		_, _ = io.WriteString(response, "archive")
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
	body, err := client.InspectImageArchive(context.Background(), "image-id", "/")
	if err != nil {
		close(releaseBody)
		t.Fatal(err)
	}
	time.Sleep(30 * time.Millisecond)
	close(releaseBody)
	data, err := io.ReadAll(body)
	if err != nil {
		t.Fatal(err)
	}
	if err := body.Close(); err != nil {
		t.Fatal(err)
	}
	if string(data) != "archive" {
		t.Fatalf("archive = %q", data)
	}
}
