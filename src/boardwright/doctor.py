from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from importlib.util import find_spec
from pathlib import Path

from .config import BoardwrightConfig
from .git_ops import branch_sha, current_branch, git_available, remote_branch_sha
from .validation import validate_project


@dataclass(frozen=True)
class DoctorCheck:
    level: str
    name: str
    message: str


def run_doctor(config: BoardwrightConfig) -> tuple[DoctorCheck, ...]:
    checks: list[DoctorCheck] = []
    root = config.root

    _check_git(config, checks)
    _check_config(config, checks)
    _check_workflows(config, checks)
    _check_github_cli(root, checks)
    _check_textual(checks)

    return tuple(checks)


def format_doctor_report(checks: tuple[DoctorCheck, ...]) -> str:
    if not checks:
        return "No doctor checks ran."
    lines = ["Boardwright Doctor", ""]
    for check in checks:
        lines.append(f"[{check.level.upper()}] {check.name}: {check.message}")
    lines.append("")
    if any(check.level == "error" for check in checks):
        lines.append("Result: blocking issues found.")
    elif any(check.level == "warning" for check in checks):
        lines.append("Result: usable, with warnings.")
    else:
        lines.append("Result: ready.")
    return "\n".join(lines)


def doctor_exit_code(checks: tuple[DoctorCheck, ...]) -> int:
    return 1 if any(check.level == "error" for check in checks) else 0


def _check_git(config: BoardwrightConfig, checks: list[DoctorCheck]) -> None:
    root = config.root
    git = shutil.which("git")
    if git is None:
        checks.append(DoctorCheck("error", "Git", "git is not available on PATH."))
        return
    checks.append(DoctorCheck("ok", "Git", f"found {git}."))

    if not git_available(root):
        checks.append(DoctorCheck("error", "Repository", f"{root} is not a git repository."))
        return
    checks.append(DoctorCheck("ok", "Repository", "git repository detected."))

    branch = current_branch(root)
    expected = {config.dev_branch, config.release_branch}
    if branch in expected:
        checks.append(DoctorCheck("ok", "Branch", f"current branch is {branch}."))
    else:
        checks.append(
            DoctorCheck(
                "warning",
                "Branch",
                f"current branch is {branch}; normal work uses {config.dev_branch}, releases use {config.release_branch}.",
            )
        )

    remotes = _git_lines(root, "remote")
    if "origin" in remotes:
        checks.append(DoctorCheck("ok", "Remote origin", "origin remote exists."))
    else:
        checks.append(DoctorCheck("error", "Remote origin", "origin remote is missing."))

    origin_url = _git(root, "remote", "get-url", "origin", check=False)
    if origin_url:
        checks.append(DoctorCheck("ok", "Remote URL", origin_url))
        if config.github_repo and config.github_repo not in origin_url:
            checks.append(
                DoctorCheck(
                    "warning",
                    "GitHub repo",
                    f"project.yaml uses {config.github_repo}, but origin is {origin_url}.",
                )
            )
    elif config.github_repo:
        checks.append(
            DoctorCheck(
                "warning",
                "GitHub repo",
                f"project.yaml uses {config.github_repo}, but origin URL could not be read.",
            )
        )
    else:
        checks.append(
            DoctorCheck(
                "warning",
                "GitHub repo",
                "project.github_repo is blank; manual GitHub fallback URLs will be limited.",
            )
        )

    _check_branch_ref(root, config.dev_branch, checks)
    _check_branch_ref(root, config.release_branch, checks)


def _check_branch_ref(root: Path, branch: str, checks: list[DoctorCheck]) -> None:
    local = branch_sha(root, branch)
    remote = remote_branch_sha(root, "origin", branch)
    if local:
        checks.append(DoctorCheck("ok", f"Local {branch}", f"{branch} exists locally."))
    else:
        checks.append(DoctorCheck("warning", f"Local {branch}", f"{branch} is not available locally."))
    if remote:
        checks.append(DoctorCheck("ok", f"Origin {branch}", f"origin/{branch} is available."))
    else:
        checks.append(
            DoctorCheck(
                "warning",
                f"Origin {branch}",
                f"origin/{branch} is not available in local refs; run git fetch before live workflow testing.",
            )
        )


def _check_config(config: BoardwrightConfig, checks: list[DoctorCheck]) -> None:
    issues = validate_project(config)
    if not issues:
        checks.append(DoctorCheck("ok", "Validation", "project validation passed."))
        return
    for issue in issues:
        checks.append(DoctorCheck(issue.level, "Validation", issue.message))


def _check_workflows(config: BoardwrightConfig, checks: list[DoctorCheck]) -> None:
    _check_workflow_inputs(config, config.preview_workflow, ("variant", "source_label"), checks)
    _check_workflow_inputs(
        config,
        config.main_workflow,
        ("variant", "commit_outputs", "source_ref", "source_sha", "source_label", "target_branch"),
        checks,
    )
    _check_workflow_inputs(
        config,
        config.prepare_release_workflow,
        ("version", "variant", "release_kind"),
        checks,
    )
    _check_workflow_inputs(config, config.release_workflow, (), checks)


def _check_workflow_inputs(
    config: BoardwrightConfig,
    workflow: str,
    inputs: tuple[str, ...],
    checks: list[DoctorCheck],
) -> None:
    path = config.root / ".github" / "workflows" / workflow
    if not path.exists():
        checks.append(DoctorCheck("warning", f"Workflow {workflow}", "workflow file is missing."))
        return
    text = path.read_text(encoding="utf-8", errors="replace")
    if "workflow_dispatch" not in text:
        checks.append(
            DoctorCheck(
                "warning",
                f"Workflow {workflow}",
                "workflow_dispatch is missing; Boardwright cannot dispatch it manually.",
            )
        )
        return
    missing = tuple(name for name in inputs if f"{name}:" not in text)
    if missing:
        checks.append(
            DoctorCheck(
                "warning",
                f"Workflow {workflow}",
                "missing expected dispatch input(s): " + ", ".join(missing),
            )
        )
        return
    checks.append(DoctorCheck("ok", f"Workflow {workflow}", "dispatch shape looks usable."))


def _check_github_cli(root: Path, checks: list[DoctorCheck]) -> None:
    gh = shutil.which("gh")
    if gh is None:
        checks.append(
            DoctorCheck(
                "warning",
                "GitHub CLI",
                "gh is not installed; dispatch/review commands will show manual fallback steps.",
            )
        )
        return
    checks.append(DoctorCheck("ok", "GitHub CLI", f"found {gh}."))

    try:
        completed = subprocess.run(
            [gh, "auth", "status"],
            cwd=root,
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        checks.append(DoctorCheck("warning", "GitHub auth", "gh auth status timed out."))
        return
    if completed.returncode == 0:
        checks.append(DoctorCheck("ok", "GitHub auth", "gh auth status passed."))
    else:
        message = (completed.stderr or completed.stdout).strip().splitlines()
        detail = message[0] if message else "gh auth status did not pass."
        checks.append(DoctorCheck("warning", "GitHub auth", detail))


def _check_textual(checks: list[DoctorCheck]) -> None:
    if find_spec("textual") is None:
        checks.append(
            DoctorCheck(
                "warning",
                "Textual",
                'Textual is not installed; install the TUI with pip install -e ".[tui]".',
            )
        )
    else:
        checks.append(DoctorCheck("ok", "Textual", "TUI dependency is installed."))


def _git_lines(root: Path, *args: str) -> tuple[str, ...]:
    return tuple(line for line in _git(root, *args, check=False).splitlines() if line.strip())


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
