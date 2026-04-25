from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass

from .config import BoardwrightConfig
from .errors import BoardwrightError
from .git_ops import current_branch
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

    @property
    def command(self) -> tuple[str, ...]:
        args = ["gh", "workflow", "run", self.workflow, "--ref", self.ref]
        if self.repo:
            args.extend(("--repo", self.repo))
        for key, value in self.fields:
            args.extend(("-f", f"{key}={value}"))
        return tuple(args)


def build_preview_action(
    config: BoardwrightConfig,
    variant: str | None = None,
) -> WorkflowAction:
    selected_variant = normalize_variant(variant or config.default_variant)
    return WorkflowAction(
        name="preview",
        workflow=config.preview_workflow,
        ref=current_branch(config.root),
        fields=(("variant", selected_variant),),
        gh_available=_gh_available(),
        repo=config.github_repo,
    )


def build_promote_action(
    config: BoardwrightConfig,
    variant: str,
    commit_outputs: bool = True,
) -> WorkflowAction:
    selected_variant = normalize_variant(variant)
    return WorkflowAction(
        name="promote",
        workflow=config.main_workflow,
        ref=config.release_branch,
        fields=(
            ("variant", selected_variant),
            ("commit_outputs", str(commit_outputs).lower()),
        ),
        gh_available=_gh_available(),
        repo=config.github_repo,
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
        gh_available=_gh_available(),
        repo=config.github_repo,
    )


def dispatch_workflow_action(config: BoardwrightConfig, action: WorkflowAction) -> None:
    if not action.gh_available:
        raise BoardwrightError("GitHub CLI is not installed. Install gh or run the workflow in GitHub.")

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
        raise BoardwrightError(f"GitHub workflow dispatch failed: {message}")


def _gh_available() -> bool:
    return shutil.which("gh") is not None
