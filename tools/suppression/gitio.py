"""Git queries the suppression ratchet needs: base resolution and tree reads.

Git materializes the *base-commit* source tree (``git archive <base> --
<paths> | tar -x``) so the same ``Scanner`` that counts the current tree's
suppressions also counts the base commit's -- there is no separate,
independently-maintained baseline blob to drift out of sync with the code it
is supposed to describe.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Self

_TIMEOUT = 10


class GitError(Exception):
    """A git command the ratchet depends on failed.

    Raised instead of degrading to a benign default, so the enforcement gate
    fails closed: a broken git call can never masquerade as "no change".
    """


class GitRepo:
    """Answer the ratchet's git questions, or degrade to ``None`` outside git."""

    _root: Path | None

    def __new__(cls, start: Path | None = None) -> Self:
        self = super().__new__(cls)
        self._root = cls._discover_root(start if start is not None else Path.cwd())
        return self

    @property
    def root(self) -> Path | None:
        """Return the repository root, or ``None`` when not inside a repo."""
        return self._root

    @property
    def available(self) -> bool:
        """Return whether git commands can run against a repository."""
        return self._root is not None

    @classmethod
    def _discover_root(cls, start: Path) -> Path | None:
        out = cls._run(["git", "rev-parse", "--show-toplevel"], cwd=start)
        return Path(out.strip()) if out is not None else None

    @staticmethod
    def _run(args: list[str], cwd: Path | None) -> str | None:
        try:
            result = subprocess.run(
                args, capture_output=True, text=True, timeout=_TIMEOUT, cwd=cwd
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0:
            return None
        return result.stdout

    def _git(self, args: list[str]) -> str | None:
        if self._root is None:
            return None  # not in a repo: degrade, never run against the ambient CWD
        return self._run(["git", *args], cwd=self._root)

    def short_head(self) -> str | None:
        """Return the abbreviated HEAD commit hash."""
        out = self._git(["rev-parse", "--short", "HEAD"])
        return out.strip() if out is not None else None

    def resolve_ref(self, ref: str) -> str | None:
        """Return the full commit hash for a ref, or ``None`` if unresolvable."""
        out = self._git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"])
        return out.strip() if out else None

    def merge_base(self, left: str, right: str) -> str | None:
        """Return the merge-base commit of two refs, or ``None``."""
        out = self._git(["merge-base", left, right])
        return out.strip() if out else None

    def resolve_base(self, base_ref: str | None) -> str | None:
        """Resolve the comparison base: explicit ref, else merge-base of main."""
        if base_ref is not None:
            return self.resolve_ref(base_ref)
        return self.merge_base("origin/main", "HEAD")

    def path_exists_at_ref(self, ref: str, path: str) -> bool:
        """Return whether ``path`` exists as a blob or tree at ``ref``.

        ``git cat-file -e`` exits 128 both for "not in this tree" and for
        unrelated failures (a corrupted object, a bad ref spelling) -- only
        the former is absence. A blank ``path`` addresses the ref's root
        tree (``git cat-file -e <ref>:``); callers must not pass ``"."``.
        """
        if self._root is None:
            msg = "not inside a git repository"
            raise GitError(msg)
        spec = f"{ref}:{path}"
        try:
            result = subprocess.run(
                ["git", "cat-file", "-e", spec],
                capture_output=True,
                text=True,
                timeout=_TIMEOUT,
                cwd=self._root,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            msg = f"git cat-file -e {spec} failed to run: {exc}"
            raise GitError(msg) from exc
        if result.returncode == 0:
            return True
        # Git's message casing can vary across versions/locales; comparing
        # lowercased avoids a case mismatch alone turning a genuine absence
        # into a raised GitError.
        stderr = result.stderr.strip().lower()
        if result.returncode == 128 and (
            "does not exist in" in stderr or "exists on disk, but not in" in stderr
        ):
            return False
        msg = f"git cat-file -e {spec} errored: {stderr or f'exit {result.returncode}'}"
        raise GitError(msg)

    def archive_paths(self, ref: str, paths: Sequence[str], dest: Path) -> None:
        """Materialize ``paths`` as they existed at ``ref`` into ``dest``.

        One ``git archive`` piped into ``tar -x`` -- far fewer subprocess
        spawns than an individual ``git show`` per file, and it preserves the
        tree layout under ``dest`` so ``Scanner`` can walk it unchanged.
        Callers must pre-filter ``paths`` to those confirmed present at
        ``ref`` (``path_exists_at_ref``): ``git archive`` errors on any
        pathspec that matches nothing, and a caller-side filter is what lets
        this method distinguish "materialize what exists" from masking a
        genuine git failure.
        """
        if self._root is None:
            msg = "not inside a git repository"
            raise GitError(msg)
        if not paths:
            return
        try:
            archive = subprocess.run(
                ["git", "archive", ref, "--", *paths],
                capture_output=True,
                timeout=_TIMEOUT,
                cwd=self._root,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            msg = f"git archive {ref} failed to run: {exc}"
            raise GitError(msg) from exc
        if archive.returncode != 0:
            detail = archive.stderr.decode(errors="replace").strip()
            msg = f"git archive {ref} errored: {detail or f'exit {archive.returncode}'}"
            raise GitError(msg)
        try:
            tar = subprocess.run(
                ["tar", "-x", "-C", str(dest)],
                input=archive.stdout,
                capture_output=True,
                timeout=_TIMEOUT,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            msg = f"tar extraction of {ref} archive failed to run: {exc}"
            raise GitError(msg) from exc
        if tar.returncode != 0:
            detail = tar.stderr.decode(errors="replace").strip()
            fallback = f"exit {tar.returncode}"
            msg = f"tar extraction of {ref} archive errored: {detail or fallback}"
            raise GitError(msg)
