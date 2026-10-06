from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path

import pytest

from tests.conftest import MockClickUpAPI
from tests.test_docs_contract import BASE, READ, doc, invoke, page, result


@pytest.mark.parametrize("format", ["markdown", "json"])
def test_export_is_full_recursive_collision_safe_private_snapshot(
    mock_api: MockClickUpAPI, tmp_path: Path, format: str
) -> None:
    content = "![blob](data:image/png;base64,abcdef)\n" + "X" * 20000
    tree = [
        page(
            name="../同じ",
            content=content,
            pages=[page("p-2", name="../同じ", parent_page_id="p-1", content="Child")],
        )
    ]
    mock_api.expect("GET", BASE + "/d-1", headers=READ, response_json=doc())
    mock_api.expect(
        "GET",
        BASE + "/d-1/pages?max_page_depth=-1&content_format=text%2Fmd",
        headers=READ,
        response_json=tree,
    )
    output = tmp_path / "snapshot"
    value = result(
        invoke(
            mock_api,
            ["export", "d-1", "--workspace-id", "123", "--format", format, "--output", str(output)],
        )
    )
    assert value["page_count"] == 2 and value["snapshot_kind"] == "text_snapshot"
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["complete"] is True and manifest["warnings"]
    assert len({entry["filename"] for entry in manifest["pages"]}) == 2
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    for entry in manifest["pages"]:
        file = output / entry["filename"]
        assert file.parent == output and "/" not in entry["filename"]
        assert stat.S_IMODE(file.stat().st_mode) == 0o600
        data = file.read_bytes()
        assert entry["file_sha256"] == hashlib.sha256(data).hexdigest()
        actual = data.decode() if format == "markdown" else json.loads(data)["content"]
        expected = content if entry["page_id"] == "p-1" else "Child"
        assert actual == expected
        assert entry["content_sha256"] == hashlib.sha256(expected.encode()).hexdigest()
    assert manifest["pages"][1]["breadcrumb"] == ["../同じ", "../同じ"]


@pytest.mark.parametrize(
    "kind", ["existing", "symlink", "parent_symlink", "writable_parent", "missing_parent"]
)
def test_export_rejects_unsafe_destination_before_http(
    mock_api: MockClickUpAPI, tmp_path: Path, kind: str
) -> None:
    output = tmp_path / "snapshot"
    if kind == "existing":
        output.mkdir()
    elif kind == "symlink":
        output.symlink_to(tmp_path / "missing")
    elif kind == "parent_symlink":
        alias = tmp_path / "alias"
        alias.symlink_to(tmp_path, target_is_directory=True)
        output = alias / "snapshot"
    elif kind == "writable_parent":
        parent = tmp_path / "shared"
        parent.mkdir(mode=0o777)
        parent.chmod(0o777)
        output = parent / "snapshot"
    else:
        output = tmp_path / "missing" / "snapshot"
    response = invoke(mock_api, ["export", "d-1", "--workspace-id", "123", "--output", str(output)])
    assert response.exit_code == 1 and not mock_api.state.requests


@pytest.mark.parametrize("failure", ["http", "invalid_identity", "filesystem", "raced_destination"])
def test_export_failure_never_installs_incomplete_or_overwrites(
    mock_api: MockClickUpAPI, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    import clickup_cli.doc_export as export

    output = tmp_path / "snapshot"
    mock_api.expect("GET", BASE + "/d-1", headers=READ, response_json=doc())
    mock_api.expect(
        "GET",
        BASE + "/d-1/pages?max_page_depth=-1&content_format=text%2Fmd",
        headers=READ,
        response_status=403 if failure == "http" else 200,
        response_json=[
            page(),
            page("p-2", doc_id="other" if failure == "invalid_identity" else "d-1"),
        ],
    )
    if failure == "filesystem":
        original = export.private_file
        count = 0

        def fail(fd: int, name: str, data: bytes) -> None:
            nonlocal count
            count += 1
            if count == 2:
                raise OSError("disk failure")
            original(fd, name, data)

        monkeypatch.setattr(export, "private_file", fail)
    elif failure == "raced_destination":
        original_install = export.install_snapshot

        def race(fd: int, stage: str, name: str) -> None:
            output.mkdir()
            (output / "sentinel").write_text("keep")
            original_install(fd, stage, name)

        monkeypatch.setattr(export, "install_snapshot", race)
    response = invoke(mock_api, ["export", "d-1", "--workspace-id", "123", "--output", str(output)])
    assert response.exit_code == 1
    if failure == "raced_destination":
        assert (output / "sentinel").read_text() == "keep"
        assert not (output / "manifest.json").exists()
    else:
        assert not output.exists()
    assert not list(tmp_path.glob(".clickup-snapshot-*"))


def test_snapshot_size_ceiling_includes_manifest(
    mock_api: MockClickUpAPI, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import clickup_cli.doc_export as export

    monkeypatch.setattr(export, "MAX_SNAPSHOT_BYTES", 100, raising=False)
    mock_api.expect("GET", BASE + "/d-1", headers=READ, response_json=doc())
    mock_api.expect(
        "GET",
        BASE + "/d-1/pages?max_page_depth=-1&content_format=text%2Fmd",
        headers=READ,
        response_json=[page(content="short")],
    )
    output = tmp_path / "snapshot"
    response = invoke(mock_api, ["export", "d-1", "--workspace-id", "123", "--output", str(output)])
    assert response.exit_code == 1 and not output.exists()
    assert not list(tmp_path.glob(".clickup-snapshot-*"))
