package main

import (
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
)

func TestGeneratePublicAPI(t *testing.T) {
	dir := t.TempDir()
	files := map[string]string{
		"client.go": `// Package sample provides a client. See [NewClient].
package sample

import "context"

// Version is the protocol version.
const Version = "v1"

// Client calls the API. See [Client.Run] and [context.Context].
type Client struct {
    // Endpoint is the API URL.
    Endpoint string
    secret string
}

// NewClient constructs a [Client].
func NewClient() *Client { return &Client{secret: "private-implementation"} }

// Run returns a result of type T.
func (c *Client) Run[T any](ctx context.Context) (T, error) {
    var result T
    return result, nil
}

// Optional represents a value that can be absent.
type Optional[T any] struct { Value T }

// Some wraps a value.
func Some[T any](value T) Optional[T] { return Optional[T]{Value: value} }

type State string
const (
    Ready State = "ready"
    Done State = "done"
)

var ErrClosed = "closed"
var DefaultClient = Client{secret: "private-default"}
var ExplicitClient Client = Client{secret: "private-explicit"}
var PublicDefault = Optional[int]{Value: 42}
type Alias = Client
func (c *Client) private() {}
type hidden struct{}
`,
		"example_test.go": `package sample_test
import (
    "fmt"
    "example.com/sample"
)
func ExampleNewClient() {
    client := sample.NewClient()
    fmt.Println(client.Endpoint)
    // Output:
}
`,
		"ignored.go": "//go:build ignore\n\npackage sample\n\nconst WrongPlatform = true\n",
	}
	for name, source := range files {
		if err := os.WriteFile(filepath.Join(dir, name), []byte(source), 0o644); err != nil {
			t.Fatal(err)
		}
	}

	page, err := generate(dir, "example.com/sample")
	if err != nil {
		t.Fatal(err)
	}
	text := string(page)
	for _, fragment := range []string{
		"(go-client)=", "(go-client-run)=", "(go-newclient)=", "(go-some)=",
		"(go-optional)=", "(go-version)=", "(go-errclosed)=", "(go-alias)=",
		"(go-ready)=", "(go-done)=", "type Optional[T any] struct",
		"func (c *Client) Run[T any](ctx context.Context) (T, error)",
		"// Endpoint is the API URL.", "Endpoint string",
		"[NewClient](#go-newclient)", "[Client.Run](#go-client-run)",
		"https://pkg.go.dev/context#Context", "**Example**", "fmt.Println(client.Endpoint)",
		"var DefaultClient Client\n", "var ExplicitClient Client\n",
		"var PublicDefault = Optional[int]{Value: 42}",
	} {
		if !strings.Contains(text, fragment) {
			t.Errorf("missing %q", fragment)
		}
	}
	for _, hidden := range []string{
		"private-implementation", "private-default", "private-explicit",
		"secret string", "func (c *Client) private", "type hidden", "WrongPlatform",
		"Client{/* contains filtered", "Optional[int]{/* contains filtered",
	} {
		if strings.Contains(text, hidden) {
			t.Errorf("unexpected private declaration %q", hidden)
		}
	}

	again, err := generate(dir, "example.com/sample")
	if err != nil {
		t.Fatal(err)
	}
	if string(again) != text {
		t.Fatal("generation is not deterministic")
	}
}

func TestClientReference(t *testing.T) {
	page, err := generate("../../v1", "github.com/nebius/contree-client/go/v1")
	if err != nil {
		t.Fatal(err)
	}
	text := string(page)
	for _, declaration := range []string{
		"var OpSpawnInstance Operation[InstanceSpawnResponse]",
		"var OpOperationSubprocessCreate Operation[int64]",
		"var OpOperationSubprocessKill Operation[struct{}]",
	} {
		if !strings.Contains(text, declaration+"\n") {
			t.Errorf("missing typed variable declaration %s", declaration)
		}
	}
	for _, name := range []string{
		"Client", "Client.SpawnInstance", "Client.SpawnInstanceWithResponse",
		"Client.ListImages", "Client.FollowOperationEvents", "Client.WaitOperation",
		"Optional", "Some", "ResolveProfile", "InstanceSpawnResponse",
		"SpawnInstanceOptions", "NewTestClient", "MockTransport.Mock", "OpSpawnInstance",
	} {
		if !strings.Contains(text, "("+anchor(name)+")=") {
			t.Errorf("missing public symbol %s", name)
		}
	}

	labels := regexp.MustCompile(`(?m)^\((go-[^)]+)\)=$`).FindAllStringSubmatch(text, -1)
	seen := make(map[string]bool)
	for _, match := range labels {
		if seen[match[1]] {
			t.Errorf("duplicate anchor %s", match[1])
		}
		seen[match[1]] = true
	}

	// Audit every exported declaration independently of go/doc's grouping.
	// A constructor can appear under its result type, and enum values and
	// operation descriptors can also move out of the package-level lists.
	files, err := filepath.Glob("../../v1/*.go")
	if err != nil {
		t.Fatal(err)
	}
	for _, name := range files {
		if strings.HasSuffix(name, "_test.go") {
			continue
		}
		file, err := parser.ParseFile(token.NewFileSet(), name, nil, 0)
		if err != nil {
			t.Fatal(err)
		}
		check := func(name string) {
			t.Helper()
			if !seen[anchor(name)] {
				t.Errorf("exported declaration is missing: %s", name)
			}
		}
		for _, declaration := range file.Decls {
			switch declaration := declaration.(type) {
			case *ast.FuncDecl:
				if !declaration.Name.IsExported() {
					continue
				}
				name := declaration.Name.Name
				if declaration.Recv != nil {
					recv := declaration.Recv.List[0].Type
					if pointer, ok := recv.(*ast.StarExpr); ok {
						recv = pointer.X
					}
					if generic, ok := recv.(*ast.IndexExpr); ok {
						recv = generic.X
					}
					if generic, ok := recv.(*ast.IndexListExpr); ok {
						recv = generic.X
					}
					typeName := recv.(*ast.Ident)
					if !typeName.IsExported() {
						continue
					}
					name = typeName.Name + "." + name
				}
				check(name)
			case *ast.GenDecl:
				for _, spec := range declaration.Specs {
					switch spec := spec.(type) {
					case *ast.TypeSpec:
						if spec.Name.IsExported() {
							check(spec.Name.Name)
						}
					case *ast.ValueSpec:
						for _, name := range spec.Names {
							if name.IsExported() {
								check(name.Name)
							}
						}
					}
				}
			}
		}
	}
}
