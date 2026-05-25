from __future__ import annotations

from dataclasses import dataclass

from .accepted import AcceptedMainState
from .config import BoardwrightConfig
from .preview import PreviewState
from .status import ProjectStatus
from .validation import ValidationIssue


@dataclass(frozen=True)
class WorkflowStep:
    label: str
    state: str
    detail: str


@dataclass(frozen=True)
class CockpitAction:
    name: str
    enabled: bool
    reason: str


@dataclass(frozen=True)
class WorkflowState:
    stage: str
    next_action: str
    reason: str
    steps: tuple[WorkflowStep, ...]
    actions: tuple[CockpitAction, ...]


def build_workflow_state(
    config: BoardwrightConfig,
    status: ProjectStatus,
    issues: tuple[ValidationIssue, ...],
    release_summary: str,
    preview_state: PreviewState | None = None,
    accepted_state: AcceptedMainState | None = None,
) -> WorkflowState:
    has_errors = any(issue.level == "error" for issue in issues)
    dirty = status.dirty_count > 0
    needs_push = status.ahead > 0
    wrong_dev_branch = status.branch != config.dev_branch

    if has_errors:
        stage = "validation_blocked"
        next_action = "Fix validation"
        reason = "Validation has blocking errors."
    elif dirty and not status.unreleased_changes:
        stage = "needs_changelog"
        next_action = "Record Changes"
        reason = "Source files changed without a changelog entry."
    elif dirty:
        stage = "ready_to_commit"
        next_action = "Commit + Push"
        reason = "Changelog exists and project changes are pending."
    elif needs_push:
        stage = "needs_push"
        next_action = "Commit + Push"
        reason = "Local commits still need to reach origin/dev."
    elif status.behind:
        stage = "behind_remote"
        next_action = "Refresh branch"
        reason = "Local branch is behind its upstream."
    elif preview_state is not None:
        stage, next_action, reason = _preview_stage(preview_state, release_summary, accepted_state)
    elif status.unreleased_changes:
        stage = "preview_missing"
        next_action = "Review Artifacts"
        reason = "Preview should run after dev is pushed."
    else:
        stage = "editing"
        next_action = "Continue editing"
        reason = "No pending local workflow action is required."

    steps = _workflow_steps(
        config,
        status,
        issues,
        release_summary,
        preview_state,
        accepted_state,
        stage,
    )
    actions = _actions(
        config,
        status,
        issues,
        release_summary,
        preview_state,
        accepted_state,
        wrong_dev_branch,
    )
    return WorkflowState(stage, next_action, reason, steps, actions)


def action_state(workflow: WorkflowState, name: str) -> CockpitAction:
    for action in workflow.actions:
        if action.name == name:
            return action
    return CockpitAction(name, False, "Unknown action.")


def _preview_stage(
    preview_state: PreviewState,
    release_summary: str,
    accepted_state: AcceptedMainState | None,
) -> tuple[str, str, str]:
    if preview_state.state == "running":
        return ("preview_running", "Wait for preview", preview_state.message)
    if preview_state.state == "failed":
        return ("preview_failed", "Review Artifacts", preview_state.message)
    if preview_state.state == "stale":
        return ("preview_stale", "Review Artifacts", preview_state.message)
    if preview_state.state == "missing":
        return ("preview_missing", "Review Artifacts", preview_state.message)
    if preview_state.ready and not preview_state.reviewed:
        return ("preview_ready", "Review Artifacts", "Fresh preview is ready to review.")
    if preview_state.ready and preview_state.reviewed:
        if accepted_state is None:
            return ("preview_reviewed", "Accept to Main", "Fresh preview has been reviewed.")
        if accepted_state.state == "running":
            return ("accepted_running", "Wait for accepted outputs", accepted_state.message)
        if not accepted_state.ready:
            return ("accepted_missing", "Accept to Main", accepted_state.message)
        if _release_ready(release_summary):
            return ("release_ready", "Create Release", "Accepted main outputs are ready for release.")
        return ("preview_reviewed", "Accept to Main", "Fresh preview has been reviewed.")
    return ("preview_missing", "Review Artifacts", "Preview state is unknown.")


def _workflow_steps(
    config: BoardwrightConfig,
    status: ProjectStatus,
    issues: tuple[ValidationIssue, ...],
    release_summary: str,
    preview_state: PreviewState | None,
    accepted_state: AcceptedMainState | None,
    stage: str,
) -> tuple[WorkflowStep, ...]:
    has_errors = any(issue.level == "error" for issue in issues)
    dirty = status.dirty_count > 0
    needs_push = status.ahead > 0

    record_state = "done" if status.unreleased_changes else "ready"
    commit_state = "needed" if dirty or needs_push else "done"
    preview_step_state = _preview_step_state(has_errors, dirty, needs_push, preview_state)
    review_state = _review_step_state(preview_state)
    accept_state = _accept_step_state(has_errors, dirty, needs_push, preview_state)
    release_state = "ready" if stage == "release_ready" else "locked"

    return (
        WorkflowStep(
            "1 Edit in KiCad",
            "external",
            "Make schematic, PCB, BOM, or documentation changes in KiCad/files.",
        ),
        WorkflowStep(
            "2 Record changes",
            record_state,
            "CHANGELOG.md has unreleased entries."
            if status.unreleased_changes
            else "Add the next design/change note.",
        ),
        WorkflowStep(
            "3 Commit + push",
            commit_state,
            _commit_push_detail(status),
        ),
        WorkflowStep(
            "4 Preview CI",
            preview_step_state,
            "Dispatch preview when dev is pushed and ready to review.",
        ),
        WorkflowStep(
            "5 Review artifacts",
            review_state,
            _review_detail(preview_state),
        ),
        WorkflowStep(
            "6 Accept to main",
            accept_state,
            _accepted_detail(config, accepted_state),
        ),
        WorkflowStep(
            "7 Create release",
            release_state,
            release_summary,
        ),
    )


def _actions(
    config: BoardwrightConfig,
    status: ProjectStatus,
    issues: tuple[ValidationIssue, ...],
    release_summary: str,
    preview_state: PreviewState | None,
    accepted_state: AcceptedMainState | None,
    wrong_dev_branch: bool,
) -> tuple[CockpitAction, ...]:
    has_errors = any(issue.level == "error" for issue in issues)
    dirty = status.dirty_count > 0
    needs_push = status.ahead > 0
    clean_pushed = not has_errors and not dirty and not needs_push
    preview_reviewed = bool(preview_state and preview_state.ready and preview_state.reviewed)
    return (
        CockpitAction("Record Changes", not has_errors, "Record a changelog entry."),
        CockpitAction(
            "Commit + Push",
            not has_errors and not wrong_dev_branch and (dirty or needs_push),
            _commit_action_reason(config, status, has_errors, wrong_dev_branch, dirty, needs_push),
        ),
        CockpitAction(
            "Generate Preview",
            clean_pushed and not wrong_dev_branch,
            "Dispatch preview CI for a selected variant."
            if clean_pushed and not wrong_dev_branch
            else "Commit and push dev before generating preview.",
        ),
        CockpitAction(
            "Review Artifacts",
            clean_pushed,
            "Review or fetch preview artifacts." if clean_pushed else "Commit and push before reviewing artifacts.",
        ),
        CockpitAction(
            "Accept to Main",
            clean_pushed and preview_reviewed,
            "Fresh reviewed preview is available." if preview_reviewed else "Review a fresh preview first.",
        ),
        CockpitAction(
            "Create Release",
            clean_pushed,
            "Open the release checklist."
            if clean_pushed
            else "Commit and push before checking release readiness.",
        ),
        CockpitAction("Project Info", True, "Edit project metadata and variant defaults."),
        CockpitAction("Refresh", True, "Refresh project state."),
    )


def _preview_step_state(
    has_errors: bool,
    dirty: bool,
    needs_push: bool,
    preview_state: PreviewState | None,
) -> str:
    if has_errors:
        return "blocked"
    if dirty or needs_push:
        return "waiting"
    if preview_state is None:
        return "ready"
    if preview_state.state == "ready":
        return "passed"
    if preview_state.state == "running":
        return "running"
    if preview_state.state in {"failed", "stale", "missing"}:
        return preview_state.state
    return "waiting"


def _review_step_state(preview_state: PreviewState | None) -> str:
    if preview_state is None:
        return "waiting"
    if preview_state.ready and preview_state.reviewed:
        return "done"
    if preview_state.ready:
        return "ready"
    if preview_state.state in {"failed", "stale", "missing"}:
        return "blocked"
    return "waiting"


def _accept_step_state(
    has_errors: bool,
    dirty: bool,
    needs_push: bool,
    preview_state: PreviewState | None,
) -> str:
    if has_errors or dirty or needs_push:
        return "locked"
    if preview_state and preview_state.ready and preview_state.reviewed:
        return "ready"
    return "locked"


def _commit_push_detail(status: ProjectStatus) -> str:
    if status.dirty_count:
        return f"{status.dirty_count} changed file(s) need commit and push."
    if status.ahead:
        return f"{status.ahead} local commit(s) need push."
    return "Working tree is clean and no local commits are waiting."


def _review_detail(preview_state: PreviewState | None) -> str:
    if preview_state is None:
        return "Poll CI, fetch preview artifacts, inspect generated outputs."
    return preview_state.message or f"Preview is {preview_state.state}."


def _accepted_detail(
    config: BoardwrightConfig,
    accepted_state: AcceptedMainState | None,
) -> str:
    if accepted_state is None:
        return f"{config.main_workflow} | {config.main_variant} | ref {config.release_branch}"
    return accepted_state.message or f"Accepted outputs are {accepted_state.state}."


def _commit_action_reason(
    config: BoardwrightConfig,
    status: ProjectStatus,
    has_errors: bool,
    wrong_dev_branch: bool,
    dirty: bool,
    needs_push: bool,
) -> str:
    if has_errors:
        return "Fix validation errors first."
    if wrong_dev_branch:
        return f"Switch to {config.dev_branch} first."
    if dirty:
        return "Commit changed files and push dev."
    if needs_push:
        return "Push local commits to origin/dev."
    return "No local changes or commits need pushing."


def _release_ready(release_summary: str) -> bool:
    return release_summary.strip().lower() == "ready for dry-run"


def _release_action_reason(
    accepted_state: AcceptedMainState | None,
    release_summary: str,
) -> str:
    if accepted_state is None:
        return "Refresh accepted main evidence first."
    if not accepted_state.ready:
        return accepted_state.message or "Accepted main outputs are not ready."
    return release_summary
