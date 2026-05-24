from __future__ import annotations

import subprocess
from pathlib import Path


def git_available(root: Path) -> bool:
    return (root / ".git").exists()


def current_branch(root: Path) -> str:
    return _git(root, "branch", "--show-current") or "detached"


def branch_sha(root: Path, branch: str) -> str:
    if not branch.strip():
        return ""
    return _git(root, "rev-parse", "--verify", branch, check=False)


def remote_branch_sha(root: Path, remote: str, branch: str) -> str:
    if not remote.strip() or not branch.strip():
        return ""
    return branch_sha(root, f"refs/remotes/{remote}/{branch}")


def dirty_files(root: Path) -> list[str]:
    output = _git(root, "status", "--short")
    return [line for line in output.splitlines() if line.strip()]


def latest_tag(root: Path) -> str | None:
    tag = _git(root, "describe", "--tags", "--abbrev=0", check=False)
    return tag or None


def ahead_behind(root: Path) -> tuple[int, int]:
    """Return commits ahead/behind the configured upstream branch."""
    output = _git(root, "rev-list", "--left-right", "--count", "@{u}...HEAD", check=False)
    parts = output.split()
    if len(parts) != 2:
        return (0, 0)
    try:
        behind, ahead = int(parts[0]), int(parts[1])
    except ValueError:
        return (0, 0)
    return (ahead, behind)


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

    staged = subprocess.run(
        ["git", "add", "-A"],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    if staged.returncode != 0:
        return staged.stderr.strip() or staged.stdout.strip()

    completed = subprocess.run(
        ["git", "commit", "-m", message],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    return completed.stdout.strip() or completed.stderr.strip()


def push_current_branch(root: Path) -> str:
    branch = current_branch(root)
    if not branch or branch == "detached":
        return "Cannot push from a detached HEAD."

    return push_branch(root, branch)


def push_branch(root: Path, branch: str) -> str:
    if not branch:
        return "Cannot push: branch name is empty."

    completed = subprocess.run(
        ["git", "push", "-u", "origin", branch],
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
