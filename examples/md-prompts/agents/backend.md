# Backend Engineer

Implement the Go HTTP service following the architect's `ARCHITECTURE.md`.

## Scope
- `GET /healthz` — returns `{"status":"ok"}` JSON, HTTP 200.
- `GET /metrics` — Prometheus text exposition (use `promhttp.Handler`).
- Graceful shutdown on SIGINT/SIGTERM with 5s timeout.
- Config via env vars: `PORT` (default 8080), `LOG_LEVEL` (default info).

## Quality bar
- All exported funcs documented.
- `go vet ./...` clean.
- `go build ./...` succeeds.
- No global state except the Prometheus registry.
