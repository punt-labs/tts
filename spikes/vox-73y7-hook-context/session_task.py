"""The realistic working-session task: seeded buggy project + bounded prompt.

The realism capture needs a session whose hook traffic looks like real
work: multiple files, a test run that FAILS, a debug/fix loop, then green
tests and new feature work. Hoping the fork stumbles into a failure is not
a measurement, so the failure is seeded: the scratch project ships a tiny
``textstat`` package with a deliberate bug and a stdlib ``unittest`` suite
that exposes it. The task prompt walks the fork through run -> debug ->
fix -> extend -> document, and the ledger analyzers key their timepoint
sampling off the resulting FAIL/OK markers in the Bash tool payloads.

``unittest`` (not pytest) keeps the fork dependency-free: the seeded suite
runs under any system ``python3``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Self, final

# The derived prompt must stay bounded: it is one working session's intent,
# and the cap keeps the fork from being handed a spec document.
MAX_TASK_CHARS = 2200

# The sentence that bounds the spawned session's work. Present verbatim in
# every derived prompt.
STOP_SENTENCE = (
    "When every test passes and the README is updated, reply with the "
    "single word DONE and stop. Do not use the network, and do not read "
    "or write anything outside this directory."
)

# The command the task tells the fork to use; the timepoint sampler keys
# off its output markers (unittest prints FAILED (...) / OK).
TEST_COMMAND = "python3 -m unittest discover -s tests -v"

_BUGGY_STATS = '''\
"""Tiny text statistics helpers."""


def word_count(text):
    """Number of whitespace-separated words in text."""
    return len(text.split(","))


def longest_word(text):
    """The longest word in text; empty string for empty input."""
    words = text.split()
    if not words:
        return ""
    return max(words, key=len)
'''

_STATS_TESTS = '''\
"""Tests for textstat.stats -- the seeded suite the session must run."""

import unittest

from textstat.stats import longest_word, word_count


class WordCountTests(unittest.TestCase):
    def test_counts_whitespace_separated_words(self):
        self.assertEqual(word_count("the quick brown fox"), 4)

    def test_single_word(self):
        self.assertEqual(word_count("hello"), 1)


class LongestWordTests(unittest.TestCase):
    def test_finds_longest(self):
        self.assertEqual(longest_word("a bb ccc"), "ccc")

    def test_empty_text(self):
        self.assertEqual(longest_word(""), "")


if __name__ == "__main__":
    unittest.main()
'''

_SEED_README = """\
# textstat

Tiny text statistics package. Work in progress.
"""


@final
@dataclass(frozen=True, slots=True)
class SeededFile:
    """One file the harness deposits into the scratch project."""

    relative_path: str
    content: str


# The project the fork wakes up in: a package with a planted bug
# (word_count splits on commas, not whitespace) and the suite that
# catches it.
SEEDED_FILES: tuple[SeededFile, ...] = (
    SeededFile("textstat/__init__.py", '"""textstat package."""\n'),
    SeededFile("textstat/stats.py", _BUGGY_STATS),
    SeededFile("tests/__init__.py", ""),
    SeededFile("tests/test_stats.py", _STATS_TESTS),
    SeededFile("README.md", _SEED_README),
)


@final
class WorkSessionTask:
    """Derives the bounded initial prompt for the realism-capture fork."""

    __slots__ = ()

    def __new__(cls) -> Self:
        return super().__new__(cls)

    def derive(self) -> str:
        """Return the initial prompt: run, debug, fix, extend, document."""
        prompt = (
            "You are working in the textstat project in the current "
            "directory. Work through these steps strictly in order.\n"
            "1. IMPORTANT: as your VERY FIRST action, before reading or "
            "editing any file, run the test suite with: "
            f"{TEST_COMMAND} 2>&1\n"
            "2. It fails. Read the failure output, then read only the "
            "code the failure points at, find the bug in "
            "textstat/stats.py, fix it, and re-run the suite until it "
            "passes.\n"
            "3. Add a new module textstat/readability.py with two "
            "functions: sentence_count(text) counting sentences ended by "
            ". ! or ?, and avg_words_per_sentence(text) returning the "
            "mean words per sentence (0.0 for no sentences). Write "
            "tests/test_readability.py covering both, including empty "
            "text, and run the whole suite until it passes.\n"
            "4. Extend textstat/stats.py: longest_word must ignore "
            "leading/trailing punctuation when comparing lengths, and add "
            "char_frequencies(text) returning a dict of letter counts, "
            "lowercased, ignoring non-letters. Add tests for both to "
            "tests/test_stats.py and run the whole suite until it "
            "passes.\n"
            "5. Add a script stats_cli.py at the project root that reads "
            "a file path from sys.argv and prints word count, longest "
            "word, and sentence count, one per line. Try it on README.md "
            "with: python3 stats_cli.py README.md\n"
            "6. Update README.md with a short usage section covering the "
            "module functions and the script. "
            f"{STOP_SENTENCE}"
        )
        if len(prompt) > MAX_TASK_CHARS:
            msg = f"derived task exceeds {MAX_TASK_CHARS} chars: {len(prompt)}"
            raise ValueError(msg)
        return prompt
