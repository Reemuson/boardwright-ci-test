# SPDX-License-Identifier: LicenseRef-Boardwright-Source-Available
# Copyright (c) Ryan Hicks

from __future__ import annotations

import re
import subprocess
from pathlib import Path

_SEMVER_TAG = re.compile(
    r"^v?(?P<major>0|[1-9]\d*)\."
    r"(?P<minor>0|[1-9]\d*)\."
    r"(?P<patch>0|[1-9]\d*)"
    r"(?P<prerelease>-[0-9A-Za-z.-]+)?"
    r"(?:\+[0-9A-Za-z.-]+)?$"
)


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


def remote_url(root: Path, remote: str = "origin") -> str:
    if not remote.strip():
        return ""
    return _git(root, "remote", "get-url", remote, check=False)


def latest_tag(root: Path) -> str | None:
    """Return the latest semantic release tag, falling back to prereleases.

    This is intentionally repository-wide rather than `git describe`, because
    the dashboard wants the most recent release label even when the current
    branch tip is not descended from that tag.
    """
    tags = [line.strip() for line in _git(root, "tag", "--list", check=False).splitlines()]
    stable: list[tuple[tuple[int, int, int], str]] = []
    prerelease: list[tuple[tuple[object, ...], str]] = []
    for tag in tags:
        parsed = _parse_semver_tag(tag)
        if parsed is None:
            continue
        version, pre = parsed
        if pre is None:
            stable.append((version, tag))
        else:
            prerelease.append((version + (_prerelease_key(pre),), tag))

    if stable:
        return max(stable, key=lambda item: item[0])[1]
    if prerelease:
        return max(prerelease, key=lambda item: item[0])[1]
    return None


def _parse_semver_tag(tag: str) -> tuple[tuple[int, int, int], str | None] | None:
    match = _SEMVER_TAG.match(tag)
    if not match:
        return None
    version = (
        int(match.group("major")),
        int(match.group("minor")),
        int(match.group("patch")),
    )
    prerelease = match.group("prerelease")
    return version, prerelease[1:] if prerelease else None


def _prerelease_key(prerelease: str) -> tuple[tuple[int, object], ...]:
    parts: list[tuple[int, object]] = []
    for part in prerelease.split("."):
        if part.isdigit():
            parts.append((0, int(part)))
        else:
            parts.append((1, part))
    return tuple(parts)


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
