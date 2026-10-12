"""Explicit task description format policy, separate from HTTP and CLI adapters."""

from dataclasses import replace
from typing import Literal, cast

from markdown_it import MarkdownIt
from markdown_it.token import Token

from clickup_cli.errors import InvalidOperationError

DescriptionFormat = Literal["plain", "markdown"]


def validate_description_format(value: str) -> DescriptionFormat:
    if value not in {"plain", "markdown"}:
        raise InvalidOperationError("Description format must be markdown or plain")
    return cast(DescriptionFormat, value)


def _comparison_tokens(tokens: list[Token]) -> list[Token]:
    """Fold only exact self-labelled provider autolinks, never named links/code."""
    normalized: list[Token] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if index + 2 < len(tokens) and token.type == "link_open":
            label, closing = tokens[index + 1 : index + 3]
            href = token.attrs.get("href")
            if (
                label.type == "text"
                and closing.type == "link_close"
                and isinstance(href, str)
                and token.attrs == {"href": href}
                and (
                    (href == label.content and href.startswith(("https://", "http://")))
                    or href == "mailto:" + label.content
                )
            ):
                token = label
                index += 2
        if token.type == "text" and normalized and normalized[-1].type == "text":
            normalized[-1] = replace(normalized[-1], content=normalized[-1].content + token.content)
        else:
            normalized.append(token)
        index += 1
    return normalized


def _semantic_tokens(tokens: list[Token]) -> tuple[object, ...]:
    """Keep rendered structure/content, not source positions or marker spelling."""
    return tuple(
        (
            token.type,
            token.tag,
            token.nesting,
            token.hidden,
            tuple(sorted(token.attrs.items())),
            token.info,
            _semantic_tokens(token.children) if token.children is not None else token.content,
        )
        for token in _comparison_tokens(tokens)
        if token.type != "text" or token.content
    )


def markdown_matches(requested: str, observed: object) -> bool:
    """Compare CommonMark meaning without unescaping literal/flattened Markdown.

    Preserve headings, emphasis, lists, links (including URLs), code and quotes.
    Exact self-labelled HTTP(S)/mailto autolinks may differ because ClickUp adds them.
    Other presentation-equivalent syntax and CRLF/terminal newlines may differ.
    Missing/null/non-text exports never prove a successful Markdown write.
    """
    if not isinstance(observed, str):
        return False
    parser = MarkdownIt("commonmark")
    return _semantic_tokens(parser.parse(requested.replace("\r\n", "\n").rstrip("\n"))) == (
        _semantic_tokens(parser.parse(observed.replace("\r\n", "\n").rstrip("\n")))
    )
