# API Contract Provenance

The localhost contracts are based on ClickUp's official API v2 OpenAPI document and scoped probes
against a disposable sandbox. This document records the source and the supported wire boundaries;
it is not a claim of full ClickUp API coverage.

## Authoritative specification

The official document is published at:

- <https://developer.clickup.com/openapi/clickup-api-v2-reference.json>

The snapshot was re-downloaded and inspected on 2026-08-18. It is OpenAPI 3.1.0, reports API info
version 2.0, contains 83 paths, and has SHA-256:

```text
a0a72ec97ddb4e4859b9ed89b997bb784ba5828412ff35119f41e87103069662
```

The hash is unchanged from the preceding inspection. No Workspace, Space, Folder, List, user,
task, comment, attachment, or time-entry identifier from a live account is stored in this
repository.

Relevant official reference areas include authorized user, Teams/Workspaces, Spaces, Folders,
Lists, tasks, comments, tags, attachments, time tracking, and rate limits:

- [Get Authorized User](https://developer.clickup.com/reference/getauthorizeduser)
- [Get Tasks](https://developer.clickup.com/reference/gettasks)
- [Get Task](https://developer.clickup.com/reference/gettask)
- [Create Task](https://developer.clickup.com/reference/createtask)
- [Update Task](https://developer.clickup.com/reference/updatetask)
- [Delete Task](https://developer.clickup.com/reference/deletetask)
- [Create Task Attachment](https://developer.clickup.com/reference/createtaskattachment)
- [Get Space Tags](https://developer.clickup.com/reference/getspacetags)
- [Time Tracking](https://developer.clickup.com/reference/gettimeentrieswithinadaterange)
- [Get Time Entry Tags](https://developer.clickup.com/reference/getalltagsfromtimeentries)
- [Rate Limits](https://developer.clickup.com/docs/rate-limits)

## Common wire rules

Direct API requests carry `Accept: application/json` and the raw personal token in
`Authorization`. JSON writes carry `Content-Type: application/json`. Time endpoints also receive
that content type on otherwise empty bodies where ClickUp's contract expects it. Multipart upload
uses one `attachment` part. The separate attachment downloader sends no authorization header.

API version construction is private to `ClickUpClient`; the supported direct endpoints are v2.

## Discovery and task-read contracts

| Operation | Request |
| --- | --- |
| Identity | `GET /api/v2/user` |
| Workspaces and members | `GET /api/v2/team` |
| Spaces | `GET /api/v2/team/{workspace_id}/space?archived=<bool>` |
| Folders | `GET /api/v2/space/{space_id}/folder?archived=<bool>` |
| Folderless Lists | `GET /api/v2/space/{space_id}/list?archived=<bool>` |
| Folder Lists | `GET /api/v2/folder/{folder_id}/list?archived=<bool>` |
| List detail/statuses | `GET /api/v2/list/{list_id}` |
| Read task/status/attachments | `GET /api/v2/task/{task_id}` |
| Tasks in List | `GET /api/v2/list/{list_id}/task?page=<n>&...` |
| Tasks in Workspace | `GET /api/v2/team/{workspace_id}/task?page=<n>&...` |
| Task comments | `GET /api/v2/task/{task_id}/comment` |
| Comment cursor page | `GET /api/v2/task/{task_id}/comment?start=<date>&start_id=<id>` |

Hierarchy traversal composes the catalog reads and sorts normalized results. Scoped task traversal
passes supported server filters and then reapplies consistent local filters. Deep search enumerates
Lists; shallow Workspace search may use the Workspace task endpoint. Pagination rejects repeated
pages and stops at hard page/result ceilings. Task-list array filters intentionally use ClickUp's
documented `statuses[]`, `assignees[]`, and `tags[]` query spelling.

Ensure has no new endpoint. It performs a List task search with closed tasks and subtasks included,
requires zero or one exact normalized name match, and delegates the zero-match path to verified
task creation.

## Task creation and mutation contracts

| Operation | Exact write shape |
| --- | --- |
| Create task | `POST /api/v2/list/{list_id}/task` with `name` and only supplied `description`, `status`, `assignees`, `due_date`, `due_date_time`, and `tags` |
| General task update | `PUT /api/v2/task/{task_id}` with only changed `name`, `description`, `priority`, `start_date`, `start_date_time`, or `archived` fields; logical empty description is wire `" "` |
| Set status | `PUT /api/v2/task/{task_id}` with only `{"status":"<canonical label>"}` |
| Set due date | `PUT /api/v2/task/{task_id}` with exact `due_date` milliseconds and `due_date_time` boolean |
| Clear due date | `PUT /api/v2/task/{task_id}` with only `{"due_date":null}` |
| Assign user | `PUT /api/v2/task/{task_id}` with `{"assignees":{"add":[<id>],"rem":[]}}` |
| Unassign user | `PUT /api/v2/task/{task_id}` with `{"assignees":{"add":[],"rem":[<id>]}}` |
| Add comment | `POST /api/v2/task/{task_id}/comment` with `{"comment_text":"<text>","notify_all":false}` |
| Add tag | `POST /api/v2/task/{task_id}/tag/{encoded_tag}` with no JSON body |
| Remove tag | `DELETE /api/v2/task/{task_id}/tag/{encoded_tag}` with no JSON body |
| Upload attachment | `POST /api/v2/task/{task_id}/attachment` with one multipart `attachment` part |
| Delete task | `DELETE /api/v2/task/{task_id}` with no body |

Task creation reads the returned ID, fetches that task, and verifies destination List, name, and
every supplied supported field. Date-only values are allowed to be canonicalized to another
millisecond value on the same UTC calendar date; timed values require exact instant equality.
Attachment-aware create validates every file before the task POST, then performs and verifies
uploads in option order.

General mutations read first and omit already-satisfied fields. A changed request is followed by a
task read that verifies each requested field. Tag and assignee changes are idempotent. Status
changes additionally fetch the task's List and resolve exactly one canonical label before the
write. Semantic completion selects only recognized completion labels/types.

Comment creation is verified by returned ID and exact text in a comment listing. Exact comment
lookup has no single-comment v2 endpoint, so it uses the documented cursor pair until the ID is
found or the bounded search reaches the end.

Attachment upload requires the returned ID and title on a fresh task read. Download is not a
ClickUp API call after that authoritative read: a credential-free client follows at most five
revalidated redirects and streams at most 100 MiB into an atomic local output operation.
Production initial and redirect URLs require HTTPS on exact `attachments.clickup.com`, exact
`attachments-public.clickup.com`, or the apex/subdomains of `clickup-attachments.com`. Plain HTTP
localhost is accepted only when the configured API base is localhost.

## Batch contracts

Batch adds no write endpoint. Strict JSONL parsing occurs before a client is created. Plan and
apply both perform a complete task-read preflight; any requested status causes the relevant List
read. Every changing `add_tag` additionally resolves the task's List, its owning Space, and
`GET /api/v2/space/{space_id}/tag` with JSON content type. List payloads and Space catalogs are
cached, exactly one case-insensitive catalog match supplies the canonical wire name, and existing
tag no-ops do not require a catalog. Missing, ambiguous, or invalid catalogs fail before any write.

Apply requires `--yes` before reading the manifest or using credentials. After preflight, each
operation delegates to the same task/status/due-date/assignee/tag/lifecycle service described
above. Operations and tasks are serial. A failure reports the manifest hash, exact line/task and
operation, completed IDs, operation results, and last verified task state. No rollback request is
issued.

## Time-entry contracts

| Operation | Request |
| --- | --- |
| Current timer | `GET /api/v2/team/{workspace_id}/time_entries/current` with optional `assignee` |
| Bounded list | `GET /api/v2/team/{workspace_id}/time_entries?start_date=<ms>&end_date=<ms>&...` |
| Single entry read | `GET /api/v2/team/{workspace_id}/time_entries/{entry_id}` |
| Workspace time tags | `GET /api/v2/team/{workspace_id}/time_entries/tags` with JSON content type |
| Start timer | `POST /api/v2/team/{workspace_id}/time_entries/start` with only supplied `tid`, `description`, and `billable` |
| Stop timer | `POST /api/v2/team/{workspace_id}/time_entries/stop` with an empty body |
| Add manual entry | `POST /api/v2/team/{workspace_id}/time_entries` with exact `start`/`duration` and only supplied `tid`, `description`, and `billable` |
| Update entry | `PUT /api/v2/team/{workspace_id}/time_entries/{entry_id}` with required preserved `tags` plus only changed supported fields |
| Delete entry | `DELETE /api/v2/team/{workspace_id}/time_entries/{entry_id}` with an empty body; official 200 JSON `data.id` must match |

Time list ranges are start-inclusive and end-exclusive and cannot exceed 366 days. Only one task,
Space, Folder, or List location filter is sent. Current timer is read before start; an existing
timer prevents the write. Stop captures the current ID before writing and then proves it is no
longer current.

The official manual-add 200 body contains created fields but no ID. A directly observed `id` or
`data.id` remains a fast path; otherwise add queries a one-second-padded range around the exact
requested start/end, applies the task filter when supplied, and requires one exact
start/duration/task/description/billable match, with compatible 200-body values as extra evidence.
Zero/multiple matches are a confirmed-created `created_but_unidentified` partial outcome with
Workspace, start, candidate IDs, and a strong no-retry warning. Exactly one ID is then verified by
the singular read. The POST is never retried.

Update reads first and sends the smallest body ClickUp permits. Complete tag objects are preserved
directly; string-only tags are mapped case-insensitively to exactly one full
`{name,tag_fg,tag_bg}` object from the Workspace catalog before PUT. Running entry timing changes
are rejected. Delete pre-reads the exact entry and treats pre-read 404 or the observed HTTP 200
null/empty singular shape as unchanged. It validates `data.id` when returned, then requires a
separate 404 or null/empty singular absence proof; production may omit the DELETE response ID.
Wrong IDs, lost responses, or failed absence checks preserve `entry_id` in a typed unknown outcome.

## Live-probe and release-harness basis

Development probes use only a dedicated disposable sandbox. The opt-in pytest repeats one broader
lifecycle, but its first possible write is dominated by an exact containment proof: configured
List ID, exact List name `ClickUp CLI Test Sandbox`, configured Space ID, and membership of that
Space/List in the configured Workspace tree.

All temporary content carries a new UUID. Cleanup maps contain only IDs returned by that run.
Before task deletion, an exact task read must still show the sandbox List and marker in both name
and description; deletion must be followed by HTTP 404. Before manual time-entry deletion, the
exact captured entry must still carry the marker and point to a run-owned task whose own sandbox
containment is re-proved. After either the CLI callback or direct-client fallback, cleanup performs
its own exact GET and requires HTTP 404 or observed HTTP 200 null/empty data before removing the
allow-list ID. Cleanup refuses and
reports the surviving ID if any proof or absence check fails. No pre-existing time entry is
deleted, and live coverage never calls timer start or stop.

Ordinary tests and CI use localhost only and select `-m 'not live'`.

## Rate-limit basis

ClickUp documents HTTP 429 responses with an absolute Unix timestamp in `X-RateLimit-Reset`. The
client also accepts standard `Retry-After` seconds or HTTP dates. Delay and retry count are bounded
and covered with injected clock/sleep functions. GET, PUT, and DELETE may be retried; potentially
non-idempotent POSTs are not blindly retried.
