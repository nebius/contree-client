package contree

import (
	"context"
	"fmt"
	"iter"
)

type pageFetcher[T any] func(pageSize, offset int64) ([]T, error)

// iteratePages starts a fresh, lazy traversal on each invocation.
// It yields a zero item with the first error, then stops.
func iteratePages[T any](
	ctx context.Context,
	pageSize int64,
	maxPageSize int64,
	limit *int64,
	fetch pageFetcher[T],
) iter.Seq2[T, error] {
	hasLimit := limit != nil
	var totalLimit int64
	if hasLimit {
		totalLimit = *limit
	}
	if pageSize == 0 {
		pageSize = maxPageSize
	}
	return func(yield func(T, error) bool) {
		var zero T
		switch {
		case ctx == nil:
			yield(zero, fmt.Errorf("contree: context must not be nil"))
			return
		case maxPageSize < 1:
			yield(zero, fmt.Errorf("contree: maximum page size must be positive"))
			return
		case pageSize < 1 || pageSize > maxPageSize:
			yield(zero, fmt.Errorf("contree: page size must be between 1 and %d", maxPageSize))
			return
		case hasLimit && totalLimit < 0:
			yield(zero, fmt.Errorf("contree: limit must be non-negative"))
			return
		case fetch == nil:
			yield(zero, fmt.Errorf("contree: page fetcher must not be nil"))
			return
		}

		var offset, fetched int64
		for {
			if err := ctx.Err(); err != nil {
				yield(zero, err)
				return
			}
			size := pageSize
			if hasLimit {
				remaining := totalLimit - fetched
				if remaining <= 0 {
					return
				}
				if remaining < size {
					size = remaining
				}
			}
			page, err := fetch(size, offset)
			if err != nil {
				yield(zero, err)
				return
			}
			for _, item := range page {
				if err := ctx.Err(); err != nil {
					yield(zero, err)
					return
				}
				if !yield(item, nil) {
					return
				}
				fetched++
				if hasLimit && fetched >= totalLimit {
					return
				}
			}
			if int64(len(page)) < size {
				return
			}
			offset += int64(len(page))
		}
	}
}
