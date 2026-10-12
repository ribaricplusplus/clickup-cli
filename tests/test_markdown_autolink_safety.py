"""Keep autolink normalization narrowly bounded to exact provider spelling."""

from __future__ import annotations

import pytest

from clickup_cli.descriptions import markdown_matches


@pytest.mark.parametrize(
    ("requested", "observed", "matches"),
    [
        (
            "See https://example.invalid/a?q=one&b=two.",
            "See [https://example.invalid/a?q=one&b=two](https://example.invalid/a?q=one&b=two).",
            True,
        ),
        (
            "See http://example.invalid/a",
            "See [http://example.invalid/a](http://example.invalid/a)",
            True,
        ),
        (
            "Contact person@example.invalid",
            "Contact [person@example.invalid](mailto:person@example.invalid)",
            True,
        ),
        (
            "See https://example.invalid/a",
            "See [https://example.invalid/a](https://example.invalid/b)",
            False,
        ),
        (
            "See https://example.invalid/a",
            "See [https://example.invalid/b](https://example.invalid/b)",
            False,
        ),
        ("[source](https://example.invalid/a)", "source", False),
        (
            "`https://example.invalid/a`",
            "[https://example.invalid/a](https://example.invalid/a)",
            False,
        ),
        (
            "Contact person@example.invalid",
            "Contact [person@example.invalid](mailto:other@example.invalid)",
            False,
        ),
        (
            "https://example.invalid/a",
            '[https://example.invalid/a](https://example.invalid/a "title")',
            False,
        ),
        (
            "https://example.invalid/a",
            "**[https://example.invalid/a](https://example.invalid/a)**",
            False,
        ),
        (
            "## Goal\nhttps://example.invalid/a",
            "\\## Goal\n[https://example.invalid/a](https://example.invalid/a)",
            False,
        ),
        (
            "```\nhttps://example.invalid/a\n```",
            "```\n[https://example.invalid/a](https://example.invalid/a)\n```",
            False,
        ),
    ],
)
def test_autolink_normalization_preserves_text_destinations_and_structure(
    requested: str, observed: str, matches: bool
) -> None:
    assert markdown_matches(requested, observed) is matches
