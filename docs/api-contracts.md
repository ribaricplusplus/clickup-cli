# API Contract Provenance

The localhost contract tests are based on ClickUp's official v2 OpenAPI specification and a scoped
live probe against a disposable sandbox List. This document records why each mock expectation is
considered valid.

## Authoritative specification

The v2 OpenAPI document is published at:

- <https://developer.clickup.com/openapi/clickup-api-v2-reference.json>

The snapshot re-inspected on 2026-08-17 had SHA-256:

```text
a0a72ec97ddb4e4859b9ed89b997bb784ba5828412ff35119f41e87103069662
```

Relevant endpoint documentation:

- [Get Authorized User](https://developer.clickup.com/reference/getauthorizeduser)
- [Get Task](https://developer.clickup.com/reference/gettask)
- [Get List](https://developer.clickup.com/reference/getlist)
- [Update Task](https://developer.clickup.com/reference/updatetask)
- [Get Task Comments](https://developer.clickup.com/reference/gettaskcomments)
- [Create Task Comment](https://developer.clickup.com/reference/createtaskcomment)
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
| List comments | `GET /api/v2/task/{task_id}/comment`, no body |
| Page comments | `GET /api/v2/task/{task_id}/comment?start=<date>&start_id=<id>`, no body |
| Add comment | `POST /api/v2/task/{task_id}/comment` with exactly `{"comment_text":"<text>","notify_all":false}` |
| Set date-only due date | `PUT /api/v2/task/{task_id}` with exactly `{"due_date":<UTC-midnight-ms>,"due_date_time":false}` |
| Set timed due date | `PUT /api/v2/task/{task_id}` with exactly `{"due_date":<instant-ms>,"due_date_time":true}` |
| Clear due date | `PUT /api/v2/task/{task_id}` with exactly `{"due_date":null}` |
| Assign user | `PUT /api/v2/task/{task_id}` with exactly `{"assignees":{"add":[<user-id>],"rem":[]}}` |
| Unassign user | `PUT /api/v2/task/{task_id}` with exactly `{"assignees":{"add":[],"rem":[<user-id>]}}` |
| Create task | `POST /api/v2/list/{list_id}/task` with `name` plus only explicitly supplied description, status, assignee, due-date, due-date-time, and tag fields |
| Delete task | `DELETE /api/v2/task/{task_id}`, no body |

`task set-status` and `task complete` deliberately add orchestration around the raw Update Task
endpoint. The required sequence is task read, List read, minimal PUT when needed, then a separate
task read that must confirm the canonical status. Invalid and idempotent transitions stop before the
PUT.

Comment creation is followed by `GET /task/{task_id}/comment`; the comment ID returned by the POST
must be present with exact text. Due-date and assignee operations read the task before a write,
avoid an observable no-op, and read the task afterward. Timed due dates require exact millisecond
read-back. ClickUp can canonicalize a date-only millisecond value for an account while retaining the
same calendar date, so date-only verification compares the UTC date and reports the observed
`due_date_ms`. If the API exposes `due_date_time`, its value is also verified. Assignment read-back
requires the target numeric user ID to be present or absent as requested.

Comment lookup has no single-comment endpoint in API v2. `task comment show` therefore extracts a
comment ID from the supplied ClickUp deep link or accepts it separately, reads the newest comment
page, and advances with the last comment's documented `start` and `start_id` cursor until it finds
the ID or reaches an empty page. Repeated cursors and excessive pagination fail closed.

Task creation accepts the same validated date-only or timezone-aware due-date model as the update
command and repeatable, normalized tag names. The initial POST includes all requested fields so the
operation does not require follow-up mutation calls. A separate `GET /task/{task_id}` then verifies
the returned ID, destination List, name, description, status, requested assignees, requested tags,
and due date before success is reported. Final stable-output normalization stays inside the same
partial-outcome boundary. If the POST response is lost or unusable before an ID is known, the CLI
returns `outcome_unknown` and instructs callers to inspect the List before retrying. Any failure
after an ID is known returns `created_but_unverified` with a structured `task_id`.

## Live probe

On 2026-08-17, development included one disposable lifecycle probe against a dedicated ClickUp
sandbox List. No production task was used, and no workspace, List, task, or user identifier is stored
in this repository.

The probe confirmed:

1. Task creation and read-back used the configured sandbox List.
2. Comment creation and listing returned the same generated comment ID and exact text.
3. Date-only due-date set, timed due-date set, and clear were each confirmed by separate task reads.
4. The date-only write was canonicalized by ClickUp to a different millisecond value on the same UTC
   calendar date, while the timed instant was preserved exactly.
5. Assigning and then unassigning the authenticated sandbox user was confirmed by separate task
   reads.
6. Status update and semantic completion were confirmed by separate task reads.
7. `DELETE /api/v2/task/{task_id}` succeeded and the final task read returned HTTP 404.

The opt-in live pytest repeats a fuller CLI lifecycle and always attempts deletion in `finally`.
Ordinary tests and CI use only the localhost mock server.

## Rate-limit basis

ClickUp documents HTTP 429 responses with an absolute Unix timestamp in
`X-RateLimit-Reset`. The client also accepts standard `Retry-After` values. Both paths are bounded
and covered by deterministic tests with an injected clock and sleep function.
