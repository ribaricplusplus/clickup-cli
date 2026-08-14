from __future__ import annotations

from pathlib import Path

import pytest

from clickup_cli.config import parse_env_file, resolve_base_url, resolve_token
from clickup_cli.errors import ConfigurationError


def test_parse_env_file_supports_safe_dotenv_syntax(tmp_path: Path) -> None:
    env_file = tmp_path / "clickup.env"
    env_file.write_text(
        "# comment\n"
        "export CLICKUP_API_TOKEN='file value' # trailing\n"
        'DOUBLE="line\\nvalue"\n'
        "LITERAL=$(not-run)\n",
        encoding="utf-8",
    )

    assert parse_env_file(env_file) == {
        "CLICKUP_API_TOKEN": "file value",
        "DOUBLE": "line\nvalue",
        "LITERAL": "$(not-run)",
    }


def test_process_token_precedes_env_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    env_file = tmp_path / "clickup.env"
    env_file.write_text("this line is intentionally malformed", encoding="utf-8")
    monkeypatch.setenv("CLICKUP_API_TOKEN", "process-value")

    assert resolve_token(env_file) == "process-value"


def test_token_falls_back_to_selected_env_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env_file = tmp_path / "clickup.env"
    env_file.write_text("CLICKUP_API_TOKEN=file-value\n", encoding="utf-8")
    monkeypatch.delenv("CLICKUP_API_TOKEN", raising=False)

    assert resolve_token(env_file) == "file-value"


def test_missing_token_has_concise_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("CLICKUP_API_TOKEN", raising=False)
    with pytest.raises(ConfigurationError, match="CLICKUP_API_TOKEN is not set"):
        resolve_token(tmp_path / "missing.env")


def test_base_url_precedence_and_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLICKUP_API_BASE_URL", "http://127.0.0.1:9123/from-env/")
    assert resolve_base_url(None) == "http://127.0.0.1:9123/from-env"
    assert resolve_base_url("https://api.example.invalid/root/") == (
        "https://api.example.invalid/root"
    )
    with pytest.raises(ConfigurationError):
        resolve_base_url("api.example.invalid/root")
    with pytest.raises(ConfigurationError, match="only for localhost"):
        resolve_base_url("http://api.example.invalid/root")
    with pytest.raises(ConfigurationError, match="cannot contain credentials"):
        resolve_base_url("https://user:password@api.example.invalid/root")
    with pytest.raises(ConfigurationError, match="invalid port"):
        resolve_base_url("https://api.example.invalid:not-a-port/root")
