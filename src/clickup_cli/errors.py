"""Public error types with concise, credential-safe messages."""


class ClickUpCLIError(Exception):
    """Base class for expected application errors."""

    error_type = "clickup_error"


class ConfigurationError(ClickUpCLIError):
    """Configuration is missing or invalid."""

    error_type = "configuration_error"


class ReferenceError(ClickUpCLIError):
    """A task reference cannot be parsed safely."""

    error_type = "invalid_task_reference"


class APIError(ClickUpCLIError):
    """ClickUp returned an unsuccessful or malformed response."""

    error_type = "api_error"


class TransportError(ClickUpCLIError):
    """The ClickUp endpoint could not be reached."""

    error_type = "transport_error"


class InvalidStatusError(ClickUpCLIError):
    """A requested status does not exist in the task's home list."""

    error_type = "invalid_status"


class CompletionStatusError(ClickUpCLIError):
    """The task's home list has no semantic completion status."""

    error_type = "completion_status_unavailable"


class VerificationError(ClickUpCLIError):
    """A write readback did not match the requested state."""

    error_type = "verification_failed"


class ConfirmationError(ClickUpCLIError):
    """A destructive operation was not explicitly confirmed."""

    error_type = "confirmation_required"
