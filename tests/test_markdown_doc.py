"""Tests for the top-level ``@``-import classifier (``MarkdownDoc``)."""

from __future__ import annotations

from punt_vox.markdown_doc import MarkdownDoc

_VOX = "@.punt-labs/vox/CLAUDE.md"


# ---------------------------------------------------------------------------
# contains / without on plain top-level lines
# ---------------------------------------------------------------------------


def test_empty_document_contains_nothing() -> None:
    doc = MarkdownDoc("")
    assert doc.contains(_VOX) is False
    assert doc.without(_VOX) == ""


def test_top_level_line_is_found_and_removed() -> None:
    text = f"# rules\n\n{_VOX}\n"
    doc = MarkdownDoc(text)
    assert doc.contains(_VOX) is True
    assert doc.without(_VOX) == "# rules\n\n"


def test_absent_line_is_a_no_op() -> None:
    text = "# rules\n\nkeep me\n"
    doc = MarkdownDoc(text)
    assert doc.contains(_VOX) is False
    assert doc.without(_VOX) == text


def test_every_top_level_occurrence_is_removed() -> None:
    # A duplicate (a racing writer could leave one) collapses to zero.
    text = f"{_VOX}\n# rules\n{_VOX}\n"
    doc = MarkdownDoc(text)
    assert doc.contains(_VOX) is True
    assert doc.without(_VOX) == "# rules\n"


def test_match_is_terminator_insensitive() -> None:
    # A CRLF and a lone-CR host still match the terminator-free canonical string.
    for terminator in ("\r\n", "\r"):
        doc = MarkdownDoc(f"# rules{terminator}{_VOX}{terminator}")
        assert doc.contains(_VOX) is True
        assert doc.without(_VOX) == f"# rules{terminator}"


# ---------------------------------------------------------------------------
# fenced blocks — a real fence makes the line inert
# ---------------------------------------------------------------------------


def test_backtick_fenced_line_is_not_top_level() -> None:
    text = f"# rules\n\n```\n{_VOX}\n```\n"
    doc = MarkdownDoc(text)
    assert doc.contains(_VOX) is False
    # An inert fenced copy is left byte-for-byte.
    assert doc.without(_VOX) == text


def test_tilde_fenced_line_is_not_top_level() -> None:
    text = f"# rules\n\n~~~\n{_VOX}\n~~~\n"
    doc = MarkdownDoc(text)
    assert doc.contains(_VOX) is False
    assert doc.without(_VOX) == text


def test_info_string_opens_a_fence() -> None:
    text = f"# rules\n\n```markdown\n{_VOX}\n```\n"
    doc = MarkdownDoc(text)
    assert doc.contains(_VOX) is False


def test_mismatched_inner_delimiter_is_content_not_a_close() -> None:
    # A ~~~ inside a ``` block does not close it; the import stays fenced.
    text = f"```\n~~~\n{_VOX}\n```\n"
    doc = MarkdownDoc(text)
    assert doc.contains(_VOX) is False


def test_shorter_inner_delimiter_is_content_not_a_close() -> None:
    # A run of four backticks opens; a three-backtick line inside is content,
    # so the import between them is still fenced (inert).
    text = f"````\n```\n{_VOX}\n````\n"
    doc = MarkdownDoc(text)
    assert doc.contains(_VOX) is False


def test_longer_close_delimiter_matches_shorter_open() -> None:
    # A three-backtick opener closes on a four-backtick run (>= the opener),
    # so a top-level import after the close is found.
    text = f"```\ncode\n````\n{_VOX}\n"
    doc = MarkdownDoc(text)
    assert doc.contains(_VOX) is True


# ---------------------------------------------------------------------------
# the unterminated-opener guard — a dangling fence swallows nothing
# ---------------------------------------------------------------------------


def test_unterminated_opener_does_not_swallow_the_import() -> None:
    # A dangling fence in the user's prose above the import must not fence it:
    # the naive "odd count of preceding delimiters" rule would misclassify the
    # column-0 import as fenced. The import stays top-level, found and removable.
    text = f"# rules\n\n```text\nnot closed\n{_VOX}\n"
    doc = MarkdownDoc(text)
    assert doc.contains(_VOX) is True
    assert doc.without(_VOX) == "# rules\n\n```text\nnot closed\n"


def test_two_unterminated_openers_still_leave_the_import_top_level() -> None:
    text = f"```\none\n~~~\ntwo\n{_VOX}\n"
    doc = MarkdownDoc(text)
    assert doc.contains(_VOX) is True


# ---------------------------------------------------------------------------
# indented code blocks
# ---------------------------------------------------------------------------


def test_four_space_indented_line_is_not_top_level() -> None:
    text = f"# rules\n\n    {_VOX}\n"
    doc = MarkdownDoc(text)
    assert doc.contains(_VOX) is False
    assert doc.without(_VOX) == text


def test_tab_indented_line_is_not_top_level() -> None:
    text = f"# rules\n\n\t{_VOX}\n"
    doc = MarkdownDoc(text)
    assert doc.contains(_VOX) is False


def test_three_space_indent_is_still_top_level() -> None:
    # Up to three leading spaces is not an indented-code line (CommonMark).
    text = f"# rules\n\n   {_VOX}\n"
    doc = MarkdownDoc(text)
    # The stored line has three leading spaces, so it is not equal net of
    # terminator to the bare canonical string -- an indented import is not the
    # canonical line. It is top-level but does not match the bare string.
    assert doc.contains(f"   {_VOX}") is True


def test_indented_fence_delimiter_does_not_open_a_block() -> None:
    # A four-space-indented ``` is inert indented code, never a delimiter, so a
    # following column-0 import stays top-level.
    text = f"    ```\n{_VOX}\n"
    doc = MarkdownDoc(text)
    assert doc.contains(_VOX) is True
