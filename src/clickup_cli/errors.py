"""Public error types with concise, credential-safe messages."""

from typing import TypeAlias

from clickup_cli.types import JsonValue

ErrorDetail: TypeAlias = JsonValue


class ClickUpCLIError(Exception):
    """Base class for expected application errors."""

    error_type = "clickup_error"

    def __init__(self, message: str, *, details: dict[str, ErrorDetail] | None = None) -> None:
        super().__init__(message)
        self.details = dict(details or {})


class ConfigurationError(ClickUpCLIError):
    """Configuration is missing or invalid."""

    error_type = "configuration_error"


class ReferenceError(ClickUpCLIError):
    """A task reference cannot be parsed safely."""

    error_type = "invalid_task_reference"


class APIError(ClickUpCLIError):
    """ClickUp returned an unsuccessful or malformed response."""

    error_type = "api_error"

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        details: dict[str, ErrorDetail] | None = None,
    ) -> None:
        super().__init__(message, details=details)
        self.status_code = status_code


class TransportError(ClickUpCLIError):
    """The ClickUp endpoint could not be reached."""

    error_type = "transport_error"


class OutcomeUnknownError(ClickUpCLIError):
    """A non-idempotent write may have succeeded before its response was lost."""

    error_type = "outcome_unknown"


class CreatedButUnverifiedError(ClickUpCLIError):
    """A created task ID is known, but final verification or normalization failed."""

    error_type = "created_but_unverified"


class CreatedButAttachmentFailedError(ClickUpCLIError):
    """A task was created, but one of its requested attachments did not finish safely."""

    error_type = "created_but_attachment_failed"


class InvalidStatusError(ClickUpCLIError):
    """A requested status does not exist in the task's home list."""

    error_type = "invalid_status"


class InvalidDueDateError(ClickUpCLIError):
    """A requested due date is not an accepted ISO date or timestamp."""

    error_type = "invalid_due_date"


class InvalidStartDateError(ClickUpCLIError):
    """A requested start date is not an accepted ISO date or timestamp."""

    error_type = "invalid_start_date"


class InvalidPriorityError(ClickUpCLIError):
    """A requested priority is not supported."""

    error_type = "invalid_priority"


class InvalidTimeRangeError(ClickUpCLIError):
    """A time boundary or requested range is invalid or unsafe."""

    error_type = "invalid_time_range"


class InvalidDurationError(ClickUpCLIError):
    """A human duration cannot be represented by the ClickUp API."""

    error_type = "invalid_duration"


class InvalidOperationError(ClickUpCLIError):
    """An operation input is unsafe or unsupported."""

    error_type = "invalid_operation"


class CommentNotFoundError(ClickUpCLIError):
    """A requested comment is not present in the task's comment history."""

    error_type = "comment_not_found"


class CompletionStatusError(ClickUpCLIError):
    """The task's home list has no semantic completion status."""

    error_type = "completion_status_unavailable"


class VerificationError(ClickUpCLIError):
    """A write readback did not match the requested state."""

    error_type = "verification_failed"


class ConfirmationError(ClickUpCLIError):
    """A destructive operation was not explicitly confirmed."""

    error_type = "confirmation_required"


class ResourceNotFoundError(ClickUpCLIError):
    """A requested catalog resource was not available to the authorized user."""

    error_type = "resource_not_found"


class AmbiguousMatchError(ClickUpCLIError):
    """An idempotent operation found more than one safe candidate."""

    error_type = "ambiguous_match"


class AttachmentNotFoundError(ClickUpCLIError):
    """A requested attachment is not present on the fetched task."""

    error_type = "attachment_not_found"


class AttachmentOutcomeUnknownError(ClickUpCLIError):
    """An attachment upload may have succeeded before its response was lost."""

    error_type = "attachment_outcome_unknown"


class AttachmentUploadedButUnverifiedError(ClickUpCLIError):
    """An uploaded attachment ID is known, but task readback did not verify it."""

    error_type = "attachment_uploaded_but_unverified"


class AttachmentDownloadError(ClickUpCLIError):
    """An attachment could not be downloaded safely."""

    error_type = "attachment_download_failed"
