"""Environment-based configuration without executable dotenv parsing."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from clickup_cli.errors import ConfigurationError

DEFAULT_API_BASE_URL = "https://api.clickup.com/api"
DEFAULT_ENV_FILE = Path("~/.config/clickup-cli/env")
DEFAULT_CONFIG_FILE = Path("~/.config/clickup-cli/config.toml")
_ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


@dataclass(frozen=True)
class Settings:
    env_file: Path
    timezone: ZoneInfo


def validate_timezone(value: str) -> ZoneInfo:
    if not value or value.startswith(("/", ".")) or "\\" in value:
        raise ConfigurationError(f"Invalid IANA timezone: {value!r}")
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigurationError(f"Invalid IANA timezone: {value!r}") from exc


def resolve_settings(
    profile: str | None,
    env_file: Path | None,
    timezone: str | None,
    *,
    config_path: Path = DEFAULT_CONFIG_FILE,
) -> Settings:
    """Resolve token-free profile settings with CLI > env > profile > defaults."""

    path = config_path.expanduser()
    config: dict[str, object] = {}
    if path.exists():
        try:
            config = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
            raise ConfigurationError(f"Invalid config file: {path}") from exc
    if set(config) - {"default_profile", "profiles"}:
        raise ConfigurationError("Config supports only default_profile and profiles (no secrets)")
    raw_profiles = config.get("profiles", {})
    if not isinstance(raw_profiles, dict):
        raise ConfigurationError("Config profiles must be a table")
    for name, entry in raw_profiles.items():
        if not isinstance(entry, dict) or set(entry) - {"env_file", "timezone"}:
            raise ConfigurationError(f"Profile {name!r} supports only env_file and timezone")
        for key, value in entry.items():
            if not isinstance(value, str) or not value.strip():
                raise ConfigurationError(f"Profile {name!r} {key} must be nonempty text")
        if "timezone" in entry:
            validate_timezone(entry["timezone"])
    default_profile = config.get("default_profile")
    if default_profile is not None and (
        not isinstance(default_profile, str) or not default_profile.strip()
    ):
        raise ConfigurationError("default_profile must be a nonempty profile name")
    if isinstance(default_profile, str) and default_profile not in raw_profiles:
        raise ConfigurationError(f"Unknown profile: {default_profile!r}")
    selected = profile if profile is not None else os.environ.get("CLICKUP_PROFILE")
    if selected is None:
        selected = default_profile
    if selected is not None and selected not in raw_profiles:
        raise ConfigurationError(f"Unknown profile: {selected!r}")
    entry = raw_profiles.get(selected, {})
    assert isinstance(entry, dict)
    profile_file = entry.get("env_file")
    if isinstance(profile_file, str):
        profile_path = Path(profile_file).expanduser()
        if not profile_path.is_absolute():
            profile_path = path.parent / profile_path
    else:
        profile_path = DEFAULT_ENV_FILE
    chosen_env = env_file or (
        Path(os.environ["CLICKUP_ENV_FILE"]) if os.environ.get("CLICKUP_ENV_FILE") else profile_path
    )
    chosen_zone = (
        timezone
        if timezone is not None
        else os.environ.get("CLICKUP_TIMEZONE", entry.get("timezone", "UTC"))
    )
    assert isinstance(chosen_zone, str)
    return Settings(env_file=chosen_env.expanduser(), timezone=validate_timezone(chosen_zone))


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
