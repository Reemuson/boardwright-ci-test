from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import BoardwrightConfig
from .errors import BoardwrightError


EXCLUDED_NAMES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "boardwright-preview",
    "boardwright-release",
    "HTML",
    "KiRI",
    "Manufacturing",
    "Reports",
    "Schematic",
    "Testing",
    "build",
    "dist",
}


@dataclass(frozen=True)
class TestbenchPlan:
    source: Path
    target: Path
    github_repo: str
    commands: tuple[str, ...]
    notes: tuple[str, ...]


def default_testbench_target(config: BoardwrightConfig) -> Path:
    return config.root.parent / f"{config.root.name}-testbench"


def build_testbench_plan(
    config: BoardwrightConfig,
    target: Path | None = None,
    github_repo: str = "",
) -> TestbenchPlan:
    selected_target = (target or default_testbench_target(config)).resolve()
    selected_repo = github_repo.strip()
    remote_note = selected_repo or "OWNER/boardwright-live-test"
    init_command = f"boardwright testbench init --target {selected_target}"
    if selected_repo:
        init_command += f" --github-repo {selected_repo}"
    commands = (
        init_command,
        f"cd {selected_target}",
        "python -m boardwright doctor",
        "git checkout dev",
        'python -m boardwright change "Live test preview path" --section Changed --suggest-commit',
        "git add -A",
        'git commit -m "test: exercise boardwright preview path"',
        "git push -u origin dev",
        "python -m boardwright preview --variant PRELIMINARY --dispatch",
        "python -m boardwright review --variant PRELIMINARY --fetch",
        "python -m boardwright promote --variant PRELIMINARY --dispatch",
        "python -m boardwright accepted",
        "python -m boardwright release 0.1.0 --variant PRELIMINARY --kind draft --dispatch",
    )
    notes = (
        "Create an empty GitHub repository before pushing, or create it with gh repo create.",
        f"Use repository slug: {remote_note}",
        "Run live tests from the testbench repo, not the template repo.",
        "Use PRELIMINARY for harness testing because the template board is not a fabrication-ready design.",
        "Use draft releases first; delete the test repository when the harness is proven.",
    )
    return TestbenchPlan(config.root.resolve(), selected_target, selected_repo, commands, notes)


def format_testbench_plan(plan: TestbenchPlan) -> str:
    lines = [
        "Boardwright Live Testbench Plan",
        "",
        f"Source: {plan.source}",
        f"Target: {plan.target}",
        f"GitHub repo: {plan.github_repo or '(set during init or manually)'}",
        "",
        "Notes:",
    ]
    lines.extend(f"- {note}" for note in plan.notes)
    lines.extend(("", "Command sequence:"))
    lines.extend(f"{index}. {command}" for index, command in enumerate(plan.commands, 1))
    return "\n".join(lines)


def init_testbench(
    config: BoardwrightConfig,
    target: Path | None = None,
    github_repo: str = "",
    force: bool = False,
    git_init: bool = True,
) -> tuple[str, ...]:
    selected_target = (target or default_testbench_target(config)).resolve()
    source = config.root.resolve()
    if selected_target == source or source in selected_target.parents:
        raise BoardwrightError("Testbench target must be outside the source template tree.")
    if selected_target.exists():
        if not force:
            raise BoardwrightError(f"Testbench target already exists: {selected_target}")
        shutil.rmtree(selected_target)

    shutil.copytree(source, selected_target, ignore=_ignore_template_artifacts)
    _rewrite_project_yaml(selected_target, github_repo.strip())

    messages = [f"Copied Boardwright template to {selected_target}."]
    if git_init:
        _git(selected_target, "init")
        _git(selected_target, "checkout", "-b", config.release_branch)
        _git(selected_target, "config", "user.name", "boardwright-testbench")
        _git(selected_target, "config", "user.email", "boardwright-testbench@example.invalid")
        _git(selected_target, "add", "-A")
        messages.append(_git(selected_target, "commit", "-m", "test: initialize boardwright live testbench"))
        _git(selected_target, "branch", config.dev_branch)
        if github_repo.strip():
            _git(
                selected_target,
                "remote",
                "add",
                "origin",
                f"https://github.com/{github_repo.strip()}.git",
            )
            messages.append(f"Configured origin for {github_repo.strip()}.")
        messages.append(
            f"Initialized branches {config.release_branch} and {config.dev_branch} in the testbench repo."
        )
    messages.append("Next: create/push the remote repo, then run boardwright doctor inside the testbench.")
    return tuple(messages)


def _ignore_template_artifacts(directory: str, names: list[str]) -> set[str]:
    ignored = {name for name in names if name in EXCLUDED_NAMES}
    ignored.update(name for name in names if name.endswith(".egg-info"))
    ignored.update(name for name in names if name.startswith("kibot") and name.endswith(".log"))
    ignored.update(name for name in names if name.startswith("boardwright-") and name.endswith("-release.zip"))
    return ignored


def _rewrite_project_yaml(target: Path, github_repo: str) -> None:
    if not github_repo:
        return
    path = target / ".boardwright" / "project.yaml"
    text = path.read_text(encoding="utf-8")
    lines = []
    replaced = False
    for line in text.splitlines():
        if line.strip().startswith("github_repo:"):
            indent = line[: len(line) - len(line.lstrip())]
            lines.append(f'{indent}github_repo: "{github_repo}"')
            replaced = True
        else:
            lines.append(line)
    if not replaced:
        raise BoardwrightError("Could not find project.github_repo in copied project.yaml.")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def _git(root: Path, *args: str, check: bool = True) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    output = completed.stdout.strip() or completed.stderr.strip()
    if check and completed.returncode != 0:
        raise BoardwrightError(output or f"git {' '.join(args)} failed.")
    return output
