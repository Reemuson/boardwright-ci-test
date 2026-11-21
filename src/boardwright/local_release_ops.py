# SPDX-License-Identifier: LicenseRef-Boardwright-Source-Available
# Copyright (c) Ryan Hicks

"""The one entry point for local-release operations, shared by the CLI and the TUI.

``boardwright release local <op>`` and the TUI release screen both call
``run``; neither front end selects a backend, checks a precondition or
decides what an operation does. Policy stays in ``local_release``,
``local_finalize``, ``local_publish`` and ``local_recovery``.

Also here, for front ends: ``backend_overview`` (which release backend this
project would use, without running anything or contacting GitHub),
``summaries`` (read-only per-version state for display) and
``confirmation_text`` (what an irreversible operation will do, shown before
it is confirmed).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from . import container
from .errors import BoardwrightError

OPERATIONS = ("plan", "candidate", "review", "approve", "finalize", "publish", "status", "recover")
# Operations that select the release backend first (as the CLI always has).
SELECTS_BACKEND = ("plan", "candidate", "approve", "finalize", "publish")
# Operations that write something that is never undone automatically: a
# front end must ask for explicit confirmation before each run.
IRREVERSIBLE = ("candidate", "approve", "finalize", "publish", "recover")
READ_ONLY = ("plan", "status")
LONG_RUNNING = ("candidate", "finalize", "publish")  # pinned-Docker builds, minutes

LOCAL_LABEL = "Local release (not server-enforced)"
GITHUB_LABEL = "GitHub release"


@dataclass
class LocalArgs:
    """The inputs of one operation; the CLI fills it from argparse, the TUI from its dialog."""

    version: str = ""
    backend: str = ""
    candidate: str = ""
    record: Path | None = None
    check: Path | None = None
    sign: bool = False
    drawing_revision: str = ""
    revision_description: str = ""
    variant: str = "RELEASED"
    kind: str = "release"
    change_class: str = ""
    justification: str = ""
    release_date: str = ""
    kicad: str = container.DEFAULT_KICAD

    def request(self):
        from .local_release import ReleaseRequest

        return ReleaseRequest(
            version=self.version, drawing_revision=self.drawing_revision,
            revision_description=self.revision_description, variant=self.variant, kind=self.kind,
            change_classes=self.change_class, justification=self.justification,
            release_date=self.release_date, kicad=self.kicad)


def store_for(config):
    from .release_store import ReleaseStore

    return ReleaseStore.for_project(Path(config.root), config.project)


def run(config, operation: str, args: LocalArgs, *, echo=print, error=None, runner=None, host_runner=None,
        inspect_remote=None, inspect_docker=None) -> int:
    """Run one local-release operation exactly as ``boardwright release local`` does. Returns the exit status.

    ``echo`` receives progress and results line by line; ``error`` (default
    ``echo``) receives the refusal or failure. Nothing here retries, switches
    backend or continues past a refusal.
    """

    from .local_release import (
        approve, build_candidate, check_review, load_candidate, plan, select_local_backend, write_review_draft)

    error = error or echo
    if operation not in OPERATIONS:
        error(f"error: unknown local release operation {operation!r}")
        return 2
    try:
        store = store_for(config)
        if operation == "status":
            from .local_recovery import status_lines

            echo("\n".join(status_lines(Path(config.root), store, args.version)))
            return 0
        if operation == "recover":
            from .local_recovery import recover

            if not store.exists():
                echo("No local release store; nothing to recover.")
                return 0
            return recover(config, store, args.version, echo=echo)
        if operation == "review":
            if args.check:
                _path, record, reviewed = load_candidate(store, args.version, args.candidate)
                problems = check_review(Path(args.check).read_text(encoding="utf-8"), record, reviewed)
                for problem in problems:
                    echo(f"ERROR {problem}")
                echo("Review record is complete and bound to the candidate." if not problems else "Review record refused.")
                return 1 if problems else 0
            draft = write_review_draft(store, args.version, args.candidate)
            echo(f"Review draft: {draft}")
            echo("Inspect the frozen candidate, tick every item, set the last line to `APPROVED: yes`, then run "
                 f"`boardwright release local approve {args.version} --record <file>`.")
            return 0
        selection = select_local_backend(config, args.backend, inspect_remote=inspect_remote,
                                         inspect_docker=inspect_docker)
        echo(f"Release backend: local ({selection.reason}). Local approval is not server-enforced; "
             "local tags are never pushed.")
        for warning in selection.warnings:
            echo(f"warning: {warning}")
        if operation == "plan":
            result = plan(config, args.version, store)
            for note in result.notes:
                echo(f"note: {note}")
            for problem in result.problems:
                echo(f"ERROR {problem}")
            echo("Plan OK: a candidate can be built from HEAD " + result.head[:12] if not result.problems
                 else "Plan refused.")
            return 1 if result.problems else 0
        if operation == "candidate":
            build_candidate(config, args.request(), store, runner=runner, host_runner=host_runner, echo=echo)
            return 0
        if operation == "approve":
            if args.record is None:
                raise BoardwrightError("approve needs the completed review record (--record).")
            approve(config, store, args.version, Path(args.record), args.candidate, echo=echo)
            return 0
        if operation == "finalize":
            from .local_finalize import finalize

            finalize(config, store, args.version, sign=args.sign, runner=runner, host_runner=host_runner, echo=echo)
            return 0
        if operation == "publish":
            from .local_publish import publish

            publish(config, store, args.version, runner=runner, echo=echo)
            return 0
    except (BoardwrightError, container.ContainerError, OSError, json.JSONDecodeError) as exc:
        error(f"error: {exc}")
        return 2
    return 2


# -- read-only views for front ends ------------------------------------------

@dataclass(frozen=True)
class BackendOverview:
    label: str           # LOCAL_LABEL, GITHUB_LABEL or "Release refused"
    default: str         # "local" | "github" | "" (refused)
    remote_kind: str     # "none" | "github" | "unsupported"
    reason: str
    problems: tuple[str, ...]
    github_available: bool  # a GitHub remote exists (access is checked when a GitHub release runs)


def backend_overview(config, *, inspect_remote=None) -> BackendOverview:
    """The release backend this project selects by default; read-only and offline.

    Uses ``select_backend`` with the remote inspected without contacting
    GitHub and Docker assumed available (Docker and GitHub access are checked
    when an operation runs; a failure is reported, never switched).
    """

    from . import backends

    inspect_remote = inspect_remote or backends.inspect_remote
    remote = inspect_remote(Path(config.root), check_access=False)
    settings = config.project.get("backends") if isinstance(config.project.get("backends"), dict) else {}
    selection = backends.select_backend(
        backends.Operation.RELEASE, variant="RELEASED", remote=remote,
        docker=backends.DockerInfo(True, "checked when an operation runs"),
        project_setting=str(settings.get("release", "auto") or "auto"))
    default = selection.backend.value if selection.backend else ""
    label = {"local": LOCAL_LABEL, "github": GITHUB_LABEL}.get(default, "Release refused")
    return BackendOverview(label, default, remote.kind, selection.reason, tuple(selection.problems),
                           remote.kind == "github")


@dataclass
class VersionSummary:
    version: str
    stage: str
    detail: str
    next_action: str
    candidate_id: str = ""
    candidates: int = 0
    source_sha: str = ""
    release_date: str = ""
    drawing_revision: str = ""
    revisions: dict[str, str] = field(default_factory=dict)
    baselines: str = ""
    approval: str = ""
    release_commit: str = ""
    package: str = ""
    signature: str = ""
    problems: list[str] = field(default_factory=list)
    recoverable: list[str] = field(default_factory=list)


def _candidate_summary(store, version: str, chosen: str) -> dict:
    """Display fields read from a frozen candidate's files (not trusted for any decision)."""

    from .release_ledger import parse_ledger_text

    entries = store.entries("candidates", version)
    entry = next((e for e in entries if e.name == chosen), entries[-1] if len(entries) == 1 else None)
    if entry is None:
        return {}
    out: dict = {"candidate_id": entry.name}
    try:
        record = json.loads((entry.path / "candidate.json").read_text(encoding="utf-8"))
        out.update(source_sha=record.get("source_sha", ""), release_date=record.get("release_date") or "",
                   drawing_revision=(record.get("request") or {}).get("drawing_revision", ""))
        ledger = entry.path / ".boardwright" / "releases.yaml"
        if ledger.is_file():
            item = next((r for r in parse_ledger_text(ledger.read_text(encoding="utf-8")) if r.version == version), None)
            if item is not None:
                out["revisions"] = {"PCB": item.pcb_revision, "schematic": item.schematic_revision,
                                    "fabrication": item.fabrication_revision, "assembly": item.assembly_revision}
                out["baselines"] = (f"fabrication {item.fabrication_baseline.id} {item.fabrication_baseline.status}; "
                                    f"assembly {item.assembly_baseline.id} {item.assembly_baseline.status}")
    except (OSError, ValueError, BoardwrightError) as exc:
        out["unreadable"] = str(exc)
    return out


def summaries(config, version: str = "") -> tuple[str, list[VersionSummary]]:
    """Read-only state of each local release (or one version): never builds, writes or repairs anything."""

    from .local_recovery import version_state, versions

    store = store_for(config)
    if not store.exists():
        return f"No local releases yet ({store.root} is created by the first candidate).", []
    index = store.verify_index()
    header = f"Release store {store.root}; index " + ("intact" if not index else "INCONSISTENT: " + "; ".join(index))
    result = []
    if index:
        return header + " (releases not evaluated)", result
    for name in [version] if version else versions(store):
        state = version_state(Path(config.root), store, name)
        approvals = store.entries("approvals", name)
        details = _candidate_summary(store, name, approvals[0].name if approvals else "")
        summary = VersionSummary(
            name, state.stage, state.detail, state.next_action, candidates=len(store.entries("candidates", name)),
            problems=list(state.problems), recoverable=list(state.recoverable), signature=state.signature,
            **{k: v for k, v in details.items() if k != "unreadable"})
        if details.get("unreadable"):
            summary.problems.append(f"candidate unreadable: {details['unreadable']}")
        if approvals:
            try:
                data = json.loads((approvals[0].path / "approval.json").read_text(encoding="utf-8"))
                summary.approval = (f"local approval by {data.get('approver')} at {data.get('approved_at')}: "
                                    "not server-enforced; approver asserted, not authenticated; "
                                    + ("signing key configured" if data.get("signing_configured") else "unsigned"))
            except (OSError, ValueError) as exc:
                summary.problems.append(f"approval unreadable: {exc}")
        finals = store.entries("finalizations", name)
        if finals:
            try:
                data = json.loads((finals[0].path / "finalize.json").read_text(encoding="utf-8"))
                summary.release_commit = data.get("release_commit", "")
            except (OSError, ValueError):
                pass
        releases = store.entries("releases", name)
        if releases:
            summary.package = str(releases[0].path)
        result.append(summary)
    return header, result


def format_summary(summary: VersionSummary) -> str:
    lines = [f"{summary.version}: {summary.stage} - {summary.detail}"]
    if summary.candidate_id:
        lines.append(f"  candidate {summary.candidate_id}"
                     + (f" (one of {summary.candidates}; none approved)" if summary.candidates > 1 and not summary.approval
                        else ""))
    elif summary.candidates > 1:
        lines.append(f"  {summary.candidates} candidates; none approved (name one with the candidate id)")
    if summary.source_sha:
        lines.append(f"  source {summary.source_sha}  release date {summary.release_date or '-'}")
    if summary.drawing_revision or summary.revisions:
        revisions = ", ".join(f"{k} {v}" for k, v in summary.revisions.items())
        lines.append(f"  drawing revision {summary.drawing_revision or '-'}" + (f"; {revisions}" if revisions else ""))
    if summary.baselines:
        lines.append(f"  baselines: {summary.baselines}")
    if summary.approval:
        lines.append(f"  {summary.approval}")
    if summary.release_commit:
        lines.append(f"  release commit {summary.release_commit[:12]}")
    if summary.signature:
        lines.append(f"  {summary.signature}")
    if summary.package:
        lines.append(f"  package {summary.package}")
    lines += [f"  PROBLEM: {p}" for p in summary.problems]
    lines += [f"  interrupted: {r}" for r in summary.recoverable]
    lines.append(f"  next: {summary.next_action}")
    return "\n".join(lines)


def overview_line(config) -> str:
    """One read-only line for a dashboard; never raises."""

    try:
        header, items = summaries(config)
    except Exception as exc:  # status must never break a dashboard
        return f"local releases unavailable: {exc}"
    if not items:
        return "release-store index INCONSISTENT" if "INCONSISTENT" in header else "no local releases"
    return "; ".join(f"{s.version} {s.stage}" + (" (PROBLEM)" if s.problems else "")
                     + (" (interrupted)" if s.recoverable else "") for s in items)


def confirmation_text(config, operation: str, args: LocalArgs) -> str:
    """What an irreversible operation will do; shown before a front end asks for confirmation."""

    version = args.version or "(no version)"
    branch = getattr(config, "release_branch", "the release branch")
    common = ("\n\nOnce started it cannot be cancelled. If it is interrupted, Status shows the state and Recover "
              "completes it; nothing is ever reset, forced or pushed.")
    texts = {
        "candidate": (f"Build release candidate {version} in the pinned KiCad container from the committed HEAD "
                      f"(drawing revision {args.drawing_revision or '-'}, variant {args.variant}, kind {args.kind}"
                      f"{', release date ' + args.release_date if args.release_date else ', release date today (UTC)'}). "
                      "Takes several minutes. The candidate is frozen once in the local release store and never "
                      "replaced. Nothing is committed, tagged or pushed."),
        "approve": (f"Record a LOCAL approval of {version}"
                    + (f" candidate {args.candidate[:12]}" if args.candidate else "")
                    + f" with the review record {args.record}. Write-once: a version has one approved candidate and an "
                    "approval is never replaced. The approval is not server-enforced; the approver is your Git identity "
                    "(asserted, not authenticated)."),
        "finalize": (f"Rebuild {version} (several minutes), verify it against the approved candidate, create the "
                     f"release commit, advance {branch} to it, update your working tree, and create the annotated tag "
                     f"{version} ({'signed: SSH only, and the signing key must be listed for the approver alone in your allowed-signers file; that local mapping is not authentication' if args.sign else 'unsigned'}). The tag is not pushed. The branch and tag "
                     "changes are never undone automatically."),
        "publish": (f"Rebuild {version} from its tag (several minutes), verify the outputs against the tagged accepted "
                    "manifest, and write the release package to the local release store once; it is never replaced. "
                    "Nothing is published on GitHub."),
        "recover": ("Complete interrupted steps without building"
                    + (f" for {args.version}" if args.version else " for every version")
                    + ": remove interrupted store writes, record missing index events of complete entries, "
                    "re-import the release commit from its bundle, and update the working tree after an interrupted "
                    "branch advance. Branches and tags are never reset, moved, deleted or pushed."),
    }
    return texts.get(operation, f"Run {operation} for {version}.") + (common if operation in IRREVERSIBLE else "")
