from __future__ import annotations

import pytest

from clickup_cli.errors import ReferenceError
from clickup_cli.refs import parse_task_ref


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
