# clickup-cli

`clickup-cli` provides deterministic ClickUp operations for people, scripts, and agents. Version
0.2.0 covers hierarchy discovery, bounded task search and ensure, attachments, verified task
mutations and lifecycle changes, strict batch manifests, and time tracking. The distribution is
named `clickup-agent-cli`; the equivalent installed commands are `clickup` and `cu`.

The project favors exact requests, bounded traversal, stable JSON, explicit confirmation, and
readback verification over broad API coverage.

## Installation

Python 3.11 or newer is required. Install directly from GitHub with uv:

```console
uv tool install git+https://github.com/ribaricplusplus/clickup-cli
clickup --version
```

For local development:

```console
git clone https://github.com/ribaricplusplus/clickup-cli
cd clickup-cli
uv sync --all-groups --locked
uv run clickup --help
```

## Configuration

Set a ClickUp personal API token in the process environment:

```console
export CLICKUP_API_TOKEN='<personal-token>'
```

If the variable is absent, the CLI reads dotenv syntax from `~/.config/clickup-cli/env`. Select a
different file with global `--env-file PATH`; the process environment always wins. The parser
treats the file as data and does not execute or interpolate shell syntax. There is intentionally no
`--token` option, keeping tokens out of command histories and process listings.

Direct requests default to `https://api.clickup.com/api`. `CLICKUP_API_BASE_URL` changes that root,
and global `--base-url URL` takes precedence. A custom base ends at the API root; the client adds
the supported API version and endpoint path. Non-local custom bases require HTTPS. Plain HTTP is
accepted only for localhost contract servers.

Personal tokens are sent as ClickUp's raw `Authorization` value. They are never prefixed with
`Bearer`, included in normal output, or retained in expected errors. The client and attachment
downloader ignore ambient proxy variables.

Global options appear before a command group:

```console
clickup --json task show '<task-id>'
clickup --env-file ./private.env auth whoami
clickup --base-url https://example.invalid/api workspace list
clickup --version
```

`--json` emits a stable envelope. `--env-file`, `--base-url`, and `--version` are the other global
options.

## Command map

The root groups and their current responsibilities are:

| Group | Commands |
| --- | --- |
| `auth` | `whoami` |
| `workspace` | `list`, `tree` |
| `member` | `list` |
| `list` | `show`, `statuses` |
| `task` | `list`, `search`, `ensure`, `show`, `create`, `update`, `status`, `set-status`, `complete`, `assign`, `unassign`, `archive`, `unarchive`, `delete` |
| `task comment` | `show`, `list`, `add` |
| `task due-date` | `set`, `clear` |
| `task priority` | `clear` |
| `task start-date` | `clear` |
| `task tag` | `add`, `remove` |
| `task attachment` | `list`, `upload`, `download` |
| `task batch` | `plan`, `apply` |
| `time` | `current`, `list`, `start`, `stop`, `add`, `update`, `delete` |

Use a group's `--help` for the complete Typer syntax. The sections below explain the behavior and
the options that affect safety or selection.

## Hierarchy and task discovery

Discover IDs rather than copying them into configuration or source files:

```console
clickup workspace list
clickup workspace tree '<workspace-id>'
clickup workspace tree '<workspace-id>' --include-archived
clickup member list --workspace-id '<workspace-id>'
clickup list show '<list-id>'
clickup list statuses '<list-id>'
```

The tree is normalized as Workspace -> Space -> Folder -> List and also includes folderless Lists.
Member output is limited to stable identity fields. List output includes its Space and optional
Folder, archived state, and statuses.

Task listing and search require exactly one scope: `--workspace-id`, `--space-id`, `--folder-id`,
or `--list-id`. Results default to a bounded `--limit`; `--all` removes that result limit while the
10,000-task and 1,000-page traversal ceilings still apply.

```console
clickup task list --space-id '<space-id>' --assignee me --status 'In Progress'
clickup task list --list-id '<list-id>' --tag focus --exclude-tag blocked --due today
clickup task list --workspace-id '<workspace-id>' --include-closed --include-subtasks
clickup task search 'customer timeout' --list-id '<list-id>'
clickup task search 'Exact task name' --folder-id '<folder-id>' --exact-name --deep
```

`--assignee`, `--status`, `--tag`, and `--exclude-tag` are repeatable. Due filters are `today`,
`overdue`, `none`, or `next:Nd`. `--include-closed`, `--include-subtasks`, and
`--include-archived` expand the default result set. Search matches names and descriptions
case-insensitively; `--exact-name` restricts it to a normalized exact name. `--deep` enumerates
Lists instead of relying on a Workspace-wide endpoint, which is useful when description coverage
or hierarchy consistency matters.

Task references elsewhere may be native IDs or either ClickUp task URL form:

```text
https://app.clickup.com/t/<task-id>
https://app.clickup.com/t/<workspace-id>/<task-id>
```

## Ensure and task creation

`task ensure` is a narrow create-if-absent operation within one List:

```console
clickup task ensure 'Investigate timeout' --list-id '<list-id>' \
  --description 'Capture a minimal reproduction' --tag focus
```

It searches that List for a case-insensitive exact task name, including closed tasks and subtasks
but excluding archived tasks. Zero matches use the same verified creation path as `task create`;
one match is returned unchanged with `created: false`; multiple matches fail as ambiguous and list
candidate IDs. Ensure deliberately does not reconcile fields on an existing task. This avoids an
innocent create-if-absent call overwriting later human edits.

Direct creation supports description, status, repeated numeric assignees, a due date, repeated
existing Workspace tags, and repeated attachments:

```console
clickup task create 'Investigate timeout' --list-id '<list-id>' \
  --description 'Reproduce first' --status Open --assignee 101 --tag focus
clickup task create 'Collect evidence' --list-id '<list-id>' \
  --due-date 2030-01-02T15:04:05Z --attach ./trace.txt --attach ./screenshot.png
```

The create POST contains only supplied task fields. A separate task read verifies the ID, List,
name, and all supplied supported fields before success. Attachments are validated before the task
POST, uploaded serially only after task verification, and verified by returned ID and title on a
fresh task read.

Non-idempotent partial outcomes are explicit:

- `outcome_unknown` means the create POST did not return a usable task ID. Inspect the destination
  List before retrying.
- `created_but_unverified` contains `task_id`; the task exists but task readback or normalization
  did not finish safely.
- `created_but_attachment_failed` contains `task_id`, `failed_path`, and the ordered
  `uploaded_attachment_ids`. Inspect that task before retrying any attachment.
- Standalone uploads distinguish `attachment_outcome_unknown` from
  `attachment_uploaded_but_unverified`, which includes the known attachment ID when available.

These errors are designed for callers to retain structured IDs and avoid duplicate creation.

## Task reads and mutations

Read task state, comments, and status with:

```console
clickup task show '<task-id-or-url>'
clickup task status '<task-id-or-url>'
clickup task comment list '<task-id-or-url>'
clickup task comment add '<task-id-or-url>' 'A concise update'
clickup task comment show '<task-id-or-url>' '<comment-id>'
clickup task comment show 'https://app.clickup.com/t/<task-id>?comment=<comment-id>'
```

Comment lookup follows ClickUp's cursor until it finds the requested ID or safely reaches the end.
Comment creation always sends `notify_all: false` and verifies the returned comment ID and exact
text; ClickUp's ordinary notification rules can still apply.

Update one or more supported fields in one minimal PUT and one readback:

```console
clickup task update '<task-id>' --name 'New name' --description 'New description' \
  --priority high --start-date 2030-01-02
clickup task update '<task-id>' --description-file ./description.md
clickup task update '<task-id>' --priority clear --clear-start-date
clickup task priority clear '<task-id>'
clickup task start-date clear '<task-id>'
```

Priority values are `urgent`, `high`, `normal`, `low`, or `clear`. A description file must be a
regular UTF-8 file no larger than 1 MiB. Due and start dates accept `YYYY-MM-DD` or an ISO 8601
timestamp with `Z` or an explicit offset. Date-only values preserve date semantics; timed values
are normalized to an exact UTC instant.

Other idempotent and verified task mutations include:

```console
clickup task due-date set '<task-id>' 2030-01-02
clickup task due-date clear '<task-id>'
clickup task assign '<task-id>' 101
clickup task unassign '<task-id>' 101
clickup task tag add '<task-id>' focus
clickup task tag remove '<task-id>' focus
clickup task archive '<task-id>'
clickup task unarchive '<task-id>'
```

Tag names are safely path-encoded and must already exist in the Workspace. Archive/unarchive is a
reversible task update. Permanent deletion is separate and refuses any request without `--yes`:

```console
clickup task delete '<task-id>' --yes
```

### Deterministic status behavior

```console
clickup task set-status '<task-id>' 'In Progress'
clickup task complete '<task-id>'
```

Both commands read the task, read its home List, select one canonical List label before writing,
avoid an already-satisfied write, send only the status field, and verify a fresh task read. Explicit
status matching is case-insensitive while the exact ClickUp label is retained on the wire.

`complete` considers only terminal statuses whose labels semantically mean completion. Its label
priority is `completed`, `complete`, `done`, then `closed`, and it accepts terminal ClickUp types
`done` and `closed`. A terminal-like type alone never makes labels such as `on hold` or `archived`
eligible.

## Attachments

```console
clickup task attachment list '<task-id>'
clickup task attachment upload '<task-id>' ./evidence.txt
clickup task attachment upload '<task-id>' ./evidence.txt --name 'renamed-evidence.txt'
clickup task attachment download '<task-id>' '<attachment-id>' --output ./evidence.txt
clickup task attachment download '<task-id>' '<attachment-id>' --output ./evidence.txt --force
```

Upload accepts one regular readable file and a plain optional upload name. Download first fetches
the task and requires that exact attachment ID, then fetches its URL without the ClickUp token.
Non-local URLs require HTTPS, redirects are revalidated, output is installed atomically, existing
files require `--force`, and the download ceiling is 100 MiB.

## Strict batch JSONL

A manifest is UTF-8 JSON Lines: one task object per nonblank line. The exact top-level keys are
`task`, `set`, `add_tags`, `remove_tags`, `add_assignees`, and `remove_assignees`. `task` is required
and may be a native ID or task URL. The optional `set` object accepts only:

| Field | Value |
| --- | --- |
| `name` | Non-empty string |
| `description` | String, including empty |
| `status` | Non-empty List status string |
| `due_date` | Accepted date/timestamp string, or `null` to clear |
| `priority` | `urgent`, `high`, `normal`, `low`, or `null` to clear |
| `start_date` | Accepted date/timestamp string, or `null` to clear |
| `archived` | JSON boolean |

Tag arrays contain unique non-empty strings. Assignee arrays contain unique positive JSON integers.
The same tag or user cannot appear in both its add and remove arrays. Unknown or duplicate object
keys, duplicate task references after URL normalization, nonstandard JSON constants, empty
operations, oversized files/lines, and invalid UTF-8 fail before any task write.

Example `changes.jsonl`:

```json
{"task":"task_a","set":{"description":"Prepared by release 0.2.0","priority":"high"},"add_tags":["focus"]}
{"task":"https://app.clickup.com/t/task_b","set":{"due_date":null,"archived":false},"remove_assignees":[101]}
```

Plan is strictly read-only and returns a SHA-256 plus before/after values and change/no-op counts:

```console
clickup task batch plan ./changes.jsonl
```

Apply requires confirmation. It loads the same strict manifest, completes preflight reads and
status validation for every task before the first write, then applies operations serially with the
same verified single-operation services used by interactive commands:

```console
clickup task batch apply ./changes.jsonl --yes
clickup task batch apply ./changes.jsonl --yes --continue-on-error
```

The default stops at the first operation failure and returns structured completed IDs, the failed
line/task/operation, the last verified task state, results, and manifest hash. With
`--continue-on-error`, later tasks continue, but the final result remains a typed partial failure.
Batch is not transactional: a successful earlier mutation is never rolled back or concealed.
Manifests are capped at 1 MiB, 64 KiB per line, 10,000 lines, and 1,000 tasks.

## Time tracking

Time commands require a numeric Workspace ID. Read current state and a bounded
start-inclusive/end-exclusive range with:

```console
clickup time current --workspace-id '<workspace-id>'
clickup time current --workspace-id '<workspace-id>' --assignee 101
clickup time list --workspace-id '<workspace-id>' \
  --from 2026-08-01 --to 2026-09-01 --list-id '<list-id>'
clickup time list --workspace-id '<workspace-id>' \
  --from 2026-08-18T09:00:00Z --to 2026-08-18T17:00:00Z --task '<task-id>' \
  --non-billable
```

List accepts at most one of `--task`, `--space-id`, `--folder-id`, or `--list-id`, plus optional
`--assignee` and exactly one of `--billable` or `--non-billable`. Dates mean midnight UTC
boundaries; timestamps require a timezone; ranges cannot exceed 366 days.

Timer start first proves no timer is running. Stop reads the current timer, stops that exact state,
and verifies it is no longer current; no current timer is a successful no-op:

```console
clickup time start --workspace-id '<workspace-id>' --task '<task-id>' \
  --description 'Investigation' --billable
clickup time stop --workspace-id '<workspace-id>'
```

Manual entries use a timezone-aware start and an ordered whole-unit duration such as `45m`,
`1h30m`, or `90s`:

```console
clickup time add --workspace-id '<workspace-id>' --task '<task-id>' \
  --start 2026-08-18T09:00:00Z --duration 1h30m --description 'Investigation'
clickup time update '<entry-id>' --workspace-id '<workspace-id>' \
  --description 'Updated investigation' --duration 2h --billable
clickup time delete '<entry-id>' --workspace-id '<workspace-id>' --yes
```

Add returns and verifies the created entry ID. Update reads first, preserves the API-required tag
array, writes only changed supported fields, and verifies the readback. Timing changes on a running
entry fail closed. Delete is permanent and confirmation-gated. Unknown create/start/update/stop
outcomes preserve every known entry ID so automation can inspect rather than retry blindly.

## Stable JSON contracts

Successful global `--json` output has one envelope:

```json
{"ok":true,"result":{"status":"Open","task_id":"<task-id>"}}
```

Expected configuration, reference, API, validation, verification, and partial-outcome failures use
stderr and exit code 1. CLI usage failures use exit code 2. Both share the error envelope:

```json
{"error":{"message":"concise explanation","type":"invalid_status"},"ok":false}
```

Current stable task fields are:

```text
archived, assignees, attachments, description,
due_date, due_date_ms, due_date_time,
id, list_id, list_name, name, priority,
start_date, start_date_ms, start_date_time,
status, status_type, tags, url
```

Assignees contain stable `id`, `username`, and `email`. Attachments contain `id`, `title`, `date`,
`extension`, `size`, and `url`. Missing scalar API fields remain `null`; collections that ClickUp
actually returns are normalized without adding credential or member metadata.

## API version and safety model

API version selection is intentionally internal. The supported endpoints currently use ClickUp API
v2, while commands and domain services never build `/v2` paths. This keeps a future v3 endpoint or
staged migration at the HTTP boundary.

The principal safety properties are:

- credentials come only from the process environment or a non-executable dotenv file;
- request bodies contain only documented fields supplied or required for that operation;
- reads needed for validation, idempotence, and minimal deltas happen before writes;
- supported writes are followed by operation-specific readback checks;
- pagination, input files, downloads, retries, time ranges, and batch sizes are bounded;
- non-idempotent partial outcomes distinguish unknown outcomes from known created IDs;
- task, batch, and time-entry deletion paths require explicit confirmation;
- ordinary tests reject every non-local network connection.

## Testing

The ordinary suite uses a real HTTP server on `127.0.0.1`, checks ordered wire contracts, and has no
credentials. Run the release checks with:

```console
uv sync --all-groups --locked
uv run ruff format .
uv run ruff check .
uv run mypy
uv run pytest -m 'not live'
uv build
```

### Opt-in live sandbox lifecycle

The live test is skipped unless explicitly enabled. It requires all six variables:

```console
CLICKUP_LIVE_TEST=1 \
CLICKUP_API_TOKEN='<personal-token>' \
CLICKUP_TEST_WORKSPACE_ID='<sandbox-workspace-id>' \
CLICKUP_TEST_SPACE_ID='<sandbox-space-id>' \
CLICKUP_TEST_LIST_ID='<sandbox-list-id>' \
CLICKUP_TEST_TAG='<existing-workspace-tag>' \
uv run pytest -m live tests/test_live.py
```

Before its first write, the test requires the configured List read to return the exact configured
ID, the exact name `ClickUp CLI Test Sandbox`, and the exact configured Space ID. A separate
Workspace tree must prove that Space and List belong to the configured Workspace.

Every run-created task name and description, attachment, batch manifest, and manual time-entry
description contains one UUID marker. Only IDs returned by the current run enter cleanup
allow-lists. Before every task deletion, the test fetches the exact task and re-proves its sandbox
List plus both task markers; after deletion it requires HTTP 404. A manual time entry is fetched and
marker/task-verified against a run-owned sandbox task before its captured ID can be deleted. If
proof fails, cleanup refuses deletion and reports the surviving ID. A `finally` block handles
structured partial-create IDs and removes manual entries before tasks.

The lifecycle covers discovery, ensure create/no-op, scoped list/search, attachment byte-equality,
task fields/tags/archive, comments/due-date/assignment/status/completion, batch plan/apply, current
time reads, and manual time-entry add/update/list/delete. It intentionally does not call
`time start` or `time stop`, because racing a human timer could affect unrelated work. Ordinary CI
always runs `-m 'not live'` without credentials.

Do not point the live test at a production List. Do not store Workspace, Space, List, user, task,
time-entry, attachment, or token values in the repository.

## Project documents

- [Architecture](docs/architecture.md)
- [API contract provenance](docs/api-contracts.md)
- [Contributing](CONTRIBUTING.md)
- [Security policy](SECURITY.md)
- [Changelog](CHANGELOG.md)
- [MIT license](LICENSE)
