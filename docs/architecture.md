# Architecture

The project has one reusable core and thin adapters.

```text
Typer CLI adapter
      |
TaskService domain operations
      |
ClickUpClient HTTP boundary
      |
ClickUp direct API
```

## Package boundaries

`clickup_cli.client.ClickUpClient` owns HTTP mechanics: raw personal-token authorization, v2 path
construction, JSON headers, bounded timeouts, bounded 429 retries using `Retry-After` or ClickUp's
`X-RateLimit-Reset`, response validation, and token redaction. It exposes only the endpoint
operations needed by the product. It ignores ambient proxy variables and validates every task or
List ID again at the HTTP boundary, including calls made outside the CLI adapter.

`clickup_cli.domain.TaskService` owns task semantics. It discovers the task's home List, resolves
canonical labels, selects semantic completion statuses, performs idempotence checks, sends minimal
updates through the client, and verifies readbacks. This layer has no Typer dependency.

`clickup_cli.cli` translates Typer inputs into core calls and translates results into stable text
or JSON. Configuration is lazy, so a refused destructive command makes no HTTP request and does not
need to resolve credentials. The console entry point also catches Click usage errors so `--json`
remains machine-readable when arguments or typed options are invalid.

`clickup_cli.config` reads process configuration and parses dotenv assignments as data. It does not
execute, source, or interpolate shell content. `clickup_cli.refs` validates task references before
placing IDs into URL paths.

## Request determinism

Every endpoint method constructs a new request body containing only supported supplied fields. A
status mutation can only reach the HTTP write after task and List reads establish the exact
canonical label. A successful write is not reported until a separate read confirms the label.

The API version is hidden in the client. Commands and domain operations do not contain `/v2`, which
keeps a future v3 endpoint or staged version migration local to the HTTP boundary.

## Testing boundary

The ordinary suite connects only to an in-process HTTP server bound to `127.0.0.1`. The fixture
checks request sequence, method, complete path and query, selected headers, and decoded JSON body.
An autouse socket guard rejects non-local connections. This makes the test an HTTP contract test,
not an in-memory mock of httpx internals.

The opt-in live test is a separate marked lifecycle check. It requires explicit enablement and a
sandbox List, assigns a unique task name, and deletes the created task in a `finally` block.

## Future Hermes adapter

A future Hermes adapter should call `ClickUpClient` and `TaskService` directly rather than invoke
the CLI or duplicate request logic. It can translate Hermes tool inputs and outputs at its own
boundary while retaining the same validation, idempotence, and readback guarantees. If async I/O
becomes necessary, add a parallel transport implementation behind a small protocol while keeping
status selection and result models in the shared domain layer.
