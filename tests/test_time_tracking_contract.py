from __future__ import annotations

import json
from typing import Any

import pytest
from typer.testing import CliRunner

from clickup_cli.cli import app
from clickup_cli.errors import InvalidDurationError, InvalidTimeRangeError
from clickup_cli.time_tracking import parse_duration, parse_time_boundary, parse_time_range
from tests.conftest import MockClickUpAPI

AUTH_VALUE = "time-unit-auth-value"
WORKSPACE_ID = "42"
ENTRY_ID = "1001"
TASK_ID = "task_123"
START_MS = 1_767_225_600_000
END_MS = 1_767_229_200_000
READ_HEADERS = {"Accept": "application/json", "Authorization": AUTH_VALUE}
TIME_HEADERS = {**READ_HEADERS, "Content-Type": "application/json"}

runner = CliRunner()


def invoke(api: MockClickUpAPI, args: list[str], *, json_output: bool = False):  # type: ignore[no-untyped-def]
    global_args = ["--base-url", api.base_url]
    if json_output:
        global_args.append("--json")
    return runner.invoke(app, [*global_args, *args], env={"CLICKUP_API_TOKEN": AUTH_VALUE})


def entry_payload(
    *,
    entry_id: str = ENTRY_ID,
    task_id: str | None = TASK_ID,
    task_name: str = "Synthetic task",
    start: int = START_MS,
    duration: int = 3_600_000,
    description: str = "Worked safely",
    billable: bool = False,
    running: bool = False,
    tags: list[Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "billable": billable,
        "description": description,
        "duration": str(-5_000 if running else duration),
        "id": entry_id,
        "source": "clickup",
        "start": str(start),
        "tags": [] if tags is None else tags,
        "user": {
            "email": "user@example.invalid",
            "id": 7,
            "username": "Example User",
        },
        "wid": WORKSPACE_ID,
    }
    if task_id is not None:
        payload["task"] = {"id": task_id, "name": task_name}
    if not running:
        payload["end"] = str(start + duration)
    return payload


def expect_current(
    api: MockClickUpAPI,
    payload: dict[str, Any] | None,
    *,
    assignee: int | None = None,
) -> None:
    path = f"/api/v2/team/{WORKSPACE_ID}/time_entries/current"
    if assignee is not None:
        path = f"{path}?assignee={assignee}"
    api.expect(
        "GET",
        path,
        headers=TIME_HEADERS,
        response_json={"data": payload},
    )


def expect_entry(api: MockClickUpAPI, payload: dict[str, Any]) -> None:
    api.expect(
        "GET",
        f"/api/v2/team/{WORKSPACE_ID}/time_entries/{payload['id']}",
        headers=TIME_HEADERS,
        response_json={"data": payload},
    )


@pytest.mark.parametrize(
    ("value", "milliseconds"),
    [
        ("45m", 2_700_000),
        ("1h30m", 5_400_000),
        ("90s", 90_000),
        ("2h3m4s", 7_384_000),
    ],
)
def test_duration_parser_returns_exact_milliseconds(value: str, milliseconds: int) -> None:
    parsed = parse_duration(value)

    assert parsed.milliseconds == milliseconds


@pytest.mark.parametrize(
    "value",
    ["", "0s", "-1h", "+1h", "1.5h", "1h 30m", "30m1h", "1ms", "h", "٢h", "2147484s"],
)
def test_duration_parser_rejects_zero_negative_ambiguous_and_overflow_values(
    value: str,
) -> None:
    with pytest.raises(InvalidDurationError):
        parse_duration(value)


def test_time_boundary_parses_dates_and_offsets_to_milliseconds() -> None:
    date_value = parse_time_boundary("2026-01-01", label="FROM")
    offset_value = parse_time_boundary("2026-01-01T02:30:00+02:00", label="FROM")
    precise_value = parse_time_boundary("2026-01-01T00:00:00.123Z", label="FROM")

    assert date_value.milliseconds == START_MS
    assert date_value.date_only is True
    assert offset_value.milliseconds == START_MS + 1_800_000
    assert offset_value.display == "2026-01-01T00:30:00Z"
    assert precise_value.milliseconds == START_MS + 123


@pytest.mark.parametrize(
    "value",
    [
        "2026-01-01T00:00:00",
        "2026-01-01T00:00:00.0001Z",
        "1969-12-31T23:59:59Z",
        "2026-02-30",
        "٢٠٢٦-01-01",
    ],
)
def test_time_boundary_rejects_naive_precise_old_and_invalid_values(value: str) -> None:
    with pytest.raises(InvalidTimeRangeError):
        parse_time_boundary(value, label="FROM")


def test_add_start_rejects_date_only_values() -> None:
    with pytest.raises(InvalidTimeRangeError, match="timezone-aware"):
        parse_time_boundary("2026-01-01", label="START", allow_date=False)


def test_time_range_is_start_inclusive_end_exclusive_and_bounded() -> None:
    parsed = parse_time_range("2026-01-01", "2026-01-02")

    assert parsed.start_ms == START_MS
    assert parsed.end_ms == START_MS + 86_400_000


@pytest.mark.parametrize(
    ("from_value", "to_value", "message"),
    [
        ("2026-01-01", "2026-01-01", "later"),
        ("2026-01-02", "2026-01-01", "later"),
        ("2025-01-01", "2026-01-03", "366 days"),
    ],
)
def test_time_range_rejects_empty_inverted_and_unreasonably_broad_ranges(
    from_value: str, to_value: str, message: str
) -> None:
    with pytest.raises(InvalidTimeRangeError, match=message):
        parse_time_range(from_value, to_value)


def test_current_exact_contract_and_stable_json(mock_api: MockClickUpAPI) -> None:
    expect_current(mock_api, entry_payload(running=True))

    result = invoke(
        mock_api,
        ["time", "current", "--workspace-id", WORKSPACE_ID],
        json_output=True,
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "ok": True,
        "result": {
            "entry": {
                "billable": False,
                "description": "Worked safely",
                "duration_ms": -5_000,
                "end": None,
                "end_ms": None,
                "id": ENTRY_ID,
                "running": True,
                "source": "clickup",
                "start": "2026-01-01T00:00:00Z",
                "start_ms": START_MS,
                "tags": [],
                "task_id": TASK_ID,
                "task_name": "Synthetic task",
                "user": {
                    "email": "user@example.invalid",
                    "id": "7",
                    "username": "Example User",
                },
                "workspace_id": WORKSPACE_ID,
            }
        },
    }


def test_current_assignee_query_encoding(mock_api: MockClickUpAPI) -> None:
    expect_current(mock_api, None, assignee=77)

    result = invoke(
        mock_api,
        ["time", "current", "--workspace-id", WORKSPACE_ID, "--assignee", "77"],
        json_output=True,
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["result"]["entry"] is None


@pytest.mark.parametrize("empty_data", [None, {}])
def test_current_no_timer_has_stable_text(
    mock_api: MockClickUpAPI, empty_data: dict[str, Any] | None
) -> None:
    expect_current(mock_api, empty_data)

    result = invoke(mock_api, ["time", "current", "--workspace-id", WORKSPACE_ID])

    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == "No running timer"


def test_current_rejects_malformed_data(mock_api: MockClickUpAPI) -> None:
    mock_api.expect(
        "GET",
        f"/api/v2/team/{WORKSPACE_ID}/time_entries/current",
        headers=TIME_HEADERS,
        response_json={"data": []},
    )

    result = invoke(mock_api, ["time", "current", "--workspace-id", WORKSPACE_ID])

    assert result.exit_code == 1
    assert "invalid current timer data" in result.stderr


def test_list_exact_query_encoding_and_stable_json(mock_api: MockClickUpAPI) -> None:
    mock_api.expect(
        "GET",
        f"/api/v2/team/{WORKSPACE_ID}/time_entries?"
        f"start_date={START_MS}&end_date={START_MS + 86_400_000}&assignee=77&"
        f"task_id={TASK_ID}&is_billable=false",
        headers=TIME_HEADERS,
        response_json={"data": [entry_payload()]},
    )

    result = invoke(
        mock_api,
        [
            "time",
            "list",
            "--workspace-id",
            WORKSPACE_ID,
            "--from",
            "2026-01-01",
            "--to",
            "2026-01-02",
            "--assignee",
            "77",
            "--task",
            f"https://app.clickup.com/t/{TASK_ID}",
            "--non-billable",
        ],
        json_output=True,
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)["result"]
    assert payload["from_ms"] == START_MS
    assert payload["to_ms"] == START_MS + 86_400_000
    assert payload["range_semantics"] == "start-inclusive,end-exclusive"
    assert payload["workspace_id"] == WORKSPACE_ID
    assert payload["entries"][0]["duration_ms"] == 3_600_000
    assert payload["entries"][0]["end"] == "2026-01-01T01:00:00Z"


@pytest.mark.parametrize(
    ("option", "value", "query"),
    [
        ("--space-id", "11", "space_id=11"),
        ("--folder-id", "12", "folder_id=12"),
        ("--list-id", "13", "list_id=13"),
    ],
)
def test_list_encodes_each_numeric_location_filter(
    mock_api: MockClickUpAPI, option: str, value: str, query: str
) -> None:
    mock_api.expect(
        "GET",
        f"/api/v2/team/{WORKSPACE_ID}/time_entries?"
        f"start_date={START_MS}&end_date={START_MS + 86_400_000}&{query}",
        headers=TIME_HEADERS,
        response_json={"data": []},
    )

    result = invoke(
        mock_api,
        [
            "time",
            "list",
            "--workspace-id",
            WORKSPACE_ID,
            "--from",
            "2026-01-01",
            "--to",
            "2026-01-02",
            option,
            value,
        ],
    )

    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == "No time entries"


@pytest.mark.parametrize(
    "extra_args",
    [
        ["--space-id", "11", "--folder-id", "12"],
        ["--billable", "--non-billable"],
        ["--assignee", "0"],
    ],
)
def test_list_rejects_conflicting_or_invalid_filters_before_network(
    mock_api: MockClickUpAPI, extra_args: list[str]
) -> None:
    result = invoke(
        mock_api,
        [
            "time",
            "list",
            "--workspace-id",
            WORKSPACE_ID,
            "--from",
            "2026-01-01",
            "--to",
            "2026-01-02",
            *extra_args,
        ],
    )

    assert result.exit_code == 1
    assert mock_api.state.requests == []


def test_list_rejects_invalid_range_before_network(mock_api: MockClickUpAPI) -> None:
    result = invoke(
        mock_api,
        [
            "time",
            "list",
            "--workspace-id",
            WORKSPACE_ID,
            "--from",
            "2026-01-02",
            "--to",
            "2026-01-01",
        ],
    )

    assert result.exit_code == 1
    assert "TO must be later" in result.stderr
    assert mock_api.state.requests == []


@pytest.mark.parametrize(
    "args",
    [
        ["time", "current", "--workspace-id", "0042"],
        [
            "time",
            "start",
            "--workspace-id",
            WORKSPACE_ID,
            "--billable",
            "--non-billable",
        ],
        [
            "time",
            "add",
            "--workspace-id",
            WORKSPACE_ID,
            "--start",
            "2026-01-01T00:00:00Z",
            "--duration",
            "0s",
        ],
        [
            "time",
            "delete",
            "../entry",
            "--workspace-id",
            WORKSPACE_ID,
            "--yes",
        ],
    ],
)
def test_time_commands_reject_strict_invalid_inputs_before_network(
    mock_api: MockClickUpAPI, args: list[str]
) -> None:
    result = invoke(mock_api, args)

    assert result.exit_code == 1
    assert mock_api.state.requests == []


@pytest.mark.parametrize("response", [{}, {"data": {}}, {"data": ["bad"]}])
def test_list_rejects_malformed_responses(
    mock_api: MockClickUpAPI, response: dict[str, Any]
) -> None:
    mock_api.expect(
        "GET",
        f"/api/v2/team/{WORKSPACE_ID}/time_entries?"
        f"start_date={START_MS}&end_date={START_MS + 86_400_000}",
        headers=TIME_HEADERS,
        response_json=response,
    )

    result = invoke(
        mock_api,
        [
            "time",
            "list",
            "--workspace-id",
            WORKSPACE_ID,
            "--from",
            "2026-01-01",
            "--to",
            "2026-01-02",
        ],
    )

    assert result.exit_code == 1
    assert "time-entry" in result.stderr


def test_start_exact_preflight_write_and_verification_sequence(
    mock_api: MockClickUpAPI,
) -> None:
    expect_current(mock_api, None)
    mock_api.expect(
        "POST",
        f"/api/v2/team/{WORKSPACE_ID}/time_entries/start",
        headers=TIME_HEADERS,
        json_body={
            "billable": True,
            "description": "Focus work",
            "tid": TASK_ID,
        },
        response_json={
            "data": entry_payload(running=True, description="Focus work", billable=True)
        },
    )
    expect_current(
        mock_api,
        entry_payload(running=True, description="Focus work", billable=True),
    )

    result = invoke(
        mock_api,
        [
            "time",
            "start",
            "--workspace-id",
            WORKSPACE_ID,
            "--task",
            TASK_ID,
            "--description",
            "Focus work",
            "--billable",
        ],
        json_output=True,
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)["result"]
    assert payload["changed"] is True
    assert payload["entry"]["id"] == ENTRY_ID
    assert payload["entry"]["running"] is True


def test_start_minimal_body_is_an_exact_empty_object(mock_api: MockClickUpAPI) -> None:
    running = entry_payload(running=True, task_id=None, description="")
    expect_current(mock_api, None)
    mock_api.expect(
        "POST",
        f"/api/v2/team/{WORKSPACE_ID}/time_entries/start",
        headers=TIME_HEADERS,
        json_body={},
        response_json={"data": running},
    )
    expect_current(mock_api, running)

    result = invoke(mock_api, ["time", "start", "--workspace-id", WORKSPACE_ID])

    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == f"Started {ENTRY_ID}"


def test_start_fails_closed_when_timer_exists(mock_api: MockClickUpAPI) -> None:
    expect_current(mock_api, entry_payload(running=True))

    result = invoke(
        mock_api,
        ["time", "start", "--workspace-id", WORKSPACE_ID],
        json_output=True,
    )

    assert result.exit_code == 1
    payload = json.loads(result.stderr)["error"]
    assert payload["type"] == "invalid_operation"
    assert payload["entry_id"] == ENTRY_ID


def test_start_disconnect_is_typed_unknown_outcome(mock_api: MockClickUpAPI) -> None:
    expect_current(mock_api, None)
    mock_api.expect(
        "POST",
        f"/api/v2/team/{WORKSPACE_ID}/time_entries/start",
        headers=TIME_HEADERS,
        json_body={},
        disconnect=True,
    )

    result = invoke(
        mock_api,
        ["time", "start", "--workspace-id", WORKSPACE_ID],
        json_output=True,
    )

    assert result.exit_code == 1
    payload = json.loads(result.stderr)["error"]
    assert payload["type"] == "outcome_unknown"
    assert payload["workspace_id"] == WORKSPACE_ID


def test_start_unusable_success_response_is_typed_unknown(mock_api: MockClickUpAPI) -> None:
    expect_current(mock_api, None)
    mock_api.expect(
        "POST",
        f"/api/v2/team/{WORKSPACE_ID}/time_entries/start",
        headers=TIME_HEADERS,
        json_body={},
        response_json={"data": {}},
    )

    result = invoke(
        mock_api,
        ["time", "start", "--workspace-id", WORKSPACE_ID],
        json_output=True,
    )

    assert result.exit_code == 1
    assert json.loads(result.stderr)["error"]["type"] == "outcome_unknown"


def test_start_known_id_verification_failure_is_distinct(mock_api: MockClickUpAPI) -> None:
    expect_current(mock_api, None)
    mock_api.expect(
        "POST",
        f"/api/v2/team/{WORKSPACE_ID}/time_entries/start",
        headers=TIME_HEADERS,
        json_body={"description": "Expected"},
        response_json={"data": entry_payload(running=True, description="Expected")},
    )
    expect_current(mock_api, entry_payload(running=True, description="Different"))

    result = invoke(
        mock_api,
        [
            "time",
            "start",
            "--workspace-id",
            WORKSPACE_ID,
            "--description",
            "Expected",
        ],
        json_output=True,
    )

    assert result.exit_code == 1
    payload = json.loads(result.stderr)["error"]
    assert payload["type"] == "created_but_unverified"
    assert payload["entry_id"] == ENTRY_ID


def test_stop_without_current_timer_is_idempotent_noop(mock_api: MockClickUpAPI) -> None:
    expect_current(mock_api, None)

    result = invoke(
        mock_api,
        ["time", "stop", "--workspace-id", WORKSPACE_ID],
        json_output=True,
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["result"] == {
        "entry": None,
        "entry_id": None,
        "stopped": False,
    }


def test_stop_exact_write_and_verified_readback(mock_api: MockClickUpAPI) -> None:
    expect_current(mock_api, entry_payload(running=True))
    stopped = entry_payload()
    mock_api.expect(
        "POST",
        f"/api/v2/team/{WORKSPACE_ID}/time_entries/stop",
        headers=TIME_HEADERS,
        response_json={"data": stopped},
    )
    expect_current(mock_api, None)

    result = invoke(
        mock_api,
        ["time", "stop", "--workspace-id", WORKSPACE_ID],
        json_output=True,
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)["result"]
    assert payload["stopped"] is True
    assert payload["entry_id"] == ENTRY_ID
    assert payload["entry"]["running"] is False


def test_stop_still_current_preserves_id_in_partial_error(mock_api: MockClickUpAPI) -> None:
    running = entry_payload(running=True)
    expect_current(mock_api, running)
    mock_api.expect(
        "POST",
        f"/api/v2/team/{WORKSPACE_ID}/time_entries/stop",
        headers=TIME_HEADERS,
        response_json={"data": entry_payload()},
    )
    expect_current(mock_api, running)

    result = invoke(
        mock_api,
        ["time", "stop", "--workspace-id", WORKSPACE_ID],
        json_output=True,
    )

    assert result.exit_code == 1
    payload = json.loads(result.stderr)["error"]
    assert payload["type"] == "verification_failed"
    assert payload["stopped_entry_id"] == ENTRY_ID


def test_stop_disconnect_preserves_id_in_unknown_outcome(mock_api: MockClickUpAPI) -> None:
    expect_current(mock_api, entry_payload(running=True))
    mock_api.expect(
        "POST",
        f"/api/v2/team/{WORKSPACE_ID}/time_entries/stop",
        headers=TIME_HEADERS,
        disconnect=True,
    )

    result = invoke(
        mock_api,
        ["time", "stop", "--workspace-id", WORKSPACE_ID],
        json_output=True,
    )

    assert result.exit_code == 1
    payload = json.loads(result.stderr)["error"]
    assert payload["type"] == "outcome_unknown"
    assert payload["stopped_entry_id"] == ENTRY_ID


def test_stop_malformed_response_still_verifies_and_reports_partial_id(
    mock_api: MockClickUpAPI,
) -> None:
    expect_current(mock_api, entry_payload(running=True))
    mock_api.expect(
        "POST",
        f"/api/v2/team/{WORKSPACE_ID}/time_entries/stop",
        headers=TIME_HEADERS,
        response_json={},
    )
    expect_current(mock_api, None)

    result = invoke(
        mock_api,
        ["time", "stop", "--workspace-id", WORKSPACE_ID],
        json_output=True,
    )

    assert result.exit_code == 1
    payload = json.loads(result.stderr)["error"]
    assert payload["type"] == "verification_failed"
    assert payload["stopped_entry_id"] == ENTRY_ID


def test_add_exact_body_readback_and_stable_output(mock_api: MockClickUpAPI) -> None:
    mock_api.expect(
        "POST",
        f"/api/v2/team/{WORKSPACE_ID}/time_entries",
        headers=TIME_HEADERS,
        json_body={
            "billable": True,
            "description": "Manual work",
            "duration": 5_400_000,
            "start": START_MS,
            "tid": TASK_ID,
        },
        response_json={"id": ENTRY_ID},
    )
    created = entry_payload(duration=5_400_000, description="Manual work", billable=True)
    expect_entry(mock_api, created)

    result = invoke(
        mock_api,
        [
            "time",
            "add",
            "--workspace-id",
            WORKSPACE_ID,
            "--task",
            TASK_ID,
            "--start",
            "2026-01-01T00:00:00Z",
            "--duration",
            "1h30m",
            "--description",
            "Manual work",
            "--billable",
        ],
        json_output=True,
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)["result"]
    assert payload["changed"] is True
    assert payload["entry"]["duration_ms"] == 5_400_000
    assert payload["entry"]["billable"] is True


def test_add_minimal_exact_body(mock_api: MockClickUpAPI) -> None:
    mock_api.expect(
        "POST",
        f"/api/v2/team/{WORKSPACE_ID}/time_entries",
        headers=TIME_HEADERS,
        json_body={"duration": 90_000, "start": START_MS},
        response_json={"data": {"id": ENTRY_ID}},
    )
    expect_entry(
        mock_api,
        entry_payload(task_id=None, duration=90_000, description=""),
    )

    result = invoke(
        mock_api,
        [
            "time",
            "add",
            "--workspace-id",
            WORKSPACE_ID,
            "--start",
            "2026-01-01T00:00:00Z",
            "--duration",
            "90s",
        ],
    )

    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == f"Added {ENTRY_ID}"


@pytest.mark.parametrize("response", [{}, {"duration": 90_000}, {"data": {}}])
def test_add_success_without_id_is_typed_unknown(
    mock_api: MockClickUpAPI, response: dict[str, Any]
) -> None:
    mock_api.expect(
        "POST",
        f"/api/v2/team/{WORKSPACE_ID}/time_entries",
        headers=TIME_HEADERS,
        json_body={"duration": 90_000, "start": START_MS},
        response_json=response,
    )

    result = invoke(
        mock_api,
        [
            "time",
            "add",
            "--workspace-id",
            WORKSPACE_ID,
            "--start",
            "2026-01-01T00:00:00Z",
            "--duration",
            "90s",
        ],
        json_output=True,
    )

    assert result.exit_code == 1
    assert json.loads(result.stderr)["error"]["type"] == "outcome_unknown"


def test_add_known_id_readback_failure_is_created_but_unverified(
    mock_api: MockClickUpAPI,
) -> None:
    mock_api.expect(
        "POST",
        f"/api/v2/team/{WORKSPACE_ID}/time_entries",
        headers=TIME_HEADERS,
        json_body={"duration": 90_000, "start": START_MS},
        response_json={"id": ENTRY_ID},
    )
    mock_api.expect(
        "GET",
        f"/api/v2/team/{WORKSPACE_ID}/time_entries/{ENTRY_ID}",
        headers=TIME_HEADERS,
        response_status=503,
        response_json={"err": "readback unavailable"},
    )

    result = invoke(
        mock_api,
        [
            "time",
            "add",
            "--workspace-id",
            WORKSPACE_ID,
            "--start",
            "2026-01-01T00:00:00Z",
            "--duration",
            "90s",
        ],
        json_output=True,
    )

    assert result.exit_code == 1
    payload = json.loads(result.stderr)["error"]
    assert payload["type"] == "created_but_unverified"
    assert payload["entry_id"] == ENTRY_ID


def test_add_field_mismatch_is_created_but_unverified(mock_api: MockClickUpAPI) -> None:
    mock_api.expect(
        "POST",
        f"/api/v2/team/{WORKSPACE_ID}/time_entries",
        headers=TIME_HEADERS,
        json_body={"duration": 90_000, "start": START_MS},
        response_json={"id": ENTRY_ID},
    )
    expect_entry(mock_api, entry_payload(duration=60_000))

    result = invoke(
        mock_api,
        [
            "time",
            "add",
            "--workspace-id",
            WORKSPACE_ID,
            "--start",
            "2026-01-01T00:00:00Z",
            "--duration",
            "90s",
        ],
        json_output=True,
    )

    assert result.exit_code == 1
    assert json.loads(result.stderr)["error"]["type"] == "created_but_unverified"


@pytest.mark.parametrize(
    "args",
    [
        ["time", "update", ENTRY_ID, "--workspace-id", WORKSPACE_ID],
        [
            "time",
            "update",
            ENTRY_ID,
            "--workspace-id",
            WORKSPACE_ID,
            "--billable",
            "--non-billable",
        ],
    ],
)
def test_update_rejects_fieldless_and_conflicting_input_before_network(
    mock_api: MockClickUpAPI, args: list[str]
) -> None:
    result = invoke(mock_api, args)

    assert result.exit_code == 1
    assert mock_api.state.requests == []


def test_update_noop_reads_once_and_skips_write(mock_api: MockClickUpAPI) -> None:
    expect_entry(mock_api, entry_payload())

    result = invoke(
        mock_api,
        [
            "time",
            "update",
            ENTRY_ID,
            "--workspace-id",
            WORKSPACE_ID,
            "--description",
            "Worked safely",
        ],
        json_output=True,
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["result"]["changed"] is False


def test_update_description_sends_required_empty_tags_and_verifies(
    mock_api: MockClickUpAPI,
) -> None:
    expect_entry(mock_api, entry_payload())
    mock_api.expect(
        "PUT",
        f"/api/v2/team/{WORKSPACE_ID}/time_entries/{ENTRY_ID}",
        headers=TIME_HEADERS,
        json_body={"description": "Updated", "tags": []},
        response_json={},
    )
    expect_entry(mock_api, entry_payload(description="Updated"))

    result = invoke(
        mock_api,
        [
            "time",
            "update",
            ENTRY_ID,
            "--workspace-id",
            WORKSPACE_ID,
            "--description",
            "Updated",
        ],
    )

    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == f"Updated {ENTRY_ID}"


def test_update_all_fields_uses_minimal_valid_paired_start_end_body(
    mock_api: MockClickUpAPI,
) -> None:
    tags: list[Any] = [{"creator": 7, "name": "focus", "tag_bg": "#000000", "tag_fg": "#ffffff"}]
    expect_entry(mock_api, entry_payload(tags=tags))
    new_start = START_MS + 7_200_000
    new_duration = 5_400_000
    mock_api.expect(
        "PUT",
        f"/api/v2/team/{WORKSPACE_ID}/time_entries/{ENTRY_ID}",
        headers=TIME_HEADERS,
        json_body={
            "billable": True,
            "description": "Updated all",
            "end": new_start + new_duration,
            "start": new_start,
            "tags": [{"name": "focus", "tag_bg": "#000000", "tag_fg": "#ffffff"}],
            "tid": "task_999",
        },
        response_json={},
    )
    expect_entry(
        mock_api,
        entry_payload(
            task_id="task_999",
            start=new_start,
            duration=new_duration,
            description="Updated all",
            billable=True,
            tags=tags,
        ),
    )

    result = invoke(
        mock_api,
        [
            "time",
            "update",
            ENTRY_ID,
            "--workspace-id",
            WORKSPACE_ID,
            "--description",
            "Updated all",
            "--task",
            "task_999",
            "--start",
            "2026-01-01T02:00:00Z",
            "--duration",
            "1h30m",
            "--billable",
        ],
        json_output=True,
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)["result"]
    assert payload["changed"] is True
    assert payload["entry"]["tags"] == ["focus"]


def test_update_duration_only_uses_documented_duration_field(
    mock_api: MockClickUpAPI,
) -> None:
    expect_entry(mock_api, entry_payload())
    mock_api.expect(
        "PUT",
        f"/api/v2/team/{WORKSPACE_ID}/time_entries/{ENTRY_ID}",
        headers=TIME_HEADERS,
        json_body={"duration": 90_000, "tags": []},
        response_json={},
    )
    expect_entry(mock_api, entry_payload(duration=90_000))

    result = invoke(
        mock_api,
        [
            "time",
            "update",
            ENTRY_ID,
            "--workspace-id",
            WORKSPACE_ID,
            "--duration",
            "90s",
        ],
    )

    assert result.exit_code == 0, result.output


def test_update_rejects_unrepresentable_existing_tags_before_put(
    mock_api: MockClickUpAPI,
) -> None:
    expect_entry(mock_api, entry_payload(tags=["focus"]))

    result = invoke(
        mock_api,
        [
            "time",
            "update",
            ENTRY_ID,
            "--workspace-id",
            WORKSPACE_ID,
            "--description",
            "Updated",
        ],
    )

    assert result.exit_code == 1
    assert "omitted tag colors" in result.stderr


def test_update_rejects_timing_change_on_running_entry(mock_api: MockClickUpAPI) -> None:
    expect_entry(mock_api, entry_payload(running=True))

    result = invoke(
        mock_api,
        [
            "time",
            "update",
            ENTRY_ID,
            "--workspace-id",
            WORKSPACE_ID,
            "--duration",
            "90s",
        ],
    )

    assert result.exit_code == 1
    assert "running entry" in result.stderr


def test_update_disconnect_is_typed_unknown_with_entry_id(mock_api: MockClickUpAPI) -> None:
    expect_entry(mock_api, entry_payload())
    mock_api.expect(
        "PUT",
        f"/api/v2/team/{WORKSPACE_ID}/time_entries/{ENTRY_ID}",
        headers=TIME_HEADERS,
        json_body={"description": "Updated", "tags": []},
        disconnect=True,
    )

    result = invoke(
        mock_api,
        [
            "time",
            "update",
            ENTRY_ID,
            "--workspace-id",
            WORKSPACE_ID,
            "--description",
            "Updated",
        ],
        json_output=True,
    )

    assert result.exit_code == 1
    payload = json.loads(result.stderr)["error"]
    assert payload["type"] == "outcome_unknown"
    assert payload["entry_id"] == ENTRY_ID


def test_update_verification_mismatch_fails(mock_api: MockClickUpAPI) -> None:
    expect_entry(mock_api, entry_payload())
    mock_api.expect(
        "PUT",
        f"/api/v2/team/{WORKSPACE_ID}/time_entries/{ENTRY_ID}",
        headers=TIME_HEADERS,
        json_body={"description": "Updated", "tags": []},
        response_json={},
    )
    expect_entry(mock_api, entry_payload())

    result = invoke(
        mock_api,
        [
            "time",
            "update",
            ENTRY_ID,
            "--workspace-id",
            WORKSPACE_ID,
            "--description",
            "Updated",
        ],
        json_output=True,
    )

    assert result.exit_code == 1
    assert json.loads(result.stderr)["error"]["type"] == "verification_failed"


def test_delete_requires_confirmation_before_any_network(mock_api: MockClickUpAPI) -> None:
    result = invoke(
        mock_api,
        ["time", "delete", ENTRY_ID, "--workspace-id", WORKSPACE_ID],
        json_output=True,
    )

    assert result.exit_code == 1
    assert json.loads(result.stderr)["error"]["type"] == "confirmation_required"
    assert mock_api.state.requests == []


def test_delete_exact_localhost_contract(mock_api: MockClickUpAPI) -> None:
    mock_api.expect(
        "DELETE",
        f"/api/v2/team/{WORKSPACE_ID}/time_entries/{ENTRY_ID}",
        headers=TIME_HEADERS,
        response_status=204,
        response_json=None,
    )

    result = invoke(
        mock_api,
        ["time", "delete", ENTRY_ID, "--workspace-id", WORKSPACE_ID, "--yes"],
        json_output=True,
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "ok": True,
        "result": {"deleted": True, "entry_id": ENTRY_ID},
    }


def test_time_errors_redact_authorization_value(mock_api: MockClickUpAPI) -> None:
    mock_api.expect(
        "GET",
        f"/api/v2/team/{WORKSPACE_ID}/time_entries/current",
        headers=TIME_HEADERS,
        response_status=401,
        response_json={"err": f"Rejected {AUTH_VALUE}"},
    )

    result = invoke(
        mock_api,
        ["time", "current", "--workspace-id", WORKSPACE_ID],
        json_output=True,
    )

    assert result.exit_code == 1
    combined = result.stdout + result.stderr
    assert AUTH_VALUE not in combined
    assert "[REDACTED]" in combined


def test_time_commands_are_discoverable_without_credentials() -> None:
    help_result = runner.invoke(app, ["time", "--help"], env={"CLICKUP_API_TOKEN": ""})

    assert help_result.exit_code == 0, help_result.output
    for command in ("current", "list", "start", "stop", "add", "update", "delete"):
        assert command in help_result.stdout
