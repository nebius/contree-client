package contree

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"regexp"
	"strings"
	"time"
)

var imageUUIDPattern = regexp.MustCompile(
	`(?i)^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`,
)

// EnsureFileOptions configures EnsureFile.
type EnsureFileOptions struct {
	// SHA256 is a known content digest. EnsureFile trusts a non-empty value
	// and does not read content before the server probe.
	SHA256 string
}

// EnsureFileResult identifies a stored file. CreatedAt and UpdatedAt are set
// when the server returns an existing file. Upload responses omit them.
type EnsureFileResult struct {
	UUID      string              `json:"uuid"`
	SHA256    string              `json:"sha256"`
	Size      int64               `json:"size"`
	CreatedAt Optional[time.Time] `json:"created_at"`
	UpdatedAt Optional[time.Time] `json:"updated_at"`
}

// ResolveImage resolves a UUID, a tag:NAME reference, or a bare tag to an
// image UUID. UUID references do not make a request.
func (c *Client) ResolveImage(ctx context.Context, ref string) (string, error) {
	if strings.HasPrefix(ref, "tag:") {
		return c.InspectFindImageByTag(ctx, strings.TrimPrefix(ref, "tag:"))
	}
	if imageUUIDPattern.MatchString(ref) {
		return ref, nil
	}
	return c.InspectFindImageByTag(ctx, ref)
}

// EnsureFile returns the existing file with the content digest, or uploads
// content when the server returns NotFoundError for that digest.
//
// If options does not contain SHA256, EnsureFile hashes seekable readers from
// their current position and restores that position before the server probe.
// Non-seekable readers upload directly. Therefore, callers can pass *os.File
// without loading the file into memory.
func (c *Client) EnsureFile(
	ctx context.Context,
	content io.Reader,
	options *EnsureFileOptions,
) (EnsureFileResult, error) {
	digest := ""
	if options != nil {
		digest = options.SHA256
	}
	if digest == "" {
		var seekable bool
		var err error
		digest, seekable, err = seekableSHA256(content)
		if err != nil {
			return EnsureFileResult{}, err
		}
		if !seekable {
			return uploadEnsureFile(ctx, c, content)
		}
	}

	file, err := c.GetFile(ctx, digest)
	if err == nil {
		return EnsureFileResult{
			UUID:      file.UUID,
			SHA256:    file.SHA256,
			Size:      file.Size,
			CreatedAt: Some(file.CreatedAt),
			UpdatedAt: Some(file.UpdatedAt),
		}, nil
	}
	var notFound *NotFoundError
	if !errors.As(err, &notFound) {
		return EnsureFileResult{}, err
	}
	return uploadEnsureFile(ctx, c, content)
}

func uploadEnsureFile(
	ctx context.Context,
	client *Client,
	content io.Reader,
) (EnsureFileResult, error) {
	file, err := client.UploadFile(ctx, content)
	if err != nil {
		return EnsureFileResult{}, err
	}
	return EnsureFileResult{
		UUID:   file.UUID,
		SHA256: file.SHA256,
		Size:   file.Size,
	}, nil
}

func seekableSHA256(content io.Reader) (string, bool, error) {
	if isNilReader(content) {
		return "", false, nil
	}
	seeker, ok := content.(io.Seeker)
	if !ok {
		return "", false, nil
	}

	start, err := seeker.Seek(0, io.SeekCurrent)
	if err != nil {
		return "", true, fmt.Errorf("contree: get file content position: %w", err)
	}
	hash := sha256.New()
	_, hashErr := io.Copy(hash, content)
	_, restoreErr := seeker.Seek(start, io.SeekStart)
	if hashErr != nil {
		hashErr = fmt.Errorf("contree: hash file content: %w", hashErr)
	}
	if restoreErr != nil {
		restoreErr = fmt.Errorf("contree: restore file content position: %w", restoreErr)
	}
	if err := errors.Join(hashErr, restoreErr); err != nil {
		return "", true, err
	}
	return hex.EncodeToString(hash.Sum(nil)), true, nil
}
