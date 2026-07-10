"""
tests/lint/test_no_stray_gitlinks.py

AUD-14 (#370) regression guard (2026-07-10).

A git *gitlink* — a tree entry with mode ``160000`` — is how git records a
submodule: "at this path lives another repository, pinned to this commit".
A gitlink is only meaningful when `.gitmodules` says where that repository
can be cloned from. A gitlink WITHOUT a matching `.gitmodules` stanza is a
dangling pointer: `git clone` produces an empty directory, `git submodule
update --init` cannot resolve it, and the commit it names may exist nowhere
but the object store of the machine that committed it.

That is exactly what happened. Two agent scratch worktrees —

    .claude/worktrees/agent-a09ca20f8e150a8fd
    .claude/worktrees/agent-a6a919879154e1756

— were swept into `main` by two `git add`-everything commits (3be20e0 #302,
d4405aa #310). They named commits that were never pushed, there was no
`.gitmodules`, and every subsequent `git status` in a checkout that still had
the scratch directories reported the tree dirty.

Removing the two entries fixes the instance. This guard fixes the class: a
gitlink may exist only if `.gitmodules` registers its exact path. Deliberately
adding a real submodule still passes — you just have to declare it.

The check reads the INDEX (`git ls-files -s`), not the working tree, because
the index is what a commit is built from and what a clone reproduces.
"""

from __future__ import annotations

import configparser
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]

_GITLINK_MODE = "160000"


def _index_gitlinks() -> list[str]:
    """Paths recorded in the git index as submodule pointers (mode 160000)."""
    out = subprocess.run(
        ["git", "ls-files", "--stage"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    paths: list[str] = []
    for line in out.splitlines():
        # "<mode> <object> <stage>\t<path>"
        meta, _, path = line.partition("\t")
        if meta.split(" ", 1)[0] == _GITLINK_MODE:
            paths.append(path)
    return paths


def _declared_submodule_paths() -> set[str]:
    """Paths declared as submodules in `.gitmodules` (empty when absent)."""
    gitmodules = _REPO_ROOT / ".gitmodules"
    if not gitmodules.exists():
        return set()

    parser = configparser.ConfigParser()
    parser.read_string(gitmodules.read_text(encoding="utf-8"))
    return {
        parser.get(section, "path").strip()
        for section in parser.sections()
        if parser.has_option(section, "path")
    }


def test_no_gitlink_lacks_a_gitmodules_declaration() -> None:
    """Every submodule pointer in the index must be declared in `.gitmodules`.

    An undeclared gitlink is unresolvable on a fresh clone. The two that
    prompted this guard were agent scratch worktrees, but the failure mode is
    generic: any `git add`-everything over a nested repository plants one.
    """
    undeclared = sorted(set(_index_gitlinks()) - _declared_submodule_paths())

    assert not undeclared, (
        "Undeclared submodule pointer(s) (mode 160000) committed to the tree:\n"
        + "\n".join(f"  {p}" for p in undeclared)
        + "\n\nA gitlink with no matching `.gitmodules` entry is a dangling "
        "pointer: it names a commit that need not exist anywhere outside the "
        "committing machine's object store, so a fresh clone cannot resolve it.\n"
        "This is almost always a nested repository (an agent worktree, a vendored "
        "checkout, a stray `git init`) captured by an over-broad `git add`.\n\n"
        "Fix: `git rm --cached <path>` and add the path to `.gitignore`.\n"
        "If you genuinely meant to add a submodule, use `git submodule add` so "
        "that `.gitmodules` records its URL."
    )


def test_agent_scratch_worktrees_are_ignored() -> None:
    """`.claude/worktrees/` must stay ignored, or the sweep repeats.

    Removing the gitlinks from the index is not enough while the directories
    still exist on developer machines: the next `git add -A` re-commits them.
    `git check-ignore` is the authority here — it consults every ignore source
    exactly as `git add` does, rather than pattern-matching `.gitignore` text.
    """
    probe = ".claude/worktrees/agent-probe"
    result = subprocess.run(
        ["git", "check-ignore", "-q", "--no-index", probe],
        cwd=_REPO_ROOT,
        capture_output=True,
    )

    assert result.returncode == 0, (
        f"`{probe}` is NOT ignored by git (check-ignore returned "
        f"{result.returncode}). Agent scratch worktrees live under "
        "`.claude/worktrees/`; if that path is committable, the next "
        "`git add -A` re-plants the dangling gitlinks this guard removed. "
        "Restore the `.claude/worktrees/` entry in `.gitignore`."
    )
