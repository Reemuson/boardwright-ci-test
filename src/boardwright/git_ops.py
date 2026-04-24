from __future__ import annotations

import subprocess
from pathlib import Path


def git_available(root: Path) -> bool:
    return (root / ".git").exists()


def current_branch(root: Path) -> str:
    return _git(root, "branch", "--show-current") or "detached"


def dirty_files(root: Path) -> list[str]:
    output = _git(root, "status", "--short")
    return [line for line in output.splitlines() if line.strip()]


def latest_tag(root: Path) -> str | None:
    tag = _git(root, "describe", "--tags", "--abbrev=0", check=False)
    return tag or None


def changed_paths(root: Path) -> list[str]:
    paths: list[str] = []
    for line in dirty_files(root):
        if len(line) > 3:
            paths.append(line[3:])
    return paths


def commit_all(root: Path, message: str, dry_run: bool = True) -> str:
    if not message.strip():
        return "Commit message cannot be empty."
    if dry_run:
        files = dirty_files(root)
        if not files:
            return "No changes to commit."
        return "Would commit:\n" + "\n".join(files)

    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    completed = subprocess.run(
        ["git", "commit", "-m", message],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    return completed.stdout.strip() or completed.stderr.strip()


def _git(root: Path, *args: str, check: bool = True) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    if check and completed.returncode != 0:
        return ""
    return completed.stdout.strip()
