from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import BoardwrightConfig
from .errors import BoardwrightError
from .git_ops import current_branch
from .variants import normalize_variant


@dataclass(frozen=True)
class PreviewPlan:
    engine: str
    workflow: str
    branch: str
    preview_branch: str
    variant: str
    output_paths: tuple[Path, ...]
    gh_available: bool


def build_preview_plan(config: BoardwrightConfig, variant: str | None = None) -> PreviewPlan:
    selected_variant = normalize_variant(variant or config.default_variant)
    return PreviewPlan(
        engine=config.preview_engine,
        workflow=config.preview_workflow,
        branch=current_branch(config.root),
        preview_branch=config.preview_branch,
        variant=selected_variant,
        output_paths=expected_output_paths(config.root),
        gh_available=shutil.which("gh") is not None,
    )


def expected_output_paths(root: Path) -> tuple[Path, ...]:
    return tuple(
        root / path
        for path in (
            "Schematic",
            "Manufacturing/Assembly",
            "Manufacturing/Fabrication",
            "Manufacturing/Fabrication/Gerbers",
            "Images",
            "HTML",
            "KiRI",
        )
    )


def dispatch_preview(plan: PreviewPlan, root: Path) -> None:
    if plan.engine != "github-actions":
        raise BoardwrightError(f"Unsupported preview engine: {plan.engine}")
    if not plan.gh_available:
        raise BoardwrightError(
            "GitHub CLI is not installed. Re-run without --dispatch, or install gh."
        )

    workflow_path = root / ".github" / "workflows" / plan.workflow
    if not workflow_path.exists():
        raise BoardwrightError(f"Missing preview workflow: {workflow_path}")

    completed = subprocess.run(
        [
            "gh",
            "workflow",
            "run",
            plan.workflow,
            "--ref",
            plan.branch,
            "-f",
            f"variant={plan.variant}",
        ],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip()
        raise BoardwrightError(f"GitHub workflow dispatch failed: {message}")
