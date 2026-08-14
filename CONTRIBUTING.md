# Contributing

Thank you for improving clickup-cli. Keep changes focused on deterministic behavior, narrow API
contracts, and safe automation.

## Development setup

Install Python 3.11 or newer and uv, then run:

```console
uv sync --all-groups
```

Before submitting a change, run:

```console
uv run ruff format .
uv run ruff check .
uv run mypy
uv run pytest -m 'not live'
uv build
```

Add or update localhost contract tests for any HTTP behavior. Tests must assert the request method,
path and query, relevant headers, body, and ordering. Ordinary tests must never connect outside
localhost or require credentials.

## Scope and style

- Support Python 3.11 and newer with complete type annotations.
- Put ClickUp HTTP details in `ClickUpClient` and semantics in the domain layer.
- Keep CLI functions as adapters rather than a second implementation.
- Do not add credentials, workspace IDs, List IDs, task data, or credential-shaped test canaries.
- Use placeholder IDs and reserved example domains in docs and tests.
- Use ASCII punctuation in public files.
- Do not weaken delete confirmation, minimal writes, or readback verification.

The live test is destructive and is only for a disposable sandbox List. See the README for its
explicit opt-in variables and cleanup behavior.
