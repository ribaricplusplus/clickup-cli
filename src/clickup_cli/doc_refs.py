"""Strict explicit context for public ClickUp Docs references."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from clickup_cli.errors import InvalidOperationError


def doc_id(value: str, label: str = "ID") -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", value):
        raise InvalidOperationError(f"{label} must be a literal safe identifier")
    return value


def workspace(value: str) -> str:
    if not re.fullmatch(r"[0-9]{1,30}", value) or not any(c != "0" for c in value):
        raise InvalidOperationError("WORKSPACE_ID must be a positive decimal identifier")
    return value


@dataclass(frozen=True)
class DocRef:
    workspace_id: str
    doc_id: str
    page_id: str | None = None

    @property
    def url(self) -> str:
        base = f"https://app.clickup.com/{self.workspace_id}"
        return (
            f"{base}/v/dc/{self.doc_id}/{self.page_id}"
            if self.page_id
            else f"{base}/docs/{self.doc_id}"
        )


def parse_doc_ref(value: str, workspace_id: str | None = None) -> DocRef:
    if workspace_id is not None:
        workspace(workspace_id)
    if ":" in value or "/" in value:
        try:
            parsed = urlsplit(value)
        except ValueError as exc:
            raise InvalidOperationError("Malformed ClickUp Doc URL") from exc
        if (
            parsed.scheme != "https"
            or parsed.netloc != "app.clickup.com"
            or parsed.query
            or parsed.fragment
        ):
            raise InvalidOperationError("Unsupported ClickUp Doc URL")
        parts = parsed.path.split("/")
        if len(parts) in {5, 6} and parts[2:4] == ["v", "dc"] and not parts[0]:
            wid, did = workspace(parts[1]), doc_id(parts[4], "DOC_ID")
            pid = doc_id(parts[5], "PAGE_ID") if len(parts) == 6 else None
        elif len(parts) == 4 and not parts[0] and parts[2] == "docs":
            wid, did, pid = workspace(parts[1]), doc_id(parts[3], "DOC_ID"), None
        else:
            raise InvalidOperationError("Unsupported ClickUp Doc URL path")
        if workspace_id is not None and workspace_id != wid:
            raise InvalidOperationError("URL workspace conflicts with --workspace-id")
        return DocRef(wid, did, pid)
    if workspace_id is None:
        raise InvalidOperationError("Bare DOC_ID requires --workspace-id")
    return DocRef(workspace_id, doc_id(value, "DOC_ID"))


def parse_page_ref(value: str, doc: str | None = None, workspace_id: str | None = None) -> DocRef:
    context = parse_doc_ref(doc, workspace_id) if doc is not None else None
    if ":" in value or "/" in value:
        ref = parse_doc_ref(value, workspace_id)
        if ref.page_id is None:
            raise InvalidOperationError("PAGE_URL must identify a page")
        if context is not None and (
            ref.workspace_id != context.workspace_id
            or ref.doc_id != context.doc_id
            or (context.page_id is not None and context.page_id != ref.page_id)
        ):
            raise InvalidOperationError("Page URL conflicts with --doc")
        return ref
    if context is None:
        raise InvalidOperationError("Bare PAGE_ID requires --doc and workspace context")
    if context.page_id is not None and context.page_id != value:
        raise InvalidOperationError("PAGE_ID conflicts with page in --doc URL")
    return DocRef(context.workspace_id, context.doc_id, doc_id(value, "PAGE_ID"))
