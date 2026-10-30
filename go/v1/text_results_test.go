package contree

import (
	"bytes"
	"encoding/json"
	"fmt"
	"strings"
	"testing"
)

func textGrepRow(path string, line int64, text, kind string) GrepMatch {
	return GrepMatch{
		Path: path, LineNumber: line, LineText: text, Type: kind,
		Submatches: []GrepSubmatch{},
	}
}

func TestGrepText(t *testing.T) {
	for _, test := range []struct {
		name string
		rows []GrepMatch
		want string
	}{
		{"empty", []GrepMatch{}, ""},
		{"newline", []GrepMatch{textGrepRow("/a", 2, "hi\n", "match")}, "/a:2:hi\n"},
		{"no newline", []GrepMatch{textGrepRow("/a", 2, "hi", "match")}, "/a:2:hi\n"},
		{"empty text", []GrepMatch{textGrepRow("/a", 2, "", "match")}, "/a:2:\n"},
		{"unicode", []GrepMatch{textGrepRow("/a b", 2, "привет\r\n", "match")}, "/a b:2:привет\r\n"},
		{"context", []GrepMatch{
			textGrepRow("/a", 1, "before\n", "context"),
			textGrepRow("/a", 2, "hit\n", "match"),
			textGrepRow("/a", 6, "next\n", "match"),
			textGrepRow("/b", 1, "other\n", "context"),
		}, "/a-1-before\n/a:2:hit\n--\n/a:6:next\n--\n/b-1-other\n"},
		{"no context groups", []GrepMatch{
			textGrepRow("/a", 1, "one\n", "match"),
			textGrepRow("/b", 7, "seven\n", "match"),
		}, "/a:1:one\n/b:7:seven\n"},
	} {
		t.Run(test.name, func(t *testing.T) {
			result := GrepResult{Path: "/", Patterns: []string{"pattern"}, Matches: test.rows, Truncated: true}
			before, err := json.Marshal(result)
			if err != nil {
				t.Fatal(err)
			}
			if got := result.AsText(); got != test.want {
				t.Fatalf("AsText() = %q, want %q", got, test.want)
			}
			if got := fmt.Sprint(result); got != test.want {
				t.Fatalf("String() = %q, want %q", got, test.want)
			}
			for _, row := range test.rows {
				if got := fmt.Sprint(row); got != row.AsText() {
					t.Fatalf("row String() = %q, want %q", got, row.AsText())
				}
			}
			after, err := json.Marshal(result)
			if err != nil || !bytes.Equal(before, after) {
				t.Fatalf("formatting changed the payload: %s -> %s (%v)", before, after, err)
			}
		})
	}
}

func textFileItem() FileItem {
	return FileItem{
		Path: "a.txt", Size: 12, Nlink: 1, Mode: 0100644,
		Owner: StringOrInt64FromString("alice"), Group: StringOrInt64FromString("staff"),
		UID: 1000, GID: 100, IsRegular: true,
	}
}

func TestDirectoryText(t *testing.T) {
	result := DirectoryList{Path: "/work", Files: []FileItem{textFileItem()}}
	want := "-rw-r--r-- 1 alice staff 12 1970-01-01 00:00 a.txt\n"
	before, err := json.Marshal(result)
	if err != nil {
		t.Fatal(err)
	}
	if result.AsText() != want || result.AsText(false) != want || fmt.Sprint(result) != want {
		t.Fatalf("detailed format = %q, want %q", result.AsText(), want)
	}
	if got := result.AsText(true); got != "a.txt\n" {
		t.Fatalf("short format = %q", got)
	}
	after, err := json.Marshal(result)
	if err != nil || !bytes.Equal(before, after) {
		t.Fatalf("formatting changed the payload: %s -> %s (%v)", before, after, err)
	}
	empty := DirectoryList{}
	if empty.AsText() != "" || empty.AsText(true) != "" || empty.String() != "" {
		t.Fatal("empty directory has nonempty text")
	}
}

func TestDirectoryTextAlignmentAndFallbacks(t *testing.T) {
	first, second := textFileItem(), textFileItem()
	first.Path, first.Owner, first.Group = "имя with spaces", StringOrInt64FromString("猫"), StringOrInt64FromString("")
	first.UID, first.GID, first.Nlink, first.Size = 999, 0, 2, 3
	second.Path, second.Owner, second.Group = "link", StringOrInt64FromString("alice"), StringOrInt64FromInt64(0)
	second.Nlink, second.Size, second.Mode, second.IsSymlink = 12, 1234, 0120777, true
	second.SymlinkTo = "target with spaces"
	result := DirectoryList{Files: []FileItem{first, second}}
	want := "-rw-r--r--  2 猫     0    3 1970-01-01 00:00 имя with spaces\n" +
		"lrwxrwxrwx 12 alice 0 1234 1970-01-01 00:00 link -> target with spaces\n"
	if got := result.AsText(); got != want {
		t.Fatalf("aligned format = %q, want %q", got, want)
	}
	if got := result.AsText(true); got != "имя with spaces\nlink\n" {
		t.Fatalf("short format = %q", got)
	}
	first.Owner, first.UID = StringOrInt64FromString(""), 0
	result.Files = []FileItem{first}
	if got := result.AsText(); !strings.Contains(got, " 0 0 ") {
		t.Fatalf("numeric identity fallback = %q", got)
	}
}

func TestDirectoryTextFileModes(t *testing.T) {
	for _, test := range []struct {
		mode int64
		flag string
		want string
	}{
		{0755, "dir", "drwxr-xr-x"},
		{0777, "symlink", "lrwxrwxrwx"},
		{0600, "socket", "srw-------"},
		{0600, "fifo", "prw-------"},
		{0644, "regular", "-rw-r--r--"},
		{0104755, "", "-rwsr-xr-x"},
		{0102644, "", "-rw-r-Sr--"},
		{0041777, "", "drwxrwxrwt"},
		{0041700, "", "drwx-----T"},
		{0020600, "", "crw-------"},
		{0060600, "", "brw-------"},
		{0644, "", "?rw-r--r--"},
	} {
		t.Run(test.want, func(t *testing.T) {
			item := textFileItem()
			item.Mode, item.IsRegular = test.mode, test.flag == "regular"
			item.IsDir, item.IsSymlink = test.flag == "dir", test.flag == "symlink"
			item.IsSocket, item.IsFifo = test.flag == "socket", test.flag == "fifo"
			result := DirectoryList{Files: []FileItem{item}}
			if got := result.AsText(); !strings.HasPrefix(got, test.want+" ") {
				t.Fatalf("mode format = %q, want prefix %q", got, test.want)
			}
		})
	}
}

func TestStreamTextStringers(t *testing.T) {
	for _, test := range []struct{ encoding, value, want string }{
		{"ascii", "hello\n", "hello\n"}, {"ascii", "", ""},
		{"base64", "aGkK", "hi\n"}, {"base64", "/w==", "�"}, {"base64", "a", ""},
	} {
		for _, stream := range []interface {
			AsText() string
			String() string
		}{
			StreamRepr{Value: test.value, Encoding: test.encoding},
			EventDataStream{Value: test.value, Encoding: test.encoding},
			ClosableStreamRepr{Value: test.value, Encoding: Some(test.encoding)},
		} {
			if stream.AsText() != test.want || fmt.Sprint(stream) != test.want {
				t.Fatalf("%T text = %q / %q, want %q", stream, stream.AsText(), fmt.Sprint(stream), test.want)
			}
		}
	}
}

func TestClosableStreamTextPreservesOptionalFields(t *testing.T) {
	for _, close := range []Optional[bool]{{}, Some(false), Some(true)} {
		for _, value := range []string{"", "stdin\n"} {
			stream := ClosableStreamRepr{Value: value, Close: close}
			before, err := json.Marshal(stream)
			if err != nil {
				t.Fatal(err)
			}
			if stream.AsText() != value || string(stream.AsBytes()) != value || stream.String() != value {
				t.Fatal("omitted encoding did not use ASCII")
			}
			after, err := json.Marshal(stream)
			if err != nil || !bytes.Equal(before, after) || stream.Encoding.IsSet() {
				t.Fatalf("formatting changed optional fields: %s -> %s (%v)", before, after, err)
			}
		}
	}
	for _, value := range []string{"", "plain\n", "привет\n"} {
		stream := NewClosableStreamReprFromText(value)
		if stream.AsText() != value || string(stream.AsBytes()) != value || stream.Close.IsSet() {
			t.Fatalf("text constructor changed payload or close: %#v", stream)
		}
	}
	stream := NewClosableStreamReprFromBytes([]byte{0, 255})
	if !bytes.Equal(stream.AsBytes(), []byte{0, 255}) || stream.String() != "\x00�" {
		t.Fatalf("binary roundtrip = %v / %q", stream.AsBytes(), stream.String())
	}
}
