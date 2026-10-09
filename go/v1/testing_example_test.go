package contree_test

import (
	"context"
	"fmt"
	"log"

	contree "github.com/nebius/contree-client/go/v1"
)

func permissions(ctx context.Context, client *contree.Client) (map[string]bool, error) {
	me, err := client.WhoAmI(ctx)
	if err != nil {
		return nil, fmt.Errorf("get token permissions: %w", err)
	}
	return me.Permissions, nil
}

func ExampleNewTestClient() {
	client, mock, err := contree.NewTestClient()
	if err != nil {
		log.Fatalf("create test client: %v", err)
	}
	err = mock.Mock("WhoAmI", contree.WhoAmIResponse{
		Permissions:    map[string]bool{"spawn": true},
		OperationsStat: map[string]int64{},
	})
	if err != nil {
		log.Fatalf("mock token permissions: %v", err)
	}
	allowed, err := permissions(context.Background(), client)
	if err != nil {
		log.Fatalf("load permissions: %v", err)
	}
	fmt.Println(allowed["spawn"])
	fmt.Println(len(mock.CallsFor("WhoAmI")))
	// Output:
	// true
	// 1
}
