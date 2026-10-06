"""Private, staged text snapshots with fail-closed filesystem boundaries."""

from __future__ import annotations

import ctypes
import errno
import json
import os
import uuid
from pathlib import Path
from typing import cast

from clickup_cli.client import ClickUpClient
from clickup_cli.doc_refs import DocRef, parse_doc_ref
from clickup_cli.docs import (
    WARNINGS,
    content_hash,
    flatten_pages,
    normalize_doc,
    page_content,
    retrieved_at,
)
from clickup_cli.errors import APIError, InvalidOperationError
from clickup_cli.types import JsonObject, JsonValue

MAX_SNAPSHOT_BYTES = 100 * 1024 * 1024


def open_parent(output: Path) -> tuple[int, str]:
    """Pin every existing directory component without following symlinks."""
    if ".." in output.parts:
        raise InvalidOperationError("Export output cannot contain '..'")
    absolute = output.absolute()
    if absolute.name in {"", ".", ".."}:
        raise InvalidOperationError("Export needs a new named destination directory")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in absolute.parent.parts[1:]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        if info.st_mode & 0o022 or info.st_uid not in {0, os.getuid()}:
            raise InvalidOperationError(
                "Export parent must be user/root-owned and not group/world writable"
            )
        try:
            os.stat(absolute.name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            return fd, absolute.name
        raise InvalidOperationError(
            "Export destination already exists; overwrite is intentionally unsupported"
        )
    except BaseException:
        os.close(fd)
        raise


def install_snapshot(parent_fd: int, stage: str, name: str) -> None:
    """Linux renameat2 NOREPLACE prevents a raced destination from being overwritten."""
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        rename = libc.renameat2
    except AttributeError as exc:
        raise InvalidOperationError("Atomic no-overwrite export requires Linux renameat2") from exc
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(parent_fd, os.fsencode(stage), parent_fd, os.fsencode(name), 1) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def private_file(directory_fd: int, name: str, data: bytes) -> None:
    fd = os.open(
        name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd
    )
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


class DocSnapshotService:
    def __init__(self, client: ClickUpClient) -> None:
        self.client = client

    def export(
        self, value: str, *, output: Path, workspace_id: str | None = None, format: str = "markdown"
    ) -> JsonObject:
        ref = parse_doc_ref(value, workspace_id)
        if format not in {"markdown", "json"}:
            raise InvalidOperationError("Export --format must be markdown or json")
        parent_fd: int | None = None
        stage_fd: int | None = None
        stage = ".clickup-snapshot-" + uuid.uuid4().hex
        installed = False
        try:
            parent_fd, name = open_parent(output)
            os.mkdir(stage, 0o700, dir_fd=parent_fd)
            stage_fd = os.open(
                stage, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd
            )
            metadata = normalize_doc(self.client.get_doc(ref.workspace_id, ref.doc_id), ref)
            roots = self.client.get_doc_pages(ref.workspace_id, ref.doc_id)
            pages = flatten_pages(roots, ref)
            raw_by_id: dict[str, JsonObject] = {}
            stack = list(roots)
            while stack:
                raw = stack.pop()
                raw_by_id[str(raw["id"])] = raw
                children = raw.get("pages", [])
                assert isinstance(children, list)
                stack.extend(cast(list[JsonObject], children))
            manifest_pages: list[JsonObject] = []
            total_bytes = 0
            for item in pages:
                pid = str(item["page_id"])
                raw = raw_by_id[pid]
                content = page_content(raw, DocRef(ref.workspace_id, ref.doc_id, pid))
                filename = f"page-{pid}." + ("md" if format == "markdown" else "json")
                serialized = (
                    content
                    if format == "markdown"
                    else json.dumps(
                        {
                            **item,
                            "content": content,
                            "sub_title": raw.get("sub_title"),
                            "warnings": WARNINGS,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                        indent=2,
                    )
                    + "\n"
                )
                data = serialized.encode("utf-8")
                total_bytes += len(data)
                if total_bytes > MAX_SNAPSHOT_BYTES:
                    raise APIError(
                        "Snapshot exceeds the 100 MiB safety ceiling; no export installed"
                    )
                private_file(stage_fd, filename, data)
                manifest_pages.append(
                    {
                        **item,
                        "filename": filename,
                        "content_format": "text/md",
                        "content_sha256": content_hash(content),
                        "file_sha256": content_hash(serialized),
                        "warnings": WARNINGS,
                    }
                )
            manifest: JsonObject = {
                "snapshot_kind": "text_snapshot",
                "complete": True,
                "format": format,
                "doc": metadata,
                "retrieved_at": retrieved_at(),
                "page_count": len(pages),
                "pages": cast(list[JsonValue], manifest_pages),
                "warnings": [
                    *WARNINGS,
                    "A multi-page read is not an atomic point-in-time backup. "
                    "No linked resources or attachments are downloaded.",
                ],
            }
            manifest_bytes = (
                json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
            ).encode("utf-8")
            if total_bytes + len(manifest_bytes) > MAX_SNAPSHOT_BYTES:
                raise APIError(
                    "Snapshot and manifest exceed the 100 MiB ceiling; no export installed"
                )
            private_file(stage_fd, "manifest.json", manifest_bytes)
            os.fsync(stage_fd)
            install_snapshot(parent_fd, stage, name)
            installed = True
            return {
                "output": str(output.absolute()),
                "manifest": str(output.absolute() / "manifest.json"),
                "page_count": len(pages),
                "snapshot_kind": "text_snapshot",
                "complete": True,
                "warnings": manifest["warnings"],
            }
        except OSError as exc:
            message = (
                "Export destination already exists"
                if exc.errno == errno.EEXIST
                else "Could not safely build/install export; no snapshot installed"
            )
            raise InvalidOperationError(message) from exc
        finally:
            if stage_fd is not None:
                if not installed:
                    for child in os.listdir(stage_fd):
                        os.unlink(child, dir_fd=stage_fd)
                os.close(stage_fd)
                if not installed and parent_fd is not None:
                    os.rmdir(stage, dir_fd=parent_fd)
            if parent_fd is not None:
                os.close(parent_fd)
