from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import BoardwrightConfig
from .errors import BoardwrightError
from .git_ops import branch_sha, current_branch, remote_branch_sha
from .release import _validate_version
from .variants import normalize_variant


RELEASE_KINDS = ("draft", "prerelease", "release")


@dataclass(frozen=True)
class WorkflowAction:
    name: str
    workflow: str
    ref: str
    fields: tuple[tuple[str, str], ...]
    gh_available: bool
    repo: str = ""
    gh_command: str = "gh"

    @property
    def command(self) -> tuple[str, ...]:
        args = [self.gh_command, "workflow", "run", self.workflow, "--ref", self.ref]
        if self.repo:
            args.extend(("--repo", self.repo))
        for key, value in self.fields:
            args.extend(("-f", f"{key}={value}"))
        return tuple(args)

    @property
    def manual_fallback(self) -> str:
        lines = [
            "GitHub CLI is unavailable or could not complete the request.",
            "",
            "Manual fallback:",
            f"Open GitHub Actions -> {self.workflow} -> Run workflow",
            f"Ref: {self.ref}",
        ]
        if self.repo:
            lines.append(f"Repository: {self.repo}")
        if self.fields:
            lines.append("Inputs:")
            lines.extend(f"- {key}: {value}" for key, value in self.fields)
        lines.extend(("", "Equivalent gh command:", " ".join(self.command)))
        return "\n".join(lines)


@dataclass(frozen=True)
class WorkflowRunStatus:
    workflow: str
    status: str
    conclusion: str
    branch: str
    title: str
    database_id: str


def build_preview_action(
    config: BoardwrightConfig,
    variant: str | None = None,
) -> WorkflowAction:
    selected_variant = normalize_variant(variant or config.preview_variant)
    selected_ref = current_branch(config.root)
    return WorkflowAction(
        name="preview",
        workflow=config.preview_workflow,
        ref=selected_ref,
        fields=(
            ("variant", selected_variant),
            ("source_label", build_source_label(config.root, selected_ref)),
        ),
        gh_available=_gh_command() is not None,
        repo=config.github_repo,
        gh_command=_gh_command() or "gh",
    )


def build_promote_action(
    config: BoardwrightConfig,
    variant: str,
    commit_outputs: bool = True,
    source_ref: str | None = None,
    source_sha: str | None = None,
) -> WorkflowAction:
    selected_variant = normalize_variant(variant)
    selected_source_ref = (source_ref or config.dev_branch).strip()
    selected_source_sha = (source_sha or remote_branch_sha(config.root, "origin", selected_source_ref)).strip()
    selected_source_label = build_source_label(config.root, selected_source_ref, selected_source_sha)
    return WorkflowAction(
        name="promote",
        workflow=config.main_workflow,
        ref=selected_source_ref,
        fields=(
            ("variant", selected_variant),
            ("commit_outputs", str(commit_outputs).lower()),
            ("source_ref", selected_source_ref),
            ("source_sha", selected_source_sha),
            ("source_label", selected_source_label),
            ("target_branch", config.release_branch),
        ),
        gh_available=_gh_command() is not None,
        repo=config.github_repo,
        gh_command=_gh_command() or "gh",
    )


def build_prepare_release_action(
    config: BoardwrightConfig,
    version: str,
    variant: str,
    release_kind: str,
) -> WorkflowAction:
    _validate_version(version)
    selected_variant = normalize_variant(variant)
    selected_kind = release_kind.lower().strip()
    if selected_kind not in RELEASE_KINDS:
        allowed = ", ".join(RELEASE_KINDS)
        raise BoardwrightError(f"Unsupported release kind '{release_kind}'. Use one of: {allowed}.")

    return WorkflowAction(
        name="prepare-release",
        workflow=config.prepare_release_workflow,
        ref=config.release_branch,
        fields=(
            ("version", version),
            ("variant", selected_variant),
            ("release_kind", selected_kind),
        ),
        gh_available=_gh_command() is not None,
        repo=config.github_repo,
        gh_command=_gh_command() or "gh",
    )


def build_source_label(root: Path, ref: str, sha: str = "") -> str:
    clean_ref = (ref or "").strip()
    clean_sha = (sha or branch_sha(root, "HEAD")).strip()
    short_sha = clean_sha[:12]
    if clean_ref and short_sha:
        return f"{clean_ref}@{short_sha}"
    return clean_ref or short_sha


def dispatch_workflow_action(config: BoardwrightConfig, action: WorkflowAction) -> None:
    if not action.gh_available:
        raise BoardwrightError(action.manual_fallback)

    workflow_path = config.root / ".github" / "workflows" / action.workflow
    if not workflow_path.exists():
        raise BoardwrightError(f"Missing workflow: {workflow_path}")

    completed = subprocess.run(
        list(action.command),
        cwd=config.root,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip()
        raise BoardwrightError(
            f"GitHub workflow dispatch failed: {message}\n\n{action.manual_fallback}"
        )


def list_recent_workflow_runs(
    config: BoardwrightConfig,
    limit: int = 5,
) -> tuple[WorkflowRunStatus, ...]:
    gh = _gh_command()
    if gh is None:
        raise BoardwrightError("GitHub CLI is not installed. Install gh to poll CI.")

    command = [
        gh,
        "run",
        "list",
        "--limit",
        str(limit),
        "--json",
        "databaseId,workflowName,status,conclusion,headBranch,displayTitle",
    ]
    if config.github_repo:
        command.extend(("--repo", config.github_repo))

    completed = subprocess.run(
        command,
        cwd=config.root,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip()
        raise BoardwrightError(f"Could not poll GitHub Actions: {message}")

    try:
        runs = json.loads(completed.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise BoardwrightError(f"Could not parse GitHub Actions status: {exc}") from exc

    parsed: list[WorkflowRunStatus] = []
    for run in runs:
        parsed.append(
            WorkflowRunStatus(
                workflow=str(run.get("workflowName") or "unknown"),
                status=str(run.get("status") or "unknown"),
                conclusion=str(run.get("conclusion") or ""),
                branch=str(run.get("headBranch") or ""),
                title=str(run.get("displayTitle") or ""),
                database_id=str(run.get("databaseId") or ""),
            )
        )
    return tuple(parsed)


def _gh_command() -> str | None:
    discovered = shutil.which("gh")
    if discovered:
        return discovered

    for candidate in (
        Path("C:/Program Files/GitHub CLI/gh.exe"),
        Path.home() / "AppData/Local/Programs/GitHub CLI/gh.exe",
    ):
        if candidate.exists():
            return str(candidate)
    return None
