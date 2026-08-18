# Changelog

All notable changes to this project will be documented in this file. The format follows Keep a
Changelog, and the project intends to use Semantic Versioning.

## [Unreleased]

## [0.2.0] - 2026-08-18

### Added

- Workspace listing and normalized Workspace/Space/Folder/List trees, Workspace member discovery,
  and List detail/status inspection.
- Bounded task listing and local filtering from exactly one Workspace, Space, Folder, or List
  scope, plus shallow or deep task search and exact-name task ensure.
- Verified attachment listing and upload, attachment-aware task creation, and isolated atomic
  downloads with redirect, HTTPS, output, and size controls.
- Minimal, readback-verified task updates for name, description, priority, and start date; explicit
  priority/start-date clear commands; idempotent tag add/remove; and reversible archive/unarchive.
- Strict, bounded JSONL task batch manifests with complete read-only planning, confirmation-gated
  serial application, deterministic operation ordering, and structured partial-failure results.
- Current timer and bounded time-entry reads; guarded timer start/stop; and verified manual
  time-entry add, update, and confirmation-gated delete commands.
- Workspace time-tag and Space task-tag catalog reads used to preserve tagged time entries and to
  validate/canonicalize all changing batch tag additions during total preflight.
- Stable task output fields for archived state, priority, start date, and attachments in addition
  to the existing task identity, List, status, description, due date, assignee, tag, and URL fields.
- Localhost contracts for discovery, ensure, attachments, task mutations/lifecycle, batch, and
  time tracking, plus sandbox-containment tests for the opt-in live lifecycle.

### Changed

- Expanded the opt-in live lifecycle to prove the exact dedicated sandbox Workspace, Space, and
  List before writing, mark all run resources with a UUID, allow-list created IDs, and verify
  ownership immediately before task or manual time-entry cleanup, including an independent final
  time-entry 404 or observed HTTP 200 null/empty absence before removing an allow-list ID.
- Manual time creation now supports the official successful response without an ID by uniquely
  matching a narrow date-range read; tagged updates use Workspace catalog metadata; deletion is
  idempotent, validates a response ID when present, and verifies absence through either documented
  404 or ClickUp's observed HTTP 200 null/empty singular response.
- Empty task-description updates now send ClickUp's required single-space clear value while
  retaining logical empty strings in CLI, batch, verification, and output contracts.
- Attachment-aware task creation retains a distinct known-but-unverified failed attachment ID in
  addition to earlier fully verified upload IDs.
- Updated the official API snapshot provenance after the 2026-08-18 re-download; the OpenAPI 3.1.0
  document remains API info version 2.0 with 83 paths and the same recorded SHA-256.

### Security

- Task deletion now remains confirmation-gated while live cleanup additionally refuses any task
  whose exact List, name marker, or description marker cannot be proven and requires HTTP 404
  after deletion.
- Live manual time-entry cleanup only deletes an ID captured during the current run after verifying
  its marker and attachment to a current run-owned sandbox task. Timer start/stop are excluded from
  live coverage to avoid interfering with a human timer.
- Attachment downloads never forward the ClickUp authorization header and ignore ambient proxy
  configuration, as does the direct API client. Production download URLs and redirects are now
  restricted to established ClickUp attachment hosts; private, loopback, link-local, internal,
  deceptive, and untrusted public destinations are rejected.

## [0.1.0] - 2026-08-17

### Added

- Typed ClickUp client and domain service for deterministic task operations.
- `clickup` and `cu` Typer entry points with credential-free version discovery and stable JSON
  success, usage-error, and operation-error envelopes.
- Safe token and dotenv configuration, HTTPS enforcement for non-local custom bases,
  proxy-environment isolation, bounded timeouts, token redaction, and bounded rate-limit retries.
- Rich task show, status inspection, List-validated status changes, semantic completion, comments,
  due-date set/clear, assignee add/remove, verified task creation, and confirmation-gated deletion.
- Structured unknown and partial creation outcomes that prevent blind retries and preserve known
  task IDs.
- Localhost HTTP contract tests, a separately gated live sandbox test, API provenance docs, and
  Python 3.11-3.13 CI for formatting, linting, typing, tests, and builds.

[Unreleased]: https://github.com/ribaricplusplus/clickup-cli/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/ribaricplusplus/clickup-cli/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/ribaricplusplus/clickup-cli/releases/tag/v0.1.0
