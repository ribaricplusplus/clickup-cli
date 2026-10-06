from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import MockClickUpAPI
from tests.test_docs_contract import BASE, READ, WRITE, doc, invoke, page, result


def test_empty_cursor_is_a_verified_end_of_docs_listing(mock_api: MockClickUpAPI) -> None:
    mock_api.expect(
        "GET",
        BASE + "?deleted=false&archived=false&limit=100",
        headers=READ,
        response_json={"docs": [doc()], "next_cursor": ""},
    )
    observed = result(invoke(mock_api, ["list", "--workspace-id", "123", "--all"]))
    assert observed["complete"] is True
    assert observed["has_more"] is False
    assert observed["total"] == observed["returned_count"] == 1


def test_content_search_uses_one_recursive_content_request(mock_api: MockClickUpAPI) -> None:
    mock_api.expect(
        "GET",
        BASE + "/d-1/pages?max_page_depth=-1&content_format=text%2Fmd",
        headers=READ,
        response_json=[
            page(content="No match", pages=[page("p-2", parent_page_id="p-1", content="Straße")]),
            *[page(f"p-{i}", content="No match") for i in range(3, 102)],
        ],
    )
    observed = result(
        invoke(
            mock_api,
            ["search", "STRASSE", "--doc", "d-1", "--workspace-id", "123", "--content", "--all"],
        )
    )
    assert len(mock_api.state.requests) == 1
    assert observed["complete"] is True
    assert observed["scanned_pages"] == observed["total_pages"] == 101
    assert observed["returned_count"] == 1
    assert observed["matches"][0]["page_id"] == "p-2"
    assert observed["matches"][0]["breadcrumb"] == ["Overview", "Overview"]


@pytest.mark.parametrize("mode", ["append", "prepend"])
@pytest.mark.parametrize("separator", ["\n", "\n\n"])
def test_native_content_edits_verify_provider_paragraph_separator(
    mock_api: MockClickUpAPI, tmp_path: Path, mode: str, separator: str
) -> None:
    source = tmp_path / "addition.md"
    source.write_text("\nNew paragraph.\n")
    mock_api.expect(
        "GET",
        BASE + "/d-1/pages/p-1?content_format=text%2Fmd",
        headers=READ,
        response_json=page(content="Old paragraph."),
    )
    mock_api.expect(
        "PUT",
        BASE + "/d-1/pages/p-1",
        headers=WRITE,
        json_body={
            "content": "\nNew paragraph.\n",
            "content_format": "text/md",
            "content_edit_mode": mode,
        },
    )
    actual = (
        "Old paragraph." + separator + "New paragraph."
        if mode == "append"
        else "New paragraph." + separator + "Old paragraph."
    )
    mock_api.expect(
        "GET",
        BASE + "/d-1/pages/p-1?content_format=text%2Fmd",
        headers=READ,
        response_json=page(content=actual),
    )
    observed = result(
        invoke(
            mock_api,
            [
                "page",
                mode,
                "p-1",
                "--doc",
                "d-1",
                "--workspace-id",
                "123",
                "--content-file",
                str(source),
            ],
        )
    )
    assert observed["verified"] is True and observed["mode"] == mode
