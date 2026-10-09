package contree_test

import (
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
)

// Compile and run the marked tutorial snippets themselves, so an edited method
// name, option, or error path cannot diverge from the published examples.
func TestTutorialExamples(t *testing.T) {
	doc, err := os.ReadFile(filepath.Join("..", "..", "docs", "tutorial.md"))
	if os.IsNotExist(err) {
		t.Skip("tutorial source is only available in the repository checkout")
	}
	if err != nil {
		t.Fatal(err)
	}
	module, err := filepath.Abs("..")
	if err != nil {
		t.Fatal(err)
	}
	var source strings.Builder
	source.WriteString(`package examples
import (
    "context"
    "encoding/json"
    "fmt"
    "io"
    "log"
    "os"
    "time"
    contree "github.com/nebius/contree-client/go/v1"
)
`)
	for _, name := range []string{"grep", "downloads", "subprocesses"} {
		marker := "<!-- go-example: " + name + " -->\n```go\n"
		_, rest, ok := strings.Cut(string(doc), marker)
		if !ok {
			t.Fatalf("tutorial has no %s example", name)
		}
		snippet, _, ok := strings.Cut(rest, "\n```")
		if !ok {
			t.Fatalf("tutorial %s example has no closing fence", name)
		}
		source.WriteString("func " + name + "(ctx context.Context, client *contree.Client, imageUUID string) error {\n")
		source.WriteString(snippet)
		source.WriteString("\nreturn nil\n}\n")
	}
	directory := t.TempDir()
	files := map[string]string{
		"go.mod":             "module examples.invalid\n\ngo 1.23\n\nrequire github.com/nebius/contree-client/go v0.0.0\nreplace github.com/nebius/contree-client/go => " + strconv.Quote(module) + "\n",
		"examples.go":        source.String(),
		"examples_test.go":   tutorialImageTests,
		"subprocess_test.go": tutorialSubprocessTests,
	}
	for name, content := range files {
		if err := os.WriteFile(filepath.Join(directory, name), []byte(content), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	command := exec.Command("go", "test", "-count=1", "./...")
	command.Dir = directory
	command.Env = append(os.Environ(), "GOWORK=off")
	if output, err := command.CombinedOutput(); err != nil {
		t.Fatalf("tutorial examples: %v\n%s", err, output)
	}
}

const tutorialImageTests = `package examples
import (
    "archive/tar"
    "bytes"
    "context"
    "errors"
    "io"
    "os"
    "reflect"
    "strings"
    "testing"
    contree "github.com/nebius/contree-client/go/v1"
)

func TestImageExamples(t *testing.T) {
    ctx := context.Background()
    client, mock, err := contree.NewTestClient()
    if err != nil { t.Fatal(err) }
    configure := func(err error) { t.Helper(); if err != nil { t.Fatal(err) } }
    content := []byte("127.0.0.1 localhost\n")
    var archive bytes.Buffer
    writer := tar.NewWriter(&archive)
    configure(writer.WriteHeader(&tar.Header{Name: "etc/hosts", Mode: 0644, Size: int64(len(content))}))
    _, err = writer.Write(content)
    configure(err)
    configure(writer.Close())
    configure(mock.Mock("InspectImage", contree.Image{UUID: contree.Some("image")}))
    configure(mock.Mock("InspectImageList", contree.DirectoryList{Path: "/etc", Files: []contree.FileItem{}}))
    configure(mock.Mock("CheckImageFile", true))
    configure(mock.Mock("CheckImageArchive", true))
    configure(mock.Mock("InspectImageDownload", content))
    configure(mock.Mock("InspectImageArchive", archive.Bytes()))
    configure(mock.Mock("InspectImageGrep", contree.GrepResult{
        Path: "/etc/hosts", Patterns: []string{"^root:", "localhost"},
        Matches: []contree.GrepMatch{{
            Path: "/etc/hosts", LineNumber: 1, LineText: string(content),
            LineBytes: int64(len(content)), Type: "match", Submatches: []contree.GrepSubmatch{},
        }},
    }))
    if err := grep(ctx, client, "image"); err != nil { t.Fatal(err) }
    call := mock.CallsFor("InspectImageGrep")[0]
    if !reflect.DeepEqual(call.Query["pattern"], []string{"^root:", "localhost"}) ||
       !reflect.DeepEqual(call.Query["path"], []string{"/etc/hosts", "/etc/passwd"}) ||
       call.Query.Get("before") != "1" || call.Query.Get("max_total") != "20" {
        t.Fatalf("grep query = %#v", call.Query)
    }
    if err := downloads(ctx, client, "image"); err != nil { t.Fatal(err) }
    hosts, err := os.ReadFile("hosts")
    if err != nil || !bytes.Equal(hosts, content) { t.Fatalf("hosts = %q, %v", hosts, err) }
    tarBytes, err := os.ReadFile("etc.tar")
    if err != nil { t.Fatal(err) }
    reader := tar.NewReader(bytes.NewReader(tarBytes))
    header, err := reader.Next()
    if err != nil || header.Name != "etc/hosts" { t.Fatalf("tar header = %#v, %v", header, err) }
    entry, err := io.ReadAll(reader)
    if err != nil || !bytes.Equal(entry, content) { t.Fatalf("tar entry = %q, %v", entry, err) }
    if len(mock.CallsFor("InspectImageDownload")) != 2 { t.Fatal("expected buffered and streamed downloads") }

    failed, errorsMock, err := contree.NewTestClient()
    configure(err)
    configure(errorsMock.MockError("InspectImageGrep", io.ErrUnexpectedEOF))
    if err := grep(ctx, failed, "image"); !errors.Is(err, io.ErrUnexpectedEOF) || !strings.Contains(err.Error(), "search image image") {
        t.Fatalf("grep error lost its context or cause: %v", err)
    }
    configure(errorsMock.Mock("InspectImage", contree.Image{}))
    configure(errorsMock.Mock("InspectImageList", contree.DirectoryList{Files: []contree.FileItem{}}))
    configure(errorsMock.Mock("CheckImageFile", false))
    configure(errorsMock.MockStream("InspectImageDownload", "partial", io.ErrUnexpectedEOF))
    if err := downloads(ctx, failed, "image"); !errors.Is(err, io.ErrUnexpectedEOF) || !strings.Contains(err.Error(), "save hosts file") {
        t.Fatalf("download error lost its context or cause: %v", err)
    }
}
`
