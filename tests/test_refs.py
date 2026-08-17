from __future__ import annotations

import pytest

from clickup_cli.errors import ReferenceError
from clickup_cli.refs import parse_comment_ref, parse_task_ref


@pytest.mark.parametrize(
    ("reference", "expected"),
    [
        ("abc123", "abc123"),
        ("task-ID_7", "task-ID_7"),
        ("https://app.clickup.com/t/abc123", "abc123"),
        ("https://app.clickup.com/t/workspace7/abc123", "abc123"),
        ("https://clickup.com/t/abc123?comment=1#bottom", "abc123"),
    ],
)
def test_parse_task_ref_variants(reference: str, expected: str) -> None:
    assert parse_task_ref(reference) == expected


@pytest.mark.parametrize(
    "reference",
    [
        "",
        "contains spaces",
        "https://example.invalid/t/abc123",
        "https://app.clickup.com/task/abc123",
        "https://app.clickup.com/t/one/two/three",
        "https://app.clickup.com/t/workspace7/bad%2Fid",
        "ftp://app.clickup.com/t/abc123",
        "https://user@app.clickup.com/t/abc123",
        "https://app.clickup.com:443/t/abc123",
        "https://app.clickup.com:invalid/t/abc123",
        "https://app.clickup.com/t//abc123",
    ],
)
def test_parse_task_ref_rejects_unsafe_or_unknown_shapes(reference: str) -> None:
    with pytest.raises(ReferenceError):
        parse_task_ref(reference)


def test_parse_comment_ref_extracts_deep_link_query() -> None:
    assert parse_comment_ref(
        "https://app.clickup.com/t/workspace7/abc123?comment=comment_7&utm_type=1"
    ) == ("abc123", "comment_7")


def test_parse_comment_ref_accepts_explicit_comment_id() -> None:
    assert parse_comment_ref("abc123", "comment_7") == ("abc123", "comment_7")


@pytest.mark.parametrize(
    ("reference", "comment_id"),
    [
        ("abc123", None),
        ("https://app.clickup.com/t/abc123?comment=", None),
        ("https://app.clickup.com/t/abc123?comment=one&comment=two", None),
        ("https://app.clickup.com/t/abc123?comment=one", "two"),
        ("abc123", "bad/comment"),
    ],
)
def test_parse_comment_ref_rejects_missing_ambiguous_or_conflicting_ids(
    reference: str, comment_id: str | None
) -> None:
    with pytest.raises(ReferenceError):
        parse_comment_ref(reference, comment_id)
