# SPDX-License-Identifier: LicenseRef-Boardwright-Source-Available
# Copyright (c) Ryan Hicks

"""Standalone local releases (``docs/LOCAL_FIRST.md`` section 5).

The GitHub release flow with the server replaced by the developer's own
repository and a local release store (``release_store``). It runs the same
engine: ``container.candidate_steps`` (release preparation, build, manifest,
baseline check, review candidate) in a stage of the clean, committed ``HEAD``,
and ``review_gate`` for the candidate identity.

Implemented here: plan, candidate, review, approve and the command line.
Nothing in this module commits, tags, pushes or publishes: finalisation is
``local_finalize``, publication ``local_publish``, status and recovery
``local_recovery``.

A local approval is labelled ``authority: local``, ``server_enforced:
false``. The approver is the Git identity: asserted, not authenticated. A
local candidate or approval never satisfies a GitHub ``approved_review_run``,
which accepts only a successful ``prepare-release`` run of the current
``main`` and takes its candidate from that run's artifact.
"""

from __future__ import annotations

import argparse
import json
import re
import socket
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import container
from .errors import BoardwrightError
from .output_manifest import DEFAULT_MANIFEST
from .release_preparation import AUTHORISED_PATHS
from .release_store import ReleaseStore, StoreError, safe_name, sha256_bytes, sha256_file
from .review_gate import CANDIDATE_FILE, CANDIDATE_SCHEMA, INPUT_KEYS
from .stage_lock import stage_lock

APPROVAL_SCHEMA = 1
CANDIDATE_RECORD = "candidate.json"
APPROVAL_FILE = "approval.json"
STAGE_PURPOSE = "local-release-candidate"
# Copied into a frozen candidate for review: the GitHub candidate artifact's set.
REVIEW_OUTPUT_DIRS = ("Manufacturing", "Schematic", "Reports", "boardwright-build")
REVIEW_FILES = ("README.md", "CHANGELOG.md")
_VERSION = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class LocalReleaseError(BoardwrightError):
    """A local release step was refused; nothing was committed, tagged or published."""


@dataclass(frozen=True)
class ReleaseRequest:
    version: str
    drawing_revision: str
    revision_description: str
    variant: str = "RELEASED"
    kind: str = "release"
    change_classes: str = ""
    justification: str = ""
    release_date: str = ""
    kicad: str = container.DEFAULT_KICAD

    def inputs(self) -> dict[str, str]:
        """The same ``IN_*`` inputs a GitHub prepare-release run records."""

        return {
            "IN_VERSION": self.version,
            "IN_VARIANT": self.variant,
            "IN_RELEASE_KIND": self.kind,
            "IN_DRAWING_REVISION": self.drawing_revision,
            "IN_REVISION_DESCRIPTION": self.revision_description,
            "IN_CHANGE_CLASSES": self.change_classes,
            "IN_JUSTIFICATION": self.justification,
        }

    def step_args(self) -> argparse.Namespace:
        return argparse.Namespace(
            version=self.version, variant=self.variant, kind=self.kind, drawing_revision=self.drawing_revision,
            revision_description=self.revision_description, change_class=self.change_classes,
            justification=self.justification, release_date=self.release_date, kicad=self.kicad,
        )


@dataclass
class LocalPlan:
    version: str
    repo: Path
    head: str = ""
    branch: str = ""
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# -- small helpers -----------------------------------------------------------

def _git(root: Path, *args: str, check: bool = True) -> str:
    done = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and done.returncode != 0:
        raise LocalReleaseError(f"git {' '.join(args)} failed: {done.stderr.strip()}")
    return done.stdout


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_tags(repo: Path) -> dict[str, str]:
    out = _git(repo, "for-each-ref", "--format=%(refname:strip=2) %(objectname)", "refs/tags")
    return dict(line.split(" ", 1) for line in out.splitlines() if line.strip())


def tag_exists(repo: Path, version: str) -> bool:
    return subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"refs/tags/{version}"], cwd=repo,
                          capture_output=True).returncode == 0


def git_identity(repo: Path) -> str:
    name = _git(repo, "config", "user.name", check=False).strip()
    email = _git(repo, "config", "user.email", check=False).strip()
    if not name or not email:
        raise LocalReleaseError("Set git user.name and user.email: the approval records the Git identity "
                                "(asserted, not authenticated).")
    return f"{name} <{email}>"


def signing_configured(repo: Path) -> bool:
    return bool(_git(repo, "config", "user.signingkey", check=False).strip())


# -- backend -----------------------------------------------------------------

def select_local_backend(config, override: str = "", *, inspect_remote=None, inspect_docker=None):
    """Select the release backend once; refuse anything but an explicit or default local selection.

    A usable GitHub remote selects GitHub unless ``--backend local`` is given;
    nothing ever switches backend after a failure.
    """

    from . import backends

    inspect_remote = inspect_remote or backends.inspect_remote
    inspect_docker = inspect_docker or backends.inspect_docker
    settings = config.project.get("backends") if isinstance(config.project.get("backends"), dict) else {}
    selection = backends.select_backend(
        backends.Operation.RELEASE, variant="RELEASED", remote=inspect_remote(config.root),
        docker=inspect_docker(root=config.root), project_setting=str(settings.get("release", "auto") or "auto"),
        override=override)
    if selection.backend is None:
        raise LocalReleaseError("Release backend refused: " + "; ".join(selection.problems or (selection.reason,)))
    if selection.backend is not backends.Backend.LOCAL:
        raise LocalReleaseError(
            f"This project releases on GitHub ({selection.reason}). A local-only release needs an explicit "
            "`--backend local`; its tag is never pushed.")
    return selection


# -- plan --------------------------------------------------------------------

def plan(config, version: str, store: ReleaseStore | None = None) -> LocalPlan:
    """Host checks before a candidate: nothing is staged, built or written."""

    from .release import build_release_plan, validate_release_plan
    from .release_ledger import load_ledger, verify_ledger_history

    repo = Path(config.root)
    result = LocalPlan(version=version, repo=repo)
    if not _VERSION.match(version):
        result.problems.append(f"Invalid release version {version!r}.")
        return result
    head = _git(repo, "rev-parse", "--verify", "--quiet", "HEAD", check=False).strip()
    if not head:
        result.problems.append("The repository has no commits.")
        return result
    result.head = head
    if container.host_tree_fingerprint(repo).strip():
        result.problems.append("The working tree has uncommitted or untracked changes; a release candidate is built "
                               "from a clean, committed HEAD only (commit or stash them; previews may include them).")
    release_plan = build_release_plan(config, version, check_remote=False)
    result.branch = release_plan.branch
    result.problems.extend(p for p in validate_release_plan(release_plan, allow_dirty=True))
    try:
        records = load_ledger(repo)
        result.problems.extend(verify_ledger_history(repo, records))
    except BoardwrightError as exc:
        result.problems.append(f"Release ledger: {exc}")
        records = []
    if records:
        previous = records[-1].version
        if not tag_exists(repo, previous):
            result.problems.append(f"Previous release {previous} has no local tag; a local release checks baselines "
                                   "and ledger history against local tags (fetch the tags first).")
        elif subprocess.run(["git", "cat-file", "-e", f"refs/tags/{previous}:{DEFAULT_MANIFEST.as_posix()}"],
                            cwd=repo, capture_output=True).returncode != 0:
            result.notes.append(f"Previous release {previous} has no committed output manifest (legacy). A reused "
                                "baseline cannot be verified locally; the candidate fails closed if one is needed.")
    if store is not None and store.exists():
        if any(store.entries("releases", version)):
            result.problems.append(f"Release {version} is already published in the local release store.")
    return result


# -- candidate ---------------------------------------------------------------

def stage_dir(repo: Path, version: str) -> Path:
    return (container.default_stage_root() / f"{repo.name}-local-{safe_name(version, 'version')}").resolve()


LOCAL_ORIGIN = "boardwright-local-release"


def write_local_refs(stage: Path, repo: Path) -> Path:
    """The release plan's tag check reads the local repository's tags (no remote is contacted).

    The stage's ``origin`` would be the host path of the repository, which the
    Linux container cannot open (and Git for Windows may not store as UTF-8).
    It becomes a fixed name that nothing fetches from; the plan's tag check
    reads this snapshot of the local tags instead (``BOARDWRIGHT_REMOTE_REFS``).
    """

    _git(stage, "remote", "set-url", "origin", LOCAL_ORIGIN)
    url = LOCAL_ORIGIN
    snapshot = {"schema": 1, "url": url, "tags": dict(sorted(local_tags(repo).items())), "source": "local"}
    path = stage / ".git" / container.REMOTE_REFS_FILE
    path.write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8", newline="\n")
    return path


def candidate_files(stage: Path) -> dict[str, Path]:
    """What a frozen candidate holds: the GitHub candidate artifact's set plus build records."""

    files: dict[str, Path] = {}
    for relative in (CANDIDATE_FILE.as_posix(), DEFAULT_MANIFEST.as_posix(), *AUTHORISED_PATHS, *REVIEW_FILES):
        if (stage / relative).is_file():
            files[relative] = stage / relative
    for name in REVIEW_OUTPUT_DIRS:
        root = stage / name
        for path in sorted(root.rglob("*")) if root.is_dir() else []:
            if path.is_file():
                files[path.relative_to(stage).as_posix()] = path
    for required in (CANDIDATE_FILE.as_posix(), DEFAULT_MANIFEST.as_posix()):
        if required not in files:
            raise LocalReleaseError(f"The candidate stage has no {required}; nothing was frozen.")
    return files


def build_candidate(config, request: ReleaseRequest, store: ReleaseStore, *, runner=None, host_runner=None,
                    echo=print) -> dict:
    """Build a candidate from the clean, committed HEAD and freeze it in the store."""

    repo = Path(config.root)
    result = plan(config, request.version, store)
    if result.problems:
        raise LocalReleaseError("Release plan refused:\n- " + "\n- ".join(result.problems))
    for note in result.notes:
        echo(f"note: {note}")
    before = container.host_tree_fingerprint(repo)
    workdir = stage_dir(repo, request.version)
    with stage_lock(workdir, purpose=f"local release candidate {request.version}"):
        container.stage(repo, dest=workdir, purpose=STAGE_PURPOSE)
        staged_head = _git(workdir, "rev-parse", "HEAD").strip()
        if staged_head != result.head:
            raise LocalReleaseError(f"HEAD moved while staging ({result.head[:12]} -> {staged_head[:12]}); nothing was built.")
        write_local_refs(workdir, repo)
        env = {**request.inputs(), "BOARDWRIGHT_SOURCE_SHA": result.head,
               "BOARDWRIGHT_REMOTE_REFS": f".git/{container.REMOTE_REFS_FILE}"}
        failed = container.run_candidate_steps(workdir, request.step_args(), env, local=True, runner=runner,
                                               host_runner=host_runner)
        if failed:
            raise LocalReleaseError(f"Candidate step failed: {failed}. Nothing was frozen, committed or tagged.")
        if _git(repo, "rev-parse", "HEAD").strip() != result.head or container.host_tree_fingerprint(repo) != before:
            raise LocalReleaseError("The repository changed while the candidate was built; nothing was frozen.")
        reviewed = (workdir / CANDIDATE_FILE).read_bytes()
        identity = json.loads(reviewed)
        if identity.get("schema") != CANDIDATE_SCHEMA or identity.get("source_sha") != result.head:
            raise LocalReleaseError("The review candidate does not bind the staged source commit; nothing was frozen.")
        candidate_id = sha256_bytes(reviewed)
        record = {
            "schema": 1,
            "kind": "local-release-candidate",
            "authority": "local",
            "version": request.version,
            "candidate_id": candidate_id,
            "source_sha": result.head,
            "branch": result.branch,
            "request": asdict(request),
            "release_date": identity.get("release_date"),
            "manifest_sha256": identity.get("manifest_sha256"),
            "image": container.image_for(request.kicad).reference,
            "created_at": _now(),
        }
        files: dict[str, Path | bytes] = dict(candidate_files(workdir))
        files[CANDIDATE_RECORD] = (json.dumps(record, indent=2, sort_keys=True) + "\n").encode("utf-8")
        with store.locked(f"freeze candidate {request.version}"):
            existing = store.entry_path("candidates", request.version, candidate_id)
            if existing.exists():
                echo(f"Candidate {candidate_id[:12]} is already frozen; nothing new was written.")
                return record
            entry = store.freeze("candidates", request.version, candidate_id, files)
            store.append_event({"event": "candidate", "version": request.version, "candidate_id": candidate_id,
                                "source_sha": result.head, "release_date": record["release_date"],
                                "manifest_sha256": record["manifest_sha256"],
                                "sums_sha256": sha256_file(entry.path / "SHA256SUMS")})
    echo(f"Candidate {request.version} frozen: {entry.path}")
    echo(f"  candidate id {candidate_id}")
    echo(f"  source {result.head}, release date {record['release_date']}, manifest {record['manifest_sha256'][:12]}")
    echo("Nothing was committed, tagged or pushed. Review it, then approve it (local, not server-enforced).")
    return record


# -- loading and verifying ---------------------------------------------------

def load_candidate(store: ReleaseStore, version: str, candidate_id: str = "") -> tuple[Path, dict, dict]:
    """The frozen candidate: ``(dir, candidate.json, review_candidate.json)``, integrity-checked."""

    store.check()
    if candidate_id:
        if not _HEX64.match(candidate_id):
            raise LocalReleaseError(f"Invalid candidate id {candidate_id!r}.")
        entries = [e for e in store.entries("candidates", version) if e.name == candidate_id]
    else:
        entries = store.entries("candidates", version)
        if len(entries) > 1:
            raise LocalReleaseError(f"Release {version} has {len(entries)} candidates; name one with --candidate.")
    if not entries:
        raise LocalReleaseError(f"No frozen candidate for {version}" + (f" with id {candidate_id}" if candidate_id else "") + ".")
    path = entries[0].path
    problems = store.verify_entry(path)
    if problems:
        raise LocalReleaseError("The frozen candidate was modified: " + "; ".join(problems))
    reviewed_bytes = (path / CANDIDATE_FILE).read_bytes()
    if sha256_bytes(reviewed_bytes) != path.name:
        raise LocalReleaseError("The frozen candidate's review_candidate.json does not match its candidate id.")
    record = json.loads((path / CANDIDATE_RECORD).read_text(encoding="utf-8"))
    reviewed = json.loads(reviewed_bytes)
    if (record.get("candidate_id"), record.get("version"), record.get("source_sha")) != (
            path.name, version, reviewed.get("source_sha")):
        raise LocalReleaseError("The frozen candidate record does not match its review candidate.")
    if sha256_file(path / DEFAULT_MANIFEST) != reviewed.get("manifest_sha256"):
        raise LocalReleaseError("The frozen accepted manifest does not match the reviewed manifest digest.")
    events = [e for e in store.events() if e.get("event") == "candidate" and e.get("candidate_id") == path.name]
    expected = {"version": version, "source_sha": record["source_sha"], "release_date": record.get("release_date"),
                "manifest_sha256": record.get("manifest_sha256"), "sums_sha256": sha256_file(path / "SHA256SUMS")}
    if len(events) != 1 or any(events[0].get(k) != v for k, v in expected.items()):
        raise LocalReleaseError("The frozen candidate does not match its release-store index event.")
    return path, record, reviewed


# -- review ------------------------------------------------------------------

REVIEW_ITEMS = (
    "Schematic PDF: revision cell, identity line and release date are correct",
    "Fabrication drawing: revision, identity and notes are correct; no unexpected change",
    "Assembly drawing: revision and identity are correct; no unexpected change",
    "Revision tables and CHANGELOG entry are correct",
    "releases.yaml entry (identities, baselines, change classes) is correct",
    "Baseline check result and build record were inspected",
)


def review_identity(record: dict, reviewed: dict) -> dict[str, str]:
    return {
        "Version": record["version"],
        "Candidate ID": record["candidate_id"],
        "Source SHA": reviewed["source_sha"],
        "Manifest sha256": reviewed["manifest_sha256"],
        "Release date": reviewed["release_date"],
    }


def review_template(record: dict, reviewed: dict, path: Path) -> str:
    rows = "\n".join(f"| {key} | {value} |" for key, value in review_identity(record, reviewed).items())
    items = "\n".join(f"- [ ] {item}" for item in REVIEW_ITEMS)
    return (
        f"# Local release review: {record['version']}\n\n"
        f"Frozen candidate: `{path}`\n\n"
        "This is a LOCAL review. Its approval is recorded with the Git identity\n"
        "(asserted, not authenticated) and is not enforced by any server.\n\n"
        "| Item | Value |\n| --- | --- |\n" + rows + "\n| Reviewer / date | |\n\n"
        "## Checks\n\n" + items + "\n\nNotes / deviations:\n\n"
        "Change the last line to `APPROVED: yes` only after every box is ticked.\n\n"
        "APPROVED: no\n"
    )


def check_review(text: str, record: dict, reviewed: dict) -> list[str]:
    problems = []
    rows = {}
    for line in text.splitlines():
        match = re.match(r"^\|\s*([^|]+?)\s*\|\s*([^|]*?)\s*\|\s*$", line)
        if match:
            rows[match.group(1)] = match.group(2).strip("` ")
    for key, value in review_identity(record, reviewed).items():
        if rows.get(key) != value:
            problems.append(f"The review record's {key} is {rows.get(key)!r}, not the candidate's {value!r}.")
    unticked = [line.strip() for line in text.splitlines() if re.match(r"^\s*[-*]\s*\[\s\]", line)]
    ticked = [line for line in text.splitlines() if re.match(r"^\s*[-*]\s*\[[xX]\]", line)]
    if unticked:
        problems.append(f"{len(unticked)} review item(s) are not ticked, first: {unticked[0]}")
    if not ticked:
        problems.append("The review record has no ticked items.")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or lines[-1] != "APPROVED: yes":
        problems.append("The review record's last line must be exactly `APPROVED: yes`.")
    return problems


def write_review_draft(store: ReleaseStore, version: str, candidate_id: str = "") -> Path:
    path, record, reviewed = load_candidate(store, version, candidate_id)
    draft = store.drafts() / f"REVIEW-{safe_name(version, 'version')}-{record['candidate_id'][:12]}.md"
    if not draft.exists():
        draft.write_text(review_template(record, reviewed, path), encoding="utf-8", newline="\n")
    return draft


# -- approval ----------------------------------------------------------------

def approve(config, store: ReleaseStore, version: str, review_path: Path, candidate_id: str = "", *,
            echo=print) -> dict:
    """Bind a completed review record to the exact frozen candidate (local, not server-enforced)."""

    repo = Path(config.root)
    with store.locked(f"approve {version}"):
        index = store.verify_index()
        if index:
            raise LocalReleaseError("The release-store index is inconsistent: " + "; ".join(index))
        path, record, reviewed = load_candidate(store, version, candidate_id)
        if tag_exists(repo, version):
            raise LocalReleaseError(f"Tag {version} already exists; a released version is never approved again.")
        if any(store.entries("releases", version)):
            raise LocalReleaseError(f"Release {version} is already published in the local release store.")
        text_bytes = Path(review_path).read_bytes()
        problems = check_review(text_bytes.decode("utf-8"), record, reviewed)
        if problems:
            raise LocalReleaseError("Review record refused:\n- " + "\n- ".join(problems))
        review_sha = sha256_bytes(text_bytes)
        others = [e for e in store.entries("approvals", version) if e.name != record["candidate_id"]]
        if others:
            raise LocalReleaseError(f"Release {version} already has an approval for candidate {others[0].name[:12]}; "
                                    "one version has one approved candidate.")
        existing = store.entry_path("approvals", version, record["candidate_id"])
        if existing.exists():
            current = json.loads((existing / APPROVAL_FILE).read_text(encoding="utf-8"))
            if current.get("review_record_sha256") == review_sha and not store.verify_entry(existing):
                echo(f"Candidate {record['candidate_id'][:12]} is already approved with this review record.")
                return current
            raise LocalReleaseError("This candidate already has a different approval; approvals are never replaced.")
        approval = {
            "schema": APPROVAL_SCHEMA,
            "authority": "local",
            "server_enforced": False,
            "authenticated": False,
            "version": version,
            "candidate_id": record["candidate_id"],
            "source_sha": reviewed["source_sha"],
            "manifest_sha256": reviewed["manifest_sha256"],
            "release_date": reviewed["release_date"],
            "inputs": {key: reviewed.get("inputs", {}).get(key, "") for key in INPUT_KEYS},
            "review_record_sha256": review_sha,
            "approver": git_identity(repo),
            "approved_at": _now(),
            "host": socket.gethostname(),
            "signing_configured": signing_configured(repo),
        }
        entry = store.freeze("approvals", version, record["candidate_id"], {
            APPROVAL_FILE: (json.dumps(approval, indent=2, sort_keys=True) + "\n").encode("utf-8"),
            f"REVIEW-{version}.md": text_bytes,
        })
        store.append_event({"event": "approval", "version": version, "candidate_id": record["candidate_id"],
                            "review_record_sha256": review_sha, "approver": approval["approver"],
                            "approval_sha256": sha256_file(entry.path / APPROVAL_FILE)})
    echo(f"Approved {version} candidate {record['candidate_id'][:12]} (local approval, not server-enforced; "
         f"approver {approval['approver']} asserted, not authenticated"
         f"{'; signing key configured for the commit and tag' if approval['signing_configured'] else '; unsigned'}).")
    echo("Nothing was committed, tagged or pushed.")
    return approval


def verify_approval(store: ReleaseStore, version: str) -> tuple[Path, dict, dict, dict]:
    """The single approval for ``version``, checked against its frozen candidate. Raises on any mismatch."""

    approvals = store.entries("approvals", version)
    if not approvals:
        raise LocalReleaseError(f"Release {version} has no local approval.")
    if len(approvals) > 1:
        raise LocalReleaseError(f"Release {version} has {len(approvals)} approvals; the store is inconsistent.")
    entry = approvals[0]
    problems = store.verify_entry(entry.path)
    if problems:
        raise LocalReleaseError("The approval was modified: " + "; ".join(problems))
    approval = json.loads((entry.path / APPROVAL_FILE).read_text(encoding="utf-8"))
    path, record, reviewed = load_candidate(store, version, entry.name)
    expected = {
        "schema": APPROVAL_SCHEMA, "authority": "local", "server_enforced": False, "authenticated": False,
        "version": version, "candidate_id": entry.name, "source_sha": reviewed["source_sha"],
        "manifest_sha256": reviewed["manifest_sha256"], "release_date": reviewed["release_date"],
        "inputs": {key: reviewed.get("inputs", {}).get(key, "") for key in INPUT_KEYS},
    }
    wrong = [key for key, value in expected.items() if approval.get(key) != value]
    review = entry.path / f"REVIEW-{version}.md"
    if not review.is_file() or sha256_file(review) != approval.get("review_record_sha256"):
        wrong.append("review_record_sha256")
    else:
        wrong.extend(f"review: {p}" for p in check_review(review.read_text(encoding="utf-8"), record, reviewed))
    if wrong:
        raise LocalReleaseError("The approval does not bind the frozen candidate: " + ", ".join(wrong))
    events = [e for e in store.events() if e.get("event") == "approval" and e.get("version") == version]
    if not events or events[-1].get("approval_sha256") != sha256_file(entry.path / APPROVAL_FILE):
        raise LocalReleaseError("The approval is not recorded in the release-store index.")
    return path, record, reviewed, approval


# -- status ------------------------------------------------------------------

def status_lines(store: ReleaseStore, version: str = "") -> list[str]:
    lines = [f"Local release store: {store.root}", f"Project id: {store.project_id}"]
    if not store.exists():
        return lines + ["No local releases yet (the store is created by the first candidate)."]
    index = store.verify_index()
    lines.append("Index: " + ("intact" if not index else "INCONSISTENT: " + "; ".join(index)))
    for path in store.abandoned():
        lines.append(f"Interrupted write: {path.name} (incomplete; `boardwright release local recover` removes it)")
    for kind in ("candidates", "approvals", "finalizations", "releases"):
        for entry in store.entries(kind, version):
            problems = store.verify_entry(entry.path)
            label = entry.name[:12] if kind in ("candidates", "approvals") else ""
            lines.append(f"{kind[:-1]:9} {entry.version} {label}".rstrip()
                         + (": intact" if not problems else ": MODIFIED: " + "; ".join(problems[:3])))
    return lines


def recover(store: ReleaseStore, echo=print) -> int:
    with store.locked("recover"):
        removed = store.remove_abandoned()
    for path in removed:
        echo(f"Removed interrupted write {path.name}.")
    if not removed:
        echo("Nothing to recover.")
    return 0


# -- command line ------------------------------------------------------------

def main(argv: list[str]) -> int:
    from .config import load_config

    parser = argparse.ArgumentParser(prog="boardwright release local",
                                     description="Standalone local releases (local approval, not server-enforced).")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("plan", help="Check that a local release candidate can be built (nothing runs).")
    p.add_argument("version")
    p.add_argument("--backend", default="", choices=("", "auto", "local", "github"))
    p = sub.add_parser("candidate", help="Build and freeze a candidate from the clean, committed HEAD.")
    p.add_argument("version")
    p.add_argument("--backend", default="", choices=("", "auto", "local", "github"))
    p.add_argument("--drawing-revision", required=True)
    p.add_argument("--revision-description", required=True)
    p.add_argument("--variant", default="RELEASED", choices=container.VARIANTS)
    p.add_argument("--kind", default="release", choices=("draft", "prerelease", "release"))
    p.add_argument("--change-class", default="")
    p.add_argument("--justification", default="")
    p.add_argument("--release-date", default="", metavar="YYYY-MM-DD")
    p.add_argument("--kicad", choices=container.SUPPORTED_KICAD, default=container.DEFAULT_KICAD)
    p = sub.add_parser("review", help="Write a review draft for a frozen candidate, or check a completed record.")
    p.add_argument("version")
    p.add_argument("--candidate", default="")
    p.add_argument("--check", type=Path, metavar="RECORD")
    p = sub.add_parser("approve", help="Bind a completed review record to the frozen candidate (local approval).")
    p.add_argument("version")
    p.add_argument("--record", type=Path, required=True)
    p.add_argument("--candidate", default="")
    p.add_argument("--backend", default="", choices=("", "auto", "local", "github"))
    p = sub.add_parser("finalize", help="Rebuild, verify against the approval, commit and tag (never pushed).")
    p.add_argument("version")
    p.add_argument("--sign", action="store_true", help="Sign the release commit and tag; fails if signing fails.")
    p.add_argument("--backend", default="", choices=("", "auto", "local", "github"))
    p = sub.add_parser("publish", help="Rebuild from the tag, verify, package into the release store.")
    p.add_argument("version")
    p.add_argument("--backend", default="", choices=("", "auto", "local", "github"))
    p = sub.add_parser("status", help="Show each version's state, integrity and next step.")
    p.add_argument("version", nargs="?", default="")
    p = sub.add_parser("recover", help="Complete interrupted steps that need no build; never resets or deletes.")
    p.add_argument("version", nargs="?", default="")
    args = parser.parse_args(argv)
    # One dispatcher for the CLI and the TUI (local_release_ops.run): parity by construction.
    from .local_release_ops import LocalArgs, run

    fields = {name: getattr(args, name) for name in LocalArgs.__dataclass_fields__ if hasattr(args, name)}
    try:
        config = load_config()
    except (BoardwrightError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return run(config, args.command, LocalArgs(**fields), echo=print,
               error=lambda line: print(line, file=sys.stderr))
