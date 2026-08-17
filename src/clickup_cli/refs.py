"""Strict parsing for native ClickUp task IDs and task URLs."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, unquote, urlsplit

from clickup_cli.errors import ReferenceError

_TASK_ID = re.compile(r"[A-Za-z0-9_-]+\Z")


def _valid_id(value: str) -> bool:
    return bool(value and _TASK_ID.fullmatch(value))


def parse_task_ref(reference: str) -> str:
    """Return a native task ID from an ID or supported clickup.com URL."""

    value = reference.strip()
    if _valid_id(value):
        return value

    parsed = urlsplit(value)
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not (
        hostname == "clickup.com" or hostname.endswith(".clickup.com")
    ):
        raise ReferenceError("TASK_REF must be a native task ID or ClickUp task URL")
    try:
        has_port = parsed.port is not None
    except ValueError as exc:
        raise ReferenceError("ClickUp task URL contains an invalid port") from exc
    if parsed.username is not None or parsed.password is not None or has_port:
        raise ReferenceError("ClickUp task URL contains unsupported authority components")

    segments = [unquote(segment) for segment in parsed.path.strip("/").split("/")]
    if len(segments) == 2 and segments[0] == "t":
        task_id = segments[1]
    elif len(segments) == 3 and segments[0] == "t" and _valid_id(segments[1]):
        task_id = segments[2]
    else:
        raise ReferenceError(
            "ClickUp task URL must use /t/<task-id> or /t/<workspace-id>/<task-id>"
        )
    if not _valid_id(task_id):
        raise ReferenceError("ClickUp task URL contains an invalid task ID")
    return task_id


def parse_comment_ref(reference: str, comment_id: str | None = None) -> tuple[str, str]:
    """Return task and comment IDs from an explicit pair or ClickUp comment deep link."""

    task_id = parse_task_ref(reference)
    parsed = urlsplit(reference.strip())
    query_values = parse_qs(parsed.query, keep_blank_values=True).get("comment", [])
    linked_comment_id: str | None = None
    if query_values:
        if len(query_values) != 1 or not _valid_id(query_values[0]):
            raise ReferenceError("ClickUp comment URL must contain one valid comment query value")
        linked_comment_id = query_values[0]

    explicit_comment_id: str | None = None
    if comment_id is not None:
        explicit_comment_id = comment_id.strip()
        if not _valid_id(explicit_comment_id):
            raise ReferenceError(
                "COMMENT_ID must contain only letters, numbers, underscores, or hyphens"
            )

    if (
        linked_comment_id is not None
        and explicit_comment_id is not None
        and linked_comment_id != explicit_comment_id
    ):
        raise ReferenceError("COMMENT_ID conflicts with the ClickUp comment URL")

    resolved_comment_id = explicit_comment_id or linked_comment_id
    if resolved_comment_id is None:
        raise ReferenceError("COMMENT_ID is required unless TASK_REF is a ClickUp comment URL")
    return task_id, resolved_comment_id


def validate_native_id(value: str, *, label: str) -> str:
    """Validate an ID used directly in an API path."""

    if not _valid_id(value):
        raise ReferenceError(f"{label} must contain only letters, numbers, underscores, or hyphens")
    return value
