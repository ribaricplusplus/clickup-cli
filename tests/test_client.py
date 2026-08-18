from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

from clickup_cli.client import ClickUpClient
from clickup_cli.errors import InvalidOperationError
from clickup_cli.errors import ReferenceError as TaskReferenceError
from tests.conftest import MockClickUpAPI


def test_retry_after_is_respected_but_bounded(mock_api: MockClickUpAPI) -> None:
    headers = {"Accept": "application/json", "Authorization": "client-auth"}
    mock_api.expect(
        "GET",
        "/api/v2/user",
        headers=headers,
        response_status=429,
        response_json={"err": "rate limited"},
        response_headers={"Retry-After": "7"},
    )
    mock_api.expect(
        "GET",
        "/api/v2/user",
        headers=headers,
        response_json={"user": {"id": 1}},
    )
    delays: list[float] = []

    with ClickUpClient(
        token="client-auth",
        base_url=mock_api.base_url,
        max_retry_after=0.25,
        sleep=delays.append,
    ) as client:
        assert client.get_user() == {"user": {"id": 1}}

    assert delays == [0.25]


def test_clickup_rate_limit_reset_timestamp_is_respected_but_bounded(
    mock_api: MockClickUpAPI,
) -> None:
    headers = {"Accept": "application/json", "Authorization": "client-auth"}
    mock_api.expect(
        "GET",
        "/api/v2/user",
        headers=headers,
        response_status=429,
        response_json={"err": "rate limited"},
        response_headers={"X-RateLimit-Reset": "104"},
    )
    mock_api.expect(
        "GET",
        "/api/v2/user",
        headers=headers,
        response_json={"user": {"id": 1}},
    )
    delays: list[float] = []

    with ClickUpClient(
        token="client-auth",
        base_url=mock_api.base_url,
        max_retry_after=0.25,
        sleep=delays.append,
        clock=lambda: 100.0,
    ) as client:
        assert client.get_user() == {"user": {"id": 1}}

    assert delays == [0.25]


def test_client_ignores_proxy_environment_for_raw_authorization(
    monkeypatch: pytest.MonkeyPatch,
    mock_api: MockClickUpAPI,
) -> None:
    proxy = MockClickUpAPI()
    try:
        proxy_origin = proxy.base_url.removesuffix("/api")
        for variable in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy"):
            monkeypatch.setenv(variable, proxy_origin)
        monkeypatch.delenv("NO_PROXY", raising=False)
        monkeypatch.delenv("no_proxy", raising=False)

        headers = {"Accept": "application/json", "Authorization": "client-auth"}
        mock_api.expect(
            "GET",
            "/api/v2/user",
            headers=headers,
            response_json={"user": {"id": 1}},
        )

        with ClickUpClient(token="client-auth", base_url=mock_api.base_url) as client:
            assert client.get_user() == {"user": {"id": 1}}

        assert proxy.state.requests == []
        proxy.assert_done()
    finally:
        proxy.close()


def test_client_rejects_unsafe_path_ids_before_network(mock_api: MockClickUpAPI) -> None:
    with ClickUpClient(token="client-auth", base_url=mock_api.base_url) as client:
        operations: tuple[Callable[[], object], ...] = (
            lambda: client.get_task("../user"),
            lambda: client.get_list("../task"),
            lambda: client.update_task_status("../user", "done"),
            lambda: client.update_task("../user", {"archived": True}),
            lambda: client.update_task_tag("../user", "focus", add=True),
            lambda: client.upload_task_attachment(
                "../user", Path("unused"), upload_name="unused.txt"
            ),
            lambda: client.get_task_comments("../user"),
            lambda: client.create_task_comment("../user", "comment"),
            lambda: client.update_task_due_date("../user", 1, due_date_time=True),
            lambda: client.update_task_assignees("../user", add=[42], remove=[]),
            lambda: client.create_task("../task", "Synthetic task"),
            lambda: client.delete_task("../user"),
        )
        for operation in operations:
            with pytest.raises(TaskReferenceError):
                operation()

    assert mock_api.state.requests == []


def test_client_rejects_invalid_operation_values_before_network(
    mock_api: MockClickUpAPI,
) -> None:
    with ClickUpClient(token="client-auth", base_url=mock_api.base_url) as client:
        operations: tuple[Callable[[], object], ...] = (
            lambda: client.create_task_comment("task_123", "   "),
            lambda: client.update_task_due_date("task_123", cast(Any, -1.5), due_date_time=True),
            lambda: client.update_task_due_date("task_123", 1, due_date_time=None),
            lambda: client.update_task_assignees("task_123", add=[cast(Any, "42")], remove=[]),
            lambda: client.update_task_assignees("task_123", add=[42], remove=[42]),
            lambda: client.update_task("task_123", {}),
            lambda: client.update_task("task_123", {"priority": 5}),
            lambda: client.update_task("task_123", {"start_date_time": True}),
            lambda: client.update_task_tag("task_123", "bad\ntag", add=True),
            lambda: client.upload_task_attachment(
                "task_123", Path("unused"), upload_name="../unsafe.txt"
            ),
        )
        for operation in operations:
            with pytest.raises(InvalidOperationError):
                operation()

    assert mock_api.state.requests == []
