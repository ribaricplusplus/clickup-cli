"""Offline contract coverage for local dates, profiles and safe task creation."""

from __future__ import annotations

import json
import tomllib
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from typer.testing import CliRunner

from clickup_cli.batch import load_manifest
from clickup_cli.cli import app
from clickup_cli.config import resolve_settings
from clickup_cli.discovery import DiscoveryService, DueFilter
from clickup_cli.domain import DueDateInput, parse_due_date
from clickup_cli.errors import APIError, ConfigurationError
from clickup_cli.task_mutations import (
    StartDateInput,
    StartDateState,
    parse_start_date,
    start_date_display,
)
from tests.conftest import MockClickUpAPI

runner = CliRunner()
TASK_ID = "task_123"
LIST_ID = "list_456"
HEADERS = {"Accept": "application/json", "Authorization": "test-token"}


def invoke(api: MockClickUpAPI, args: list[str], *, timezone: str | None = None):  # type: ignore[no-untyped-def]
    globals = ["--json", "--base-url", api.base_url]
    if timezone is not None:
        globals += ["--timezone", timezone]
    return runner.invoke(app, [*globals, *args], env={"CLICKUP_API_TOKEN": "test-token"})


def task(*, description: str = "text", due: int | None = None) -> dict[str, object]:
    return {
        "id": TASK_ID,
        "list": {"id": LIST_ID},
        "name": "Example",
        "description": description,
        "status": {"status": "Open", "type": "open"},
        "assignees": [],
        "tags": [],
        "due_date": str(due) if due is not None else None,
        "due_date_time": False if due is not None else None,
    }


def expect_create(api: MockClickUpAPI, description: str, *, readback: str | None = None) -> None:
    api.expect(
        "POST",
        f"/api/v2/list/{LIST_ID}/task",
        headers={**HEADERS, "Content-Type": "application/json"},
        json_body={"name": "Example", "description": description},
        response_json={"id": TASK_ID},
    )
    api.expect(
        "GET",
        f"/api/v2/task/{TASK_ID}",
        headers=HEADERS,
        response_json=task(description=description if readback is None else readback),
    )


def test_date_only_uses_local_midnight_across_dst() -> None:
    zone = ZoneInfo("Europe/Zurich")
    for day in ("2030-03-30", "2030-04-01", "2030-10-28"):
        expected = int(datetime.fromisoformat(day).replace(tzinfo=zone).timestamp() * 1000)
        assert parse_due_date(day, timezone=zone).milliseconds == expected
    assert (
        parse_due_date("2030-04-01T00:00:00Z", timezone=zone).milliseconds
        == parse_due_date("2030-04-01T00:00:00Z").milliseconds
    )


def test_start_date_only_uses_local_midnight_and_displays_local_date() -> None:
    zone = ZoneInfo("Europe/Zurich")
    expected = int(datetime(2030, 4, 1, tzinfo=zone).timestamp() * 1000)
    parsed = parse_start_date("2030-04-01", timezone=zone)
    assert parsed.milliseconds == expected
    assert (
        start_date_display(StartDateState(expected + 3_600_000, False), timezone=zone)
        == "2030-04-01"
    )
    assert (
        parse_start_date("2030-04-01T00:00:00Z", timezone=zone).milliseconds
        == parse_start_date("2030-04-01T00:00:00Z").milliseconds
    )


def test_local_start_date_overflow_is_stable_api_error() -> None:
    end_of_year = int(datetime(9999, 12, 31, 23, tzinfo=ZoneInfo("UTC")).timestamp() * 1000)
    with pytest.raises(APIError, match="out-of-range start date"):
        start_date_display(StartDateState(end_of_year, False), timezone=ZoneInfo("Europe/Zurich"))


def test_batch_manifest_date_only_uses_selected_timezone(tmp_path: Path) -> None:
    path = tmp_path / "updates.jsonl"
    path.write_text(
        '{"task":"task_123","set":{"due_date":"2030-04-01","start_date":"2030-04-01"}}\n',
        encoding="utf-8",
    )
    manifest = load_manifest(path, timezone=ZoneInfo("Europe/Zurich"))
    expected = int(datetime(2030, 4, 1, tzinfo=ZoneInfo("Europe/Zurich")).timestamp() * 1000)
    values = [
        op.value.milliseconds
        for op in manifest.tasks[0].operations
        if isinstance(op.value, (DueDateInput, StartDateInput))
    ]
    assert values == [expected, expected]


def test_show_date_only_is_local_calendar_day(mock_api: MockClickUpAPI) -> None:
    local = datetime(2030, 4, 1, tzinfo=ZoneInfo("Europe/Zurich"))
    api_task = task(due=int(local.timestamp() * 1000))
    mock_api.expect("GET", f"/api/v2/task/{TASK_ID}", headers=HEADERS, response_json=api_task)
    result = invoke(mock_api, ["task", "show", TASK_ID], timezone="Europe/Zurich")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["result"]["task"]["due_date"] == "2030-04-01"


def test_create_due_date_accepts_clickup_same_local_day_normalization(
    mock_api: MockClickUpAPI,
) -> None:
    zone = ZoneInfo("Europe/Zurich")
    midnight = int(datetime(2030, 4, 1, tzinfo=zone).timestamp() * 1000)
    mock_api.expect(
        "POST",
        f"/api/v2/list/{LIST_ID}/task",
        headers={**HEADERS, "Content-Type": "application/json"},
        json_body={"name": "Example", "due_date": midnight, "due_date_time": False},
        response_json={"id": TASK_ID},
    )
    mock_api.expect(
        "GET",
        f"/api/v2/task/{TASK_ID}",
        headers=HEADERS,
        response_json=task(due=midnight + 3_600_000),
    )
    result = invoke(
        mock_api,
        ["task", "create", "Example", "--list-id", LIST_ID, "--due-date", "2030-04-01"],
        timezone=zone.key,
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["result"]["task"]["due_date"] == "2030-04-01"


def test_update_start_date_accepts_same_local_day_normalization(mock_api: MockClickUpAPI) -> None:
    zone = ZoneInfo("Europe/Zurich")
    midnight = int(datetime(2030, 4, 1, tzinfo=zone).timestamp() * 1000)
    before = task()
    before["start_date"] = None
    before["start_date_time"] = None
    after = {**before, "start_date": str(midnight + 3_600_000), "start_date_time": False}
    mock_api.expect("GET", f"/api/v2/task/{TASK_ID}", headers=HEADERS, response_json=before)
    mock_api.expect(
        "PUT",
        f"/api/v2/task/{TASK_ID}",
        headers={**HEADERS, "Content-Type": "application/json"},
        json_body={"start_date": midnight, "start_date_time": False},
        response_json={},
    )
    mock_api.expect("GET", f"/api/v2/task/{TASK_ID}", headers=HEADERS, response_json=after)
    result = invoke(
        mock_api, ["task", "update", TASK_ID, "--start-date", "2030-04-01"], timezone=zone.key
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["result"]["task"]["start_date"] == "2030-04-01"


def test_cli_rejects_invalid_timezone_as_configuration_error(mock_api: MockClickUpAPI) -> None:
    result = invoke(mock_api, ["task", "show", TASK_ID], timezone="Mars/Olympus")
    assert result.exit_code == 1
    assert json.loads(result.stderr)["error"]["type"] == "configuration_error"
    assert mock_api.state.requests == []


def test_due_filter_local_day_crosses_dst(monkeypatch: pytest.MonkeyPatch) -> None:
    zone = ZoneInfo("Europe/Zurich")
    now = datetime(2030, 3, 31, 12, tzinfo=zone)
    service = DiscoveryService(None, now=lambda: now, timezone=zone)  # type: ignore[arg-type]
    start, end = service._due_bounds(DueFilter(kind="today"))
    assert start == int(datetime(2030, 3, 31, tzinfo=zone).timestamp() * 1000)
    assert end == int(datetime(2030, 4, 1, tzinfo=zone).timestamp() * 1000)
    assert end - start == 23 * 3_600_000


def test_description_readback_accepts_only_trailing_whitespace(mock_api: MockClickUpAPI) -> None:
    expect_create(mock_api, "First line\nSecond line\n", readback="First line\nSecond line")
    result = invoke(
        mock_api,
        [
            "task",
            "create",
            "Example",
            "--list-id",
            LIST_ID,
            "--description",
            "First line\nSecond line\n",
        ],
    )
    assert result.exit_code == 0, result.output


def test_description_truncation_preserves_created_id(mock_api: MockClickUpAPI) -> None:
    expect_create(mock_api, "First line\nSecond line", readback="First line")
    result = invoke(
        mock_api,
        [
            "task",
            "create",
            "Example",
            "--list-id",
            LIST_ID,
            "--description",
            "First line\nSecond line",
        ],
    )
    assert result.exit_code == 1
    assert json.loads(result.stderr)["error"]["type"] == "created_but_unverified"
    assert json.loads(result.stderr)["error"]["task_id"] == TASK_ID


@pytest.mark.parametrize(
    ("sent", "returned"),
    [
        ("- first\n- second\n", "* first\n* second"),
        ("Review version 2\\.0, please.\n", "Review version 2.0, please."),
    ],
)
def test_create_accepts_narrow_markdown_equivalence(
    mock_api: MockClickUpAPI, sent: str, returned: str
) -> None:
    expect_create(mock_api, sent, readback=returned)
    result = invoke(
        mock_api, ["task", "create", "Example", "--list-id", LIST_ID, "--description", sent]
    )
    assert result.exit_code == 0, result.output


def test_create_missing_markdown_link_preserves_id(mock_api: MockClickUpAPI) -> None:
    expect_create(mock_api, "Read [source](https://example.invalid/doc).", readback="Read source.")
    result = invoke(
        mock_api,
        [
            "task",
            "create",
            "Example",
            "--list-id",
            LIST_ID,
            "--description",
            "Read [source](https://example.invalid/doc).",
        ],
    )
    assert result.exit_code == 1
    error = json.loads(result.stderr)["error"]
    assert (error["type"], error["task_id"]) == ("created_but_unverified", TASK_ID)


def test_created_task_local_summary_error_still_preserves_id(mock_api: MockClickUpAPI) -> None:
    end_of_year = int(datetime(9999, 12, 31, 23, 0, tzinfo=ZoneInfo("UTC")).timestamp() * 1000)
    mock_api.expect(
        "POST",
        f"/api/v2/list/{LIST_ID}/task",
        headers={**HEADERS, "Content-Type": "application/json"},
        json_body={"name": "Example"},
        response_json={"id": TASK_ID},
    )
    mock_api.expect(
        "GET", f"/api/v2/task/{TASK_ID}", headers=HEADERS, response_json=task(due=end_of_year)
    )
    result = invoke(
        mock_api, ["task", "create", "Example", "--list-id", LIST_ID], timezone="Europe/Zurich"
    )
    assert result.exit_code == 1
    error = json.loads(result.stderr)["error"]
    assert (error["type"], error["task_id"]) == ("created_but_unverified", TASK_ID)


def test_ensure_local_summary_error_still_preserves_id(mock_api: MockClickUpAPI) -> None:
    end_of_year = int(datetime(9999, 12, 31, 23, tzinfo=ZoneInfo("UTC")).timestamp() * 1000)
    mock_api.expect(
        "GET",
        f"/api/v2/list/{LIST_ID}/task?page=0&subtasks=true&include_closed=true",
        headers=HEADERS,
        response_json={"last_page": True, "tasks": []},
    )
    mock_api.expect(
        "POST",
        f"/api/v2/list/{LIST_ID}/task",
        headers={**HEADERS, "Content-Type": "application/json"},
        json_body={"name": "Example"},
        response_json={"id": TASK_ID},
    )
    mock_api.expect(
        "GET", f"/api/v2/task/{TASK_ID}", headers=HEADERS, response_json=task(due=end_of_year)
    )
    result = invoke(
        mock_api, ["task", "ensure", "Example", "--list-id", LIST_ID], timezone="Europe/Zurich"
    )
    assert result.exit_code == 1
    error = json.loads(result.stderr)["error"]
    assert (error["type"], error["task_id"]) == ("created_but_unverified", TASK_ID)


def test_create_description_file_reads_utf8(mock_api: MockClickUpAPI, tmp_path: Path) -> None:
    path = tmp_path / "description.md"
    path.write_text("Grüezi\n", encoding="utf-8")
    expect_create(mock_api, "Grüezi\n", readback="Grüezi")
    result = invoke(
        mock_api,
        ["task", "create", "Example", "--list-id", LIST_ID, "--description-file", str(path)],
    )
    assert result.exit_code == 0, result.output


def test_create_description_file_conflict_is_preflight(
    mock_api: MockClickUpAPI, tmp_path: Path
) -> None:
    path = tmp_path / "description.md"
    path.write_text("text", encoding="utf-8")
    result = invoke(
        mock_api,
        [
            "task",
            "create",
            "Example",
            "--list-id",
            LIST_ID,
            "--description",
            "text",
            "--description-file",
            str(path),
        ],
    )
    assert result.exit_code == 1
    assert json.loads(result.stderr)["error"]["type"] == "invalid_operation"
    assert mock_api.state.requests == []


@pytest.mark.parametrize("data", [b"\xff", b"x" * (1024 * 1024 + 1)])
def test_create_description_file_rejects_invalid_content_before_post(
    mock_api: MockClickUpAPI, tmp_path: Path, data: bytes
) -> None:
    path = tmp_path / "description.md"
    path.write_bytes(data)
    result = invoke(
        mock_api,
        ["task", "create", "Example", "--list-id", LIST_ID, "--description-file", str(path)],
    )
    assert result.exit_code == 1
    assert json.loads(result.stderr)["error"]["type"] == "invalid_operation"
    assert mock_api.state.requests == []


def test_ensure_description_file_uses_verified_creation(
    mock_api: MockClickUpAPI, tmp_path: Path
) -> None:
    path = tmp_path / "description.md"
    path.write_text("Details\n", encoding="utf-8")
    mock_api.expect(
        "GET",
        f"/api/v2/list/{LIST_ID}/task?page=0&subtasks=true&include_closed=true",
        headers=HEADERS,
        response_json={"last_page": True, "tasks": []},
    )
    expect_create(mock_api, "Details\n", readback="Details")
    result = invoke(
        mock_api,
        ["task", "ensure", "Example", "--list-id", LIST_ID, "--description-file", str(path)],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["result"]["created"] is True


def test_profile_precedence_and_environment_token(
    mock_api: MockClickUpAPI, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        'default_profile = "work"\n[profiles.work]\n'
        'env_file = "work.env"\ntimezone = "Europe/Zurich"\n'
        '[profiles.home]\nenv_file = "home.env"\ntimezone = "America/New_York"\n',
        encoding="utf-8",
    )
    (tmp_path / "work.env").write_text("CLICKUP_API_TOKEN=work-token\n", encoding="utf-8")
    (tmp_path / "home.env").write_text("CLICKUP_API_TOKEN=home-token\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLICKUP_PROFILE", raising=False)
    monkeypatch.delenv("CLICKUP_ENV_FILE", raising=False)
    monkeypatch.delenv("CLICKUP_TIMEZONE", raising=False)
    settings = resolve_settings(None, None, None, config_path=config)
    assert settings.env_file == tmp_path / "work.env"
    assert settings.timezone.key == "Europe/Zurich"
    monkeypatch.setenv("CLICKUP_PROFILE", "home")
    monkeypatch.setenv("CLICKUP_ENV_FILE", str(tmp_path / "override.env"))
    monkeypatch.setenv("CLICKUP_TIMEZONE", "Asia/Tokyo")
    settings = resolve_settings(None, None, None, config_path=config)
    assert settings.env_file == tmp_path / "override.env"
    assert settings.timezone.key == "Asia/Tokyo"
    assert (
        resolve_settings("work", tmp_path / "cli.env", "UTC", config_path=config).env_file
        == tmp_path / "cli.env"
    )
    assert (
        resolve_settings("work", tmp_path / "cli.env", "UTC", config_path=config).timezone.key
        == "UTC"
    )
    # A process token is never superseded by a profile's env file.
    from clickup_cli.config import resolve_token

    monkeypatch.setenv("CLICKUP_API_TOKEN", "process-token")
    assert resolve_token(tmp_path / "home.env") == "process-token"


@pytest.mark.parametrize(
    "body",
    [
        'default_profile = "missing"\n',
        '[profiles.work]\ntoken = "secret"\n',
        "bad = [\n",
        '[profiles.work]\ntimezone = "Mars/Olympus"\n',
    ],
)
def test_invalid_configuration_is_explicit(tmp_path: Path, body: str) -> None:
    config = tmp_path / "config.toml"
    config.write_text(body, encoding="utf-8")
    with pytest.raises(ConfigurationError):
        resolve_settings(None, None, None, config_path=config)


def test_unknown_profile_is_not_silently_defaulted(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="Unknown profile"):
        resolve_settings("missing", None, None, config_path=tmp_path / "absent.toml")


def test_explicit_empty_timezone_does_not_fall_back(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="timezone"):
        resolve_settings(None, None, "", config_path=tmp_path / "absent.toml")


def test_runtime_bundles_iana_database_for_windows() -> None:
    project_path = Path(__file__).parents[1] / "pyproject.toml"
    project = tomllib.loads(project_path.read_text(encoding="utf-8"))
    assert any(dependency.startswith("tzdata") for dependency in project["project"]["dependencies"])


def test_cli_profile_selects_env_file_and_timezone(
    mock_api: MockClickUpAPI, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / ".config" / "clickup-cli"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text(
        '[profiles.work]\nenv_file = "work.env"\ntimezone = "Europe/Zurich"\n',
        encoding="utf-8",
    )
    (config_dir / "work.env").write_text("CLICKUP_API_TOKEN=profile-token\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLICKUP_API_TOKEN", raising=False)
    monkeypatch.delenv("CLICKUP_ENV_FILE", raising=False)
    monkeypatch.delenv("CLICKUP_TIMEZONE", raising=False)
    monkeypatch.delenv("CLICKUP_PROFILE", raising=False)
    due = int(datetime(2030, 4, 1, tzinfo=ZoneInfo("Europe/Zurich")).timestamp() * 1000)
    mock_api.expect(
        "GET",
        f"/api/v2/task/{TASK_ID}",
        headers={"Accept": "application/json", "Authorization": "profile-token"},
        response_json=task(due=due),
    )
    result = runner.invoke(
        app,
        ["--json", "--profile", "work", "--base-url", mock_api.base_url, "task", "show", TASK_ID],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["result"]["task"]["due_date"] == "2030-04-01"
