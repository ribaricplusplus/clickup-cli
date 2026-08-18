# Security Policy

## Supported versions

Until the first stable release, security fixes are made on the latest release line only.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting feature for this repository. Do not open a public
issue containing exploit details, credentials, task data, Workspace/Space/Folder/List identifiers,
user identifiers, attachment URLs, time-entry identifiers, or other sensitive information.

Include the affected version, impact, and reproduction steps using synthetic data. Maintainers will
acknowledge a complete report as soon as practical and coordinate a fix and disclosure timeline.

## Credential and transport boundaries

The CLI has no `--token` option. Tokens are read from `CLICKUP_API_TOKEN` or a non-executable dotenv
file and are sent as the raw ClickUp `Authorization` value. Expected errors redact the configured
token. Tokens do not belong in normal output, manifests, docs, fixtures, issue reports, or source
control.

Custom non-local API bases require HTTPS; plaintext HTTP is accepted only for localhost tests. The
direct client disables ambient proxy discovery so `HTTP_PROXY`, `HTTPS_PROXY`, and related process
configuration cannot redirect a raw token. Attachment downloads use an entirely separate client
and never receive the ClickUp authorization header. Production initial URLs and every redirect
require HTTPS on exact `attachments.clickup.com`, exact `attachments-public.clickup.com`, or the
apex/subdomains of `clickup-attachments.com`. Private, loopback, link-local, internal, deceptive,
and other public hosts are rejected. Plain HTTP localhost is permitted only with a localhost API
base for socket contracts. Byte and redirect counts remain bounded.

Users remain responsible for restrictive env-file permissions and immediate rotation of any token
that may have been exposed.

## Mutation and automation boundaries

Supported task and time mutations read the state needed to validate or minimize a write and verify
the result afterward. Statuses are resolved against the task's List. Batch manifests are treated as
untrusted input, strictly parsed and bounded, and completely preflighted before apply writes.
`batch apply`, task deletion, and time-entry deletion require explicit confirmation. Batch is
serial but not transactional; callers must inspect structured partial results rather than assume a
rollback.

Unknown outcomes from non-idempotent operations are never presented as safe failures to retry.
When an ID is known, partial errors preserve it for inspection, including a distinct attachment ID
whose upload returned successfully but whose readback failed. A successful no-ID time-entry create
performs its own narrow exact-match search; zero/multiple matches are explicitly confirmed-created
and unsafe to retry. Delete response loss, invalid response IDs, and failed absence checks preserve
the affected entry ID as an unknown outcome.

## Live-test containment

The opt-in live test is destructive only within one dedicated List. It is disabled unless
`CLICKUP_LIVE_TEST=1` and requires separate Workspace, Space, List, and existing tag configuration.
Before writing, the configured List must return its exact ID, the exact name
`ClickUp CLI Test Sandbox`, and the exact configured Space; a Workspace tree must independently
prove the Space/List membership.

Every temporary resource carries a per-run UUID. Cleanup allow-lists contain only returned IDs.
Task cleanup fetches the exact task and re-proves the sandbox List plus markers in both name and
description before deletion, then requires HTTP 404. Manual time-entry cleanup fetches only the ID
captured during that run and requires its marker and attachment to a run-owned sandbox task. After
either deletion path it independently requires exact-entry HTTP 404 before allow-list removal.
Failed proof or absence means refusal and a surviving-ID report, never deletion. Time `start` and
`stop` are excluded from live coverage to avoid racing a human timer.

Never configure the live test with a production List, and never commit the configured IDs or token.
Ordinary CI must continue to run `-m 'not live'` without credentials and with non-local sockets
blocked.
