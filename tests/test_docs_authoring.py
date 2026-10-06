from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import Result

from tests.conftest import MockClickUpAPI
from tests.test_docs_contract import BASE, READ, WRITE, doc, invoke, page, result


def error(output: Result) -> dict[str, object]:
    assert output.exit_code == 1, output.output
    return json.loads(output.stderr)["error"]  # type: ignore[no-any-return]


def test_create_doc_serializes_private_no_blank_page_and_verifies(mock_api: MockClickUpAPI) -> None:
    mock_api.expect(
        "POST",
        BASE,
        headers=WRITE,
        json_body={"name": "Handbook", "visibility": "PRIVATE", "create_page": False},
        response_json=doc(),
    )
    mock_api.expect("GET", BASE + "/d-1", headers=READ, response_json=doc())
    value = result(invoke(mock_api, ["create", "Handbook", "--workspace-id", "123"]))
    assert value["doc"]["doc_id"] == "d-1"
    assert value["verified"] is True
    assert value["requested_visibility"] == "private"


def test_create_child_page_identifies_parent_and_verifies_content(
    mock_api: MockClickUpAPI, tmp_path: Path
) -> None:
    content_file = tmp_path / "body.md"
    content_file.write_text("Child content\n", encoding="utf-8")
    mock_api.expect("GET", BASE + "/d-1", headers=READ, response_json=doc())
    mock_api.expect(
        "GET", BASE + "/d-1/pages/p-1?content_format=text%2Fmd", headers=READ, response_json=page()
    )
    mock_api.expect(
        "POST",
        BASE + "/d-1/pages",
        headers=WRITE,
        json_body={
            "name": "Child",
            "content": "Child content\n",
            "content_format": "text/md",
            "parent_page_id": "p-1",
            "sub_title": "Subtitle",
        },
        response_json={"id": "p-2"},
    )
    mock_api.expect(
        "GET",
        BASE + "/d-1/pages/p-2?content_format=text%2Fmd",
        headers=READ,
        response_json=page(
            "p-2", name="Child", content="Child content", parent_page_id="p-1", sub_title="Subtitle"
        ),
    )
    value = result(
        invoke(
            mock_api,
            [
                "page",
                "create",
                "d-1",
                "--workspace-id",
                "123",
                "--name",
                "Child",
                "--parent-page",
                "https://app.clickup.com/123/v/dc/d-1/p-1",
                "--content-file",
                str(content_file),
                "--sub-title",
                "Subtitle",
            ],
        )
    )
    assert value["page"]["page_id"] == "p-2" and value["verified"] is True
    assert value["page"]["parent_page_id"] == "p-1"


def test_metadata_update_does_not_send_or_replace_content(mock_api: MockClickUpAPI) -> None:
    mock_api.expect(
        "GET",
        BASE + "/d-1/pages/p-1?content_format=text%2Fmd",
        headers=READ,
        response_json=page(sub_title="Old"),
    )
    mock_api.expect(
        "PUT",
        BASE + "/d-1/pages/p-1",
        headers=WRITE,
        json_body={"name": "Renamed", "sub_title": "New"},
        response_body=b"",
        response_status=200,
    )
    mock_api.expect(
        "GET",
        BASE + "/d-1/pages/p-1?content_format=text%2Fmd",
        headers=READ,
        response_json=page(name="Renamed", sub_title="New"),
    )
    value = result(
        invoke(
            mock_api,
            [
                "page",
                "update",
                "p-1",
                "--doc",
                "d-1",
                "--workspace-id",
                "123",
                "--name",
                "Renamed",
                "--sub-title",
                "New",
            ],
        )
    )
    assert value["verified"] is True


@pytest.mark.parametrize("mode", ["append", "prepend"])
def test_native_addition_uses_edit_mode_not_read_replace(
    mock_api: MockClickUpAPI, tmp_path: Path, mode: str
) -> None:
    addition = tmp_path / "addition.md"
    addition.write_text("added\n", encoding="utf-8")
    mock_api.expect(
        "GET",
        BASE + "/d-1/pages/p-1?content_format=text%2Fmd",
        headers=READ,
        response_json=page(content="before\n", sub_title="Keep"),
    )
    mock_api.expect(
        "PUT",
        BASE + "/d-1/pages/p-1",
        headers=WRITE,
        json_body={"content": "added\n", "content_format": "text/md", "content_edit_mode": mode},
        response_body=b"not json",
    )
    expected = "before\nadded\n" if mode == "append" else "added\nbefore\n"
    mock_api.expect(
        "GET",
        BASE + "/d-1/pages/p-1?content_format=text%2Fmd",
        headers=READ,
        response_json=page(content=expected, sub_title="Keep"),
    )
    value = result(
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
                str(addition),
            ],
        )
    )
    assert value["verified"] is True and value["mode"] == mode


def test_replace_is_hash_guarded_and_acknowledges_loss(
    mock_api: MockClickUpAPI, tmp_path: Path
) -> None:
    import hashlib

    body = tmp_path / "replacement.md"
    body.write_text("replacement", encoding="utf-8")
    revision = hashlib.sha256(b"before").hexdigest()
    mock_api.expect(
        "GET",
        BASE + "/d-1/pages/p-1?content_format=text%2Fmd",
        headers=READ,
        response_json=page(content="before", sub_title="Keep"),
    )
    mock_api.expect(
        "PUT",
        BASE + "/d-1/pages/p-1",
        headers=WRITE,
        json_body={
            "content": "replacement",
            "content_format": "text/md",
            "content_edit_mode": "replace",
        },
    )
    mock_api.expect(
        "GET",
        BASE + "/d-1/pages/p-1?content_format=text%2Fmd",
        headers=READ,
        response_json=page(content="replacement", sub_title="Keep"),
    )
    value = result(
        invoke(
            mock_api,
            [
                "page",
                "replace",
                "p-1",
                "--doc",
                "d-1",
                "--workspace-id",
                "123",
                "--content-file",
                str(body),
                "--expect-sha256",
                revision,
                "--acknowledge-loss",
            ],
        )
    )
    assert value["verified"] is True and value["hash_guard_atomic"] is False
    assert value["warnings"]


def test_stale_replace_never_puts(mock_api: MockClickUpAPI, tmp_path: Path) -> None:
    body = tmp_path / "replacement.md"
    body.write_text("replacement", encoding="utf-8")
    mock_api.expect(
        "GET",
        BASE + "/d-1/pages/p-1?content_format=text%2Fmd",
        headers=READ,
        response_json=page(content="newer"),
    )
    output = invoke(
        mock_api,
        [
            "page",
            "replace",
            "p-1",
            "--doc",
            "d-1",
            "--workspace-id",
            "123",
            "--content-file",
            str(body),
            "--expect-sha256",
            "0" * 64,
            "--acknowledge-loss",
        ],
    )
    assert output.exit_code == 1
    assert "stale" in output.stderr.lower()


@pytest.mark.parametrize("count", [0, 1, 2])
def test_ensure_matches_only_exact_normalized_siblings(
    mock_api: MockClickUpAPI, tmp_path: Path, count: int
) -> None:
    content = tmp_path / "ensure.md"
    content.write_text("new", encoding="utf-8")
    mock_api.expect("GET", BASE + "/d-1", headers=READ, response_json=doc())
    children = [page("nested", name="Desired", parent_page_id="parent")]
    roots = [
        page("parent", name="Parent", pages=children),
        *[page(f"match-{i}", name=" Desired ") for i in range(count)],
    ]
    mock_api.expect(
        "GET", BASE + "/d-1/page_listing?max_page_depth=-1", headers=READ, response_json=roots
    )
    if count == 0:
        mock_api.expect("GET", BASE + "/d-1", headers=READ, response_json=doc())
        mock_api.expect(
            "POST",
            BASE + "/d-1/pages",
            headers=WRITE,
            json_body={"name": "Desired", "content": "new", "content_format": "text/md"},
            response_json={"id": "p-1"},
        )
        mock_api.expect(
            "GET",
            BASE + "/d-1/pages/p-1?content_format=text%2Fmd",
            headers=READ,
            response_json=page(name="Desired", content="new"),
        )
    output = invoke(
        mock_api,
        [
            "page",
            "ensure",
            "d-1",
            "--workspace-id",
            "123",
            "--name",
            "Desired",
            "--content-file",
            str(content),
        ],
    )
    if count == 2:
        e = error(output)
        assert e["type"] == "ambiguous_match" and e["page_ids"] == ["match-0", "match-1"]
    else:
        value = result(output)
        assert value["created"] is (count == 0)
        if count == 1:
            assert value["page"]["page_id"] == "match-0" and value["changed"] is False
