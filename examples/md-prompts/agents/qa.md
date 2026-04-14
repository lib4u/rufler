# QA Engineer

Write tests for the Go service.

## Required coverage
- Unit test for `/healthz` using `httptest.NewRecorder`.
- Unit test that `/metrics` returns 200 and includes at least one default
  Go collector metric (e.g. `go_goroutines`).
- Table-driven test style where applicable.

## Commands to run
- `go test ./... -race -count=1`
- Coverage target: 70%+ on `internal/`.

Store any flaky/test-gap notes in memory namespace `md-prompts-demo`.
