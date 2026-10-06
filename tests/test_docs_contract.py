from __future__ import annotations

import json
from typing import Any

from typer.testing import CliRunner, Result

from clickup_cli.cli import app
from tests.conftest import MockClickUpAPI

AUTH = "docs-local-auth"
READ = {"Accept": "application/json", "Authorization": AUTH}
WRITE = {**READ, "Content-Type": "application/json"}
BASE = "/api/v3/workspaces/123/docs"
runner = CliRunner()


def invoke(api: MockClickUpAPI, args: list[str], *, machine: bool = True) -> Result:
    return runner.invoke(
        app,
        ["--base-url", api.base_url, *(["--json"] if machine else []), "doc", *args],
        env={"CLICKUP_API_TOKEN": AUTH},
    )


def doc(**fields: Any) -> dict[str, Any]:
    return {"id": "d-1", "workspace_id": 123, "name": "Handbook", "public": False, **fields}


def page(pid: str = "p-1", **fields: Any) -> dict[str, Any]:
    return {
        "id": pid,
        "workspace_id": 123,
        "doc_id": "d-1",
        "name": "Overview",
        "parent_page_id": None,
        "content": "Hello\nWorld",
        **fields,
    }


def result(output: Result) -> dict[str, Any]:
    assert output.exit_code == 0, output.output
    return json.loads(output.stdout)["result"]  # type: ignore[no-any-return]


def test_show_doc_url_uses_v3_and_literal_context(mock_api: MockClickUpAPI) -> None:
    mock_api.expect("GET", BASE + "/d-1", headers=READ, response_json=doc())
    value = result(invoke(mock_api, ["show", "https://app.clickup.com/123/docs/d-1"]))
    assert value["doc"]["doc_id"] == "d-1"
    assert value["doc"]["workspace_id"] == "123"
    assert value["doc"]["url"] == "https://app.clickup.com/123/docs/d-1"
    assert value["doc"]["date_updated"] is None


def test_doc_list_follows_opaque_cursor_with_bounded_output(mock_api: MockClickUpAPI) -> None:
    mock_api.expect(
        "GET",
        BASE + "?deleted=false&archived=false&limit=10",
        headers=READ,
        response_json={"docs": [doc()], "next_cursor": "a+/=opaque"},
    )
    mock_api.expect(
        "GET",
        BASE + "?deleted=false&archived=false&limit=10&cursor=a%2B%2F%3Dopaque",
        headers=READ,
        response_json={"docs": [doc(id="d-2")], "next_cursor": None},
    )
    value = result(invoke(mock_api, ["list", "--workspace-id", "123", "--all", "--limit", "2"]))
    assert [d["doc_id"] for d in value["docs"]] == ["d-1", "d-2"]
    assert value["complete"] is True
    assert value["total"] == 2


def test_pages_tree_counts_every_descendant_and_bounds_output(mock_api: MockClickUpAPI) -> None:
    tree = [
        page(
            pages=[
                page(
                    "p-2",
                    name="Child",
                    parent_page_id="p-1",
                    pages=[page("p-3", name="Grandchild", parent_page_id="p-2")],
                )
            ]
        )
    ]
    mock_api.expect(
        "GET", BASE + "/d-1/page_listing?max_page_depth=-1", headers=READ, response_json=tree
    )
    value = result(
        invoke(mock_api, ["pages", "d-1", "--workspace-id", "123", "--tree", "--limit", "2"])
    )
    assert value["total"] == 3
    assert value["returned_count"] == 2
    assert value["has_more"] is True and value["complete"] is False
    assert value["pages"][1]["breadcrumb"] == ["Overview", "Child"]
    assert value["pages"][1]["parent_page_id"] == "p-1"
    assert "pages" not in value["pages"][0]


def test_page_show_omits_inline_images_and_has_full_revision_and_continuation(
    mock_api: MockClickUpAPI,
) -> None:
    import hashlib

    content = (
        "First\n![blob](data:image/png;base64,"
        + "a" * 2000000
        + ")\n![link](https://example.invalid/image.png)\nLast"
    )
    mock_api.expect(
        "GET",
        BASE + "/d-1/pages/p-1?content_format=text%2Fmd",
        headers=READ,
        response_json=page(content=content),
    )
    mock_api.expect(
        "GET", BASE + "/d-1/page_listing?max_page_depth=-1", headers=READ, response_json=[page()]
    )
    value = result(
        invoke(
            mock_api, ["page", "show", "https://app.clickup.com/123/v/dc/d-1/p-1", "--limit", "3"]
        )
    )
    p = value["page"]
    assert p["sha256"] == hashlib.sha256(content.encode()).hexdigest()
    assert p["has_more"] is True and p["next_offset"] == 3
    assert p["total"] == 4 and p["returned_count"] == 3
    assert p["omitted_data_images"] == 1
    assert "[inline data image omitted]" in p["content"]
    assert "https://example.invalid/image.png" in p["content"]
    assert len(value.__str__()) < 5000


def test_search_is_local_unicode_content_search_with_line_source(mock_api: MockClickUpAPI) -> None:
    mock_api.expect(
        "GET",
        BASE + "/d-1/pages?max_page_depth=-1&content_format=text%2Fmd",
        headers=READ,
        response_json=[page(content="Intro\nStraße procedure\nEnd")],
    )
    value = result(
        invoke(
            mock_api, ["search", "STRASSE", "--doc", "d-1", "--workspace-id", "123", "--content"]
        )
    )
    assert value["search_mode"] == "local_content"
    assert value["complete"] is True
    assert value["matches"][0]["line_start"] == 2
    assert value["matches"][0]["url"] == "https://app.clickup.com/123/v/dc/d-1/p-1"
    assert "Straße" in value["matches"][0]["snippet"]
