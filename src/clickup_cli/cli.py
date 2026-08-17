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
from clickup_cli.errors import APIError, ClickUpCLIError, ConfirmationError
from clickup_cli.refs import parse_task_ref, validate_native_id
from clickup_cli.types import JsonObject, JsonValue

app = typer.Typer(no_args_is_help=True, help="Deterministic ClickUp operations.")
auth_app = typer.Typer(no_args_is_help=True, help="Authentication inspection.")
task_app = typer.Typer(no_args_is_help=True, help="Read and mutate ClickUp tasks.")
comment_app = typer.Typer(no_args_is_help=True, help="List and add task comments.")
due_date_app = typer.Typer(no_args_is_help=True, help="Set and clear task due dates.")
app.add_typer(auth_app, name="auth")
app.add_typer(task_app, name="task")
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
        _emit_json(
            {"error": {"message": str(error), "type": error.error_type}, "ok": False},
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
        return "\n".join(
            (
                f"ID: {task.get('id') or ''}",
                f"Name: {task.get('name') or ''}",
                f"Status: {task.get('status') or ''}",
                f"List: {task.get('list_id') or ''}",
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
) -> None:
    """Create a task with only explicitly supplied supported fields."""

    state = _state(context)

    def operation() -> JsonObject:
        native_list_id = validate_native_id(list_id, label="LIST_ID")
        task = _with_client(
            state,
            lambda client: client.create_task(
                native_list_id,
                name,
                description=description,
                status=status,
                assignees=assignees,
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
