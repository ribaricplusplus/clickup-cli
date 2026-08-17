# Changelog

All notable changes to this project will be documented in this file. The format follows Keep a
Changelog, and the project intends to use Semantic Versioning.

## [Unreleased]

### Added

- Typed ClickUp client and domain service for deterministic task operations.
- `clickup` and `cu` Typer entry points with stable JSON output.
- Credential-free `--version` discovery.
- Machine-readable `--json` envelopes for CLI usage errors.
- Safe token and dotenv configuration without a command-line token option.
- HTTPS enforcement for non-local custom API bases.
- Proxy-environment isolation and HTTP-boundary path-ID validation.
- Validated, idempotent, minimal, and readback-verified status mutation.
- Verified task comment list/add commands.
- Date-only and timezone-aware due-date set/clear commands with ClickUp
  canonicalization-aware read-back checks.
- Idempotent numeric assignee assign/unassign commands with minimal `add`/`rem` payloads.
- Confirmation-gated deletion.
- Localhost HTTP contract suite and explicitly gated live sandbox lifecycle test covering comments,
  due dates, assignments, status operations, deletion, and post-delete HTTP 404.
- OpenAPI and live-probe contract provenance documentation.
- Python 3.11, 3.12, and 3.13 CI for formatting, linting, typing, tests, and builds.
