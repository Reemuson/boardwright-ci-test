from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from datetime import date

from .changelog import promote_unreleased_file, read_changelog, unreleased_has_content
from .config import BoardwrightConfig
from .errors import BoardwrightError
from .git_ops import current_branch, dirty_files
from .revision_history import write_revision_variables


SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")


@dataclass(frozen=True)
class ReleasePlan:
    version: str
    branch: str
    release_branch: str
    dirty_count: int
    local_tag_exists: bool
    remote_tag_exists: bool
    has_unreleased_changes: bool


def build_release_plan(
    config: BoardwrightConfig,
    version: str,
    check_remote: bool = True,
) -> ReleasePlan:
    _validate_version(version)
    changelog = read_changelog(config.root)
    return ReleasePlan(
        version=version,
        branch=current_branch(config.root),
        release_branch=config.release_branch,
        dirty_count=len(dirty_files(config.root)),
        local_tag_exists=_local_tag_exists(config, version),
        remote_tag_exists=_remote_tag_exists(config, version) if check_remote else False,
        has_unreleased_changes=unreleased_has_content(changelog),
    )


def validate_release_plan(plan: ReleasePlan, allow_dirty: bool = False) -> list[str]:
    problems: list[str] = []
    if plan.branch != plan.release_branch:
        problems.append(
            f"Current branch is {plan.branch}; expected release branch {plan.release_branch}."
        )
    if plan.dirty_count and not allow_dirty:
        problems.append("Working tree is dirty.")
    if plan.local_tag_exists:
        problems.append(f"Local tag already exists: {plan.version}")
    if plan.remote_tag_exists:
        problems.append(f"Remote tag already exists: {plan.version}")
    if not plan.has_unreleased_changes:
        problems.append("CHANGELOG.md has no unreleased changes.")
    return problems


def prepare_release(
    config: BoardwrightConfig,
    version: str,
    allow_dirty: bool = False,
    dry_run: bool = True,
) -> ReleasePlan:
    plan = build_release_plan(config, version)
    problems = validate_release_plan(plan, allow_dirty=allow_dirty)
    if problems:
        raise BoardwrightError("; ".join(problems))

    if not dry_run:
        promote_unreleased_file(config.root, version, date.today())
        write_revision_variables(config)

    return plan


def _validate_version(version: str) -> None:
    if not SEMVER_RE.match(version):
        raise BoardwrightError("Release version must use semantic form like 0.1.0.")


def _local_tag_exists(config: BoardwrightConfig, version: str) -> bool:
    completed = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/tags/{version}"],
        cwd=config.root,
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )
    return completed.returncode == 0


def _remote_tag_exists(config: BoardwrightConfig, version: str) -> bool:
    try:
        completed = subprocess.run(
            ["git", "ls-remote", "--tags", "origin", f"refs/tags/{version}"],
            cwd=config.root,
            text=True,
            capture_output=True,
            check=False,
            timeout=5,
        )
    except subprocess.TimeoutExpired:
        return False
    return bool(completed.stdout.strip())
