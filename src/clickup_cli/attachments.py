"""Safe attachment normalization, upload verification, and isolated downloads."""

from __future__ import annotations

import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from urllib.parse import urljoin, urlsplit

import httpx

from clickup_cli.client import ClickUpClient
from clickup_cli.errors import (
    APIError,
    AttachmentDownloadError,
    AttachmentNotFoundError,
    AttachmentOutcomeUnknownError,
    AttachmentUploadedButUnverifiedError,
    InvalidOperationError,
    TransportError,
)
from clickup_cli.types import JsonObject, JsonValue

_LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}
_TRUSTED_ATTACHMENT_HOSTS = {"attachments.clickup.com", "attachments-public.clickup.com"}
_TRUSTED_ATTACHMENT_DOMAIN = "clickup-attachments.com"
_MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024
_MAX_REDIRECTS = 5
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}


@dataclass(frozen=True)
class AttachmentUploadResult:
    task_id: str
    attachment: JsonObject
    task: JsonObject


@dataclass(frozen=True)
class AttachmentDownloadResult:
    task_id: str
    attachment_id: str
    output: str
    size: int


def _optional_text(value: JsonValue | None) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    return str(value)


def _optional_nonnegative_integer(value: JsonValue | None, *, label: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise APIError(f"ClickUp response contains an invalid attachment {label}")
    try:
        normalized = int(value)
    except ValueError as exc:
        raise APIError(f"ClickUp response contains an invalid attachment {label}") from exc
    if normalized < 0 or str(normalized) != str(value):
        raise APIError(f"ClickUp response contains an invalid attachment {label}")
    return normalized


def normalize_attachment(attachment: JsonObject) -> JsonObject:
    """Return the stable minimal attachment shape used by every command."""

    raw_title = attachment.get("title")
    if raw_title is None:
        raw_title = attachment.get("name")
    return {
        "date": _optional_nonnegative_integer(attachment.get("date"), label="date"),
        "extension": _optional_text(attachment.get("extension")),
        "id": _optional_text(attachment.get("id")),
        "size": _optional_nonnegative_integer(attachment.get("size"), label="size"),
        "title": _optional_text(raw_title),
        "url": (str(attachment["url"]) if isinstance(attachment.get("url"), str) else None),
    }


def normalize_attachments(task: JsonObject) -> list[JsonObject]:
    raw_attachments = task.get("attachments")
    if raw_attachments is None:
        return []
    if not isinstance(raw_attachments, list):
        raise APIError("ClickUp response contains invalid task attachments")
    normalized: list[JsonObject] = []
    for raw_attachment in raw_attachments:
        if not isinstance(raw_attachment, dict):
            raise APIError("ClickUp response contains an invalid task attachment")
        normalized.append(normalize_attachment(cast(JsonObject, raw_attachment)))
    return normalized


def _validate_upload_name(value: str) -> str:
    if not value or not value.strip():
        raise InvalidOperationError("Attachment name cannot be empty")
    if value in {".", ".."} or any(
        character in value for character in ("/", "\\", "\0", "\r", "\n")
    ):
        raise InvalidOperationError("Attachment name must be a plain file name")
    return value


def validate_attachment_file(path: Path, *, name: str | None = None) -> tuple[Path, str]:
    """Validate a local upload path without retaining an open file handle."""

    try:
        metadata = path.stat()
    except OSError as exc:
        raise InvalidOperationError(f"Could not access attachment file: {path}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise InvalidOperationError(f"Attachment path is not a regular file: {path}")
    try:
        with path.open("rb") as handle:
            handle.read(0)
    except OSError as exc:
        raise InvalidOperationError(f"Attachment file is not readable: {path}") from exc
    upload_name = _validate_upload_name(name if name is not None else path.name)
    return path, upload_name


def _normalized_hostname(value: str) -> str:
    try:
        return value.encode("idna").decode("ascii").casefold()
    except UnicodeError as exc:
        raise AttachmentDownloadError("Attachment URL contains an invalid hostname") from exc


def _validated_download_url(value: str, *, allow_localhost: bool) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.hostname is None:
        raise AttachmentDownloadError("Attachment URL must be an absolute HTTPS URL")
    try:
        _port = parsed.port
    except ValueError as exc:
        raise AttachmentDownloadError("Attachment URL contains an invalid port") from exc
    if parsed.username is not None or parsed.password is not None:
        raise AttachmentDownloadError("Attachment URL cannot contain credentials")
    if parsed.fragment:
        raise AttachmentDownloadError("Attachment URL cannot contain a fragment")
    hostname = _normalized_hostname(parsed.hostname)
    if parsed.scheme == "http" and allow_localhost and hostname in _LOCAL_HOSTS:
        return value
    if parsed.scheme != "https":
        raise AttachmentDownloadError(
            "Attachment downloads require HTTPS on a trusted ClickUp attachment host"
        )
    if hostname in _TRUSTED_ATTACHMENT_HOSTS:
        return value
    if hostname == _TRUSTED_ATTACHMENT_DOMAIN or hostname.endswith(
        f".{_TRUSTED_ATTACHMENT_DOMAIN}"
    ):
        return value
    raise AttachmentDownloadError("Attachment URL host is not a trusted ClickUp attachment host")


def _validate_output_path(output: Path, *, force: bool) -> None:
    if not output.name:
        raise InvalidOperationError("Attachment output must name a file")
    parent = output.parent
    if not parent.is_dir():
        raise InvalidOperationError(f"Attachment output directory does not exist: {parent}")
    if output.exists() and output.is_dir():
        raise InvalidOperationError(f"Attachment output is a directory: {output}")
    if not force and output.exists():
        raise InvalidOperationError(
            f"Attachment output already exists; use --force to replace it: {output}"
        )


def _install_download(temp_path: Path, output: Path, *, force: bool) -> None:
    if force:
        os.replace(temp_path, output)
        return
    try:
        os.link(temp_path, output)
    except FileExistsError as exc:
        raise InvalidOperationError(
            f"Attachment output already exists; use --force to replace it: {output}"
        ) from exc
    temp_path.unlink()


def _download_without_authorization(
    url: str,
    output: Path,
    *,
    force: bool,
    allow_localhost: bool,
) -> int:
    _validate_output_path(output, force=force)
    current_url = _validated_download_url(url, allow_localhost=allow_localhost)
    temp_path: Path | None = None
    try:
        with httpx.Client(
            timeout=httpx.Timeout(30.0, connect=5.0),
            headers={"Accept": "*/*"},
            follow_redirects=False,
            trust_env=False,
        ) as http:
            for redirect_count in range(_MAX_REDIRECTS + 1):
                try:
                    stream_context = http.stream("GET", current_url)
                    with stream_context as response:
                        if response.status_code in _REDIRECT_STATUSES:
                            if redirect_count == _MAX_REDIRECTS:
                                raise AttachmentDownloadError(
                                    f"Attachment download exceeded {_MAX_REDIRECTS} redirects"
                                )
                            location = response.headers.get("Location")
                            if not location:
                                raise AttachmentDownloadError(
                                    "Attachment redirect did not include a Location header"
                                )
                            current_url = _validated_download_url(
                                urljoin(current_url, location),
                                allow_localhost=allow_localhost,
                            )
                            continue
                        if response.is_error:
                            raise AttachmentDownloadError(
                                f"Attachment host returned HTTP {response.status_code}"
                            )
                        raw_length = response.headers.get("Content-Length")
                        if raw_length:
                            try:
                                content_length = int(raw_length)
                            except ValueError as exc:
                                raise AttachmentDownloadError(
                                    "Attachment host returned an invalid Content-Length"
                                ) from exc
                            if content_length < 0 or content_length > _MAX_DOWNLOAD_BYTES:
                                raise AttachmentDownloadError(
                                    "Attachment exceeds the 100 MiB download safety limit"
                                )

                        descriptor, raw_temp_path = tempfile.mkstemp(
                            prefix=".clickup-attachment-",
                            dir=output.parent,
                        )
                        temp_path = Path(raw_temp_path)
                        total = 0
                        with os.fdopen(descriptor, "wb") as handle:
                            for chunk in response.iter_bytes():
                                total += len(chunk)
                                if total > _MAX_DOWNLOAD_BYTES:
                                    raise AttachmentDownloadError(
                                        "Attachment exceeds the 100 MiB download safety limit"
                                    )
                                handle.write(chunk)
                            handle.flush()
                            os.fsync(handle.fileno())
                        _install_download(temp_path, output, force=force)
                        temp_path = None
                        return total
                except httpx.RequestError as exc:
                    raise AttachmentDownloadError("Attachment download request failed") from exc
        raise AttachmentDownloadError("Attachment download did not produce a response")
    except OSError as exc:
        raise AttachmentDownloadError(f"Could not write attachment output: {output}") from exc
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                pass


class AttachmentService:
    """Orchestrate attachment operations around authoritative task readbacks."""

    def __init__(self, client: ClickUpClient) -> None:
        self._client = client

    def list(self, task_id: str) -> list[JsonObject]:
        return normalize_attachments(self._client.get_task(task_id))

    def upload(
        self,
        task_id: str,
        path: Path,
        *,
        name: str | None = None,
    ) -> AttachmentUploadResult:
        validated_path, upload_name = validate_attachment_file(path, name=name)
        try:
            created = self._client.upload_task_attachment(
                task_id,
                validated_path,
                upload_name=upload_name,
            )
        except TransportError as exc:
            raise AttachmentOutcomeUnknownError(
                "Attachment upload outcome is unknown; inspect the task before retrying",
                details={"path": str(path), "task_id": task_id},
            ) from exc
        except APIError as exc:
            status_code = exc.status_code
            if (
                status_code is None
                or status_code == 408
                or status_code >= 500
                or 200 <= status_code < 300
            ):
                raise AttachmentOutcomeUnknownError(
                    "Attachment upload outcome is unknown; inspect the task before retrying",
                    details={"path": str(path), "task_id": task_id},
                ) from exc
            raise

        attachment_id = _optional_text(created.get("id"))
        raw_title = created.get("title")
        if raw_title is None:
            raw_title = created.get("name")
        title = _optional_text(raw_title)
        if not isinstance(attachment_id, str) or not attachment_id:
            raise AttachmentOutcomeUnknownError(
                "Attachment upload returned no usable ID; inspect the task before retrying",
                details={"path": str(path), "task_id": task_id},
            )
        if not isinstance(title, str) or not title:
            raise AttachmentUploadedButUnverifiedError(
                "Attachment upload returned no usable title; inspect the task before retrying",
                details={"attachment_id": attachment_id, "task_id": task_id},
            )
        try:
            readback = self._client.get_task(task_id)
            attachments = normalize_attachments(readback)
        except (APIError, TransportError) as exc:
            raise AttachmentUploadedButUnverifiedError(
                "Attachment was uploaded but task readback failed",
                details={"attachment_id": attachment_id, "task_id": task_id},
            ) from exc
        match = next(
            (
                attachment
                for attachment in attachments
                if attachment.get("id") == attachment_id and attachment.get("title") == title
            ),
            None,
        )
        if match is None:
            raise AttachmentUploadedButUnverifiedError(
                "Attachment verification failed: returned ID and title were not on the task",
                details={"attachment_id": attachment_id, "task_id": task_id},
            )
        return AttachmentUploadResult(task_id=task_id, attachment=match, task=readback)

    def download(
        self,
        task_id: str,
        attachment_id: str,
        output: Path,
        *,
        force: bool,
    ) -> AttachmentDownloadResult:
        if (
            not attachment_id
            or not attachment_id.strip()
            or any(character in attachment_id for character in ("\0", "\r", "\n"))
        ):
            raise InvalidOperationError("ATTACHMENT_ID cannot be empty or contain control data")
        attachments = self.list(task_id)
        match = next(
            (attachment for attachment in attachments if attachment.get("id") == attachment_id),
            None,
        )
        if match is None:
            raise AttachmentNotFoundError(
                f"Attachment {attachment_id} was not found on task {task_id}",
                details={"attachment_id": attachment_id, "task_id": task_id},
            )
        url = match.get("url")
        if not isinstance(url, str) or not url:
            raise AttachmentDownloadError(
                f"Attachment {attachment_id} does not have a download URL"
            )
        size = _download_without_authorization(
            url,
            output,
            force=force,
            allow_localhost=self._client.allows_local_attachment_downloads,
        )
        return AttachmentDownloadResult(
            task_id=task_id,
            attachment_id=attachment_id,
            output=str(output),
            size=size,
        )
