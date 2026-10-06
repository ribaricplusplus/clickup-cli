"""Authorized public Docs domain operations; no inferred account context."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from clickup_cli.client import ClickUpClient
from clickup_cli.doc_refs import DocRef, parse_doc_ref
from clickup_cli.errors import APIError
from clickup_cli.types import JsonObject, JsonValue


def retrieved_at() -> str:
    return datetime.now(UTC).isoformat()


def validate_provider_metadata(raw: JsonObject) -> None:
    import math

    from clickup_cli.doc_refs import doc_id
    from clickup_cli.errors import InvalidOperationError

    for key in ("name", "sub_title"):
        value = raw.get(key)
        if value is None and key == "sub_title":
            continue
        if not isinstance(value, str) or len(value) > 4096:
            raise APIError("Doc/page response has malformed or oversized text metadata")
        try:
            value.encode("utf-8")
        except UnicodeError as exc:
            raise APIError("Doc/page metadata is not valid Unicode") from exc
    for key in ("date_created", "date_updated", "date_edited"):
        value = raw.get(key)
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, (int, float, str))
            or (isinstance(value, str) and len(value) > 256)
            or (isinstance(value, float) and not math.isfinite(value))
        ):
            raise APIError("Doc/page response has invalid provider timestamp")
    for key in ("public", "archived", "deleted"):
        if raw.get(key) is not None and not isinstance(raw[key], bool):
            raise APIError("Doc/page response has invalid boolean metadata")
    if raw.get("parent_page_id") is not None:
        try:
            doc_id(raw["parent_page_id"], "PARENT_PAGE_ID")  # type: ignore[arg-type]
        except InvalidOperationError as exc:
            raise APIError("Page has unsafe parent identity") from exc


def normalize_doc(raw: JsonObject, ref: DocRef) -> JsonObject:
    from clickup_cli.doc_refs import doc_id
    from clickup_cli.errors import InvalidOperationError

    validate_provider_metadata(raw)
    try:
        doc_id(ref.doc_id)
    except InvalidOperationError as exc:
        raise APIError("Doc response has unsafe identity") from exc
    parent = raw.get("parent")
    if parent is not None:
        if (
            not isinstance(parent, dict)
            or not isinstance(parent.get("id"), str)
            or type(parent.get("type")) is not int
            or parent["type"] not in {4, 5, 6, 7, 12}
        ):
            raise APIError("Doc response has malformed parent metadata")
        try:
            doc_id(str(parent["id"]), "PARENT_ID")
        except InvalidOperationError as exc:
            raise APIError("Doc response has unsafe parent identity") from exc
        parent = {"id": parent["id"], "type": parent["type"]}
    if (
        raw.get("id") != ref.doc_id
        or str(raw.get("workspace_id")) != ref.workspace_id
        or not isinstance(raw.get("name"), str)
    ):
        raise APIError("Doc response has inconsistent identity or name")
    return {
        "doc_id": ref.doc_id,
        "workspace_id": ref.workspace_id,
        "name": raw["name"],
        "url": DocRef(ref.workspace_id, ref.doc_id).url,
        "public": raw.get("public"),
        "parent": parent,
        "date_created": raw.get("date_created"),
        "date_updated": raw.get("date_updated"),
        "archived": raw.get("archived"),
        "deleted": raw.get("deleted"),
        "retrieved_at": retrieved_at(),
    }


PARENT_TYPES = {"SPACE": 4, "FOLDER": 5, "LIST": 6, "EVERYTHING": 7, "WORKSPACE": 12}


def parent_spec(parent_id: str | None, parent_type: str | None) -> JsonObject | None:
    from clickup_cli.doc_refs import doc_id
    from clickup_cli.errors import InvalidOperationError

    if (parent_id is None) != (parent_type is None):
        raise InvalidOperationError("--parent-id and --parent-type must be supplied together")
    if parent_id is None:
        return None
    if parent_type not in PARENT_TYPES:
        raise InvalidOperationError(
            "--parent-type must be SPACE, FOLDER, LIST, EVERYTHING, or WORKSPACE"
        )
    return {"id": doc_id(parent_id, "PARENT_ID"), "type": PARENT_TYPES[parent_type]}


def flatten_pages(roots: list[JsonObject], ref: DocRef) -> list[JsonObject]:
    from clickup_cli.doc_refs import doc_id
    from clickup_cli.errors import InvalidOperationError

    found: list[JsonObject] = []
    seen: set[str] = set()
    stack: list[tuple[JsonObject, str | None, list[JsonValue]]] = [
        (item, None, []) for item in reversed(roots)
    ]
    while stack:
        raw, parent, trail = stack.pop()
        validate_provider_metadata(raw)
        if len(trail) > 128:
            raise APIError("Page hierarchy exceeds the 128-level safety ceiling")
        pid = raw.get("id")
        if not isinstance(pid, str) or pid in seen or len(found) >= 10000:
            raise APIError("Page hierarchy has missing/duplicate IDs or exceeds 10000 pages")
        try:
            doc_id(pid, "PAGE_ID")
        except InvalidOperationError as exc:
            raise APIError("Page response has unsafe identity") from exc
        if (
            raw.get("doc_id") != ref.doc_id
            or str(raw.get("workspace_id")) != ref.workspace_id
            or not isinstance(raw.get("name"), str)
        ):
            raise APIError("Page response has inconsistent Doc/workspace identity or name")
        supplied_parent = raw.get("parent_page_id")
        if supplied_parent is not None and supplied_parent != parent:
            raise APIError("Page hierarchy has inconsistent parent identity")
        seen.add(pid)
        name = str(raw["name"])
        breadcrumb = [*trail, name]
        item: JsonObject = {
            "page_id": pid,
            "doc_id": ref.doc_id,
            "workspace_id": ref.workspace_id,
            "name": name,
            "parent_page_id": parent,
            "breadcrumb": breadcrumb,
            "depth": len(trail),
            "url": DocRef(ref.workspace_id, ref.doc_id, pid).url,
            "date_created": raw.get("date_created"),
            "date_updated": raw.get("date_updated"),
            "date_edited": raw.get("date_edited"),
            "retrieved_at": retrieved_at(),
        }
        found.append(item)
        children = raw.get("pages", [])
        if not isinstance(children, list) or any(not isinstance(child, dict) for child in children):
            raise APIError("Page children must be an array")
        for child in reversed(children):
            assert isinstance(child, dict)
            stack.append((child, pid, breadcrumb))
    return found


def content_hash(content: str) -> str:
    import hashlib

    return hashlib.sha256(content.encode("utf-8")).hexdigest()


WARNINGS: list[JsonValue] = [
    "Text snapshot only: embeds, synced content, views, comments, "
    "and rich formatting may be absent."
]


def page_content(raw: JsonObject, ref: DocRef) -> str:
    validate_provider_metadata(raw)
    if (
        raw.get("id") != ref.page_id
        or raw.get("doc_id") != ref.doc_id
        or str(raw.get("workspace_id")) != ref.workspace_id
        or not isinstance(raw.get("content"), str)
        or not isinstance(raw.get("name"), str)
    ):
        raise APIError("Page response has inconsistent identity or missing content/name")
    return str(raw["content"])


def display_content(content: str, *, offset: int, limit: int, column: int = 0) -> JsonObject:
    import re

    from clickup_cli.errors import InvalidOperationError

    if offset < 0 or column < 0 or not 1 <= limit <= 1000:
        raise InvalidOperationError(
            "Use non-negative --offset/--column and --limit between 1 and 1000"
        )
    safe, omissions = re.subn(
        r"data:image/[^\s)\"'<>]+", "[inline data image omitted]", content, flags=re.IGNORECASE
    )
    lines = safe.splitlines(keepends=True)
    if column and (offset >= len(lines) or column >= len(lines[offset])):
        raise InvalidOperationError("--column is outside the requested line")
    selected = "".join(lines[offset : offset + limit])
    if column:
        selected = selected[column:]
    bounded = selected[:16000]
    import json

    if len(json.dumps(bounded)) > 48000:
        lower, upper = 0, len(bounded)
        while lower < upper:
            midpoint = (lower + upper + 1) // 2
            if len(json.dumps(bounded[:midpoint])) <= 48000:
                lower = midpoint
            else:
                upper = midpoint - 1
        bounded = bounded[:lower]
    consumed = column + len(bounded)
    row = offset
    while row < min(len(lines), offset + limit) and consumed >= len(lines[row]):
        consumed -= len(lines[row])
        row += 1
    more = row < len(lines)
    return {
        "content": bounded,
        "offset": offset,
        "column": column,
        "has_more": more,
        "next_offset": row if more else None,
        "next_column": consumed if more else None,
        "returned_count": bounded.count("\n")
        + (1 if bounded and not bounded.endswith("\n") else 0),
        "total": len(lines),
        "omitted_data_images": omissions,
        "display_truncated": len(selected) > len(bounded),
    }


def comparable_content(text: str) -> str:
    """Only normalize CRLF and terminal newlines, never spaces or rich structure."""
    return text.replace("\r\n", "\n").rstrip("\n")


def page_summary(raw: JsonObject, ref: DocRef) -> JsonObject:
    content = page_content(raw, ref)
    return {
        "workspace_id": ref.workspace_id,
        "doc_id": ref.doc_id,
        "page_id": ref.page_id,
        "name": raw["name"],
        "sub_title": raw.get("sub_title"),
        "parent_page_id": raw.get("parent_page_id"),
        "url": ref.url,
        "date_created": raw.get("date_created"),
        "date_updated": raw.get("date_updated"),
        "date_edited": raw.get("date_edited"),
        "retrieved_at": retrieved_at(),
        "sha256": content_hash(content),
        "warnings": WARNINGS,
    }


def bounded_items(items: list[JsonObject], limit: int) -> list[JsonObject]:
    import json

    selected: list[JsonObject] = []
    remaining = 48000
    for item in items[:limit]:
        cost = len(json.dumps(item, ensure_ascii=True, separators=(",", ":"))) + 1
        if cost > remaining:
            break
        selected.append(item)
        remaining -= cost
    return selected


class DocsService:
    def __init__(self, client: ClickUpClient) -> None:
        self.client = client

    def list(
        self,
        workspace_id: str,
        *,
        limit: int | None = None,
        all: bool = False,
        deleted: bool = False,
        archived: bool = False,
        creator: str | None = None,
        parent_id: str | None = None,
        parent_type: str | None = None,
    ) -> JsonObject:
        from clickup_cli.doc_refs import workspace
        from clickup_cli.errors import InvalidOperationError

        workspace(workspace_id)
        limit = limit if limit is not None else 1000 if all else 50
        if not 1 <= limit <= 1000:
            raise InvalidOperationError("--limit must be between 1 and 1000")
        query: list[tuple[str, str | int]] = [
            ("deleted", str(deleted).lower()),
            ("archived", str(archived).lower()),
        ]
        if creator is not None:
            query.append(("creator", workspace(creator)))
        parent = parent_spec(parent_id, parent_type)
        if parent is not None:
            query.extend(
                [("parent_id", str(parent["id"])), ("parent_type", int(str(parent["type"])))]
            )
        query.append(("limit", max(10, min(100, limit))))
        found: list[JsonObject] = []
        seen: set[str] = set()
        cursors: set[str] = set()
        cursor: str | None = None
        complete = False
        for _ in range(100):
            payload = self.client.list_docs(
                workspace_id, [*query, *([("cursor", cursor)] if cursor is not None else [])]
            )
            docs = payload.get("docs")
            if not isinstance(docs, list) or any(not isinstance(d, dict) for d in docs):
                raise APIError("Docs listing response must contain a docs array")
            for raw in docs:
                assert isinstance(raw, dict)
                did = raw.get("id")
                if not isinstance(did, str) or did in seen:
                    raise APIError("Docs listing has missing or duplicate identity")
                seen.add(did)
                found.append(normalize_doc(raw, DocRef(workspace_id, did)))
            next_cursor = payload.get("next_cursor")
            if next_cursor == "":
                next_cursor = None
            if next_cursor is not None and not isinstance(next_cursor, str):
                raise APIError("Docs listing has invalid cursor")
            if next_cursor in cursors:
                raise APIError("Docs listing repeated cursor; traversal stopped")
            if next_cursor is None:
                complete = True
                break
            cursors.add(next_cursor)
            cursor = next_cursor
            if not all or len(found) >= limit:
                break
        returned = bounded_items(found, limit)
        return {
            "workspace_id": workspace_id,
            "docs": cast(list[JsonValue], returned),
            "returned_count": len(returned),
            "total": len(found) if complete else None,
            "complete": complete and len(found) == len(returned),
            "has_more": not complete or len(found) > len(returned),
            "warnings": []
            if complete and len(found) == len(returned)
            else [
                "Partial Doc traversal/output; use --all or raise --limit. "
                "Console byte ceiling also applies."
            ],
        }

    def pages(
        self,
        value: str,
        workspace_id: str | None = None,
        *,
        limit: int | None = None,
        all: bool = False,
        tree: bool = False,
    ) -> JsonObject:
        from clickup_cli.errors import InvalidOperationError

        ref = parse_doc_ref(value, workspace_id)
        limit = limit if limit is not None else 1000 if all else 50
        if not 1 <= limit <= 1000:
            raise InvalidOperationError("--limit must be between 1 and 1000")
        found = flatten_pages(self.client.get_doc_page_listing(ref.workspace_id, ref.doc_id), ref)
        selected = bounded_items(found, limit)
        return {
            "workspace_id": ref.workspace_id,
            "doc_id": ref.doc_id,
            "pages": cast(list[JsonValue], selected),
            "tree": tree,
            "complete": len(found) == len(selected),
            "has_more": len(found) > len(selected),
            "returned_count": len(selected),
            "total": len(found),
            "warnings": []
            if len(found) == len(selected)
            else [
                "Partial page output; node and console byte ceilings apply. "
                "Use export for full snapshot."
            ],
        }

    def search(
        self,
        query: str,
        *,
        doc: str | None,
        workspace_id: str | None = None,
        content: bool = False,
        limit: int | None = None,
        max_pages: int | None = None,
        all: bool = False,
        max_docs: int | None = None,
    ) -> JsonObject:
        from clickup_cli.errors import InvalidOperationError

        limit = limit if limit is not None else 1000 if all else 50
        max_pages = max_pages if max_pages is not None else 1000 if all else 50
        max_docs = max_docs if max_docs is not None else 100 if all else 10
        if (
            not query.strip()
            or len(query) > 1000
            or not 1 <= limit <= 1000
            or not 1 <= max_pages <= 1000
            or not 1 <= max_docs <= 100
        ):
            raise InvalidOperationError("Search requires non-empty QUERY and safe limits")
        if doc is None:
            if workspace_id is None:
                raise InvalidOperationError("Search requires --doc or explicit --workspace-id")
            catalog = self.list(workspace_id, limit=max_docs, all=all)
            resources = catalog["docs"]
            assert isinstance(resources, list)
            workspace_matches: list[JsonObject] = []
            scanned_docs = scanned_pages = total_pages = 0
            complete = bool(catalog["complete"])
            for resource in resources:
                assert isinstance(resource, dict)
                remaining = max_pages - scanned_pages
                if remaining <= 0:
                    complete = False
                    break
                scoped = self.search(
                    query,
                    doc=str(resource["doc_id"]),
                    workspace_id=workspace_id,
                    content=content,
                    limit=1000,
                    max_pages=remaining,
                )
                values = scoped["matches"]
                assert isinstance(values, list)
                workspace_matches.extend(cast(list[JsonObject], values))
                scanned_pages += int(str(scoped["scanned_pages"]))
                total_pages += int(str(scoped["total_pages"]))
                scanned_docs += 1
                complete = complete and bool(scoped["complete"])
            selected = bounded_items(workspace_matches, limit)
            complete = complete and len(selected) == len(workspace_matches)
            return {
                "workspace_id": workspace_id,
                "scope": "workspace",
                "search_mode": "local_content" if content else "local_title",
                "matches": cast(list[JsonValue], selected),
                "returned_count": len(selected),
                "scanned_docs": scanned_docs,
                "scanned_pages": scanned_pages,
                "total_pages": total_pages
                if scanned_docs == len(resources) and catalog["complete"]
                else None,
                "complete": complete,
                "has_more": not complete,
                "warnings": [
                    *WARNINGS,
                    *(
                        []
                        if complete
                        else ["Partial workspace search: Doc/page/match/output ceiling reached."]
                    ),
                ],
            }
        ref = parse_doc_ref(doc, workspace_id)
        roots = (
            self.client.get_doc_pages(ref.workspace_id, ref.doc_id)
            if content
            else self.client.get_doc_page_listing(ref.workspace_id, ref.doc_id)
        )
        found = flatten_pages(roots, ref)
        raw_by_id: dict[str, JsonObject] = {}
        if content:
            pending = list(roots)
            while pending:
                raw = pending.pop()
                raw_by_id[str(raw["id"])] = raw
                children = raw.get("pages", [])
                assert isinstance(children, list)
                pending.extend(cast(list[JsonObject], children))
        matches: list[JsonObject] = []
        needle = query.casefold()
        for item in found[:max_pages]:
            pid = str(item["page_id"])
            text = str(item["name"])
            if content:
                text = page_content(raw_by_id[pid], DocRef(ref.workspace_id, ref.doc_id, pid))
            for number, line in enumerate(text.splitlines(), 1):
                if needle in line.casefold():
                    snippet = display_content(line, offset=0, limit=1)["content"]
                    matches.append(
                        {
                            **item,
                            "snippet": str(snippet)[:500],
                            "line_start": number if content else None,
                            "line_end": number if content else None,
                        }
                    )
                    break
        selected = bounded_items(matches, limit)
        complete = len(found) <= max_pages and len(matches) == len(selected)
        return {
            "workspace_id": ref.workspace_id,
            "doc_id": ref.doc_id,
            "search_mode": "local_content" if content else "local_title",
            "matches": cast(list[JsonValue], selected),
            "returned_count": len(selected),
            "scanned_pages": min(len(found), max_pages),
            "total_pages": len(found),
            "complete": complete,
            "has_more": not complete,
            "warnings": [
                *WARNINGS,
                *([] if complete else ["Partial local search: page or match ceiling reached."]),
            ],
        }

    def page_show(
        self,
        value: str,
        *,
        doc: str | None = None,
        workspace_id: str | None = None,
        format: str = "markdown",
        offset: int = 0,
        limit: int = 100,
        column: int = 0,
    ) -> JsonObject:
        from clickup_cli.doc_refs import parse_page_ref
        from clickup_cli.errors import InvalidOperationError

        ref = parse_page_ref(value, doc, workspace_id)
        if format not in {"markdown", "plain"}:
            raise InvalidOperationError("--format must be markdown or plain")
        display_content("", offset=offset, limit=limit, column=0)
        assert ref.page_id is not None
        raw = self.client.get_doc_page(
            ref.workspace_id,
            ref.doc_id,
            ref.page_id,
            content_format="text/md" if format == "markdown" else "text/plain",
        )
        content = page_content(raw, ref)
        listing = flatten_pages(self.client.get_doc_page_listing(ref.workspace_id, ref.doc_id), ref)
        matches = [item for item in listing if item["page_id"] == ref.page_id]
        if len(matches) != 1 or matches[0]["parent_page_id"] != raw.get("parent_page_id"):
            raise APIError("Page is absent or inconsistent in authorized hierarchy")
        metadata = matches[0]
        metadata.update(
            {
                "name": raw["name"],
                "date_created": raw.get("date_created"),
                "date_updated": raw.get("date_updated"),
                "date_edited": raw.get("date_edited"),
                "sha256": content_hash(content),
                "content_format": "text/md" if format == "markdown" else "text/plain",
                "warnings": WARNINGS,
            }
        )
        import json

        if len(json.dumps(metadata)) > 12000:
            raise APIError(
                "Page hierarchy metadata exceeds console budget; use full snapshot export"
            )
        metadata.update(display_content(content, offset=offset, limit=limit, column=column))
        return {"page": metadata}

    def create(
        self,
        name: str,
        *,
        workspace_id: str,
        visibility: str = "private",
        parent_id: str | None = None,
        parent_type: str | None = None,
        create_page: bool = False,
    ) -> JsonObject:
        from clickup_cli.doc_refs import doc_id, workspace
        from clickup_cli.errors import (
            ClickUpCLIError,
            CreatedButUnverifiedError,
            InvalidOperationError,
            OutcomeUnknownError,
            TransportError,
            VerificationError,
        )

        workspace(workspace_id)
        if (
            not name.strip()
            or len(name) > 4096
            or visibility not in {"private", "public", "personal", "hidden"}
        ):
            raise InvalidOperationError("Provide a non-empty NAME and supported --visibility")
        parent = parent_spec(parent_id, parent_type)
        body: JsonObject = {
            "name": name,
            "visibility": visibility.upper(),
            "create_page": create_page,
        }
        if parent is not None:
            body["parent"] = parent
        details: JsonObject = {"workspace_id": workspace_id, "retry_safe": False}
        try:
            created = self.client.create_doc(workspace_id, body)
        except (TransportError, APIError) as exc:
            if (
                isinstance(exc, APIError)
                and exc.status_code is not None
                and 400 <= exc.status_code < 500
            ):
                raise
            raise OutcomeUnknownError(
                "Doc create outcome unknown; inspect before retrying", details=details
            ) from exc
        known = created.get("id")
        if not isinstance(known, str) or not known:
            raise OutcomeUnknownError(
                "Doc create returned no usable ID; inspect before retrying", details=details
            )
        details["doc_id"] = known
        try:
            doc_id(known, "DOC_ID")
            ref = DocRef(workspace_id, known)
            observed = normalize_doc(self.client.get_doc(workspace_id, known), ref)
            if (
                observed["name"] != name
                or observed["public"] is not (visibility == "public")
                or (parent is not None and observed["parent"] != parent)
            ):
                raise VerificationError(
                    "Created Doc readback differs from intended name/public/parent"
                )
            return {
                "doc": observed,
                "verified": True,
                "requested_visibility": visibility,
                "warnings": [
                    "GET exposes only public, not complete visibility; "
                    "personal/hidden semantics cannot be independently verified."
                ]
                if visibility in {"personal", "hidden"}
                else [],
            }
        except (ClickUpCLIError, ValueError, TypeError, KeyError, OverflowError) as exc:
            raise CreatedButUnverifiedError(
                "Doc created but readback or normalization failed; do not recreate", details=details
            ) from exc

    def page_create(
        self,
        value: str,
        *,
        name: str,
        content_file: Path,
        workspace_id: str | None = None,
        parent_page: str | None = None,
        sub_title: str | None = None,
        format: str = "markdown",
    ) -> JsonObject:
        from clickup_cli.doc_refs import doc_id, parse_page_ref
        from clickup_cli.errors import (
            ClickUpCLIError,
            CreatedButUnverifiedError,
            InvalidOperationError,
            OutcomeUnknownError,
            TransportError,
            VerificationError,
        )
        from clickup_cli.task_mutations import read_description_file

        ref = parse_doc_ref(value, workspace_id)
        if (
            not name.strip()
            or len(name) > 4096
            or (sub_title is not None and len(sub_title) > 4096)
            or format not in {"markdown", "plain"}
        ):
            raise InvalidOperationError("Invalid NAME, subtitle, or content format")
        parent = (
            parse_page_ref(parent_page, value, workspace_id) if parent_page is not None else None
        )
        content = read_description_file(content_file)
        content_format = "text/md" if format == "markdown" else "text/plain"
        normalize_doc(self.client.get_doc(ref.workspace_id, ref.doc_id), ref)
        if parent is not None:
            assert parent.page_id is not None
            page_content(
                self.client.get_doc_page(parent.workspace_id, parent.doc_id, parent.page_id), parent
            )
        body: JsonObject = {"name": name, "content": content, "content_format": content_format}
        if parent is not None:
            body["parent_page_id"] = parent.page_id
        if sub_title is not None:
            body["sub_title"] = sub_title
        details: JsonObject = {
            "workspace_id": ref.workspace_id,
            "doc_id": ref.doc_id,
            "parent_page_id": parent.page_id if parent else None,
            "retry_safe": False,
        }
        try:
            created = self.client.create_doc_page(ref.workspace_id, ref.doc_id, body)
        except (TransportError, APIError) as exc:
            if (
                isinstance(exc, APIError)
                and exc.status_code is not None
                and 400 <= exc.status_code < 500
            ):
                raise
            raise OutcomeUnknownError(
                "Page create outcome unknown; inspect before retrying", details=details
            ) from exc
        known = created.get("id")
        if not isinstance(known, str) or not known:
            raise OutcomeUnknownError(
                "Page create returned no usable ID; inspect before retrying", details=details
            )
        details["page_id"] = known
        try:
            doc_id(known, "PAGE_ID")
            target = DocRef(ref.workspace_id, ref.doc_id, known)
            observed = self.client.get_doc_page(
                ref.workspace_id, ref.doc_id, known, content_format=content_format
            )
            actual = page_content(observed, target)
            if (
                observed["name"] != name
                or observed.get("parent_page_id") != details["parent_page_id"]
                or (sub_title is not None and observed.get("sub_title") != sub_title)
                or comparable_content(actual) != comparable_content(content)
            ):
                raise VerificationError(
                    "Created page readback differs from intended fields/content"
                )
            return {"page": page_summary(observed, target), "created": True, "verified": True}
        except (ClickUpCLIError, ValueError, TypeError, KeyError, OverflowError) as exc:
            raise CreatedButUnverifiedError(
                "Page created but readback or normalization failed; do not recreate",
                details=details,
            ) from exc

    def page_update(
        self,
        value: str,
        *,
        name: str,
        doc: str | None = None,
        workspace_id: str | None = None,
        sub_title: str | None = None,
    ) -> JsonObject:
        from clickup_cli.doc_refs import parse_page_ref
        from clickup_cli.errors import (
            ClickUpCLIError,
            EditedButUnverifiedError,
            InvalidOperationError,
            OutcomeUnknownError,
            TransportError,
            VerificationError,
        )

        ref = parse_page_ref(value, doc, workspace_id)
        if (
            not name.strip()
            or len(name) > 4096
            or (sub_title is not None and len(sub_title) > 4096)
        ):
            raise InvalidOperationError("Invalid name or subtitle")
        assert ref.page_id is not None
        before = self.client.get_doc_page(ref.workspace_id, ref.doc_id, ref.page_id)
        content = page_content(before, ref)
        body: JsonObject = {"name": name}
        if sub_title is not None:
            body["sub_title"] = sub_title
        if all(before.get(key) == value for key, value in body.items()):
            return {"page": page_summary(before, ref), "changed": False, "verified": True}
        details: JsonObject = {
            "workspace_id": ref.workspace_id,
            "doc_id": ref.doc_id,
            "page_id": ref.page_id,
            "retry_safe": False,
        }
        try:
            self.client.edit_doc_page(ref.workspace_id, ref.doc_id, ref.page_id, body)
        except (TransportError, APIError) as exc:
            if (
                isinstance(exc, APIError)
                and exc.status_code is not None
                and 400 <= exc.status_code < 500
            ):
                raise
            raise OutcomeUnknownError(
                "Page edit outcome unknown; inspect before retrying", details=details
            ) from exc
        try:
            observed = self.client.get_doc_page(ref.workspace_id, ref.doc_id, ref.page_id)
            actual = page_content(observed, ref)
            if (
                any(observed.get(key) != value for key, value in body.items())
                or comparable_content(actual) != comparable_content(content)
                or observed.get("parent_page_id") != before.get("parent_page_id")
            ):
                raise VerificationError(
                    "Metadata edit readback differs from fields or original content/parent"
                )
            return {"page": page_summary(observed, ref), "changed": True, "verified": True}
        except (ClickUpCLIError, ValueError, TypeError, KeyError, OverflowError) as exc:
            raise EditedButUnverifiedError(
                "Page edited but readback or normalization failed; inspect before retrying",
                details=details,
            ) from exc

    def page_add(
        self,
        value: str,
        *,
        content_file: Path,
        mode: str,
        doc: str | None = None,
        workspace_id: str | None = None,
        format: str = "markdown",
        expect_sha256: str | None = None,
        acknowledge_loss: bool = False,
    ) -> JsonObject:
        from clickup_cli.doc_refs import parse_page_ref
        from clickup_cli.errors import (
            ClickUpCLIError,
            EditedButUnverifiedError,
            InvalidOperationError,
            OutcomeUnknownError,
            TransportError,
            VerificationError,
        )
        from clickup_cli.task_mutations import read_description_file

        ref = parse_page_ref(value, doc, workspace_id)
        if mode not in {"append", "prepend", "replace"} or format not in {"markdown", "plain"}:
            raise InvalidOperationError("Unsupported content mode or format")
        if mode == "replace":
            import re

            from clickup_cli.errors import ConfirmationError

            if not acknowledge_loss:
                raise ConfirmationError(
                    "Whole-content replacement can lose rich blocks; pass --acknowledge-loss"
                )
            if expect_sha256 is None or re.fullmatch(r"[0-9a-f]{64}", expect_sha256) is None:
                raise InvalidOperationError(
                    "--expect-sha256 requires full lowercase SHA256 from a same-format page read"
                )
        content_format = "text/md" if format == "markdown" else "text/plain"
        addition = read_description_file(content_file)
        assert ref.page_id is not None
        before = self.client.get_doc_page(
            ref.workspace_id, ref.doc_id, ref.page_id, content_format=content_format
        )
        old_content = page_content(before, ref)
        if mode == "replace" and content_hash(old_content) != expect_sha256:
            raise InvalidOperationError(
                "Stale page revision: SHA256 preflight differs; no PUT sent"
            )
        expected = (
            old_content + addition
            if mode == "append"
            else addition + old_content
            if mode == "prepend"
            else addition
        )
        body: JsonObject = {
            "content": addition,
            "content_format": content_format,
            "content_edit_mode": mode,
        }
        details: JsonObject = {
            "workspace_id": ref.workspace_id,
            "doc_id": ref.doc_id,
            "page_id": ref.page_id,
            "retry_safe": False,
            "mode": mode,
        }
        try:
            self.client.edit_doc_page(ref.workspace_id, ref.doc_id, ref.page_id, body)
        except (TransportError, APIError) as exc:
            if (
                isinstance(exc, APIError)
                and exc.status_code is not None
                and 400 <= exc.status_code < 500
            ):
                raise
            raise OutcomeUnknownError(
                "Page content edit outcome unknown; inspect before retrying", details=details
            ) from exc
        try:
            observed = self.client.get_doc_page(
                ref.workspace_id, ref.doc_id, ref.page_id, content_format=content_format
            )
            actual = page_content(observed, ref)
            expected_contents = {comparable_content(expected)}
            if mode in {"append", "prepend"}:
                # The native editor joins imported blocks with one or two
                # newlines, depending on the adjacent Markdown block types.
                # Only trim import boundaries; internal content stays exact.
                normalized_addition = comparable_content(addition).lstrip("\n")
                original = comparable_content(old_content)
                for separator in ("\n", "\n\n"):
                    paragraph_join = (
                        original + separator + normalized_addition
                        if mode == "append"
                        else normalized_addition + separator + original
                    )
                    expected_contents.add(paragraph_join)
            if comparable_content(actual) not in expected_contents or any(
                observed.get(key) != before.get(key)
                for key in ("name", "sub_title", "parent_page_id")
            ):
                raise VerificationError(
                    "Page readback differs from native edit expectation or unchanged metadata"
                )
            return {
                "page": page_summary(observed, ref),
                "changed": True,
                "verified": True,
                "mode": mode,
                "hash_guard_atomic": False,
                "warnings": [
                    *WARNINGS,
                    "Revision guard is preflight only, not atomic CAS; "
                    "another edit can occur between GET and PUT.",
                ]
                if mode == "replace"
                else WARNINGS,
            }
        except (ClickUpCLIError, ValueError, TypeError, KeyError, OverflowError) as exc:
            raise EditedButUnverifiedError(
                "Page edited but readback or normalization failed; inspect before retrying",
                details=details,
            ) from exc

    def page_ensure(
        self,
        value: str,
        *,
        name: str,
        content_file: Path,
        workspace_id: str | None = None,
        parent_page: str | None = None,
        sub_title: str | None = None,
        format: str = "markdown",
    ) -> JsonObject:
        import unicodedata

        from clickup_cli.doc_refs import parse_page_ref
        from clickup_cli.errors import AmbiguousMatchError, InvalidOperationError
        from clickup_cli.task_mutations import read_description_file

        ref = parse_doc_ref(value, workspace_id)
        if (
            not name.strip()
            or len(name) > 4096
            or (sub_title is not None and len(sub_title) > 4096)
            or format not in {"markdown", "plain"}
        ):
            raise InvalidOperationError("Invalid NAME, subtitle, or content format")
        parent = (
            parse_page_ref(parent_page, value, workspace_id) if parent_page is not None else None
        )
        read_description_file(content_file)
        normalize_doc(self.client.get_doc(ref.workspace_id, ref.doc_id), ref)
        listing = flatten_pages(self.client.get_doc_page_listing(ref.workspace_id, ref.doc_id), ref)
        parent_id = parent.page_id if parent is not None else None
        if parent_id is not None and not any(item["page_id"] == parent_id for item in listing):
            raise InvalidOperationError("Parent page is absent from authorized Doc hierarchy")
        normalized = unicodedata.normalize("NFC", name.strip())
        matches = [
            item
            for item in listing
            if item["parent_page_id"] == parent_id
            and unicodedata.normalize("NFC", str(item["name"]).strip()) == normalized
        ]
        if len(matches) > 1:
            raise AmbiguousMatchError(
                "Multiple same-name siblings; no write sent",
                details={
                    "workspace_id": ref.workspace_id,
                    "doc_id": ref.doc_id,
                    "parent_page_id": parent_id,
                    "page_ids": [item["page_id"] for item in matches],
                },
            )
        if matches:
            return {
                "page": matches[0],
                "created": False,
                "changed": False,
                "warnings": [
                    "Ensure is a preflight lookup, not concurrency-proof uniqueness; "
                    "existing content is unchanged."
                ],
            }
        return self.page_create(
            value,
            name=name,
            content_file=content_file,
            workspace_id=workspace_id,
            parent_page=parent_page,
            sub_title=sub_title,
            format=format,
        )

    def show(self, value: str, workspace_id: str | None = None) -> JsonObject:
        ref = parse_doc_ref(value, workspace_id)
        return {"doc": normalize_doc(self.client.get_doc(ref.workspace_id, ref.doc_id), ref)}
