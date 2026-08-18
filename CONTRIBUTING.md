# Contributing

Thank you for improving clickup-cli. Keep changes focused on deterministic behavior, narrow API
contracts, explicit partial outcomes, and safe automation.

## Development setup

Install Python 3.11 or newer and uv, then sync the committed lockfile:

```console
uv sync --all-groups --locked
```

Before submitting a change, run the release checks exactly:

```console
uv run ruff format .
uv run ruff check .
uv run mypy
uv run pytest -m 'not live'
uv build
```

Formatting is intentionally run in write mode so the checked worktree is canonical. Review the
result and commit it. Do not include build artifacts.

## Code and contract boundaries

- Support Python 3.11 and newer with complete type annotations.
- Put HTTP paths, headers, bodies, retries, and transport validation in `ClickUpClient`.
- Put task creation/status/due-date/assignment semantics in `TaskService`; discovery, attachments,
  task field mutations, batch, and time tracking belong in their focused service modules.
- Keep CLI functions as input/output adapters rather than a second implementation.
- Keep stable JSON additions deliberate; missing scalar values should not change the schema.
- Preserve minimal writes, bounded traversal/input, readback verification, confirmation gates,
  structured partial IDs, proxy isolation, and attachment credential separation.
- Do not add credentials, credential-shaped canaries, Workspace/Space/Folder/List IDs, user IDs,
  live task data, attachment URLs, or time-entry IDs from any real account.
- Use synthetic placeholder IDs and reserved example domains in documentation and fixtures.
- Use ASCII punctuation in public files.

Any HTTP behavior needs a localhost contract that asserts method, complete path/query, relevant
headers, decoded JSON or multipart body, ordering, and response handling. Tests should prove unsafe
input stops before network access and that partial failures retain the identifiers needed for safe
inspection. Ordinary tests must never connect outside localhost or require credentials.

Changes to strict batch parsing need both accepted-schema and fail-closed cases. Changes to live
cleanup helpers need localhost tests proving every ownership check happens before DELETE and that a
failed check issues no DELETE.

## OpenAPI provenance

When refreshing the official specification, record the inspection date, OpenAPI version, API info
version, path count, and SHA-256 in `docs/api-contracts.md`. Do not commit the full snapshot unless
the project explicitly adopts vendoring. Reconcile each supported request contract against the
official document and localhost tests.

## Opt-in live lifecycle

Routine development and CI do not run live tests. A maintainer may explicitly run the single live
lifecycle only with a dedicated sandbox and all required variables:

```text
CLICKUP_LIVE_TEST
CLICKUP_API_TOKEN
CLICKUP_TEST_WORKSPACE_ID
CLICKUP_TEST_SPACE_ID
CLICKUP_TEST_LIST_ID
CLICKUP_TEST_TAG
```

The List must be named exactly `ClickUp CLI Test Sandbox`. Never weaken the Workspace/Space/List
proof, per-run UUID markers, created-ID allow-lists, pre-delete fetches, post-task-delete HTTP 404,
or refusal-to-delete behavior. A live-test addition may mutate only a resource created during that
same run. Do not add live coverage for `time start` or `time stop`; localhost contracts cover them
without risking a human's timer.

Do not place the variable values in commands committed to source, test parametrizations, logs,
screenshots, pull requests, or issue descriptions.
