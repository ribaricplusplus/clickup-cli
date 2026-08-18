from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_changelog_has_empty_unreleased_and_dated_release() -> None:
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    unreleased = changelog.split("## [Unreleased]", 1)[1].split("## [0.2.0]", 1)[0]

    assert unreleased.strip() == ""
    assert "## [0.2.0] - 2026-08-18" in changelog


def test_api_snapshot_provenance_is_exact() -> None:
    contracts = (ROOT / "docs" / "api-contracts.md").read_text(encoding="utf-8")

    assert "2026-08-18" in contracts
    assert "OpenAPI 3.1.0" in contracts
    assert "API info\nversion 2.0" in contracts
    assert "contains 83 paths" in contracts
    assert "a0a72ec97ddb4e4859b9ed89b997bb784ba5828412ff35119f41e87103069662" in contracts


def test_readme_documents_all_command_groups_and_stable_task_fields() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    groups = (
        "`auth`",
        "`workspace`",
        "`member`",
        "`list`",
        "`task`",
        "`task comment`",
        "`task due-date`",
        "`task priority`",
        "`task start-date`",
        "`task tag`",
        "`task attachment`",
        "`task batch`",
        "`time`",
    )
    stable_fields = (
        "archived",
        "assignees",
        "attachments",
        "description",
        "due_date",
        "due_date_ms",
        "due_date_time",
        "id",
        "list_id",
        "list_name",
        "name",
        "priority",
        "start_date",
        "start_date_ms",
        "start_date_time",
        "status",
        "status_type",
        "tags",
        "url",
    )

    assert all(group in readme for group in groups)
    stable_section = readme.split("Current stable task fields are:", 1)[1].split("## ", 1)[0]
    assert all(field in stable_section for field in stable_fields)
