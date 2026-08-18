# Architecture

The project has one synchronous core and a thin Typer adapter. Commands compose focused services;
only the client knows HTTP paths and API versions.

```text
Typer CLI adapter
  |-- DiscoveryService ------ hierarchy, scoped list/search, ensure
  |-- TaskService ----------- create, status, comments, due date, assignees
  |-- TaskMutationService --- fields, tags, archive state
  |-- AttachmentService ----- upload/list/download
  |-- BatchService ---------- strict preflight and serial composition
  `-- TimeTrackingService --- time reads and verified mutations
                    |
              ClickUpClient
                    |
            ClickUp direct API
```

## Package boundaries

### HTTP and configuration

`clickup_cli.client.ClickUpClient` owns raw personal-token authorization, internal v2 path
construction, headers, encoded queries and tag paths, multipart upload, bounded timeouts, bounded
429 retries, response validation, and token redaction. It exposes only endpoint methods needed by
the product. Task, hierarchy, Workspace, List, time-entry, and attachment-upload IDs are validated
again at this boundary, including when the client is used without the CLI. The HTTP pool disables
ambient proxy discovery.

`clickup_cli.config` resolves the API base and token lazily and parses dotenv assignments as data.
It does not execute, source, or interpolate shell content. Lazy configuration lets confirmation
failures stop before credential resolution or network access.

`clickup_cli.refs` validates native IDs and supported ClickUp task URLs before an ID can enter an
HTTP path. It extracts a comment ID only from a validated task deep link.

### Stable task and discovery semantics

`clickup_cli.domain.TaskService` owns the original task semantics: stable task/comment
normalization, due-date parsing, verified creation, exact comment lookup, assignee deltas,
List-validated status changes, and semantic completion. Date parsing never depends on the host
timezone. Creation sends supplied task fields in the initial POST and verifies them with a separate
task read.

`clickup_cli.discovery.DiscoveryService` normalizes Workspaces, members, Spaces, Folders, Lists,
and statuses. It traverses a Workspace tree deterministically, paginates tasks with page/result
ceilings, applies consistent local filters, and supports shallow or per-List deep search. Ensure
uses an exact-name List search, fails on ambiguity, returns an existing task unchanged, or delegates
new creation to `TaskService`.

### Focused mutation services

`clickup_cli.task_mutations.TaskMutationService` handles name, description, priority, start date,
tag, and archive state. It compares requested state to a fresh task read, builds one body containing
only changed fields, and verifies the operation-specific state afterward. Description files are
bounded regular UTF-8 files. Tag changes use separate safely encoded endpoints and are idempotent.

`clickup_cli.attachments.AttachmentService` normalizes attachments from authoritative task reads.
Upload validates a regular local file, uses the client multipart boundary, retains known IDs in
partial errors, and requires the returned ID/title on a fresh task read. Download first proves the
attachment belongs to the fetched task, then uses a separate credential-free HTTP client. It
revalidates redirect URLs, requires non-local HTTPS, bounds redirects and bytes, and atomically
installs output.

`clickup_cli.time_tracking.TimeTrackingService` parses bounded UTC ranges and whole-unit durations,
normalizes API response variants, and performs current/list/add/update/delete plus guarded
start/stop orchestration. Non-idempotent operations distinguish an unknown outcome from a known ID.
Updates preserve ClickUp's required tag array, reject unsafe running-entry timing changes, send the
smallest valid body, and verify a single-entry readback.

### Batch composition and CLI output

`clickup_cli.batch` treats the JSONL manifest as untrusted input. It bounds the file, line, and task
counts; parses strict JSON with duplicate-key and nonstandard-constant rejection; normalizes task
references; rejects unknown/conflicting fields; and fixes a deterministic operation order.
`BatchService` fetches every target and validates every status before apply can write. Plan stops
after this preflight. Apply invokes the existing verified service method for each operation,
serially, and exposes partial progress instead of implying rollback.

`clickup_cli.cli` translates Typer values into core inputs and translates service results into
stable text or JSON. It does not reproduce domain logic. The console entry point catches Click
usage errors as well as application errors so global `--json` keeps one envelope for exit codes 1
and 2.

## Request determinism and partial outcomes

Every endpoint method constructs a new request body from supported supplied fields. Domain and
focused services read only the state needed to validate, canonicalize, or minimize a mutation.
Successful writes are not reported until a separate API read confirms the operation-specific
invariant, except permanent deletes where the command reports the successful HTTP response.

Create and time operations can cross a non-idempotent boundary before a response is usable. The
error model therefore separates:

- unknown outcomes, where no reliable created ID exists and callers must inspect before retrying;
- known created IDs whose final verification failed;
- attachment-aware task creation where the task ID and ordered uploaded attachment IDs survive a
  later attachment failure;
- mutation failures that carry the affected task, time-entry, failed manifest line, or last
  verified state needed for safe inspection.

The API version remains hidden in `ClickUpClient`. Commands and services never construct `/v2`, so
a future v3 endpoint can be introduced at the HTTP boundary.

## Test boundaries

The ordinary suite connects only to an in-process HTTP server on `127.0.0.1`. The server records
request sequence, method, complete path/query, selected headers, decoded JSON or multipart body,
and response handling. An autouse socket guard rejects non-local connections. These are transport
contracts over real httpx sockets rather than transport mocks.

The opt-in live test is one marked lifecycle and is disabled unless `CLICKUP_LIVE_TEST=1`. Before
the first write it proves the exact configured List ID/name/Space and the Workspace -> Space -> List
tree. A UUID is carried in all temporary content. Run-created task and manual time-entry IDs are
stored in ownership maps. Cleanup always fetches first, refuses an unowned or unmarked resource,
and requires post-delete task HTTP 404. Manual time-entry cleanup also proves its task is a
run-owned task still in the sandbox. The test never starts or stops a timer.

Containment helpers live under `tests/` rather than the production package and have their own
localhost contracts proving that a failed marker, List, task, or entry check issues no delete.

## Extension boundary

A future agent or Hermes adapter should call `ClickUpClient` and the appropriate service directly,
not invoke the CLI or duplicate request logic. It can translate inputs and outputs at its own
boundary while retaining validation, idempotence, readback, and partial-outcome guarantees. If
async I/O becomes necessary, add a parallel transport behind a small protocol while keeping
selection, parsing, and verification logic in the shared services.
