from __future__ import annotations

import json

import pytest

from clickup_cli.cli import app
from tests.conftest import MockClickUpAPI
from tests.test_docs_contract import BASE, READ, doc, invoke, page, result, runner


@pytest.mark.parametrize(
    "args",
    [
        ["show", "d-1"],
        ["show", "d-1", "--workspace-id", "abc"],
        ["show", "https://app.clickup.com/123/docs/d-1", "--workspace-id", "456"],
        ["show", "https://user@app.clickup.com/123/docs/d-1"],
        ["show", "https://app.clickup.com.evil.invalid/123/docs/d-1"],
        ["show", "http://app.clickup.com/123/docs/d-1"],
        ["show", "https://app.clickup.com/123/docs/%2e%2e"],
        ["show", "https://app.clickup.com/123/docs/d-1?page-id=p-1"],
        ["page", "show", "p-1", "--workspace-id", "123"],
        [
            "page",
            "show",
            "https://app.clickup.com/123/v/dc/d-1/p-1",
            "--doc",
            "d-2",
            "--workspace-id",
            "123",
        ],
        ["page", "show", "p-1", "--doc", "d-1", "--workspace-id", "123", "--format", "html"],
        ["search", "x"],
        ["pages", "d-1", "--workspace-id", "123", "--limit", "0"],
    ],
)
def test_refs_and_bounds_fail_before_http(mock_api: MockClickUpAPI, args: list[str]) -> None:
    output = invoke(mock_api, args)
    assert output.exit_code != 0
    assert not mock_api.state.requests


@pytest.mark.parametrize("mutate", ["duplicate", "identity", "parent", "cycle"])
def test_hierarchy_fails_closed(mock_api: MockClickUpAPI, mutate: str) -> None:
    child = page("p-2", parent_page_id="p-1")
    if mutate == "duplicate":
        child["id"] = "p-1"
    elif mutate == "identity":
        child["workspace_id"] = 456
    elif mutate == "parent":
        child["parent_page_id"] = "other"
    else:
        child["pages"] = [page("p-1", parent_page_id="p-2")]
    mock_api.expect(
        "GET",
        BASE + "/d-1/page_listing?max_page_depth=-1",
        headers=READ,
        response_json=[page(pages=[child])],
    )
    assert invoke(mock_api, ["pages", "d-1", "--workspace-id", "123"]).exit_code == 1


@pytest.mark.parametrize("code", [403, 404, 429])
def test_inaccessible_search_is_error_not_no_match(mock_api: MockClickUpAPI, code: int) -> None:
    for _ in range(3 if code == 429 else 1):
        mock_api.expect(
            "GET",
            BASE + "/d-1/pages?max_page_depth=-1&content_format=text%2Fmd",
            headers=READ,
            response_status=code,
            response_json={"err": "unavailable"},
        )
    output = invoke(
        mock_api, ["search", "missing", "--doc", "d-1", "--workspace-id", "123", "--content"]
    )
    assert output.exit_code == 1
    assert json.loads(output.stderr)["ok"] is False


def test_title_search_reports_partial_scope(mock_api: MockClickUpAPI) -> None:
    mock_api.expect(
        "GET",
        BASE + "/d-1/page_listing?max_page_depth=-1",
        headers=READ,
        response_json=[page(name="Straße"), page("p-2", name="Other")],
    )
    value = result(
        invoke(
            mock_api,
            [
                "search",
                "STRASSE",
                "--doc",
                "d-1",
                "--workspace-id",
                "123",
                "--max-pages",
                "1",
                "--limit",
                "1",
            ],
        )
    )
    assert value["matches"][0]["page_id"] == "p-1"
    assert value["complete"] is False and value["scanned_pages"] == 1


def test_repeated_cursor_fails_without_exposing_cursor(mock_api: MockClickUpAPI) -> None:
    mock_api.expect(
        "GET",
        BASE + "?deleted=false&archived=false&limit=100",
        headers=READ,
        response_json={"docs": [doc()], "next_cursor": "privatecursor"},
    )
    mock_api.expect(
        "GET",
        BASE + "?deleted=false&archived=false&limit=100&cursor=privatecursor",
        headers=READ,
        response_json={"docs": [doc(id="d-2")], "next_cursor": "privatecursor"},
    )
    output = invoke(mock_api, ["list", "--workspace-id", "123", "--all"])
    assert output.exit_code == 1 and "privatecursor" not in output.output


def test_list_documented_filters_are_serialized(mock_api: MockClickUpAPI) -> None:
    mock_api.expect(
        "GET",
        BASE + "?deleted=true&archived=true&creator=42&parent_id=987&parent_type=6&limit=10",
        headers=READ,
        response_json={"docs": [], "next_cursor": None},
    )
    value = result(
        invoke(
            mock_api,
            [
                "list",
                "--workspace-id",
                "123",
                "--deleted",
                "--archived",
                "--creator",
                "42",
                "--parent-id",
                "987",
                "--parent-type",
                "LIST",
                "--limit",
                "1",
            ],
        )
    )
    assert value["complete"] is True


def test_help_is_discoverable_without_credentials() -> None:
    assert "doc" in runner.invoke(app, ["--help"]).stdout
    assert runner.invoke(app, ["doc", "page", "--help"]).exit_code == 0


def test_collection_output_has_byte_ceiling_even_with_all(mock_api: MockClickUpAPI) -> None:
    pages = [page(f"p-{i}", name="Long " + "x" * 4000) for i in range(100)]
    mock_api.expect(
        "GET", BASE + "/d-1/page_listing?max_page_depth=-1", headers=READ, response_json=pages
    )
    output = invoke(
        mock_api, ["pages", "d-1", "--workspace-id", "123", "--all", "--tree", "--limit", "1000"]
    )
    value = result(output)
    assert len(output.stdout.encode()) <= 65536
    assert value["complete"] is False and value["has_more"] is True
    assert value["returned_count"] == len(value["pages"]) < value["total"] == 100


def test_plain_page_display_is_readable_not_json_escaped(mock_api: MockClickUpAPI) -> None:
    mock_api.expect(
        "GET",
        BASE + "/d-1/pages/p-1?content_format=text%2Fplain",
        headers=READ,
        response_json=page(content="First\nSecond"),
    )
    mock_api.expect(
        "GET", BASE + "/d-1/page_listing?max_page_depth=-1", headers=READ, response_json=[page()]
    )
    output = invoke(
        mock_api,
        ["page", "show", "p-1", "--doc", "d-1", "--workspace-id", "123", "--format", "plain"],
        machine=False,
    )
    assert output.exit_code == 0
    assert "First\nSecond" in output.stdout
    assert "https://app.clickup.com/123/v/dc/d-1/p-1" in output.stdout


def test_workspace_search_is_explicit_bounded_and_local(mock_api: MockClickUpAPI) -> None:
    mock_api.expect(
        "GET",
        BASE + "?deleted=false&archived=false&limit=10",
        headers=READ,
        response_json={"docs": [doc(), doc(id="d-2")], "next_cursor": None},
    )
    mock_api.expect(
        "GET",
        BASE + "/d-1/page_listing?max_page_depth=-1",
        headers=READ,
        response_json=[page(name="STRASSE")],
    )
    mock_api.expect(
        "GET",
        BASE + "/d-2/page_listing?max_page_depth=-1",
        headers=READ,
        response_json=[page("p-2", doc_id="d-2", name="Not a match")],
    )
    value = result(
        invoke(mock_api, ["search", "Straße", "--workspace-id", "123", "--max-docs", "2"])
    )
    assert value["scope"] == "workspace" and value["complete"] is True
    assert value["scanned_docs"] == 2 and value["scanned_pages"] == 2
    assert value["matches"][0]["doc_id"] == "d-1"


def test_unicode_long_line_continuation_bounds_actual_json_bytes(mock_api: MockClickUpAPI) -> None:
    content = "😀" * 20000
    mock_api.expect(
        "GET",
        BASE + "/d-1/pages/p-1?content_format=text%2Fmd",
        headers=READ,
        response_json=page(content=content),
    )
    mock_api.expect(
        "GET", BASE + "/d-1/page_listing?max_page_depth=-1", headers=READ, response_json=[page()]
    )
    output = invoke(
        mock_api, ["page", "show", "p-1", "--doc", "d-1", "--workspace-id", "123", "--limit", "1"]
    )
    value = result(output)["page"]
    assert len(output.stdout.encode()) <= 65536
    assert value["has_more"] is True and value["next_offset"] == 0
    assert value["next_column"] == len(value["content"])


@pytest.mark.parametrize("field", ["name", "parent", "date_created", "public"])
def test_doc_malformed_metadata_never_floods_console(mock_api: MockClickUpAPI, field: str) -> None:
    mock_api.expect("GET", BASE + "/d-1", headers=READ, response_json=doc(**{field: "x" * 1000000}))
    output = invoke(mock_api, ["show", "d-1", "--workspace-id", "123"])
    assert output.exit_code == 1
    assert len(output.output) < 1000


def test_all_pages_expands_default_but_respects_explicit_limit(mock_api: MockClickUpAPI) -> None:
    pages = [page(f"p-{i}", name=f"Page {i}") for i in range(100)]
    for _ in range(2):
        mock_api.expect(
            "GET", BASE + "/d-1/page_listing?max_page_depth=-1", headers=READ, response_json=pages
        )
    full = result(invoke(mock_api, ["pages", "d-1", "--workspace-id", "123", "--all"]))
    tiny = result(
        invoke(mock_api, ["pages", "d-1", "--workspace-id", "123", "--all", "--limit", "1"])
    )
    assert full["returned_count"] == 100 and full["complete"] is True
    assert tiny["returned_count"] == 1 and tiny["complete"] is False


@pytest.mark.parametrize("command", ["list", "search"])
def test_all_defaults_expand_other_scoped_commands(mock_api: MockClickUpAPI, command: str) -> None:
    if command == "list":
        mock_api.expect(
            "GET",
            BASE + "?deleted=false&archived=false&limit=100",
            headers=READ,
            response_json={"docs": [doc(id=f"d-{i}") for i in range(100)], "next_cursor": None},
        )
        args = ["list", "--workspace-id", "123", "--all"]
    else:
        mock_api.expect(
            "GET",
            BASE + "/d-1/page_listing?max_page_depth=-1",
            headers=READ,
            response_json=[page(f"p-{i}", name="Match") for i in range(100)],
        )
        args = ["search", "Match", "--doc", "d-1", "--workspace-id", "123", "--all"]
    value = result(invoke(mock_api, args))
    assert value["returned_count"] == 100 and value["complete"] is True


def test_malformed_url_keeps_stable_error_envelope_before_http(mock_api: MockClickUpAPI) -> None:
    output = invoke(mock_api, ["show", "https://[invalid/123/docs/d-1"])
    assert output.exit_code == 1
    assert json.loads(output.stderr)["error"]["type"] == "invalid_operation"


def test_page_id_conflict_inside_doc_context_is_rejected_before_http(
    mock_api: MockClickUpAPI,
) -> None:
    output = invoke(
        mock_api, ["page", "show", "p-2", "--doc", "https://app.clickup.com/123/v/dc/d-1/p-1"]
    )
    assert output.exit_code == 1
    assert not mock_api.state.requests
