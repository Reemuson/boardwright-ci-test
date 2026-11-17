# SPDX-License-Identifier: LicenseRef-Boardwright-Source-Available
# Copyright (c) Ryan Hicks

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from datetime import date

from .changelog import promote_unreleased_file, read_changelog, unreleased_has_content
from .config import BoardwrightConfig
from .errors import BoardwrightError
from .git_ops import current_branch, dirty_files
from .impact import ImpactAssessment, assess_release_impact
from .release_ledger import append_record, load_ledger, verify_ledger_history
from .revision_history import record_document_revision_release, write_revision_variables


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
    remote_tag_state: str = "unchecked"
    remote_error: str = ""
    ledger_has_version: bool = False


def build_release_plan(
    config: BoardwrightConfig,
    version: str,
    check_remote: bool = True,
) -> ReleasePlan:
    _validate_version(version)
    changelog = read_changelog(config.root)
    remote_state, remote_error = (
        remote_tag_state(config, version) if check_remote else ("unchecked", "")
    )
    try:
        ledger_versions = {record.version for record in load_ledger(config.root)}
    except BoardwrightError:
        ledger_versions = set()
    return ReleasePlan(
        version=version,
        branch=current_branch(config.root),
        release_branch=config.release_branch,
        dirty_count=len(dirty_files(config.root)),
        local_tag_exists=_local_tag_exists(config, version),
        remote_tag_exists=remote_state == "exists",
        has_unreleased_changes=unreleased_has_content(changelog),
        remote_tag_state=remote_state,
        remote_error=remote_error,
        ledger_has_version=version in ledger_versions,
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
    if plan.remote_tag_state == "unknown":
        problems.append(
            f"Could not confirm that remote tag {plan.version} is free: {plan.remote_error or 'remote check failed'}"
        )
    if plan.ledger_has_version:
        problems.append(f"Release {plan.version} is already recorded in .boardwright/releases.yaml.")
    return problems


def prepare_release(
    config: BoardwrightConfig,
    version: str,
    allow_dirty: bool = False,
    dry_run: bool = True,
    *,
    drawing_revision: str = "",
    revision_description: str = "",
    document_key: str = "schematic",
    change_classes: tuple[str, ...] | list[str] | str | None = None,
    justification: str = "",
    variant: str = "",
    check_remote: bool = True,
) -> ReleasePlan:
    plan = build_release_plan(config, version, check_remote=check_remote)
    problems = validate_release_plan(plan, allow_dirty=allow_dirty)
    problems.extend(verify_ledger_history(config.root))
    impact = assess_release_impact(
        config,
        version,
        change_classes=change_classes,
        justification=justification or revision_description,
        schematic_revision=drawing_revision if document_key == "schematic" else "",
    )
    problems.extend(impact.problems)
    if problems:
        raise BoardwrightError("; ".join(problems))

    if not dry_run:
        source_commit = _head_commit(config)
        release_date = date.today()
        # Record the ledger first: it validates and refuses duplicates before
        # any other release file is touched.
        append_record(
            config.root,
            impact.record(date=release_date.isoformat(), source_commit=source_commit, variant=variant),
        )
        promote_unreleased_file(config.root, version, release_date, allow_empty=True)
        record_document_revision_release(
            config,
            revision=drawing_revision,
            description=revision_description,
            date=release_date.isoformat(),
            document_key=document_key,
        )
        from .config import load_config

        write_revision_variables(load_config(config.root), document_key)

    return plan


def assess_plan_impact(
    config: BoardwrightConfig,
    version: str,
    *,
    change_classes: tuple[str, ...] | list[str] | str | None = None,
    justification: str = "",
    drawing_revision: str = "",
) -> ImpactAssessment:
    return assess_release_impact(
        config,
        version,
        change_classes=change_classes,
        justification=justification,
        schematic_revision=drawing_revision,
    )


def _head_commit(config: BoardwrightConfig) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=config.root,
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    if completed.returncode != 0:
        raise BoardwrightError(f"Cannot resolve the source commit: {completed.stderr.strip()}")
    return completed.stdout.strip()


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


def remote_tag_state(config: BoardwrightConfig, version: str) -> tuple[str, str]:
    """Return ("exists" | "absent" | "no-remote" | "unknown", error detail).

    ``git ls-remote --exit-code`` returns 2 when no matching ref exists. Any
    other failure (network, auth, missing remote, timeout) is "unknown" and must
    never be treated as confirmation that the tag is free.
    """
    try:
        configured = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=config.root,
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "unknown", str(exc)
    if configured.returncode != 0:
        return "no-remote", "no `origin` remote is configured"
    try:
        completed = subprocess.run(
            ["git", "ls-remote", "--exit-code", "--tags", "origin", f"refs/tags/{version}"],
            cwd=config.root,
            text=True,
            capture_output=True,
            check=False,
            timeout=20,
        )
    except subprocess.TimeoutExpired:
        return "unknown", "git ls-remote timed out"
    except OSError as exc:
        return "unknown", str(exc)
    if completed.returncode == 0 and completed.stdout.strip():
        return "exists", ""
    if completed.returncode == 2:
        return "absent", ""
    detail = (completed.stderr or completed.stdout).strip() or f"exit code {completed.returncode}"
    return "unknown", detail


def _remote_tag_exists(config: BoardwrightConfig, version: str) -> bool:
    """Backward-compatible boolean; raises when the remote cannot be checked."""
    state, detail = remote_tag_state(config, version)
    if state == "unknown":
        raise BoardwrightError(f"Cannot check remote tag {version}: {detail}")
    return state == "exists"
