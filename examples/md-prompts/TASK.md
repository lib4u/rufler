# Goal

Build a small Go HTTP service that exposes `/healthz` and `/metrics`
(Prometheus format), with unit tests and a Dockerfile.

## Requirements
- `GET /healthz` → `{"status":"ok"}`, HTTP 200
- `GET /metrics` → Prometheus text exposition via `promhttp.Handler`
- Graceful shutdown on SIGINT/SIGTERM (5s timeout)
- Config via env: `PORT` (default 8080), `LOG_LEVEL` (default info)
- Multi-stage Dockerfile, final image < 30MB
- `go test ./... -race` clean, 70%+ coverage on `internal/`

## Non-goals
- No database
- No auth
- No extra routes beyond the two above

## Verify
- `go build ./...` succeeds
- `go test ./... -race -count=1` is green
- `docker build .` succeeds
