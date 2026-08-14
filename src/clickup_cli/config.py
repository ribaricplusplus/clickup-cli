"""Environment-based configuration without executable dotenv parsing."""

from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from clickup_cli.errors import ConfigurationError

DEFAULT_API_BASE_URL = "https://api.clickup.com/api"
DEFAULT_ENV_FILE = Path("~/.config/clickup-cli/env")
_ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def _quoted_value(value: str, quote: str, *, line_number: int) -> str:
    output: list[str] = []
    escaped = False
    closing_index: int | None = None
    for index, character in enumerate(value[1:], start=1):
        if escaped and quote == '"':
            output.append({"n": "\n", "r": "\r", "t": "\t"}.get(character, character))
            escaped = False
        elif character == "\\" and quote == '"':
            escaped = True
        elif character == quote:
            closing_index = index
            break
        else:
            output.append(character)
    if escaped or closing_index is None:
        raise ConfigurationError(f"Malformed quoted value on env-file line {line_number}")
    trailing = value[closing_index + 1 :].strip()
    if trailing and not trailing.startswith("#"):
        raise ConfigurationError(f"Unexpected content on env-file line {line_number}")
    return "".join(output)


def _unquoted_value(value: str) -> str:
    for index, character in enumerate(value):
        if character == "#" and (index == 0 or value[index - 1].isspace()):
            return value[:index].rstrip()
    return value.strip()


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse simple dotenv assignments as data, never as shell code."""

    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigurationError(f"Could not read env file: {path}") from exc

    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(content.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ConfigurationError(f"Malformed env-file assignment on line {line_number}")
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if _ENV_KEY.fullmatch(key) is None:
            raise ConfigurationError(f"Invalid env-file key on line {line_number}")
        value = raw_value.strip()
        if value.startswith(("'", '"')):
            values[key] = _quoted_value(value, value[0], line_number=line_number)
        else:
            values[key] = _unquoted_value(value)
    return values


def resolve_token(env_file: Path) -> str:
    """Resolve a personal token from the process, then the selected env file."""

    process_token = os.environ.get("CLICKUP_API_TOKEN")
    if process_token:
        return process_token

    expanded_path = env_file.expanduser()
    if expanded_path.is_file():
        file_token = parse_env_file(expanded_path).get("CLICKUP_API_TOKEN")
        if file_token:
            return file_token
    raise ConfigurationError(
        "CLICKUP_API_TOKEN is not set in the environment or configured env file"
    )


def resolve_base_url(option_value: str | None) -> str:
    """Resolve and validate the direct API base URL."""

    value = option_value or os.environ.get("CLICKUP_API_BASE_URL") or DEFAULT_API_BASE_URL
    value = value.rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConfigurationError("API base URL must be an absolute http or https URL")
    try:
        _port = parsed.port
    except ValueError as exc:
        raise ConfigurationError("API base URL contains an invalid port") from exc
    if parsed.username is not None or parsed.password is not None:
        raise ConfigurationError("API base URL cannot contain credentials")
    if parsed.query or parsed.fragment:
        raise ConfigurationError("API base URL cannot contain a query or fragment")
    hostname = (parsed.hostname or "").casefold()
    if parsed.scheme == "http" and hostname not in {"127.0.0.1", "::1", "localhost"}:
        raise ConfigurationError("Plain HTTP API base URLs are allowed only for localhost")
    return value
