# SPDX-License-Identifier: LicenseRef-Boardwright-Source-Available
# Copyright (c) Ryan Hicks

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import BoardwrightConfig, _normalize_github_repo_slug, update_project_config
from .errors import BoardwrightError
from .git_ops import remote_url


@dataclass(frozen=True)
class RepositorySetupResult:
    messages: tuple[str, ...]
    remote_url: str = ""
    github_repo: str = ""
    initialized_git: bool = False
    configured_origin: bool = False


def setup_project_repository(
    config: BoardwrightConfig,
    remote: str = "",
    *,
    git: bool = True,
) -> RepositorySetupResult:
    """Initialize local Git and repository metadata for a Boardwright project."""
    messages: list[str] = []
    initialized_git = False
    configured_origin = False

    if git and not (config.root / ".git").exists():
        _git(config.root, "init", "-b", config.release_branch)
        initialized_git = True
        messages.append(f"Initialized git repository on {config.release_branch}.")

    normalized_remote = normalize_remote_url(remote)
    github_repo = _normalize_github_repo_slug(normalized_remote or remote)

    if normalized_remote and git and (config.root / ".git").exists():
        existing_remote = remote_url(config.root)
        if existing_remote:
            if existing_remote != normalized_remote:
                _git(config.root, "remote", "set-url", "origin", normalized_remote)
                messages.append("Updated origin remote.")
                configured_origin = True
        else:
            _git(config.root, "remote", "add", "origin", normalized_remote)
            messages.append("Configured origin remote.")
            configured_origin = True

    updates: dict[str, str] = {}
    if normalized_remote:
        current_git_url = str(config.project.get("project", {}).get("git_url", "")).strip()
        if current_git_url != normalized_remote:
            updates["git_url"] = normalized_remote
    if updates:
        update_project_config(config, project_fields=updates)
        messages.append("Updated Boardwright repository metadata.")

    if not messages:
        messages.append("Repository setup already up to date.")

    return RepositorySetupResult(
        messages=tuple(messages),
        remote_url=normalized_remote,
        github_repo=github_repo,
        initialized_git=initialized_git,
        configured_origin=configured_origin,
    )


def normalize_remote_url(value: str) -> str:
    text = value.strip()
    if not text:
        return ""
    if text.startswith(("https://", "http://", "git@", "ssh://")):
        return text
    slug = _normalize_github_repo_slug(text)
    if slug.count("/") == 1:
        return f"https://github.com/{slug}.git"
    raise BoardwrightError("Remote must be a GitHub URL or OWNER/repo slug.")


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip()
        raise BoardwrightError(f"git {' '.join(args)} failed: {message}")
    return completed.stdout.strip()
