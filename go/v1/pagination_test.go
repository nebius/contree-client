package contree

import (
	"context"
	"errors"
	"iter"
	"reflect"
	"testing"
)

type pageRequest struct {
	size   int64
	offset int64
}

func collectPages[T any](sequence iter.Seq2[T, error]) ([]T, error) {
	var items []T
	for item, err := range sequence {
		if err != nil {
			return items, err
		}
		items = append(items, item)
	}
	return items, nil
}

func TestPaginationIsLazyAndStopsOnShortPage(t *testing.T) {
	var requests []pageRequest
	sequence := iteratePages(context.Background(), 3, 10, nil, func(size, offset int64) ([]int, error) {
		requests = append(requests, pageRequest{size, offset})
		if offset == 0 {
			return []int{1, 2, 3}, nil
		}
		return []int{4, 5}, nil
	})
	if len(requests) != 0 {
		t.Fatal("iterator construction fetched a page")
	}
	items, err := collectPages(sequence)
	if err != nil || !reflect.DeepEqual(items, []int{1, 2, 3, 4, 5}) {
		t.Fatalf("items = %v, %v", items, err)
	}
	if !reflect.DeepEqual(requests, []pageRequest{{3, 0}, {3, 3}}) {
		t.Fatalf("requests = %v", requests)
	}
}

func TestPaginationRequestsOnlyTheRemainingLimit(t *testing.T) {
	limit := int64(5)
	var requests []pageRequest
	sequence := iteratePages(context.Background(), 3, 10, &limit, func(size, offset int64) ([]int, error) {
		requests = append(requests, pageRequest{size, offset})
		if offset == 0 {
			return []int{1, 2, 3}, nil
		}
		return []int{4, 5, 6}, nil // Enforce the limit even if the server over-delivers.
	})
	limit = 99 // Construction snapshots the total limit.
	items, err := collectPages(sequence)
	if err != nil || !reflect.DeepEqual(items, []int{1, 2, 3, 4, 5}) {
		t.Fatalf("items = %v, %v", items, err)
	}
	if !reflect.DeepEqual(requests, []pageRequest{{3, 0}, {2, 3}}) {
		t.Fatalf("requests = %v", requests)
	}
}

func TestPaginationZeroLimitAndEmptyPage(t *testing.T) {
	zero := int64(0)
	for _, limit := range []*int64{&zero, nil} {
		requests := 0
		items, err := collectPages(iteratePages(context.Background(), 4, 10, limit, func(int64, int64) ([]int, error) {
			requests++
			return nil, nil
		}))
		wantRequests := 1
		if limit != nil {
			wantRequests = 0
		}
		if err != nil || len(items) != 0 || requests != wantRequests {
			t.Fatalf("empty iterator = %v, %v, %d requests", items, err, requests)
		}
	}
}

func TestPaginationYieldsOneFetchErrorAndStops(t *testing.T) {
	want := errors.New("page failed")
	requests, yieldedErrors := 0, 0
	var items []int
	for item, err := range iteratePages(context.Background(), 1, 10, nil, func(int64, int64) ([]int, error) {
		requests++
		if requests == 1 {
			return []int{1}, nil
		}
		return nil, want
	}) {
		if err != nil {
			yieldedErrors++
			if item != 0 || !errors.Is(err, want) {
				t.Fatalf("error item = %d, %v", item, err)
			}
			continue // An error terminates the sequence even when the caller continues.
		}
		items = append(items, item)
	}
	if requests != 2 || yieldedErrors != 1 || !reflect.DeepEqual(items, []int{1}) {
		t.Fatalf("requests=%d errors=%d items=%v", requests, yieldedErrors, items)
	}
}

func TestPaginationValidatesBeforeFetching(t *testing.T) {
	negative := int64(-1)
	fetch := func(int64, int64) ([]int, error) { t.Fatal("invalid iterator fetched a page"); return nil, nil }
	for name, sequence := range map[string]iter.Seq2[int, error]{
		"negative page size": iteratePages(context.Background(), -1, 10, nil, fetch),
		"large page size":    iteratePages(context.Background(), 11, 10, nil, fetch),
		"invalid maximum":    iteratePages(context.Background(), 1, 0, nil, fetch),
		"negative limit":     iteratePages(context.Background(), 1, 10, &negative, fetch),
		"nil fetcher":        iteratePages[int](context.Background(), 1, 10, nil, nil),
		"nil context":        iteratePages(nil, 1, 10, nil, fetch),
	} {
		t.Run(name, func(t *testing.T) {
			count := 0
			for item, err := range sequence {
				count++
				if item != 0 || err == nil {
					t.Fatalf("validation = %d, %v", item, err)
				}
			}
			if count != 1 {
				t.Fatalf("error count = %d", count)
			}
		})
	}
}

func TestPaginationUsesMaximumAsDefaultPageSize(t *testing.T) {
	var request pageRequest
	_, err := collectPages(iteratePages(context.Background(), 0, 7, nil, func(size, offset int64) ([]int, error) {
		request = pageRequest{size, offset}
		return nil, nil
	}))
	if err != nil || request != (pageRequest{7, 0}) {
		t.Fatalf("request = %v, %v", request, err)
	}
}

func TestPaginationBreakStopsFetchingAndNextTraversalRestarts(t *testing.T) {
	var requests []pageRequest
	sequence := iteratePages(context.Background(), 1, 10, nil, func(size, offset int64) ([]int, error) {
		requests = append(requests, pageRequest{size, offset})
		return []int{int(offset) + 1}, nil
	})
	for range 2 {
		for item, err := range sequence {
			if err != nil || item != 1 {
				t.Fatalf("first item = %d, %v", item, err)
			}
			break
		}
	}
	if !reflect.DeepEqual(requests, []pageRequest{{1, 0}, {1, 0}}) {
		t.Fatalf("break fetched ahead or resumed an old traversal: %v", requests)
	}
}

func TestPaginationChecksCancellationBetweenBufferedItems(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	requests, errorsSeen := 0, 0
	var items []int
	for item, err := range iteratePages(ctx, 3, 10, nil, func(int64, int64) ([]int, error) {
		requests++
		return []int{1, 2, 3}, nil
	}) {
		if err != nil {
			errorsSeen++
			if !errors.Is(err, context.Canceled) {
				t.Fatal(err)
			}
			continue
		}
		items = append(items, item)
		cancel()
	}
	if requests != 1 || errorsSeen != 1 || !reflect.DeepEqual(items, []int{1}) {
		t.Fatalf("requests=%d errors=%d items=%v", requests, errorsSeen, items)
	}
}
