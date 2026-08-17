# clickup-cli

`clickup-cli` provides deterministic ClickUp task operations for people, scripts, and agents. It
uses one typed Python client and domain layer with a small Typer adapter. The distribution name is
`clickup-agent-cli`; the installed commands are `clickup` and `cu`.

The current MVP deliberately supports a narrow set of ClickUp API v2 operations. It favors exact
requests, validated status transitions, stable JSON, and safe failure over broad API coverage.

## Installation

Python 3.11 or newer is required. Install directly from GitHub with uv:

```console
uv tool install git+https://github.com/ribaricplusplus/clickup-cli
```

Both entry points expose the same CLI:

```console
clickup --help
clickup --version
cu --help
```

For local development:

```console
git clone https://github.com/ribaricplusplus/clickup-cli
cd clickup-cli
uv sync --all-groups
uv run clickup --help
```

## Configuration

Set a ClickUp personal API token in the process environment:

```console
export CLICKUP_API_TOKEN='<personal-token>'
```

If the variable is absent, the CLI reads dotenv syntax from
`~/.config/clickup-cli/env` by default:

```dotenv
CLICKUP_API_TOKEN='<personal-token>'
```

Choose another file with the global `--env-file PATH` option. Process environment always wins.
The parser treats the file as data and never executes shell syntax. There is intentionally no
`--token` option, which keeps tokens out of command histories and process listings.

Direct API requests default to `https://api.clickup.com/api`. Override the base with
`CLICKUP_API_BASE_URL` or the global `--base-url URL` option. The option takes precedence. A custom
base should end at the API root; the client appends the selected version and endpoint path. Custom
non-local bases must use HTTPS. Plain HTTP is accepted only for localhost contract servers.

Personal tokens are sent as the raw `Authorization` header value, as required by ClickUp. They are
never prefixed with `Bearer`, printed, or included in errors.

## Commands

Global options must appear before the command group:

```text
--json             Emit stable machine-readable JSON.
--base-url URL     Override CLICKUP_API_BASE_URL.
--env-file PATH    Select the fallback dotenv file.
--version          Show the installed version and exit.
```

Supported commands:

```console
clickup auth whoami
clickup task show '<task-id-or-url>'
clickup task status '<task-id-or-url>'
clickup task set-status '<task-id-or-url>' 'In Progress'
clickup task complete '<task-id-or-url>'
clickup task comment list '<task-id-or-url>'
clickup task comment add '<task-id-or-url>' 'A concise update'
clickup task due-date set '<task-id-or-url>' 2030-01-02
clickup task due-date set '<task-id-or-url>' 2030-01-02T15:04:05+01:00
clickup task due-date clear '<task-id-or-url>'
clickup task assign '<task-id-or-url>' 101
clickup task unassign '<task-id-or-url>' 101
clickup task create 'Investigate failure' --list-id '<list-id>'
clickup task create 'Fix failure' --list-id '<list-id>' --description 'Reproduce first' \
  --status 'Open' --assignee 101 --assignee 202
clickup task delete '<task-id-or-url>' --yes
```

Task references can be native IDs or ClickUp URLs in either supported form:

```text
https://app.clickup.com/t/<task-id>
https://app.clickup.com/t/<workspace-id>/<task-id>
```

Delete refuses to make an API request unless `--yes` is present.

Date-only due dates use `YYYY-MM-DD` and send `due_date_time: false`. Timed due dates must
include `Z` or an explicit UTC offset; the CLI converts them to milliseconds and sends
`due_date_time: true`. ClickUp can canonicalize the stored millisecond value for a date-only
due date. The CLI verifies the returned UTC calendar date, reports ClickUp's read-back value as
`due_date_ms`, and verifies timed values exactly.

Comment creation always sends `notify_all: false`; ClickUp's ordinary notification rules still
apply. Assignee commands accept ClickUp numeric user IDs and avoid a write when the requested
membership is already present.

## Deterministic status behavior

`set-status` and `complete` use the same guarded flow:

1. Read the task and identify its home List.
2. Read that List and discover its valid statuses.
3. Select a canonical status label before any write.
4. Return a no-op when the task is already in that status.
5. Send only `{"status":"<canonical label>"}` in the update.
6. Read the task again and require the returned status to match.

Explicit status matching is case-insensitive, while the exact ClickUp label is retained on the
wire. Invalid labels fail before the update and show the available labels.

`complete` considers only terminal statuses whose labels semantically mean completion. Its label
priority is `completed`, `complete`, `done`, then `closed`, and it accepts ClickUp status types
`done` and `closed`. Labels such as `on hold`, `review`, and `archived` are never selected only
because they have a terminal-like type.

## JSON output

Pass global `--json` for a stable envelope. Successful output has this form:

```json
{"ok":true,"result":{"status":"Open","task_id":"<task-id>"}}
```

Expected API, configuration, and domain failures use stderr and exit code 1. CLI usage validation
failures use exit code 2. With `--json`, both use this machine-readable form:

```json
{"error":{"message":"concise explanation","type":"invalid_status"},"ok":false}
```

Task results use the stable fields `id`, `name`, `description`, `status`, `status_type`, `list_id`,
and `url`. Missing API fields are represented as `null` instead of changing the schema.

## API v2 and v3

The ClickUp API root is configurable, but API version selection is intentionally not a CLI
concern. The supported user, task, and List endpoints currently use v2. Version construction lives
inside `ClickUpClient`, so a later endpoint can use v3 or a migration can happen without leaking
version details into commands and domain logic.

## Safety model

- The token comes only from the process environment or a non-executable dotenv file.
- JSON writes include only the fields documented for each operation.
- Status writes are list-validated, minimal, idempotent, and verified by readback.
- Comment creation is verified by comment ID and exact text.
- Due-date and assignee writes read first, send only the requested field delta, and read back.
- Delete requires explicit confirmation and does no request otherwise.
- HTTP timeouts are bounded. HTTP 429 honors both `Retry-After` and ClickUp's documented
  `X-RateLimit-Reset` timestamp with bounded delay and retry count.
- The HTTP client ignores ambient proxy variables so the raw token cannot be redirected to a
  configured proxy. Explicit proxy support would require a separate credential-safe design.
- Errors are concise and redact the configured token, including when an API error echoes it.
- The ordinary test suite blocks every network connection except localhost.

## Testing

The contract suite starts a real in-process HTTP server on `127.0.0.1`. CLI commands run through
Typer's `CliRunner` against that server. The server records requests in order and validates exact
methods, paths and queries, relevant headers, and decoded JSON bodies. This observes real httpx
socket traffic rather than replacing its transport.

Run all ordinary checks:

```console
uv sync --all-groups
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest -m 'not live'
uv build
```

### Opt-in live sandbox test

The live test is skipped unless explicitly enabled. It requires a disposable sandbox List. It
invokes every shipped operation through the CLI: identity, create, show, comment add/list,
date-only and timed due-date set/clear, assign/unassign, status, set-status, semantic completion,
and guarded deletion. API read-backs verify each mutation, the normal path requires post-delete
HTTP `404`, and a `finally` block provides fallback cleanup.

```console
CLICKUP_LIVE_TEST=1 \
CLICKUP_API_TOKEN='<personal-token>' \
CLICKUP_TEST_LIST_ID='<sandbox-list-id>' \
uv run pytest -m live tests/test_live.py
```

Never point this test at a production List. CI always runs with `-m 'not live'` and has no ClickUp
credentials.

## Project documents

- [Architecture](docs/architecture.md)
- [API contract provenance](docs/api-contracts.md)
- [Contributing](CONTRIBUTING.md)
- [Security policy](SECURITY.md)
- [Changelog](CHANGELOG.md)
- [MIT license](LICENSE)
