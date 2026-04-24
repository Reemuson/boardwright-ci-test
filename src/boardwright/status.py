from __future__ import annotations

from dataclasses import dataclass

from . import git_ops
from .changelog import read_changelog, unreleased_has_content
from .config import BoardwrightConfig


@dataclass(frozen=True)
class ProjectStatus:
    project_id: str
    project_name: str
    branch: str
    dirty_count: int
    latest_tag: str | None
    unreleased_changes: bool
    variant: str


def collect_status(config: BoardwrightConfig) -> ProjectStatus:
    dirty = git_ops.dirty_files(config.root)
    try:
        changelog_text = read_changelog(config.root)
        has_unreleased = unreleased_has_content(changelog_text)
    except Exception:
        has_unreleased = False

    return ProjectStatus(
        project_id=config.project_id,
        project_name=config.project_name,
        branch=git_ops.current_branch(config.root),
        dirty_count=len(dirty),
        latest_tag=git_ops.latest_tag(config.root),
        unreleased_changes=has_unreleased,
        variant=config.default_variant,
    )
