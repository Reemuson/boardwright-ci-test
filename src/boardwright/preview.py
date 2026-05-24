from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path

from .actions import _gh_command
from .config import BoardwrightConfig
from .errors import BoardwrightError
from .git_ops import current_branch, remote_branch_sha
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
    gh_command: str = "gh"


@dataclass(frozen=True)
class PreviewRun:
    database_id: str
    status: str
    conclusion: str
    branch: str
    head_sha: str
    created_at: str
    title: str


@dataclass(frozen=True)
class PreviewArtifact:
    name: str
    size_in_bytes: int = 0
    expired: bool = False


@dataclass(frozen=True)
class PreviewState:
    state: str
    artifact_name: str
    expected_sha: str
    run: PreviewRun | None = None
    message: str = ""
    reviewed: bool = False

    @property
    def ready(self) -> bool:
        return self.state == "ready"


def build_preview_plan(config: BoardwrightConfig, variant: str | None = None) -> PreviewPlan:
    selected_variant = normalize_variant(variant or config.preview_variant)
    return PreviewPlan(
        engine=config.preview_engine,
        workflow=config.preview_workflow,
        branch=current_branch(config.root),
        preview_branch=config.preview_branch,
        variant=selected_variant,
        output_paths=expected_output_paths(config.root),
        gh_available=_gh_command() is not None,
        gh_command=_gh_command() or "gh",
    )


def expected_output_paths(root: Path) -> tuple[Path, ...]:
    return tuple(
        root / path
        for path in (
            "Schematic",
            "Manufacturing/Assembly",
            "Manufacturing/Fabrication",
            "Manufacturing/Fabrication/Gerbers",
            "assets/renders",
            "assets/3d",
            "HTML",
            "KiRI",
        )
    )


def dispatch_preview(plan: PreviewPlan, root: Path) -> None:
    if plan.engine != "github-actions":
        raise BoardwrightError(f"Unsupported preview engine: {plan.engine}")
    if not plan.gh_available:
        raise BoardwrightError(preview_manual_fallback(plan))

    workflow_path = root / ".github" / "workflows" / plan.workflow
    if not workflow_path.exists():
        raise BoardwrightError(f"Missing preview workflow: {workflow_path}")

    completed = subprocess.run(
        [
            plan.gh_command,
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
        raise BoardwrightError(
            f"GitHub workflow dispatch failed: {message}\n\n{preview_manual_fallback(plan)}"
        )


def preview_manual_fallback(plan: PreviewPlan) -> str:
    return "\n".join(
        [
            "GitHub CLI is unavailable or could not dispatch the preview.",
            "",
            "Manual fallback:",
            f"Open GitHub Actions -> {plan.workflow} -> Run workflow",
            f"Ref: {plan.branch}",
            "Inputs:",
            f"- variant: {plan.variant}",
            "",
            "Equivalent gh command:",
            f"{plan.gh_command} workflow run {plan.workflow} --ref {plan.branch} -f variant={plan.variant}",
        ]
    )


def preview_artifact_name(variant: str) -> str:
    return f"boardwright-preview-{normalize_variant(variant)}"


def latest_pushed_dev_sha(config: BoardwrightConfig) -> str:
    return remote_branch_sha(config.root, "origin", config.dev_branch)


def list_preview_runs(
    config: BoardwrightConfig,
    limit: int = 10,
) -> tuple[PreviewRun, ...]:
    gh = _gh_command()
    if gh is None:
        raise BoardwrightError(
            "\n".join(
                [
                    "GitHub CLI is not installed, so Boardwright cannot inspect preview artifacts.",
                    "",
                    "Manual fallback:",
                    f"Open GitHub Actions -> {config.preview_workflow}",
                    f"Branch: {config.dev_branch}",
                    "Confirm the latest successful run matches the latest pushed dev commit.",
                    "Download artifact: boardwright-preview-<VARIANT>",
                ]
            )
        )

    command = [
        gh,
        "run",
        "list",
        "--workflow",
        config.preview_workflow,
        "--branch",
        config.dev_branch,
        "--limit",
        str(limit),
        "--json",
        "databaseId,status,conclusion,displayTitle,headBranch,headSha,createdAt",
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
        raise BoardwrightError(
            "\n".join(
                [
                    f"Could not list preview workflow runs: {message}",
                    "",
                    "Manual fallback:",
                    f"Open GitHub Actions -> {config.preview_workflow}",
                    f"Branch: {config.dev_branch}",
                    "Confirm the latest successful run matches the latest pushed dev commit.",
                ]
            )
        )

    return parse_preview_runs(completed.stdout)


def list_run_artifacts(config: BoardwrightConfig, run_id: str) -> tuple[PreviewArtifact, ...]:
    gh = _gh_command()
    if gh is None:
        raise BoardwrightError("GitHub CLI is not installed, so Boardwright cannot inspect run artifacts.")
    if not run_id.strip():
        raise BoardwrightError("Cannot inspect artifacts without a workflow run id.")

    command = [
        gh,
        "run",
        "view",
        run_id,
        "--json",
        "artifacts",
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
        raise BoardwrightError(f"Could not inspect artifacts for run {run_id}: {message}")
    return parse_run_artifacts(completed.stdout)


def parse_run_artifacts(raw_json: str) -> tuple[PreviewArtifact, ...]:
    try:
        payload = json.loads(raw_json or "{}")
    except json.JSONDecodeError as exc:
        raise BoardwrightError(f"Could not parse GitHub artifact list: {exc}") from exc

    artifacts = payload.get("artifacts", [])
    parsed: list[PreviewArtifact] = []
    for artifact in artifacts if isinstance(artifacts, list) else []:
        parsed.append(
            PreviewArtifact(
                name=str(artifact.get("name") or "").strip(),
                size_in_bytes=int(artifact.get("sizeInBytes") or artifact.get("size_in_bytes") or 0),
                expired=bool(artifact.get("expired") or False),
            )
        )
    return tuple(parsed)


def parse_preview_runs(raw_json: str) -> tuple[PreviewRun, ...]:
    try:
        runs = json.loads(raw_json or "[]")
    except json.JSONDecodeError as exc:
        raise BoardwrightError(f"Could not parse GitHub run list: {exc}") from exc

    parsed: list[PreviewRun] = []
    for run in runs:
        parsed.append(
            PreviewRun(
                database_id=str(run.get("databaseId") or "").strip(),
                status=str(run.get("status") or "unknown"),
                conclusion=str(run.get("conclusion") or ""),
                branch=str(run.get("headBranch") or ""),
                head_sha=str(run.get("headSha") or "").strip(),
                created_at=str(run.get("createdAt") or ""),
                title=str(run.get("displayTitle") or ""),
            )
        )
    return tuple(parsed)


def evaluate_preview_state(
    runs: tuple[PreviewRun, ...],
    expected_sha: str,
    variant: str,
) -> PreviewState:
    artifact_name = preview_artifact_name(variant)
    if not expected_sha:
        return PreviewState(
            state="missing",
            artifact_name=artifact_name,
            expected_sha="",
            message=f"Could not resolve origin/dev. Push {variant} preview source first.",
        )
    if not runs:
        return PreviewState(
            state="missing",
            artifact_name=artifact_name,
            expected_sha=expected_sha,
            message="No preview workflow runs found for dev.",
        )

    latest = runs[0]
    if latest.status != "completed":
        return PreviewState(
            state="running",
            artifact_name=artifact_name,
            expected_sha=expected_sha,
            run=latest,
            message=f"Latest preview run {latest.database_id} is {latest.status}.",
        )
    if latest.conclusion != "success":
        return PreviewState(
            state="failed",
            artifact_name=artifact_name,
            expected_sha=expected_sha,
            run=latest,
            message=f"Latest preview run {latest.database_id} ended with {latest.conclusion or 'unknown'}.",
        )
    if latest.head_sha != expected_sha:
        return PreviewState(
            state="stale",
            artifact_name=artifact_name,
            expected_sha=expected_sha,
            run=latest,
            message=(
                f"Latest preview run {latest.database_id} was built from "
                f"{_short_sha(latest.head_sha)}, expected {_short_sha(expected_sha)}."
            ),
        )
    return PreviewState(
        state="ready",
        artifact_name=artifact_name,
        expected_sha=expected_sha,
        run=latest,
        message=f"Preview artifact {artifact_name} is fresh for {_short_sha(expected_sha)}.",
    )


def build_preview_state(
    config: BoardwrightConfig,
    variant: str | None = None,
    runs: tuple[PreviewRun, ...] | None = None,
    output_dir: Path | None = None,
) -> PreviewState:
    selected_variant = normalize_variant(variant or config.preview_variant)
    preview_runs = runs if runs is not None else list_preview_runs(config)
    state = evaluate_preview_state(
        preview_runs,
        latest_pushed_dev_sha(config),
        selected_variant,
    )
    return replace(state, reviewed=preview_reviewed(config, state, output_dir))


def preview_reviewed(
    config: BoardwrightConfig,
    state: PreviewState,
    output_dir: Path | None = None,
) -> bool:
    if state.run is None or not state.ready:
        return False

    marker = _review_marker_path(config, output_dir)
    if not marker.exists():
        return False

    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False

    return (
        str(data.get("artifact_name") or "") == state.artifact_name
        and str(data.get("run_id") or "") == state.run.database_id
        and str(data.get("head_sha") or "") == state.run.head_sha
        and str(data.get("expected_sha") or "") == state.expected_sha
    )


def mark_preview_reviewed(
    config: BoardwrightConfig,
    state: PreviewState,
    output_dir: Path | None = None,
) -> None:
    if state.run is None or not state.ready:
        raise BoardwrightError("Cannot mark preview reviewed until the artifact is ready.")

    marker = _review_marker_path(config, output_dir)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps(
            {
                "artifact_name": state.artifact_name,
                "run_id": state.run.database_id,
                "head_sha": state.run.head_sha,
                "expected_sha": state.expected_sha,
                "created_at": state.run.created_at,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )


def fetch_latest_preview_artifact(
    config: BoardwrightConfig,
    variant: str | None = None,
    output_dir: Path | None = None,
) -> str:
    gh = _gh_command()
    if gh is None:
        raise BoardwrightError(
            "\n".join(
                [
                    "GitHub CLI is not installed, so Boardwright cannot fetch preview artifacts.",
                    "",
                    "Manual fallback:",
                    f"Open GitHub Actions -> {config.preview_workflow}",
                    f"Branch: {config.dev_branch}",
                    f"Download artifact: {preview_artifact_name(variant or config.preview_variant)}",
                ]
            )
        )

    selected_variant = normalize_variant(variant or config.preview_variant)
    destination = output_dir or (config.root / "boardwright-preview")
    state = build_preview_state(config, selected_variant)
    if not state.ready or state.run is None:
        raise BoardwrightError(f"Preview artifact is not ready: {state.state}. {state.message}")

    run_id = state.run.database_id
    if not run_id:
        raise BoardwrightError("Latest preview workflow run did not include an id.")

    destination.mkdir(parents=True, exist_ok=True)
    download_command = [
        gh,
        "run",
        "download",
        run_id,
        "--name",
        state.artifact_name,
        "--dir",
        str(destination),
    ]
    if config.github_repo:
        download_command.extend(("--repo", config.github_repo))

    downloaded = subprocess.run(
        download_command,
        cwd=config.root,
        text=True,
        capture_output=True,
        check=False,
    )
    if downloaded.returncode != 0:
        message = downloaded.stderr.strip() or downloaded.stdout.strip()
        artifact_hint = _artifact_hint(config, run_id)
        raise BoardwrightError(
            "\n".join(
                [
                    f"Could not download preview artifact {state.artifact_name} from run {run_id}:",
                    message or "GitHub CLI returned no output.",
                    artifact_hint,
                    "",
                    "Command:",
                    " ".join(download_command),
                ]
            )
        )

    downloaded_files = _downloaded_files(destination)
    if not downloaded_files:
        message = downloaded.stderr.strip() or downloaded.stdout.strip()
        raise BoardwrightError(
            "\n".join(
                [
                    f"Downloaded {state.artifact_name} from run {run_id}, but {destination} is empty.",
                    "This usually means the workflow uploaded an empty artifact or GitHub CLI extracted nothing.",
                    "",
                    "Command:",
                    " ".join(download_command),
                    "",
                    "GitHub CLI output:",
                    message or "(none)",
                ]
            )
        )

    mark_preview_reviewed(config, state, destination)
    preview_files = ", ".join(str(path.relative_to(destination)) for path in downloaded_files[:5])
    more = "" if len(downloaded_files) <= 5 else f" and {len(downloaded_files) - 5} more file(s)"
    return (
        f"Fetched {state.artifact_name} from run {run_id} "
        f"({state.run.status}/{state.run.conclusion}) to {destination}. "
        f"Files: {preview_files}{more}."
    )


def format_preview_state(state: PreviewState) -> str:
    lines = [
        f"Artifact: {state.artifact_name}",
        f"State: {state.state}",
    ]
    if state.expected_sha:
        lines.append(f"Expected dev SHA: {_short_sha(state.expected_sha)}")
    if state.run is not None:
        lines.extend(
            [
                f"Run: {state.run.database_id}",
                f"Branch: {state.run.branch}",
                f"Run SHA: {_short_sha(state.run.head_sha)}",
                f"Created: {state.run.created_at or 'unknown'}",
                f"Status: {state.run.status}/{state.run.conclusion or 'pending'}",
                f"Reviewed: {'yes' if state.reviewed else 'no'}",
            ]
        )
    if state.message:
        lines.append(state.message)
    return "\n".join(lines)


def _short_sha(value: str) -> str:
    return value[:12] if value else "unknown"


def _review_marker_path(config: BoardwrightConfig, output_dir: Path | None = None) -> Path:
    destination = output_dir or (config.root / "boardwright-preview")
    return destination / ".boardwright-preview-reviewed.json"


def _downloaded_files(destination: Path) -> tuple[Path, ...]:
    if not destination.exists():
        return ()
    files = [
        path
        for path in destination.rglob("*")
        if path.is_file() and path.name != ".boardwright-preview-reviewed.json"
    ]
    return tuple(sorted(files))


def _artifact_hint(config: BoardwrightConfig, run_id: str) -> str:
    try:
        artifacts = list_run_artifacts(config, run_id)
    except BoardwrightError as exc:
        return f"Could not list run artifacts: {exc}"
    names = [artifact.name for artifact in artifacts if artifact.name]
    if not names:
        return "Run has no downloadable artifacts."
    return "Artifacts on run: " + ", ".join(names)
