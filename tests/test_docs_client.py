from __future__ import annotations

import pytest

from clickup_cli.client import ClickUpClient
from clickup_cli.errors import InvalidOperationError
from clickup_cli.types import JsonObject
from tests.conftest import MockClickUpAPI
from tests.test_docs_contract import AUTH


@pytest.mark.parametrize(
    "body",
    [
        {"name": "New", "visibility": [], "create_page": False},
        {
            "name": "New",
            "visibility": "PRIVATE",
            "create_page": False,
            "parent": {"id": "987", "type": 99},
        },
        {
            "name": "New",
            "visibility": "PRIVATE",
            "create_page": False,
            "parent": {"id": "../987", "type": 6},
        },
    ],
)
def test_direct_doc_client_rejects_unsafe_create_before_http(
    mock_api: MockClickUpAPI, body: JsonObject
) -> None:
    with ClickUpClient(token=AUTH, base_url=mock_api.base_url) as client:
        with pytest.raises(InvalidOperationError):
            client.create_doc("123", body)


@pytest.mark.parametrize(
    "parameters",
    [
        [("query", "imaginary")],
        [("limit", 1)],
        [("creator", "abc")],
        [("limit", 10), ("limit", 20)],
        [("parent_type", 99)],
    ],
)
def test_direct_list_client_rejects_undocumented_or_invalid_query(
    mock_api: MockClickUpAPI, parameters: list[tuple[str, str | int]]
) -> None:
    with ClickUpClient(token=AUTH, base_url=mock_api.base_url) as client:
        with pytest.raises(InvalidOperationError):
            client.list_docs("123", parameters)
