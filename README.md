# clickup-cli

`clickup-cli` provides deterministic ClickUp operations for people, scripts, and agents. Version
0.3.0 adds first-class ClickUp Docs retrieval, verified authoring, and private Markdown/JSON text
snapshots to hierarchy discovery, task operations, attachments, batch manifests, and time tracking.
The distribution is
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
different file with global `--env-file PATH` or `CLICKUP_ENV_FILE`; the process token always wins.
The parser treats the file as data and does not execute or interpolate shell syntax. There is
intentionally no `--token` option, keeping tokens out of command histories and process listings.

Optional `~/.config/clickup-cli/config.toml` contains **no secrets**. Profiles select only an env
file and an IANA timezone (relative env-file paths resolve against the config directory):

```toml
default_profile = "work"

[profiles.work]
env_file = "work.env"
timezone = "Europe/Zurich"

[profiles.personal]
env_file = "personal.env"
timezone = "America/New_York"
```

Select with global `--profile NAME` or `CLICKUP_PROFILE`. The precedence for each setting is
CLI option > environment (`CLICKUP_ENV_FILE`, `CLICKUP_TIMEZONE`, `CLICKUP_PROFILE`) > selected
profile > legacy defaults (env file above; UTC timezone). `CLICKUP_API_TOKEN` in the process
always outranks the selected env file. Malformed config, unknown profiles, and invalid IANA
timezones fail before network access. Keep tokens only in process environment or private env files.

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
clickup --profile work --timezone Europe/Zurich task show '<task-id>'
clickup --base-url https://example.invalid/api workspace list
clickup --version
```

`--json` emits a stable envelope. `--env-file`, `--profile`, `--timezone`, `--base-url`, and
`--version` are the other global options.

## Command map

The root groups and their current responsibilities are:

| Group | Commands |
| --- | --- |
| `auth` | `whoami` |
| `workspace` | `list`, `tree` |
| `member` | `list` |
| `list` | `show`, `statuses` |
| `task` | `list`, `search`, `ensure`, `show`, `context`, `create`, `update`, `status`, `set-status`, `complete`, `assign`, `unassign`, `archive`, `unarchive`, `delete` |
| `task comment` | `show`, `list`, `add`, `edit` |
| `task due-date` | `set`, `clear` |
| `task priority` | `clear` |
| `task start-date` | `clear` |
| `task tag` | `add`, `remove` |
| `task attachment` | `list`, `upload`, `download` |
| `task batch` | `plan`, `apply` |
| `time` | `current`, `list`, `start`, `stop`, `add`, `update`, `delete` |
| `doc` | `list`, `show`, `pages`, `search`, `create`, `export` |
| `doc page` | `show`, `create`, `update`, `append`, `prepend`, `replace`, `ensure` |

Use a group's `--help` for the complete Typer syntax. The sections below explain the behavior and
the options that affect safety or selection.

## ClickUp Docs

Docs use the public v3 API internally; existing task operations remain v2. Reads are live, with no
cache. Only authorized Docs/pages are accessed. Bare Doc IDs require an explicit numeric
`--workspace-id`; bare page IDs also require `--doc`. No workspace is inferred from an ID prefix.
Supported reference forms (all IDs below are synthetic) are:

```text
https://app.clickup.com/123/docs/d-1
https://app.clickup.com/123/v/dc/d-1
https://app.clickup.com/123/v/dc/d-1/p-1
```

URL/flag conflicts, path injection, deceptive hosts, userinfo, non-HTTPS URLs, and unrecognized
query/fragment forms are rejected before HTTP. In particular, query `page-id` links are not yet
supported; use a literal page ID with explicit Doc/workspace context instead. Source links preserve
literal authorized IDs and point to the exact page, not a guessed workspace.

### Retrieval and local search

```console
clickup doc list --workspace-id 123 --all --limit 100
clickup doc list --workspace-id 123 --archived --creator 42 --parent-id 987 --parent-type LIST
clickup doc show 'https://app.clickup.com/123/docs/d-1'
clickup doc pages d-1 --workspace-id 123 --tree --all
clickup doc search 'Procedure' --doc d-1 --workspace-id 123
clickup doc search 'STRASSE' --doc d-1 --workspace-id 123 --content --max-pages 100
clickup doc search 'Procedure' --workspace-id 123 --max-docs 10 --max-pages 100
clickup doc page show 'https://app.clickup.com/123/v/dc/d-1/p-1' --format markdown
clickup --json doc page show p-1 --doc d-1 --workspace-id 123 --format plain --offset 0 --limit 20
```

Doc listing supports `--archived`, `--deleted`, `--creator`, and paired `--parent-id`/`--parent-type`
(SPACE, FOLDER, LIST, EVERYTHING, WORKSPACE). It follows opaque `next_cursor` using the documented
`cursor` request parameter only with `--all`, never displaying cursor values. Repeated cursors fail
closed. A missing, null, or empty-string `next_cursor` marks the verified end of traversal.
API page sizes are 10-100 even when the requested result limit is smaller.

Page listing requests all recursive descendants, validates identities/parents, and represents every
returned node with parent ID, depth, full breadcrumb, and source URL. Tree rendering does not hide
nested pages or bypass output bounds. All descendants count toward the limit; duplicate/cyclic or
inconsistent hierarchies are errors, not silent deduplication. Hierarchies are capped at 10,000
pages and 128 levels. `total` is the observed recursive count, not the number of roots.

Search is **local page-title search** by default; `--content` fetches recursive page bodies with one
bulk request per Doc, avoiding a request per page, then applies Unicode `casefold` matching.
There is no invented provider full-text query parameter. A Doc or
explicit Workspace scope is mandatory. Workspace search discovers accessible Docs and shares one
page-scan ceiling across them. Inaccessible pages fail the command rather than yielding a successful
no-match. Matches include bounded snippets, 1-based source line ranges for body matches, and exact
deep links. Text not returned by ClickUp's export API is not evidence that it is absent in the UI.

Defaults: at most 50 results, 50 searched pages, 10 searched Docs, and one Doc-list cursor request.
`--all` follows Doc cursors (at most 100 requests) and raises unspecified ceilings to 1,000 results/
searched pages and 100 searched Docs. Explicit `--limit`, `--max-pages`, and `--max-docs` still win.
Console collection payloads also have a 48,000-byte JSON budget; `complete`, `has_more`,
`returned_count`, warnings, and nullable totals distinguish complete from partial output/search.
Use full export rather than treating a console ceiling as an exhaustive snapshot.

Page `show` offsets are 0-based **lines**. `--limit` defaults to 100 lines, maximum 1,000. Console
body output is additionally capped at 16,000 characters and 48,000 escaped JSON bytes. A long line
returns `next_offset` and `next_column`; resume with both `--offset` and `--column`, retaining the
same content format. JSON includes literal workspace/doc/page IDs, name, parent, breadcrumb, URL,
nullable provider timestamps, `retrieved_at`, full original-content `sha256`, line continuation,
counts, and export warnings. Inline `data:image` blobs are replaced only for display by a clear
marker with `omitted_data_images`; ordinary image URLs remain. The hash covers full untruncated
provider text, including inline images. Oversized metadata fails safely rather than flooding output.

### Safe authoring

```console
clickup doc create 'Handbook' --workspace-id 123
clickup doc create 'Shared handbook' --workspace-id 123 --visibility public --create-page
clickup doc create 'List handbook' --workspace-id 123 --parent-id 987 --parent-type LIST
clickup doc page create d-1 --workspace-id 123 --name 'Overview' --content-file ./overview.md
clickup doc page create d-1 --workspace-id 123 --name 'Child' --parent-page p-1 \
  --content-file ./child.md --sub-title 'Notes'
clickup doc page update p-1 --doc d-1 --workspace-id 123 --name 'Renamed' --sub-title 'New subtitle'
clickup doc page append p-1 --doc d-1 --workspace-id 123 --content-file ./addition.md
clickup doc page prepend p-1 --doc d-1 --workspace-id 123 --content-file ./introduction.md
clickup doc page replace p-1 --doc d-1 --workspace-id 123 --content-file ./replacement.md \
  --expect-sha256 '<full-sha256-from-same-format-read>' --acknowledge-loss
clickup doc page ensure d-1 --workspace-id 123 --name 'Overview' --content-file ./overview.md
```

Doc create explicitly sends PRIVATE visibility and `create_page: false` by default, avoiding blank
pages. Supported `--visibility` values are private, public, personal, hidden. GET verifies stable
identity, name, public boolean, and any supplied parent. GET does not expose complete visibility or
the create-page option: personal/hidden privacy semantics and initial-page intent are not independently
verified; the result retains requested visibility and warnings rather than overclaiming.

Page content files are regular UTF-8 files capped at 1 MiB, read before writing; stdin is not
supported. `--format markdown|plain` explicitly serializes `text/md|text/plain`. Parent-page URLs
must identify the same Doc/workspace. Metadata-only update never sends an empty content field.
Append/prepend use native `content_edit_mode`, not read-export-and-replace. Content-only edits omit
name/subtitle and verify those fields remained unchanged. Every successful create/edit has a separate
GET readback; content verification normalizes CRLF and terminal newlines. Native append/prepend
also accept the provider's one- or two-newline separator between imported blocks, without relaxing
spaces, links, or internal Markdown structures.

Whole-content replace requires both a current full SHA256 and `--acknowledge-loss` because exported
text can lose rich blocks. A stale hash rejects before PUT. **This is preflight only, not atomic
compare-and-swap**: another writer can edit between GET and PUT. Ensure NFC-normalizes/strips only
outer whitespace from names and exact-matches siblings in the specified parent, preserving case.
Zero matches create, one returns unchanged, multiple return structured ambiguity. It never reconciles
content and is not concurrency-proof uniqueness.

Doc/page creates and all page edits are never automatically retried, including native append/prepend
PUTs. Ambiguous dispatched writes return `outcome_unknown`; known create IDs survive as
`created_but_unverified`, and completed edits with failed readback are `edited_but_unverified`.
Structured details retain known workspace/doc/page IDs and `retry_safe: false`; inspect before retrying.
No unsupported Docs delete/archive, page move, Doc rename, ACL, or private API operation is offered.

### Private text snapshot export

```console
clickup doc export d-1 --workspace-id 123 --format markdown --output ./handbook-snapshot
clickup doc export 'https://app.clickup.com/123/docs/d-1' --format json --output ./handbook-json
```

Export fetches full recursive content, not console excerpts. `manifest.json` records Doc/page IDs,
hierarchy, names, parent IDs, breadcrumbs, source URLs, nullable provider timestamps, retrieval time,
filenames, original `content_sha256`, actual-byte `file_sha256`, warnings, and `text_snapshot` kind.
Markdown files contain exact raw exported text; JSON files wrap equivalent full content and metadata,
so their file/content hashes are intentionally distinct. Page-ID-based filenames prevent duplicate
title collisions/path traversal; Unicode titles are retained in the manifest.

This is **not a full-fidelity backup or guaranteed round-trip restore**. Embeds, synced content, views,
comments, covers, styles, and some formatting are unavailable in the public text API. No internal links
are followed and no attachments/images are downloaded. Reads across a Doc are not an atomic
point-in-time revision.

The parent directory must already exist and be user/root-owned, without group/world write permission.
All path components are pinned with no-follow directory handles; symlink output/parents are refused.
A private staged directory is installed atomically with Linux `renameat2` no-overwrite semantics;
Linux is currently required for export. Existing targets are always rejected, including a raced target;
there is no unsafe `--force`. Directories are mode 0700 and files 0600. HTTP/filesystem failure removes
the hidden stage and installs no apparently complete snapshot. The full snapshot ceiling is 100 MiB.

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
`overdue`, `none`, or `next:Nd` in the selected timezone, including DST-length days.
`--include-closed`, `--include-subtasks`, and
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
clickup task ensure 'Investigate timeout' --list-id '<list-id>' \
  --description-file ./brief.md
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
clickup task create 'Review brief' --list-id '<list-id>' --description-file ./brief.md
clickup task create 'Collect evidence' --list-id '<list-id>' \
  --due-date 2030-01-02T15:04:05Z --attach ./trace.txt --attach ./screenshot.png
```

`--description-file PATH` and `--description TEXT` are mutually exclusive for create and ensure.
Files must be regular UTF-8 files no larger than 1 MiB; they are read before the create POST.
The create POST contains only supplied task fields. A separate task read verifies the ID, List,
name, and all supplied supported fields before success. Description readback permits stripped
trailing padding and narrow Markdown-equivalent bullet/escaping changes, but not missing links
or truncated content. Attachments are validated before the task
POST, uploaded serially only after task verification, and verified by returned ID and title on a
fresh task read.

Non-idempotent partial outcomes are explicit:

- `outcome_unknown` means the create POST did not return a usable task ID. Inspect the destination
  List before retrying.
- `created_but_unverified` contains `task_id`; the task exists but task readback or normalization
  did not finish safely.
- `created_but_attachment_failed` contains `task_id`, `failed_path`, the ordered verified
  `uploaded_attachment_ids`, and the nested `cause_type`. If the failed upload returned an ID but
  its readback failed, distinct `failed_attachment_id` preserves that known-but-unverified ID.
  Inspect that task before retrying any attachment.
- Standalone uploads distinguish `attachment_outcome_unknown` from
  `attachment_uploaded_but_unverified`, which includes the known attachment ID when available.

These errors are designed for callers to retain structured IDs and avoid duplicate creation.

## Task reads and mutations

Read task state, comments, and status with:

```console
clickup task show '<task-id-or-url>'
clickup --json task context '<task-id-or-url>' --comments 10 --attachments 5
clickup task status '<task-id-or-url>'
clickup task comment list '<task-id-or-url>'
clickup task comment add '<task-id-or-url>' 'A concise update'
clickup task comment add '<task-id-or-url>' 'Please review' --mention 101 --mention alex@example.org
clickup task comment show '<task-id-or-url>' '<comment-id>'
clickup task comment show 'https://app.clickup.com/t/<task-id>?comment=<comment-id>'
clickup task comment edit '<task-id-or-url>' '<comment-id>' 'Updated message' --expect-sha256 '<revision_sha256>'
```

`task context` reads the exact task, its home List and valid statuses, then only enough comment
cursor pages to fulfill `--comments` (default 10, maximum 100); attachments come from that same
task response (`--attachments` default 10, maximum 100). Zero skips comment retrieval. JSON
includes `task`, `list`, `path` (Space / real Folder / List), `statuses`, `parent_id`,
`subtask_ids`, and bounded `comments` and `attachments` objects with `items`, `returned_count`,
`has_more`, and `cursor`. `has_more: null` means the API did not establish completeness; `true`
means a local page contained additional items, `false` means an empty comment page proved the end.
Only comments have an API cursor (`start` and `start_id`); attachments have no supported cursor.
ClickUp's synthetic `hidden` folder on folderless Lists is omitted from the context path.

Comment lookup follows ClickUp's cursor until it finds the requested ID or safely reaches the end.
Comment creation always sends `notify_all: false` and verifies the returned comment ID and exact
text when no mention is supplied. With one or more `--mention` options, it reads the task's
Workspace membership, resolves only exact case-insensitive username/email or numeric member ID,
refuses ambiguous names and nonmembers, and sends native rich `tag` segments followed by text.
`--notify-all` explicitly sets `notify_all: true`; `false` does **not** suppress normal
assignee/watcher notifications. Rich writes require the returned tag IDs and text segments to
match the request. A disconnected POST is `outcome_unknown` and is never automatically retried; a
known created ID with failed readback is returned as `created_but_unverified` with `comment_id`.
ClickUp can return an opaque `{"type":"tag"}` segment without `user.id` (observed for a
self-mention in the Test Workspace), even though flattened `comment_text` renders `@Name`. The
CLI preserves such segments for reads but **does not infer a user ID from display text**: the
mention write remains `created_but_unverified` with its exact ID, and editing that opaque tag is
refused. Inspect the comment in ClickUp before deciding whether to retry; do not post a duplicate.

Comment `show`/`list` JSON now adds `revision_sha256`, `segments`, and `mentions` to each comment
without removing existing fields. The hash covers the normalized comment ID, flattened text and
rich segments (not the mutable display date). Editing requires a current hash: the CLI reads the
exact task/comment, rejects a stale hash before the PUT, sends only a rich `comment` array to
`PUT /api/v2/comment/{id}`, preserves **identifiable** native mention tags before the new text,
and reads back that exact ID and segments. This preflight is **not atomic compare-and-swap**:
another writer can edit between GET and PUT. Editing replaces the message's non-tag content, so
review any rich formatting before editing; never blindly retry an uncertain write. A failed
post-PUT readback reports `edited_but_unverified` with the exact `comment_id`.

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
timestamp with `Z` or an explicit offset. Date-only values use midnight in the selected timezone
(UTC by default) for writes and local calendar dates for readback verification, including batch
plan/apply; timed values remain exact UTC instants. When ClickUp explicitly returns a false time
flag, the CLI displays a local calendar date. ClickUp may instead omit the flag and normalize the
stored timestamp (observed as `03:00Z` for a date-only due date); then the CLI preserves the ISO
timestamp and null flag rather than guessing intent. Use the configured timezone with
`due_date_ms`/`start_date_ms` when a local day is needed from an ambiguous response. Logical empty
descriptions remain `""` in CLI, batch, and output contracts; task update serializes that clear
request as ClickUp's required single space and accepts either empty or single-space cleared
readback.

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
Production URLs and every redirect require HTTPS plus an exact trusted attachment host:
`attachments.clickup.com`, `attachments-public.clickup.com`, or the apex/subdomains of
`clickup-attachments.com`. Private, loopback, link-local, internal, deceptive, and other public
hosts are rejected. Plain HTTP localhost is enabled only when the configured API base is itself
localhost for socket contracts. Output is installed atomically, existing files require `--force`,
and the download ceiling is 100 MiB.

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
status validation for every task, and resolves every changing tag addition through cached owning
List and Space tag catalogs before the first write. Tag names are case-insensitively canonicalized;
existing-tag no-ops require no catalog. It then applies operations serially with the same verified
single-operation services used by interactive commands:

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

ClickUp's official manual-entry success body has no ID. Add therefore accepts an observed direct
ID as a fast path, otherwise searches a narrow start range (with the task filter when supplied),
requires one exact start/duration/task/description/billable match, then verifies that ID through a
single-entry read. Zero or multiple matches return `created_but_unidentified` with Workspace,
start, candidate IDs, and `retry_safe:false`; do not retry because the POST already succeeded.

Update reads first, preserves the API-required tag array, and maps string-only singular-read tags
through the Workspace time-tag catalog before writing. Empty tags need no catalog and complete tag
objects remain accepted. Delete pre-reads the exact ID, treats an initial 404 or observed
`200 {"data": null}` as an idempotent no-op, validates the deleted `data.id` when ClickUp returns
one, and independently requires either 404 or the null/empty singular absence shape. A missing
delete-response ID is accepted only after that absence proof. Wrong IDs, lost responses, or failed
absence checks return `outcome_unknown` with `entry_id`. Timing changes on a
running entry fail closed. Unknown create/start/update/stop outcomes preserve every known entry ID
so automation can inspect rather than retry blindly.

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
- production attachment sockets are restricted to established ClickUp attachment hosts;
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
marker/task-verified against a run-owned sandbox task before its captured ID can be deleted;
cleanup independently requires either HTTP 404 or ClickUp's observed HTTP 200 null/empty singular
absence after either the CLI delete callback or direct-client fallback before removing the ID from
its allow-list. If proof or absence fails, cleanup reports the
surviving ID. A `finally` block handles structured partial-create IDs and removes manual entries
before tasks.

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
