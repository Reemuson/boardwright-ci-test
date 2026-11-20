# SPDX-License-Identifier: LicenseRef-Boardwright-Source-Available
# Copyright (c) Ryan Hicks

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from dataclasses import dataclass, replace
from datetime import date, datetime

from .changelog import promote_unreleased_file, read_changelog, unreleased_has_content
from .config import BoardwrightConfig
from .errors import BoardwrightError
from .git_ops import current_branch, dirty_files
from .impact import ImpactAssessment, assess_release_impact
from .release_ledger import append_record, load_ledger, verify_ledger_history
from .config import update_project_config
from .drawing_revisions import RevisionHistoryPlan, apply_revision_history, plan_revision_history
from .impact import proposed_identities
from .release_ledger import latest_baseline
from .revision_history import write_revision_variables
from .release_date import resolve_release_date, utc_today


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
    release_date: str = ""


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
    release_date: str = "",
    now: datetime | None = None,
) -> ReleasePlan:
    """Prepare the release state; ``release_date`` (YYYY-MM-DD) defaults to the UTC date now.

    The date is fixed once and used for the ledger, the CHANGELOG heading and
    the drawings (through ``release.env``); see ``release_date``.
    """
    fixed_date = resolve_release_date(release_date, now)
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
    release_date = fixed_date
    history = plan_release_revision_history(
        config,
        drawing_revision=drawing_revision if document_key == "schematic" else "",
        revision_description=revision_description,
        release_date=release_date.isoformat(),
    )
    problems.extend(history.problems)
    if problems:
        raise BoardwrightError("; ".join(problems))

    if not dry_run:
        source_commit = _head_commit(config)
        # Record the ledger first: it validates and refuses duplicates before
        # any other release file is touched.
        append_record(
            config.root,
            impact.record(date=release_date.isoformat(), source_commit=source_commit, variant=variant),
        )
        promote_unreleased_file(config.root, version, release_date, allow_empty=True)
        # Drawing revision tables change only for documents whose revision
        # advances; issued rows are never rewritten.
        apply_revision_history(config, history)
        schematic = next(item for item in history.documents if item.document == "schematic")
        if schematic.changed:
            settings = dict(config.document_settings("schematic"))
            settings["revision"] = schematic.proposed_revision
            update_project_config(config, document_fields={"schematic": settings})
        from .config import load_config

        write_revision_variables(load_config(config.root, local=False), document_key)

    return replace(plan, release_date=fixed_date.isoformat())


def plan_release_revision_history(
    config: BoardwrightConfig,
    *,
    drawing_revision: str = "",
    revision_description: str = "",
    release_date: str = "",
) -> RevisionHistoryPlan:
    """Plan every drawing's revision rows against the previous release."""

    base = latest_baseline(config.root)
    proposed = proposed_identities(config, drawing_revision)
    documents = ("schematic", "fabrication", "assembly")
    previous = {name: getattr(base, f"{name}_revision", "") if base else "" for name in documents}
    return plan_revision_history(
        config,
        previous_version=base.version if base else "",
        previous=previous,
        proposed={name: getattr(proposed, f"{name}_revision") for name in documents},
        description=revision_description,
        date=release_date or utc_today().isoformat(),
    )


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
    pinned = os.environ.get("BOARDWRIGHT_REMOTE_REFS", "").strip()
    if pinned:
        return _pinned_tag_state(config.root / pinned, configured.stdout.strip(), version)
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


def _pinned_tag_state(path: Path, origin_url: str, version: str) -> tuple[str, str]:
    """Remote-tag state from a snapshot taken on the host (``container candidate``).

    The snapshot must be for the configured ``origin``; anything unreadable or
    inconsistent is "unknown", never "absent".
    """

    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return "unknown", f"pinned remote references unreadable: {exc}"
    if not isinstance(snapshot, dict) or snapshot.get("schema") != 1 or not isinstance(snapshot.get("tags"), dict):
        return "unknown", "pinned remote references have an unsupported format"
    if snapshot.get("url") != origin_url:
        return "unknown", f"pinned remote references are for {snapshot.get('url')!r}, not origin {origin_url!r}"
    return ("exists", "") if version in snapshot["tags"] else ("absent", "")


def _remote_tag_exists(config: BoardwrightConfig, version: str) -> bool:
    """Backward-compatible boolean; raises when the remote cannot be checked."""
    state, detail = remote_tag_state(config, version)
    if state == "unknown":
        raise BoardwrightError(f"Cannot check remote tag {version}: {detail}")
    return state == "exists"
