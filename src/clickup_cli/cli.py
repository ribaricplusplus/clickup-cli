"""Typer adapter for the reusable ClickUp client and domain library."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar, cast

import typer
from typer._click.exceptions import ClickException
from typer.main import get_command

from clickup_cli import __version__
from clickup_cli.client import ClickUpClient
from clickup_cli.config import DEFAULT_ENV_FILE, resolve_base_url, resolve_token
from clickup_cli.domain import (
    AssignmentMutationResult,
    CommentMutationResult,
    DueDateMutationResult,
    MutationResult,
    TaskService,
    parse_due_date,
    summarize_task,
    task_status,
)
from clickup_cli.errors import (
    APIError,
    ClickUpCLIError,
    ConfirmationError,
    InvalidOperationError,
)
from clickup_cli.refs import (
    parse_comment_ref,
    parse_task_ref,
    validate_native_id,
    validate_numeric_id,
)
from clickup_cli.time_tracking import (
    StopTimeResult,
    TimeListResult,
    TimeMutationResult,
    TimeTrackingService,
    parse_duration,
    parse_time_boundary,
    parse_time_range,
)
from clickup_cli.types import JsonObject, JsonValue

app = typer.Typer(no_args_is_help=True, help="Deterministic ClickUp operations.")
auth_app = typer.Typer(no_args_is_help=True, help="Authentication inspection.")
task_app = typer.Typer(no_args_is_help=True, help="Read and mutate ClickUp tasks.")
comment_app = typer.Typer(no_args_is_help=True, help="Show, list, and add task comments.")
due_date_app = typer.Typer(no_args_is_help=True, help="Set and clear task due dates.")
time_app = typer.Typer(no_args_is_help=True, help="Read and safely mutate time entries.")
app.add_typer(auth_app, name="auth")
app.add_typer(task_app, name="task")
app.add_typer(time_app, name="time")
task_app.add_typer(comment_app, name="comment")
task_app.add_typer(due_date_app, name="due-date")

T = TypeVar("T")


@dataclass(frozen=True)
class AppState:
    base_url: str
    env_file: Path
    json_output: bool

    def client(self) -> ClickUpClient:
        return ClickUpClient(token=resolve_token(self.env_file), base_url=self.base_url)


def _state(context: typer.Context) -> AppState:
    state = context.find_root().obj
    if not isinstance(state, AppState):
        raise RuntimeError("CLI context was not initialized")
    return state


def _emit_json(payload: JsonObject, *, error: bool = False) -> None:
    typer.echo(json.dumps(payload, sort_keys=True, separators=(",", ":")), err=error)


def _fail(state: AppState, error: ClickUpCLIError) -> None:
    if state.json_output:
        error_payload: JsonObject = {"message": str(error), "type": error.error_type}
        error_payload.update(error.details)
        _emit_json(
            {"error": error_payload, "ok": False},
            error=True,
        )
    else:
        typer.echo(f"Error: {error}", err=True)
    raise typer.Exit(code=1)


def _execute(
    state: AppState,
    operation: Callable[[], T],
    *,
    json_result: Callable[[T], JsonObject],
    text_result: Callable[[T], str],
) -> None:
    try:
        result = operation()
    except ClickUpCLIError as exc:
        _fail(state, exc)
    if state.json_output:
        _emit_json({"ok": True, "result": json_result(result)})
    else:
        typer.echo(text_result(result))


def _with_client(state: AppState, operation: Callable[[ClickUpClient], T]) -> T:
    with state.client() as client:
        return operation(client)


def _mutation_json(result: MutationResult) -> JsonObject:
    return {
        "changed": result.changed,
        "previous_status": result.previous_status,
        "status": result.status,
        "task_id": result.task_id,
    }


def _mutation_text(result: MutationResult) -> str:
    if result.changed:
        return f"{result.task_id}: {result.previous_status} -> {result.status}"
    return f"{result.task_id}: already {result.status} (no change)"


def _due_date_json(result: DueDateMutationResult) -> JsonObject:
    return {
        "changed": result.changed,
        "due_date": result.due_date,
        "due_date_ms": result.due_date_ms,
        "due_date_time": result.due_date_time,
        "previous_due_date_ms": result.previous_due_date_ms,
        "task_id": result.task_id,
    }


def _due_date_text(result: DueDateMutationResult) -> str:
    if result.due_date is None:
        return (
            f"{result.task_id}: due date cleared"
            if result.changed
            else f"{result.task_id}: already has no due date (no change)"
        )
    return (
        f"{result.task_id}: due date set to {result.due_date}"
        if result.changed
        else f"{result.task_id}: already due {result.due_date} (no change)"
    )


def _assignment_json(result: AssignmentMutationResult) -> JsonObject:
    return {
        "assigned": result.assigned,
        "assignee_ids": cast(list[JsonValue], result.assignee_ids),
        "changed": result.changed,
        "task_id": result.task_id,
        "user_id": result.user_id,
    }


def _assignment_text(result: AssignmentMutationResult) -> str:
    action = "assigned" if result.assigned else "unassigned"
    if result.changed:
        return f"{result.task_id}: {action} user {result.user_id}"
    return f"{result.task_id}: user {result.user_id} already {action} (no change)"


def _comment_json(result: CommentMutationResult) -> JsonObject:
    return {"comment": result.comment, "task_id": result.task_id}


def _time_mutation_json(result: TimeMutationResult) -> JsonObject:
    return {"changed": result.changed, "entry": result.entry}


def _time_entry_text(entry: JsonObject) -> str:
    duration = entry.get("duration_ms")
    state = (
        "running"
        if entry.get("running")
        else f"{duration}ms"
        if isinstance(duration, int) and not isinstance(duration, bool)
        else "duration=unknown"
    )
    task = f" task={entry['task_id']}" if entry.get("task_id") else ""
    description = f" {entry['description']}" if entry.get("description") else ""
    return f"{entry.get('id')} {state}{task}{description}"


def _billable_value(*, billable: bool, non_billable: bool) -> bool | None:
    if billable and non_billable:
        raise InvalidOperationError("--billable and --non-billable cannot be used together")
    if billable:
        return True
    if non_billable:
        return False
    return None


def _positive_assignee(user_id: int | None) -> int | None:
    if user_id is not None and (isinstance(user_id, bool) or user_id <= 0):
        raise InvalidOperationError("ASSIGNEE must be a positive integer")
    return user_id


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"clickup {__version__}")
        raise typer.Exit()


def _json_requested(arguments: list[str]) -> bool:
    for argument in arguments:
        if argument == "--":
            return False
        if argument == "--json":
            return True
    return False


@app.callback()
def configure(
    context: typer.Context,
    _version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show the installed version and exit.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit stable JSON output."),
    base_url: str | None = typer.Option(
        None,
        "--base-url",
        help="Override CLICKUP_API_BASE_URL.",
        metavar="URL",
    ),
    env_file: Path = typer.Option(
        DEFAULT_ENV_FILE,
        "--env-file",
        help="Dotenv file used after CLICKUP_API_TOKEN.",
        metavar="PATH",
    ),
) -> None:
    """Configure output and direct API access."""

    try:
        resolved_base_url = resolve_base_url(base_url)
    except ClickUpCLIError as exc:
        provisional = AppState(base_url="", env_file=env_file, json_output=json_output)
        _fail(provisional, exc)
    context.obj = AppState(
        base_url=resolved_base_url,
        env_file=env_file,
        json_output=json_output,
    )


@auth_app.command("whoami")
def whoami(context: typer.Context) -> None:
    """Show the user associated with the configured personal token."""

    state = _state(context)

    def operation() -> JsonObject:
        payload = _with_client(state, lambda client: client.get_user())
        user = payload.get("user")
        if not isinstance(user, dict):
            raise APIError("ClickUp response is missing user data")
        return {
            "email": str(user["email"]) if isinstance(user.get("email"), str) else None,
            "id": str(user["id"]) if isinstance(user.get("id"), (str, int)) else None,
            "username": (str(user["username"]) if isinstance(user.get("username"), str) else None),
        }

    def text(user: JsonObject) -> str:
        username = user.get("username") or "unknown user"
        email = f" <{user['email']}>" if user.get("email") else ""
        identifier = f" [{user['id']}]" if user.get("id") else ""
        return f"{username}{email}{identifier}"

    _execute(
        state,
        operation,
        json_result=lambda user: {"user": user},
        text_result=text,
    )


@task_app.command("show")
def show_task(
    context: typer.Context, task_ref: str = typer.Argument(..., metavar="TASK_REF")
) -> None:
    """Show a task in a stable normalized shape."""

    state = _state(context)

    def operation() -> JsonObject:
        task_id = parse_task_ref(task_ref)
        task = _with_client(state, lambda client: client.get_task(task_id))
        return summarize_task(task)

    def text(task: JsonObject) -> str:
        raw_assignees = task.get("assignees")
        assignee_labels: list[str] = []
        if isinstance(raw_assignees, list):
            for raw_assignee in raw_assignees:
                if isinstance(raw_assignee, dict):
                    label = raw_assignee.get("username") or raw_assignee.get("id")
                    if label is not None:
                        assignee_labels.append(str(label))
        raw_tags = task.get("tags")
        tag_labels = [str(tag) for tag in raw_tags] if isinstance(raw_tags, list) else []
        return "\n".join(
            (
                f"ID: {task.get('id') or ''}",
                f"Name: {task.get('name') or ''}",
                f"Status: {task.get('status') or ''}",
                f"List: {task.get('list_name') or task.get('list_id') or ''}",
                f"Due: {task.get('due_date') or ''}",
                f"Assignees: {', '.join(assignee_labels)}",
                f"Tags: {', '.join(tag_labels)}",
                f"URL: {task.get('url') or ''}",
            )
        )

    _execute(
        state,
        operation,
        json_result=lambda task: {"task": task},
        text_result=text,
    )


@task_app.command("status")
def show_status(
    context: typer.Context, task_ref: str = typer.Argument(..., metavar="TASK_REF")
) -> None:
    """Show only a task's current status label."""

    state = _state(context)

    def operation() -> tuple[str, str]:
        task_id = parse_task_ref(task_ref)
        task = _with_client(state, lambda client: client.get_task(task_id))
        return task_id, task_status(task)

    _execute(
        state,
        operation,
        json_result=lambda result: {"status": result[1], "task_id": result[0]},
        text_result=lambda result: result[1],
    )


@task_app.command("set-status")
def set_status(
    context: typer.Context,
    task_ref: str = typer.Argument(..., metavar="TASK_REF"),
    status: str = typer.Argument(..., metavar="STATUS"),
) -> None:
    """Set a valid list status with a minimal write and verified readback."""

    state = _state(context)

    def operation() -> MutationResult:
        task_id = parse_task_ref(task_ref)
        return _with_client(state, lambda client: TaskService(client).set_status(task_id, status))

    _execute(state, operation, json_result=_mutation_json, text_result=_mutation_text)


@task_app.command("complete")
def complete_task(
    context: typer.Context, task_ref: str = typer.Argument(..., metavar="TASK_REF")
) -> None:
    """Select the highest-priority semantic completion status."""

    state = _state(context)

    def operation() -> MutationResult:
        task_id = parse_task_ref(task_ref)
        return _with_client(state, lambda client: TaskService(client).complete(task_id))

    _execute(state, operation, json_result=_mutation_json, text_result=_mutation_text)


@comment_app.command("show")
def show_comment(
    context: typer.Context,
    task_ref: str = typer.Argument(..., metavar="TASK_REF"),
    comment_id: str | None = typer.Argument(None, metavar="COMMENT_ID"),
) -> None:
    """Show one comment by explicit ID or directly from a ClickUp comment URL."""

    state = _state(context)

    def operation() -> CommentMutationResult:
        task_id, native_comment_id = parse_comment_ref(task_ref, comment_id)
        return _with_client(
            state,
            lambda client: TaskService(client).get_comment(task_id, native_comment_id),
        )

    def text(result: CommentMutationResult) -> str:
        author = result.comment.get("username") or result.comment.get("user_id") or "unknown"
        return f"{result.comment.get('id')} {author}: {result.comment.get('text') or ''}"

    _execute(state, operation, json_result=_comment_json, text_result=text)


@comment_app.command("list")
def list_comments(
    context: typer.Context, task_ref: str = typer.Argument(..., metavar="TASK_REF")
) -> None:
    """List the latest task comments in a stable normalized shape."""

    state = _state(context)

    def operation() -> tuple[str, list[JsonObject]]:
        task_id = parse_task_ref(task_ref)
        comments = _with_client(state, lambda client: TaskService(client).list_comments(task_id))
        return task_id, comments

    def text(result: tuple[str, list[JsonObject]]) -> str:
        comments = result[1]
        if not comments:
            return "No comments"
        lines: list[str] = []
        for comment in comments:
            author = comment.get("username") or comment.get("user_id") or "unknown"
            lines.append(f"{comment.get('id')} {author}: {comment.get('text') or ''}")
        return "\n".join(lines)

    _execute(
        state,
        operation,
        json_result=lambda result: {
            "comments": cast(list[JsonValue], result[1]),
            "task_id": result[0],
        },
        text_result=text,
    )


@comment_app.command("add")
def add_comment(
    context: typer.Context,
    task_ref: str = typer.Argument(..., metavar="TASK_REF"),
    text: str = typer.Argument(..., metavar="TEXT"),
) -> None:
    """Add a plain-text task comment and verify it by readback."""

    state = _state(context)

    def operation() -> CommentMutationResult:
        task_id = parse_task_ref(task_ref)
        return _with_client(state, lambda client: TaskService(client).add_comment(task_id, text))

    _execute(
        state,
        operation,
        json_result=_comment_json,
        text_result=lambda result: f"Added comment {result.comment.get('id')} to {result.task_id}",
    )


@due_date_app.command("set")
def set_due_date(
    context: typer.Context,
    task_ref: str = typer.Argument(..., metavar="TASK_REF"),
    due_at: str = typer.Argument(..., metavar="DUE_AT"),
) -> None:
    """Set YYYY-MM-DD or a timezone-aware ISO timestamp and verify readback."""

    state = _state(context)

    def operation() -> DueDateMutationResult:
        task_id = parse_task_ref(task_ref)
        requested = parse_due_date(due_at)
        return _with_client(
            state, lambda client: TaskService(client).set_due_date(task_id, requested)
        )

    _execute(state, operation, json_result=_due_date_json, text_result=_due_date_text)


@due_date_app.command("clear")
def clear_due_date(
    context: typer.Context, task_ref: str = typer.Argument(..., metavar="TASK_REF")
) -> None:
    """Clear a task due date with a minimal update and verified readback."""

    state = _state(context)

    def operation() -> DueDateMutationResult:
        task_id = parse_task_ref(task_ref)
        return _with_client(state, lambda client: TaskService(client).clear_due_date(task_id))

    _execute(state, operation, json_result=_due_date_json, text_result=_due_date_text)


@task_app.command("assign")
def assign_task(
    context: typer.Context,
    task_ref: str = typer.Argument(..., metavar="TASK_REF"),
    user_id: int = typer.Argument(..., metavar="USER_ID"),
) -> None:
    """Assign one user with an idempotent minimal update and verified readback."""

    state = _state(context)

    def operation() -> AssignmentMutationResult:
        task_id = parse_task_ref(task_ref)
        return _with_client(state, lambda client: TaskService(client).assign(task_id, user_id))

    _execute(state, operation, json_result=_assignment_json, text_result=_assignment_text)


@task_app.command("unassign")
def unassign_task(
    context: typer.Context,
    task_ref: str = typer.Argument(..., metavar="TASK_REF"),
    user_id: int = typer.Argument(..., metavar="USER_ID"),
) -> None:
    """Unassign one user with an idempotent minimal update and verified readback."""

    state = _state(context)

    def operation() -> AssignmentMutationResult:
        task_id = parse_task_ref(task_ref)
        return _with_client(state, lambda client: TaskService(client).unassign(task_id, user_id))

    _execute(state, operation, json_result=_assignment_json, text_result=_assignment_text)


@task_app.command("create")
def create_task(
    context: typer.Context,
    name: str = typer.Argument(..., metavar="NAME"),
    list_id: str = typer.Option(..., "--list-id", metavar="LIST_ID"),
    description: str | None = typer.Option(None, "--description"),
    status: str | None = typer.Option(None, "--status"),
    assignees: list[int] | None = typer.Option(None, "--assignee", metavar="USER_ID"),
    due_at: str | None = typer.Option(
        None,
        "--due-date",
        metavar="DUE_AT",
        help="YYYY-MM-DD or timezone-aware ISO 8601 timestamp.",
    ),
    tags: list[str] | None = typer.Option(None, "--tag", metavar="TAG"),
) -> None:
    """Create and read back a task with only explicitly supplied supported fields."""

    state = _state(context)

    def operation() -> JsonObject:
        native_list_id = validate_native_id(list_id, label="LIST_ID")
        requested_due_date = parse_due_date(due_at) if due_at is not None else None
        task = _with_client(
            state,
            lambda client: TaskService(client).create_task(
                native_list_id,
                name,
                description=description,
                status=status,
                assignees=assignees,
                due_date=requested_due_date,
                tags=tags,
            ),
        )
        return summarize_task(task)

    _execute(
        state,
        operation,
        json_result=lambda task: {"task": task},
        text_result=lambda task: f"Created {task.get('id')}: {task.get('name')}",
    )


@task_app.command("delete")
def delete_task(
    context: typer.Context,
    task_ref: str = typer.Argument(..., metavar="TASK_REF"),
    yes: bool = typer.Option(False, "--yes", help="Confirm permanent deletion."),
) -> None:
    """Permanently delete a task after explicit confirmation."""

    state = _state(context)
    if not yes:
        _fail(state, ConfirmationError("Refusing to delete without --yes"))

    def operation() -> str:
        task_id = parse_task_ref(task_ref)

        def remove(client: ClickUpClient) -> str:
            client.delete_task(task_id)
            return task_id

        return _with_client(state, remove)

    _execute(
        state,
        operation,
        json_result=lambda task_id: {"deleted": True, "task_id": task_id},
        text_result=lambda task_id: f"Deleted {task_id}",
    )


@time_app.command("current")
def current_time(
    context: typer.Context,
    workspace_id: str = typer.Option(..., "--workspace-id", metavar="WORKSPACE_ID"),
    assignee: int | None = typer.Option(None, "--assignee", metavar="USER_ID"),
) -> None:
    """Show the authenticated user's current timer, or another user's when authorized."""

    state = _state(context)

    def operation() -> JsonObject | None:
        native_workspace_id = validate_numeric_id(workspace_id, label="WORKSPACE_ID")
        user_id = _positive_assignee(assignee)
        return _with_client(
            state,
            lambda client: TimeTrackingService(client).current(
                native_workspace_id, assignee=user_id
            ),
        )

    _execute(
        state,
        operation,
        json_result=lambda entry: {"entry": entry},
        text_result=lambda entry: (
            _time_entry_text(entry) if entry is not None else "No running timer"
        ),
    )


@time_app.command("list")
def list_time(
    context: typer.Context,
    workspace_id: str = typer.Option(..., "--workspace-id", metavar="WORKSPACE_ID"),
    from_value: str = typer.Option(
        ..., "--from", metavar="FROM", help="Inclusive date or timezone-aware timestamp."
    ),
    to_value: str = typer.Option(
        ..., "--to", metavar="TO", help="Exclusive date or timezone-aware timestamp."
    ),
    assignee: int | None = typer.Option(None, "--assignee", metavar="USER_ID"),
    task: str | None = typer.Option(None, "--task", metavar="TASK_REF"),
    space_id: str | None = typer.Option(None, "--space-id", metavar="SPACE_ID"),
    folder_id: str | None = typer.Option(None, "--folder-id", metavar="FOLDER_ID"),
    list_id: str | None = typer.Option(None, "--list-id", metavar="LIST_ID"),
    billable: bool = typer.Option(False, "--billable", help="Only billable entries."),
    non_billable: bool = typer.Option(False, "--non-billable", help="Only non-billable entries."),
) -> None:
    """List entries in a bounded start-inclusive, end-exclusive interval."""

    state = _state(context)

    def operation() -> TimeListResult:
        native_workspace_id = validate_numeric_id(workspace_id, label="WORKSPACE_ID")
        locations = [task, space_id, folder_id, list_id]
        if sum(value is not None for value in locations) > 1:
            raise InvalidOperationError(
                "Only one of --task, --space-id, --folder-id, or --list-id may be used"
            )
        requested_range = parse_time_range(from_value, to_value)
        task_id = parse_task_ref(task) if task is not None else None
        native_space_id = (
            validate_numeric_id(space_id, label="SPACE_ID") if space_id is not None else None
        )
        native_folder_id = (
            validate_numeric_id(folder_id, label="FOLDER_ID") if folder_id is not None else None
        )
        native_list_id = (
            validate_numeric_id(list_id, label="LIST_ID") if list_id is not None else None
        )
        user_id = _positive_assignee(assignee)
        billable_state = _billable_value(billable=billable, non_billable=non_billable)
        return _with_client(
            state,
            lambda client: TimeTrackingService(client).list_entries(
                native_workspace_id,
                requested_range,
                assignee=user_id,
                task_id=task_id,
                space_id=native_space_id,
                folder_id=native_folder_id,
                list_id=native_list_id,
                billable=billable_state,
            ),
        )

    def json_result(result: TimeListResult) -> JsonObject:
        return {
            "entries": cast(list[JsonValue], result.entries),
            "from_ms": result.start_ms,
            "range_semantics": "start-inclusive,end-exclusive",
            "to_ms": result.end_ms,
            "workspace_id": result.workspace_id,
        }

    def text_result(result: TimeListResult) -> str:
        if not result.entries:
            return "No time entries"
        return "\n".join(_time_entry_text(entry) for entry in result.entries)

    _execute(state, operation, json_result=json_result, text_result=text_result)


@time_app.command("start")
def start_time(
    context: typer.Context,
    workspace_id: str = typer.Option(..., "--workspace-id", metavar="WORKSPACE_ID"),
    task: str | None = typer.Option(None, "--task", metavar="TASK_REF"),
    description: str | None = typer.Option(None, "--description", metavar="TEXT"),
    billable: bool = typer.Option(False, "--billable"),
    non_billable: bool = typer.Option(False, "--non-billable"),
) -> None:
    """Start one timer after proving that no timer is already running."""

    state = _state(context)

    def operation() -> TimeMutationResult:
        native_workspace_id = validate_numeric_id(workspace_id, label="WORKSPACE_ID")
        task_id = parse_task_ref(task) if task is not None else None
        billable_state = _billable_value(billable=billable, non_billable=non_billable)
        return _with_client(
            state,
            lambda client: TimeTrackingService(client).start(
                native_workspace_id,
                task_id=task_id,
                description=description,
                billable=billable_state,
            ),
        )

    _execute(
        state,
        operation,
        json_result=_time_mutation_json,
        text_result=lambda result: f"Started {result.entry.get('id')}",
    )


@time_app.command("stop")
def stop_time(
    context: typer.Context,
    workspace_id: str = typer.Option(..., "--workspace-id", metavar="WORKSPACE_ID"),
) -> None:
    """Stop and verify the current timer, or return an idempotent no-op."""

    state = _state(context)

    def operation() -> StopTimeResult:
        native_workspace_id = validate_numeric_id(workspace_id, label="WORKSPACE_ID")
        return _with_client(
            state, lambda client: TimeTrackingService(client).stop(native_workspace_id)
        )

    _execute(
        state,
        operation,
        json_result=lambda result: {
            "entry": result.entry,
            "entry_id": result.entry_id,
            "stopped": result.stopped,
        },
        text_result=lambda result: (
            f"Stopped {result.entry_id}" if result.stopped else "No running timer (no change)"
        ),
    )


@time_app.command("add")
def add_time(
    context: typer.Context,
    workspace_id: str = typer.Option(..., "--workspace-id", metavar="WORKSPACE_ID"),
    start_at: str = typer.Option(..., "--start", metavar="DATETIME"),
    duration_value: str = typer.Option(..., "--duration", metavar="DURATION"),
    task: str | None = typer.Option(None, "--task", metavar="TASK_REF"),
    description: str | None = typer.Option(None, "--description", metavar="TEXT"),
    billable: bool = typer.Option(False, "--billable"),
    non_billable: bool = typer.Option(False, "--non-billable"),
) -> None:
    """Create one completed entry and verify every explicitly requested field."""

    state = _state(context)

    def operation() -> TimeMutationResult:
        native_workspace_id = validate_numeric_id(workspace_id, label="WORKSPACE_ID")
        requested_start = parse_time_boundary(start_at, label="START", allow_date=False)
        requested_duration = parse_duration(duration_value)
        task_id = parse_task_ref(task) if task is not None else None
        billable_state = _billable_value(billable=billable, non_billable=non_billable)
        return _with_client(
            state,
            lambda client: TimeTrackingService(client).add(
                native_workspace_id,
                start=requested_start,
                duration=requested_duration,
                task_id=task_id,
                description=description,
                billable=billable_state,
            ),
        )

    _execute(
        state,
        operation,
        json_result=_time_mutation_json,
        text_result=lambda result: f"Added {result.entry.get('id')}",
    )


@time_app.command("update")
def update_time(
    context: typer.Context,
    entry_id: str = typer.Argument(..., metavar="ENTRY_ID"),
    workspace_id: str = typer.Option(..., "--workspace-id", metavar="WORKSPACE_ID"),
    description: str | None = typer.Option(None, "--description", metavar="TEXT"),
    task: str | None = typer.Option(None, "--task", metavar="TASK_REF"),
    start_at: str | None = typer.Option(None, "--start", metavar="DATETIME"),
    duration_value: str | None = typer.Option(None, "--duration", metavar="DURATION"),
    billable: bool = typer.Option(False, "--billable"),
    non_billable: bool = typer.Option(False, "--non-billable"),
) -> None:
    """Update supported fields with the smallest valid body and verified readback."""

    state = _state(context)

    def operation() -> TimeMutationResult:
        native_workspace_id = validate_numeric_id(workspace_id, label="WORKSPACE_ID")
        native_entry_id = validate_native_id(entry_id, label="ENTRY_ID")
        billable_state = _billable_value(billable=billable, non_billable=non_billable)
        if all(
            value is None for value in (description, task, start_at, duration_value, billable_state)
        ):
            raise InvalidOperationError("At least one time-entry field must be provided")
        requested_start = (
            parse_time_boundary(start_at, label="START", allow_date=False)
            if start_at is not None
            else None
        )
        requested_duration = parse_duration(duration_value) if duration_value is not None else None
        task_id = parse_task_ref(task) if task is not None else None
        return _with_client(
            state,
            lambda client: TimeTrackingService(client).update(
                native_workspace_id,
                native_entry_id,
                description=description,
                task_id=task_id,
                start=requested_start,
                duration=requested_duration,
                billable=billable_state,
            ),
        )

    _execute(
        state,
        operation,
        json_result=_time_mutation_json,
        text_result=lambda result: (
            f"Updated {result.entry.get('id')}"
            if result.changed
            else f"{result.entry.get('id')}: no change"
        ),
    )


@time_app.command("delete")
def delete_time(
    context: typer.Context,
    entry_id: str = typer.Argument(..., metavar="ENTRY_ID"),
    workspace_id: str = typer.Option(..., "--workspace-id", metavar="WORKSPACE_ID"),
    yes: bool = typer.Option(False, "--yes", help="Confirm permanent deletion."),
) -> None:
    """Permanently delete a time entry after explicit confirmation."""

    state = _state(context)
    if not yes:
        _fail(state, ConfirmationError("Refusing to delete without --yes"))

    def operation() -> str:
        native_workspace_id = validate_numeric_id(workspace_id, label="WORKSPACE_ID")
        native_entry_id = validate_native_id(entry_id, label="ENTRY_ID")

        def remove(client: ClickUpClient) -> str:
            TimeTrackingService(client).delete(native_workspace_id, native_entry_id)
            return native_entry_id

        return _with_client(state, remove)

    _execute(
        state,
        operation,
        json_result=lambda deleted_id: {"deleted": True, "entry_id": deleted_id},
        text_result=lambda deleted_id: f"Deleted {deleted_id}",
    )


def main(args: list[str] | None = None, *, prog_name: str | None = None) -> int:
    """Console-script entry point."""

    arguments = list(sys.argv[1:] if args is None else args)
    resolved_prog_name = prog_name or Path(sys.argv[0]).name or "clickup"
    command = get_command(app)
    try:
        result = command.main(
            args=arguments,
            prog_name=resolved_prog_name,
            standalone_mode=False,
        )
    except ClickException as exc:
        if _json_requested(arguments):
            _emit_json(
                {
                    "error": {"message": exc.format_message(), "type": "usage_error"},
                    "ok": False,
                },
                error=True,
            )
        else:
            exc.show()
        return exc.exit_code
    return result if isinstance(result, int) else 0


if __name__ == "__main__":
    raise SystemExit(main())
