# Backend subtask

Implement a REST API in Go (Echo or net/http) with:

- `POST /users` create
- `GET /users/:id` fetch
- `PUT /users/:id` update
- `DELETE /users/:id` delete
- In-memory store with sync.RWMutex
- Unit tests with table-driven style

Acceptance: `go test ./...` green, binary builds to `./bin/api`.
