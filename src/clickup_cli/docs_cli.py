"""Thin Docs command adapters."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import typer

from clickup_cli.docs import DocsService
from clickup_cli.types import JsonObject, JsonValue

CONTENT_FILE_OPTION = typer.Option(
    ..., help="Regular UTF-8 file, at most 1 MiB; stdin is unsupported."
)
OUTPUT_DIRECTORY_OPTION = typer.Option(..., help="New snapshot directory; parent must exist.")

doc_app = typer.Typer(
    no_args_is_help=True, help="Read, author, and export authorized ClickUp Docs."
)
page_app = typer.Typer(no_args_is_help=True, help="Read and safely author Doc pages.")
doc_app.add_typer(page_app, name="page")


@page_app.command("create")
def page_create(
    context: typer.Context,
    doc_ref: str,
    name: str = typer.Option(...),
    content_file: Path = CONTENT_FILE_OPTION,
    workspace_id: str | None = typer.Option(None),
    parent_page: str | None = typer.Option(None),
    sub_title: str | None = typer.Option(None),
    format: str = typer.Option("markdown"),
) -> None:
    """Create one root/child page from a regular UTF-8 file (at most 1 MiB); verify GET."""
    run(
        context,
        lambda service: service.page_create(
            doc_ref,
            name=name,
            content_file=content_file,
            workspace_id=workspace_id,
            parent_page=parent_page,
            sub_title=sub_title,
            format=format,
        ),
    )


@page_app.command("update")
def page_update(
    context: typer.Context,
    page_ref: str,
    name: str = typer.Option(...),
    doc: str | None = typer.Option(None),
    workspace_id: str | None = typer.Option(None),
    sub_title: str | None = typer.Option(None),
) -> None:
    """Metadata-only rename/subtitle edit: never sends empty content."""
    run(
        context,
        lambda service: service.page_update(
            page_ref, name=name, doc=doc, workspace_id=workspace_id, sub_title=sub_title
        ),
    )


@page_app.command("append")
def page_append(
    context: typer.Context,
    page_ref: str,
    content_file: Path = CONTENT_FILE_OPTION,
    doc: str | None = typer.Option(None),
    workspace_id: str | None = typer.Option(None),
    format: str = typer.Option("markdown"),
) -> None:
    """Append natively; never auto-retry or replace exported Markdown."""
    run(
        context,
        lambda service: service.page_add(
            page_ref,
            content_file=content_file,
            mode="append",
            doc=doc,
            workspace_id=workspace_id,
            format=format,
        ),
    )


@page_app.command("prepend")
def page_prepend(
    context: typer.Context,
    page_ref: str,
    content_file: Path = CONTENT_FILE_OPTION,
    doc: str | None = typer.Option(None),
    workspace_id: str | None = typer.Option(None),
    format: str = typer.Option("markdown"),
) -> None:
    """Prepend natively; never auto-retry or replace exported Markdown."""
    run(
        context,
        lambda service: service.page_add(
            page_ref,
            content_file=content_file,
            mode="prepend",
            doc=doc,
            workspace_id=workspace_id,
            format=format,
        ),
    )


@page_app.command("replace")
def page_replace(
    context: typer.Context,
    page_ref: str,
    content_file: Path = CONTENT_FILE_OPTION,
    expect_sha256: str = typer.Option(...),
    acknowledge_loss: bool = typer.Option(False, "--acknowledge-loss"),
    doc: str | None = typer.Option(None),
    workspace_id: str | None = typer.Option(None),
    format: str = typer.Option("markdown"),
) -> None:
    """Replace whole content with rich-block loss acknowledgement; guard is NOT atomic CAS."""
    run(
        context,
        lambda service: service.page_add(
            page_ref,
            content_file=content_file,
            mode="replace",
            doc=doc,
            workspace_id=workspace_id,
            format=format,
            expect_sha256=expect_sha256,
            acknowledge_loss=acknowledge_loss,
        ),
    )


@page_app.command("ensure")
def page_ensure(
    context: typer.Context,
    doc_ref: str,
    name: str = typer.Option(...),
    content_file: Path = CONTENT_FILE_OPTION,
    workspace_id: str | None = typer.Option(None),
    parent_page: str | None = typer.Option(None),
    sub_title: str | None = typer.Option(None),
    format: str = typer.Option("markdown"),
) -> None:
    """Exact siblings: create zero, leave one unchanged, reject multiple. Not atomic uniqueness."""
    run(
        context,
        lambda service: service.page_ensure(
            doc_ref,
            name=name,
            content_file=content_file,
            workspace_id=workspace_id,
            parent_page=parent_page,
            sub_title=sub_title,
            format=format,
        ),
    )


@page_app.command("show")
def page_show(
    context: typer.Context,
    page_ref: str,
    doc: str | None = typer.Option(None),
    workspace_id: str | None = typer.Option(None),
    format: str = typer.Option("markdown"),
    offset: int = typer.Option(0),
    limit: int = typer.Option(100),
    column: int = typer.Option(0),
) -> None:
    """Read bounded page lines. Resume long lines with next_offset/next_column."""
    run(
        context,
        lambda service: service.page_show(
            page_ref,
            doc=doc,
            workspace_id=workspace_id,
            format=format,
            offset=offset,
            limit=limit,
            column=column,
        ),
    )


def warning_lines(value: JsonValue | None) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else []


def text_result(result: JsonObject) -> str:
    page = result.get("page")
    if isinstance(page, dict):
        header = f"{page.get('name')} [{page.get('page_id')}]\n{page.get('url')}"
        if "content" in page:
            continuation = (
                f"\nContinue: --offset {page['next_offset']} --column {page['next_column']}"
                if page.get("has_more")
                else ""
            )
            warnings = "\n".join(warning_lines(page.get("warnings")))
            return f"{header}\n\n{page['content']}{continuation}\n{warnings}"
        return header + f"\nverified={result.get('verified')} changed={result.get('changed')}"
    for key in ("pages", "matches", "docs"):
        items = result.get(key)
        if isinstance(items, list):
            lines = []
            for item in items:
                assert isinstance(item, dict)
                indent = (
                    "  " * min(int(str(item.get("depth", 0))), 128) if result.get("tree") else ""
                )
                lines.append(
                    f"{indent}{item.get('page_id', item.get('doc_id'))} {item.get('name')}"
                )
                if "snippet" in item:
                    lines.append(
                        f"  {item['url']} lines={item.get('line_start')}-{item.get('line_end')}"
                        f"\n  {item['snippet']}"
                    )
            lines.append(
                f"returned={result['returned_count']} complete={result.get('complete')} "
                f"has_more={result.get('has_more')}"
            )
            lines.extend(warning_lines(result.get("warnings")))
            return "\n".join(lines)
    return json.dumps(result, ensure_ascii=False, indent=2)


def run(context: typer.Context, operation: Callable[[DocsService], JsonObject]) -> None:
    from clickup_cli.cli import _execute, _state, _with_client

    state = _state(context)
    _execute(
        state,
        lambda: _with_client(state, lambda client: operation(DocsService(client))),
        json_result=lambda result: result,
        text_result=text_result,
    )


@doc_app.command("list")
def list_docs(
    context: typer.Context,
    workspace_id: str = typer.Option(...),
    limit: int | None = typer.Option(
        None, help="Result ceiling, 1-1000; default 50 or 1000 with --all."
    ),
    all: bool = typer.Option(False, "--all"),
    deleted: bool = typer.Option(False),
    archived: bool = typer.Option(False),
    creator: str | None = typer.Option(None),
    parent_id: str | None = typer.Option(None),
    parent_type: str | None = typer.Option(None),
) -> None:
    """List accessible Docs; --all follows cursors up to the result/request ceiling."""
    run(
        context,
        lambda service: service.list(
            workspace_id,
            limit=limit,
            all=all,
            deleted=deleted,
            archived=archived,
            creator=creator,
            parent_id=parent_id,
            parent_type=parent_type,
        ),
    )


@doc_app.command("pages")
def pages(
    context: typer.Context,
    doc_ref: str,
    workspace_id: str | None = typer.Option(None),
    limit: int | None = typer.Option(
        None, help="Result ceiling, 1-1000; default 50 or 1000 with --all."
    ),
    all: bool = typer.Option(False, "--all"),
    tree: bool = typer.Option(False, "--tree"),
) -> None:
    """Recursively list pages with parents/breadcrumbs; every node counts toward --limit."""
    run(
        context,
        lambda service: service.pages(doc_ref, workspace_id, limit=limit, all=all, tree=tree),
    )


@doc_app.command("search")
def search(
    context: typer.Context,
    query: str,
    doc: str | None = typer.Option(None),
    workspace_id: str | None = typer.Option(None),
    content: bool = typer.Option(False, "--content"),
    limit: int | None = typer.Option(
        None, help="Result ceiling, 1-1000; default 50 or 1000 with --all."
    ),
    max_pages: int | None = typer.Option(
        None, help="Search page ceiling, 1-1000; default 50, --all 1000."
    ),
    max_docs: int | None = typer.Option(
        None, help="Workspace Doc ceiling, 1-100; default 10, --all 100."
    ),
    all: bool = typer.Option(False, "--all"),
) -> None:
    """Local page title/body search in an explicit Doc or Workspace. Never provider text search."""
    run(
        context,
        lambda service: service.search(
            query,
            doc=doc,
            workspace_id=workspace_id,
            content=content,
            limit=limit,
            max_pages=max_pages,
            max_docs=max_docs,
            all=all,
        ),
    )


@doc_app.command("create")
def create(
    context: typer.Context,
    name: str,
    workspace_id: str = typer.Option(...),
    visibility: str = typer.Option("private"),
    parent_id: str | None = typer.Option(None),
    parent_type: str | None = typer.Option(None),
    create_page: bool = typer.Option(False, "--create-page/--no-create-page"),
) -> None:
    """Create a private Doc by default, without a blank page, then read back."""
    run(
        context,
        lambda service: service.create(
            name,
            workspace_id=workspace_id,
            visibility=visibility,
            parent_id=parent_id,
            parent_type=parent_type,
            create_page=create_page,
        ),
    )


@doc_app.command("export")
def export(
    context: typer.Context,
    doc_ref: str,
    output: Path = OUTPUT_DIRECTORY_OPTION,
    workspace_id: str | None = typer.Option(None),
    format: str = typer.Option("markdown"),
) -> None:
    """Install a full private snapshot in a NEW directory. No attachments or overwrite."""
    from clickup_cli.doc_export import DocSnapshotService

    run(
        context,
        lambda service: DocSnapshotService(service.client).export(
            doc_ref, output=output, workspace_id=workspace_id, format=format
        ),
    )


@doc_app.command("show")
def show(
    context: typer.Context, doc_ref: str, workspace_id: str | None = typer.Option(None)
) -> None:
    """Read Doc metadata by URL or explicitly scoped literal ID."""
    run(context, lambda service: service.show(doc_ref, workspace_id))
