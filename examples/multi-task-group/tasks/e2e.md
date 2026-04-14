# E2E subtask

Write end-to-end tests wiring backend + frontend together. Use Playwright
or cypress. At least:

- happy-path: create → list → edit → delete
- validation: empty form errors
- 404 on missing user

Acceptance: `npm run test:e2e` green in CI.
