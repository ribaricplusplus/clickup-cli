from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import clickup_cli.docs as docs
from tests.conftest import MockClickUpAPI
from tests.test_docs_authoring import error
from tests.test_docs_contract import BASE, READ, WRITE, doc, invoke, page, result


@pytest.mark.parametrize("kind", ["disconnect", "malformed", "no_id", "429"])
def test_doc_create_uncertain_outcome_never_retries(mock_api: MockClickUpAPI, kind: str) -> None:
    kwargs: dict[str, Any] = {"response_json": {}}
    if kind == "disconnect":
        kwargs = {"disconnect": True}
    elif kind == "malformed":
        kwargs = {"response_body": b"not-json"}
    elif kind == "429":
        kwargs = {"response_status": 429, "response_json": {"err": "limited"}}
    mock_api.expect(
        "POST",
        BASE,
        headers=WRITE,
        json_body={"name": "Handbook", "visibility": "PRIVATE", "create_page": False},
        **kwargs,
    )
    e = error(invoke(mock_api, ["create", "Handbook", "--workspace-id", "123"]))
    assert e["type"] == ("api_error" if kind == "429" else "outcome_unknown")
    if kind != "429":
        assert e["retry_safe"] is False
    assert len(mock_api.state.requests) == 1


@pytest.mark.parametrize("failure", ["readback", "name", "identity", "normalization"])
def test_known_doc_id_survives_all_post_create_failures(
    mock_api: MockClickUpAPI, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    mock_api.expect(
        "POST",
        BASE,
        headers=WRITE,
        json_body={"name": "Handbook", "visibility": "PRIVATE", "create_page": False},
        response_json={"id": "d-1"},
    )
    payload = doc()
    if failure == "name":
        payload["name"] = "Wrong"
    elif failure == "identity":
        payload["workspace_id"] = 456
    elif failure == "normalization":

        def broken(raw: object, ref: object) -> object:
            raise ValueError("normalization failed")

        monkeypatch.setattr(docs, "normalize_doc", broken)
    mock_api.expect(
        "GET",
        BASE + "/d-1",
        headers=READ,
        response_json=payload,
        response_status=403 if failure == "readback" else 200,
    )
    e = error(invoke(mock_api, ["create", "Handbook", "--workspace-id", "123"]))
    assert e["type"] == "created_but_unverified" and e["doc_id"] == "d-1"
    assert e["workspace_id"] == "123" and e["retry_safe"] is False


@pytest.mark.parametrize(
    "failure", ["disconnect", "malformed", "readback", "identity", "normalization"]
)
def test_page_create_partial_ids_and_no_unsafe_retry(
    mock_api: MockClickUpAPI, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    file = tmp_path / "page.md"
    file.write_text("New", encoding="utf-8")
    mock_api.expect("GET", BASE + "/d-1", headers=READ, response_json=doc())
    kwargs: dict[str, Any] = {"response_json": {"id": "p-2"}}
    if failure == "disconnect":
        kwargs = {"disconnect": True}
    elif failure == "malformed":
        kwargs = {"response_body": b"invalid"}
    mock_api.expect(
        "POST",
        BASE + "/d-1/pages",
        headers=WRITE,
        json_body={"name": "New", "content": "New", "content_format": "text/md"},
        **kwargs,
    )
    if failure not in {"disconnect", "malformed"}:
        if failure == "normalization":

            def broken(raw: object, ref: object) -> object:
                raise ValueError("page normalization failed")

            monkeypatch.setattr(docs, "page_summary", broken)
        mock_api.expect(
            "GET",
            BASE + "/d-1/pages/p-2?content_format=text%2Fmd",
            headers=READ,
            response_status=404 if failure == "readback" else 200,
            response_json=page(
                "p-2", name="New", content="New", doc_id="other" if failure == "identity" else "d-1"
            ),
        )
    e = error(
        invoke(
            mock_api,
            [
                "page",
                "create",
                "d-1",
                "--workspace-id",
                "123",
                "--name",
                "New",
                "--content-file",
                str(file),
            ],
        )
    )
    assert e["type"] == (
        "outcome_unknown" if failure in {"disconnect", "malformed"} else "created_but_unverified"
    )
    assert e["doc_id"] == "d-1" and e["retry_safe"] is False
    if failure not in {"disconnect", "malformed"}:
        assert e["page_id"] == "p-2"
    assert [r.method for r in mock_api.state.requests].count("POST") == 1


@pytest.mark.parametrize("mode", ["append", "prepend", "update"])
@pytest.mark.parametrize("failure", ["disconnect", "429", "readback", "content", "normalization"])
def test_page_edits_are_not_retried_and_keep_target_ids(
    mock_api: MockClickUpAPI,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    failure: str,
) -> None:
    file = tmp_path / "addition.md"
    file.write_text("Add", encoding="utf-8")
    mock_api.expect(
        "GET",
        BASE + "/d-1/pages/p-1?content_format=text%2Fmd",
        headers=READ,
        response_json=page(content="Old"),
    )
    body = (
        {"name": "New"}
        if mode == "update"
        else {"content": "Add", "content_format": "text/md", "content_edit_mode": mode}
    )
    mock_api.expect(
        "PUT",
        BASE + "/d-1/pages/p-1",
        headers=WRITE,
        json_body=body,
        disconnect=failure == "disconnect",
        response_status=429 if failure == "429" else 200,
    )
    if failure not in {"disconnect", "429"}:
        if failure == "normalization":

            def broken(raw: object, ref: object) -> object:
                raise ValueError("page normalization failed")

            monkeypatch.setattr(docs, "page_summary", broken)
        expected = "Old" if mode == "update" else "OldAdd" if mode == "append" else "AddOld"
        mock_api.expect(
            "GET",
            BASE + "/d-1/pages/p-1?content_format=text%2Fmd",
            headers=READ,
            response_status=403 if failure == "readback" else 200,
            response_json=page(
                name="New" if mode == "update" else "Overview",
                content="Wrong" if failure == "content" else expected,
            ),
        )
    flags = ["--name", "New"] if mode == "update" else ["--content-file", str(file)]
    e = error(
        invoke(mock_api, ["page", mode, "p-1", "--doc", "d-1", "--workspace-id", "123", *flags])
    )
    assert e["type"] == (
        "api_error"
        if failure == "429"
        else "outcome_unknown"
        if failure == "disconnect"
        else "edited_but_unverified"
    )
    if failure != "429":
        assert e["page_id"] == "p-1" and e["doc_id"] == "d-1" and e["retry_safe"] is False
    assert [r.method for r in mock_api.state.requests].count("PUT") == 1


@pytest.mark.parametrize(
    "kind", ["missing", "directory", "utf8", "oversize", "parent_conflict", "loss_ack", "hash"]
)
def test_authoring_invalid_preflight_never_sends_http(
    mock_api: MockClickUpAPI, tmp_path: Path, kind: str
) -> None:
    file = tmp_path / "input.md"
    if kind == "directory":
        file.mkdir()
    elif kind == "utf8":
        file.write_bytes(b"\xff")
    elif kind == "oversize":
        file.write_bytes(b"x" * (1024 * 1024 + 1))
    elif kind != "missing":
        file.write_text("Body")
    if kind in {"loss_ack", "hash"}:
        args = [
            "page",
            "replace",
            "p-1",
            "--doc",
            "d-1",
            "--workspace-id",
            "123",
            "--content-file",
            str(file),
            "--expect-sha256",
            "0" * 64 if kind == "loss_ack" else "not-hash",
            *(["--acknowledge-loss"] if kind == "hash" else []),
        ]
    else:
        args = [
            "page",
            "create",
            "d-1",
            "--workspace-id",
            "123",
            "--name",
            "New",
            "--content-file",
            str(file),
            *(
                ["--parent-page", "https://app.clickup.com/123/v/dc/other/p-1"]
                if kind == "parent_conflict"
                else []
            ),
        ]
    assert invoke(mock_api, args).exit_code == 1
    assert not mock_api.state.requests


@pytest.mark.parametrize("visibility", ["public", "personal", "hidden"])
def test_doc_visibility_parent_and_create_page_explicit(
    mock_api: MockClickUpAPI, visibility: str
) -> None:
    parent = {"id": "987", "type": 6}
    mock_api.expect(
        "POST",
        BASE,
        headers=WRITE,
        json_body={
            "name": "Handbook",
            "visibility": visibility.upper(),
            "create_page": True,
            "parent": parent,
        },
        response_json={"id": "d-1"},
    )
    mock_api.expect(
        "GET",
        BASE + "/d-1",
        headers=READ,
        response_json=doc(parent=parent, public=visibility == "public"),
    )
    value = result(
        invoke(
            mock_api,
            [
                "create",
                "Handbook",
                "--workspace-id",
                "123",
                "--visibility",
                visibility,
                "--parent-id",
                "987",
                "--parent-type",
                "LIST",
                "--create-page",
            ],
        )
    )
    assert value["requested_visibility"] == visibility
    if visibility != "public":
        assert value["warnings"]
