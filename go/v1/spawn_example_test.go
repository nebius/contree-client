package contree_test

import (
	"context"
	"fmt"
	"log"
	"time"

	contree "github.com/nebius/contree-client/go/v1"
)

func ExampleClient_SpawnInstance() {
	client, mock, err := contree.NewTestClient()
	if err != nil {
		log.Fatalf("create test client: %v", err)
	}
	if err := mock.Mock("SpawnInstance", map[string]string{"uuid": "operation-1"}); err != nil {
		log.Fatalf("mock instance creation: %v", err)
	}
	operationID, err := client.SpawnInstance(
		context.Background(),
		"wc -l < /work/data.txt",
		"tag:ubuntu:latest",
		contree.SpawnInstanceOptions{
			Shell:      true,
			Env:        map[string]string{"LC_ALL": "C"},
			Cwd:        "/work",
			Timeout:    time.Minute,
			Disposable: true,
		},
	)
	if err != nil {
		log.Fatalf("spawn word-count command: %v", err)
	}
	fmt.Println(operationID)
	// Output: operation-1
}
