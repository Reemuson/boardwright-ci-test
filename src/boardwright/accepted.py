from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass

from .actions import _gh_command
from .config import BoardwrightConfig
from .errors import BoardwrightError
from .git_ops import remote_branch_sha


@dataclass(frozen=True)
class AcceptedRun:
    database_id: str
    status: str
    conclusion: str
    branch: str
    head_sha: str
    created_at: str
    title: str


@dataclass(frozen=True)
class AcceptedMainState:
    state: str
    workflow: str
    expected_sha: str
    run: AcceptedRun | None = None
    message: str = ""

    @property
    def ready(self) -> bool:
        return self.state == "ready"


def latest_pushed_main_sha(config: BoardwrightConfig) -> str:
    return remote_branch_sha(config.root, "origin", config.release_branch)


def latest_pushed_source_sha(config: BoardwrightConfig) -> str:
    return remote_branch_sha(config.root, "origin", config.dev_branch)


def list_accepted_runs(
    config: BoardwrightConfig,
    limit: int = 10,
) -> tuple[AcceptedRun, ...]:
    gh = _gh_command()
    if gh is None:
        raise BoardwrightError(
            "\n".join(
                [
                    "GitHub CLI is not installed, so Boardwright cannot inspect accepted main outputs.",
                    "",
                    "Manual fallback:",
                    f"Open GitHub Actions -> {config.main_workflow}",
                    f"Branch: {config.release_branch}",
                    "Confirm the latest successful run matches the latest reviewed source commit.",
                ]
            )
        )

    command = [
        gh,
        "run",
        "list",
        "--workflow",
        config.main_workflow,
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
                    f"Could not list accepted main workflow runs: {message}",
                    "",
                    "Manual fallback:",
                    f"Open GitHub Actions -> {config.main_workflow}",
                    f"Branch: {config.dev_branch}",
                    "Confirm the latest successful run matches the latest reviewed source commit.",
                ]
            )
        )

    return parse_accepted_runs(completed.stdout)


def parse_accepted_runs(raw_json: str) -> tuple[AcceptedRun, ...]:
    try:
        runs = json.loads(raw_json or "[]")
    except json.JSONDecodeError as exc:
        raise BoardwrightError(f"Could not parse GitHub accepted-output run list: {exc}") from exc

    parsed: list[AcceptedRun] = []
    for run in runs:
        parsed.append(
            AcceptedRun(
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


def evaluate_accepted_state(
    runs: tuple[AcceptedRun, ...],
    expected_sha: str,
    workflow: str,
) -> AcceptedMainState:
    if not expected_sha:
        return AcceptedMainState(
            state="missing",
            workflow=workflow,
            expected_sha="",
            message="Could not resolve the expected source SHA. Push dev and accept outputs to main first.",
        )
    if not runs:
        return AcceptedMainState(
            state="missing",
            workflow=workflow,
            expected_sha=expected_sha,
            message="No accepted main-output workflow runs found.",
        )

    latest = runs[0]
    if latest.status != "completed":
        return AcceptedMainState(
            state="running",
            workflow=workflow,
            expected_sha=expected_sha,
            run=latest,
            message=f"Latest accepted-output run {latest.database_id} is {latest.status}.",
        )
    if latest.conclusion != "success":
        return AcceptedMainState(
            state="failed",
            workflow=workflow,
            expected_sha=expected_sha,
            run=latest,
            message=f"Latest accepted-output run {latest.database_id} ended with {latest.conclusion or 'unknown'}.",
        )
    if latest.head_sha != expected_sha:
        return AcceptedMainState(
            state="stale",
            workflow=workflow,
            expected_sha=expected_sha,
            run=latest,
            message=(
                f"Latest accepted-output run {latest.database_id} was built from "
                f"{_short_sha(latest.head_sha)}, expected source {_short_sha(expected_sha)}."
            ),
        )
    return AcceptedMainState(
        state="ready",
        workflow=workflow,
        expected_sha=expected_sha,
        run=latest,
        message=f"Accepted main outputs are fresh for {_short_sha(expected_sha)}.",
    )


def build_accepted_main_state(config: BoardwrightConfig) -> AcceptedMainState:
    return evaluate_accepted_state(
        list_accepted_runs(config),
        latest_pushed_source_sha(config),
        config.main_workflow,
    )


def format_accepted_state(state: AcceptedMainState) -> str:
    lines = [
        f"Workflow: {state.workflow}",
        f"State: {state.state}",
    ]
    if state.expected_sha:
        lines.append(f"Expected source SHA: {_short_sha(state.expected_sha)}")
    if state.run is not None:
        lines.extend(
            [
                f"Run: {state.run.database_id}",
                f"Branch: {state.run.branch}",
                f"Run SHA: {_short_sha(state.run.head_sha)}",
                f"Created: {state.run.created_at or 'unknown'}",
                f"Status: {state.run.status}/{state.run.conclusion or 'pending'}",
            ]
        )
    if state.message:
        lines.append(state.message)
    return "\n".join(lines)


def _short_sha(value: str) -> str:
    return value[:12] if value else "unknown"
