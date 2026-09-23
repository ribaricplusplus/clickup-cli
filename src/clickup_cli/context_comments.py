"""Bounded task context assembled from one task, list, and comment pages."""

from __future__ import annotations

import re
from typing import cast

from clickup_cli.attachments import normalize_attachments
from clickup_cli.client import ClickUpClient
from clickup_cli.discovery import DiscoveryService, summarize_list
from clickup_cli.domain import (
    CommentMutationResult,
    TaskService,
    summarize_comments,
    summarize_task,
    task_list_id,
)
from clickup_cli.errors import (
    AmbiguousMatchError,
    APIError,
    ClickUpCLIError,
    CreatedButUnverifiedError,
    EditedButUnverifiedError,
    InvalidOperationError,
    OutcomeUnknownError,
    StaleCommentRevisionError,
    TransportError,
    VerificationError,
)
from clickup_cli.errors import ReferenceError as ClickUpReferenceError
from clickup_cli.refs import validate_native_id
from clickup_cli.types import JsonObject, JsonValue

MAX_CONTEXT_ITEMS = 100


def _limit(value: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_CONTEXT_ITEMS:
        raise InvalidOperationError(f"--{label} must be between 0 and {MAX_CONTEXT_ITEMS}")
    return value


def _identifier(value: JsonValue | None, *, label: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise APIError(f"ClickUp response contains an invalid {label}")
    try:
        return validate_native_id(str(value), label=label)
    except ClickUpReferenceError as exc:
        raise APIError(f"ClickUp response contains an invalid {label}") from exc


def _comment_cursor(comment: JsonObject) -> JsonObject | None:
    date, identifier = comment.get("date"), comment.get("id")
    if not isinstance(date, str) or not date.isascii() or not date.isdecimal():
        return None
    if not isinstance(identifier, str):
        return None
    return {"start": int(date), "start_id": identifier}


class TaskContextService:
    """Never traverses whole workspaces or fetches more than bounded comment pages."""

    def __init__(self, client: ClickUpClient) -> None:
        self._client = client

    def get(self, task_id: str, *, comments: int = 10, attachments: int = 10) -> JsonObject:
        comments = _limit(comments, "comments")
        attachments = _limit(attachments, "attachments")
        task = self._client.get_task(task_id)
        listing = summarize_list(self._client.get_list(task_list_id(task)))
        # ClickUp reports folderless Space Lists under a synthetic "hidden" folder.
        if str(listing.get("folder_name") or "").casefold() == "hidden":
            listing["folder_id"] = None
            listing["folder_name"] = None
        raw_children = task.get("subtasks")
        if raw_children is not None and not isinstance(raw_children, list):
            raise APIError("ClickUp response contains invalid subtasks")
        child_ids: list[str] = []
        for child in raw_children or []:
            if not isinstance(child, dict):
                raise APIError("ClickUp response contains an invalid subtask")
            identifier = _identifier(child.get("id"), label="subtask ID")
            if identifier is None:
                raise APIError("ClickUp response is missing subtask ID")
            child_ids.append(identifier)
        all_attachments = normalize_attachments(task)
        recent: list[JsonObject] = []
        cursor: JsonObject | None = None
        has_more: bool | None = None
        if comments:
            seen: set[tuple[int, str]] = set()
            while len(recent) < comments:
                payload = self._client.get_task_comments(
                    task_id,
                    start=cast(int, cursor["start"]) if cursor else None,
                    start_id=cast(str, cursor["start_id"]) if cursor else None,
                )
                page = summarize_comments(payload)
                remaining = comments - len(recent)
                recent.extend(page[:remaining])
                explicit_more = payload.get("has_more")
                if explicit_more is not None and not isinstance(explicit_more, bool):
                    raise APIError("ClickUp comment response has an invalid has_more flag")
                has_more = True if len(page) > remaining else explicit_more
                # The provider does not promise a total. An empty page proves exhaustion.
                if not page:
                    has_more = False
                    cursor = None
                    break
                if has_more is False:
                    cursor = None
                    break
                cursor = _comment_cursor(recent[-1])

                if len(recent) >= comments:
                    break
                if cursor is None:
                    raise APIError("ClickUp comment pagination response is missing a cursor")
                key = (cast(int, cursor["start"]), cast(str, cursor["start_id"]))
                if key in seen:
                    raise APIError("ClickUp comment pagination did not advance")
                seen.add(key)
        folder = listing.get("folder_name")
        space = listing.get("space_name")
        task_summary = summarize_task(task)
        task_summary["attachments"] = cast(list[JsonValue], all_attachments[:attachments])
        return {
            "task": task_summary,
            "list": listing,
            "path": cast(
                list[JsonValue], [part for part in (space, folder, listing["name"]) if part]
            ),
            "statuses": listing["statuses"],
            "parent_id": _identifier(task.get("parent"), label="parent ID"),
            "subtask_ids": cast(list[JsonValue], child_ids),
            "comments": {
                "items": cast(list[JsonValue], recent),
                "returned_count": len(recent),
                "has_more": has_more,
                "cursor": cursor,
            },
            "attachments": {
                "items": cast(list[JsonValue], all_attachments[:attachments]),
                "returned_count": min(len(all_attachments), attachments),
                "has_more": True if len(all_attachments) > attachments else None,
                "cursor": None,
            },
        }


class RichCommentService:
    """Resolve real Workspace members and check the created comment by exact ID."""

    def __init__(self, client: ClickUpClient) -> None:
        self._client = client

    def _members(self, task: JsonObject) -> list[JsonObject]:
        workspace_id = _identifier(task.get("team_id"), label="workspace ID")
        if workspace_id is None:
            raise InvalidOperationError(
                "Task response has no workspace ID; cannot resolve mentions safely"
            )
        return DiscoveryService(self._client).list_members(workspace_id)

    def add(
        self, task_id: str, text: str, *, mentions: list[str], notify_all: bool = False
    ) -> CommentMutationResult:
        if not isinstance(text, str) or not text.strip():
            raise InvalidOperationError("Comment text cannot be empty")
        task = self._client.get_task(task_id)
        if str(task.get("id")) != task_id:
            raise VerificationError("Task preflight ID did not match requested task")
        members = self._members(task) if mentions else []
        ids: list[int] = []
        for reference in mentions:
            if not reference.strip():
                raise InvalidOperationError("--mention must be a member ID, username or email")
            needle = reference.casefold()
            matches = [
                member
                for member in members
                if str(member["id"]) == reference
                or any(
                    isinstance(member.get(key), str) and str(member[key]).casefold() == needle
                    for key in ("username", "email")
                )
            ]
            if len({str(member["id"]) for member in matches}) > 1:
                raise AmbiguousMatchError(
                    f"Mention {reference!r} matches multiple workspace members"
                )
            if not matches:
                raise InvalidOperationError(f"Mention {reference!r} is not a workspace member")
            identifier = int(str(matches[0]["id"]))
            if identifier not in ids:
                ids.append(identifier)
        segments: list[JsonObject] = [
            {"type": "tag", "user": {"id": identifier}} for identifier in ids
        ]
        segments.append({"text": f" {text}" if ids else text})
        try:
            created = self._client.create_rich_comment(task_id, segments, notify_all=notify_all)
        except TransportError as exc:
            raise OutcomeUnknownError(
                "Comment POST outcome unknown; do not retry without reconciliation"
            ) from exc
        except APIError as exc:
            if exc.status_code is None or exc.status_code >= 500:
                raise OutcomeUnknownError(
                    "Comment POST outcome unknown; do not retry without reconciliation"
                ) from exc
            raise
        raw_id = created.get("id")
        if not isinstance(raw_id, (str, int)) or isinstance(raw_id, bool):
            raise OutcomeUnknownError("Comment POST succeeded without a usable ID; do not retry")
        comment_id = str(raw_id)
        try:
            readback = TaskService(self._client).get_comment(task_id, comment_id).comment
            observed = readback.get("segments")
            if not isinstance(observed, list):
                raise VerificationError("Rich comment readback has no native segments")
            observed_ids = [
                str(cast(JsonObject, segment["user"])["id"])
                for segment in observed
                if isinstance(segment, dict)
                and segment.get("type") == "tag"
                and isinstance(segment.get("user"), dict)
            ]
            observed_text = "".join(
                str(cast(JsonObject, segment).get("text", ""))
                for segment in observed
                if isinstance(segment, dict) and segment.get("type") != "tag"
            )
            if observed_ids != [str(identifier) for identifier in ids] or observed_text != (
                f" {text}" if ids else text
            ):
                raise VerificationError("Rich comment mention or text verification failed")
        except ClickUpCLIError as exc:
            raise CreatedButUnverifiedError(
                f"Comment {comment_id} was created but readback failed: {exc}",
                details={"comment_id": comment_id, "task_id": task_id},
            ) from exc
        return CommentMutationResult(task_id=task_id, comment=readback)

    def edit(
        self, task_id: str, comment_id: str, text: str, *, expected_sha256: str
    ) -> CommentMutationResult:
        if not isinstance(text, str) or not text.strip():
            raise InvalidOperationError("Comment text cannot be empty")
        if re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None:
            raise InvalidOperationError("--expect-sha256 must be a lowercase 64-character SHA256")
        task = self._client.get_task(task_id)
        if str(task.get("id")) != task_id:
            raise VerificationError("Task preflight ID did not match requested task")
        previous = TaskService(self._client).get_comment(task_id, comment_id).comment
        if previous.get("revision_sha256") != expected_sha256:
            raise StaleCommentRevisionError(
                f"Comment {comment_id} changed; fetch its current revision before editing",
                details={
                    "comment_id": comment_id,
                    "task_id": task_id,
                    "current_sha256": previous.get("revision_sha256"),
                },
            )
        old_segments = previous.get("segments")
        if not isinstance(old_segments, list):
            raise APIError("Comment has no readable segments")
        tags: list[JsonObject] = []
        seen_text = False
        for segment in old_segments:
            if not isinstance(segment, dict):
                raise InvalidOperationError("Cannot safely edit unsupported rich comment segments")
            if segment.get("type") == "tag":
                if seen_text or set(segment) - {"type", "user", "text"}:
                    raise InvalidOperationError(
                        "Cannot safely reorder or flatten rich comment tags"
                    )
                user = segment.get("user")
                if not isinstance(user, dict) or not str(user.get("id", "")).isdecimal():
                    raise APIError("Comment has an invalid native mention")
                tags.append({"type": "tag", "user": {"id": int(str(user["id"]))}})
            elif set(segment) == {"text"}:
                seen_text = True
            else:
                raise InvalidOperationError("Cannot safely edit unsupported rich comment segments")
        segments: list[JsonObject] = [*tags, {"text": f" {text}" if tags else text}]
        try:
            self._client.update_rich_comment(comment_id, segments)
        except TransportError as exc:
            raise OutcomeUnknownError(
                f"Comment {comment_id} PUT outcome unknown; inspect before retrying",
                details={"comment_id": comment_id, "task_id": task_id},
            ) from exc
        except APIError as exc:
            if exc.status_code is None or exc.status_code >= 500:
                raise OutcomeUnknownError(
                    f"Comment {comment_id} PUT outcome unknown; inspect before retrying",
                    details={"comment_id": comment_id, "task_id": task_id},
                ) from exc
            raise
        try:
            readback = TaskService(self._client).get_comment(task_id, comment_id).comment
            if readback.get("mentions") != [
                str(cast(JsonObject, tag["user"])["id"]) for tag in tags
            ]:
                raise VerificationError("Edited comment native mentions did not match")
            observed = readback.get("segments")
            if not isinstance(observed, list) or "".join(
                str(segment.get("text", ""))
                for segment in observed
                if isinstance(segment, dict) and segment.get("type") != "tag"
            ) != (f" {text}" if tags else text):
                raise VerificationError("Edited comment text did not match")
        except ClickUpCLIError as exc:
            raise EditedButUnverifiedError(
                f"Comment {comment_id} was edited but readback failed: {exc}",
                details={"comment_id": comment_id, "task_id": task_id},
            ) from exc
        return CommentMutationResult(task_id=task_id, comment=readback)
