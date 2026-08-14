# API Contract Provenance

The localhost contract tests are based on ClickUp's official v2 OpenAPI specification and a scoped
live probe against a disposable sandbox List. This document records why each mock expectation is
considered valid.

## Authoritative specification

The v2 OpenAPI document is published at:

- <https://developer.clickup.com/openapi/clickup-api-v2-reference.json>

The snapshot inspected on 2026-08-14 had SHA-256:

```text
a0a72ec97ddb4e4859b9ed89b997bb784ba5828412ff35119f41e87103069662
```

Relevant endpoint documentation:

- [Get Authorized User](https://developer.clickup.com/reference/getauthorizeduser)
- [Get Task](https://developer.clickup.com/reference/gettask)
- [Get List](https://developer.clickup.com/reference/getlist)
- [Update Task](https://developer.clickup.com/reference/updatetask)
- [Create Task](https://developer.clickup.com/reference/createtask)
- [Delete Task](https://developer.clickup.com/reference/deletetask)
- [Rate Limits](https://developer.clickup.com/docs/rate-limits)

## Supported request contracts

All requests carry `Accept: application/json` and the raw personal token in `Authorization`.
Requests with a JSON body also carry `Content-Type: application/json`.

| Operation | Exact request contract |
| --- | --- |
| Identity | `GET /api/v2/user`, no body |
| Read task or status | `GET /api/v2/task/{task_id}`, no body |
| Discover valid statuses | `GET /api/v2/list/{list_id}`, no body |
| Set status | `PUT /api/v2/task/{task_id}` with only `{"status":"<canonical label>"}` |
| Create task | `POST /api/v2/list/{list_id}/task` with `name` plus only explicitly supplied supported fields |
| Delete task | `DELETE /api/v2/task/{task_id}`, no body |

`task set-status` and `task complete` deliberately add orchestration around the raw Update Task
endpoint. The required sequence is task read, List read, minimal PUT when needed, then a separate
task read that must confirm the canonical status. Invalid and idempotent transitions stop before the
PUT.

## Live probe

On 2026-08-14, development included one disposable lifecycle probe against a dedicated ClickUp
sandbox List. No production task was used, and no workspace, List, task, or user identifier is stored
in this repository.

The probe confirmed:

1. `POST /api/v2/list/{list_id}/task` accepted `{"name":"...","status":"backlog"}` and returned the
   created task.
2. `GET /api/v2/task/{task_id}` returned the same home List and initial status.
3. `PUT /api/v2/task/{task_id}` accepted exactly `{"status":"complete"}` and returned the updated
   task.
4. A separate GET returned the requested `complete` status with terminal type `closed`.
5. `DELETE /api/v2/task/{task_id}` returned HTTP 204 with an empty body.
6. A final List query showed that the disposable task had been removed.

The opt-in live pytest repeats a fuller CLI lifecycle and always attempts deletion in `finally`.
Ordinary tests and CI use only the localhost mock server.

## Rate-limit basis

ClickUp documents HTTP 429 responses with an absolute Unix timestamp in
`X-RateLimit-Reset`. The client also accepts standard `Retry-After` values. Both paths are bounded
and covered by deterministic tests with an injected clock and sleep function.
