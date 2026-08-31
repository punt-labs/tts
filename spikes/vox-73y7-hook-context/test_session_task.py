"""Pins for the seeded work-session task.

The realism capture keys its timepoint sampling off a seeded test failure;
if the planted bug does not actually fail the planted suite, the whole
FAIL -> fix -> OK arc never happens and the ledger analyzers sample
nothing. These pins prove the seed is genuinely broken, the suite
genuinely catches it, and the derived prompt stays inside its bound.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

from session_task import (
    MAX_TASK_CHARS,
    SEEDED_FILES,
    STOP_SENTENCE,
    TEST_COMMAND,
    WorkSessionTask,
)


def _deposit(tmp_path: Path) -> Path:
    for seeded in SEEDED_FILES:
        target = tmp_path / seeded.relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(seeded.content, encoding="utf-8")
    return tmp_path


def _load_module(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestSeededProject:
    """The planted bug is real and the planted suite catches it."""

    def test_seeded_word_count_is_genuinely_buggy(self, tmp_path: Path) -> None:
        root = _deposit(tmp_path)
        stats = _load_module(root / "textstat" / "stats.py", "seeded_stats")
        # The suite expects 4; the comma-split bug yields 1. If these were
        # ever equal the seeded failure would vanish and the capture would
        # have no FAIL timepoint.
        assert stats.word_count("the quick brown fox") == 1

    def test_seeded_longest_word_is_correct(self, tmp_path: Path) -> None:
        # Exactly one planted bug: the rest of the seed must pass so the
        # fork's fix converges instead of chasing extra failures.
        root = _deposit(tmp_path)
        stats = _load_module(root / "textstat" / "stats.py", "seeded_stats2")
        assert stats.longest_word("a bb ccc") == "ccc"
        assert stats.longest_word("") == ""

    def test_seeded_suite_expectations_match_correct_semantics(
        self, tmp_path: Path
    ) -> None:
        # The suite asserts whitespace-word counts (4 for four words). A
        # fixed implementation (split on whitespace) satisfies every case
        # the seeded tests assert, so the fork's fix can actually go green.
        root = _deposit(tmp_path)
        source = (root / "tests" / "test_stats.py").read_text(encoding="utf-8")
        assert 'word_count("the quick brown fox"), 4' in source
        assert 'word_count("hello"), 1' in source

    def test_seeded_paths_stay_inside_the_project(self) -> None:
        for seeded in SEEDED_FILES:
            path = Path(seeded.relative_path)
            assert not path.is_absolute()
            assert ".." not in path.parts


class TestDerivedPrompt:
    """Bounded, self-terminating, and keyed to the sampled test command."""

    def test_prompt_is_within_the_char_bound(self) -> None:
        prompt = WorkSessionTask().derive()
        assert len(prompt) <= MAX_TASK_CHARS

    def test_prompt_carries_the_stop_sentence_verbatim(self) -> None:
        assert STOP_SENTENCE in WorkSessionTask().derive()

    def test_prompt_names_the_exact_test_command_the_sampler_keys_on(self) -> None:
        assert TEST_COMMAND in WorkSessionTask().derive()
