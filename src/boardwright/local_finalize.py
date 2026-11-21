# SPDX-License-Identifier: LicenseRef-Boardwright-Source-Available
# Copyright (c) Ryan Hicks

"""Finalise an approved local release: release commit, branch, annotated tag.

The local counterpart of ``prepare-release.yaml``'s approved run
(``docs/LOCAL_FIRST.md`` section 5). It rebuilds in a fresh stage of the
reviewed source commit with the reviewed inputs and date, runs
``review_gate.verify_candidate`` against the frozen candidate, discards build
side effects (``ci_cleanup``) and creates the release commit with
``release_commit`` (``Reviewed-Candidate``, ``Approval-Authority: local``,
``.boardwright/approval.json``).

State machine (each state is proven by objects, never by a status flag)::

    approved
      -> prepared   finalizations/<version>/ (finalize.json + release.bundle),
                    then refs/boardwright/local-release/<version> from the bundle
      -> advanced   refs/heads/<branch>: source -> release commit
                    (update-ref with the expected old SHA), then the working
                    tree follows (two-tree read-tree, never a reset)
      -> tagged     annotated tag <version> on the release commit

The bundle is frozen before any ref changes, so an interruption at any point
can be completed without rebuilding. Running ``finalize`` again continues
from the proven state; anything that conflicts (a different tag, a moved
branch, a changed candidate or approval) is refused. Nothing is pushed,
forced, reset or deleted.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import container
from .errors import BoardwrightError
from .local_release import (
    LocalReleaseError,
    ReleaseRequest,
    git_identity,
    tag_exists,
    verify_approval,
    write_local_refs,
)
from .release_store import ReleaseStore, safe_name, sha256_bytes, sha256_file
from .stage_lock import stage_lock

FINALIZE_FILE = "finalize.json"
BUNDLE_FILE = "release.bundle"
BUNDLE_BRANCH = "refs/heads/boardwright-release"
STAGE_PURPOSE = "local-release-finalize"
KEEP = ("README.md", "assets/renders", "CHANGELOG.md", ".boardwright")  # prepare-release's ci_cleanup --keep
SIGNING_KEYS = ("user.signingkey", "gpg.format", "gpg.program", "gpg.ssh.program", "gpg.ssh.allowedSignersFile",
                "gpg.x509.program")


def checkpoint(name: str) -> None:
    """A point immediately before or after a Git ref change (tests interrupt here)."""


def pending_ref(version: str) -> str:
    return f"refs/boardwright/local-release/{safe_name(version, 'version')}"


def _git(root: Path, *args: str, check: bool = True, env: dict | None = None) -> subprocess.CompletedProcess:
    done = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env)
    if check and done.returncode != 0:
        raise LocalReleaseError(f"git {' '.join(args[:3])} failed: {(done.stderr or done.stdout).strip()}")
    return done


def _rev(root: Path, ref: str) -> str:
    return _git(root, "rev-parse", "--verify", "--quiet", ref, check=False).stdout.strip()


def _is_ancestor(root: Path, older: str, newer: str) -> bool:
    return _git(root, "merge-base", "--is-ancestor", older, newer, check=False).returncode == 0


def _clean(root: Path) -> bool:
    return not container.host_tree_fingerprint(root).strip()


def tag_message(version: str, record: dict) -> str:
    return (f"Release {version}\n\n"
            "Local release: the approval is local, not server-enforced; the approver is the Git identity "
            "(asserted, not authenticated).\n\n"
            f"Source-Commit: {record['source_sha']}\n"
            f"Reviewed-Candidate: {record['candidate_id']}\n"
            "Approval-Authority: local\n")


@dataclass(frozen=True)
class FinalizeState:
    version: str
    stage: str            # "approved" | "prepared" | "imported" | "advanced" | "tagged"
    release_commit: str
    detail: str


# -- inspection --------------------------------------------------------------

def load_finalization(store: ReleaseStore, version: str) -> tuple[Path, dict] | None:
    entries = store.entries("finalizations", version)
    if not entries:
        return None
    path = entries[0].path
    problems = store.verify_entry(path)
    if problems:
        raise LocalReleaseError("The finalisation record was modified: " + "; ".join(problems))
    record = json.loads((path / FINALIZE_FILE).read_text(encoding="utf-8"))
    events = [e for e in store.events() if e.get("event") == "prepared" and e.get("version") == version]
    if len(events) != 1 or events[0].get("finalize_sha256") != sha256_file(path / FINALIZE_FILE):
        raise LocalReleaseError("The finalisation record does not match its release-store index event.")
    return path, record


def verify_release_commit(repo: Path, commit: str, record: dict, approval_bytes: bytes) -> list[str]:
    """What a release commit must be: one commit on the source, local trailers, the approval committed."""

    problems = []
    if _git(repo, "cat-file", "-t", commit, check=False).stdout.strip() != "commit":
        return [f"Release commit {commit[:12]} is not in the repository."]
    parents = _git(repo, "rev-list", "--parents", "-n", "1", commit).stdout.split()[1:]
    if parents != [record["source_sha"]]:
        problems.append(f"Release commit {commit[:12]} is not a single commit on the reviewed source.")
    body = _git(repo, "log", "-1", "--format=%B", commit).stdout
    for line in (f"Source-Commit: {record['source_sha']}", f"Reviewed-Candidate: {record['candidate_id']}",
                 "Approval-Authority: local"):
        if line not in body.splitlines():
            problems.append(f"Release commit {commit[:12]} lacks the trailer `{line}`.")
    if "Reviewed-Run:" in body:
        problems.append(f"Release commit {commit[:12]} claims a GitHub Reviewed-Run.")
    shown = subprocess.run(["git", "show", f"{commit}:.boardwright/approval.json"], cwd=repo, capture_output=True)
    if shown.returncode != 0 or shown.stdout != approval_bytes:
        problems.append(f"Release commit {commit[:12]} does not carry the store's approval.json.")
    if record.get("release_tree") and _rev(repo, f"{commit}^{{tree}}") != record["release_tree"]:
        problems.append(f"Release commit {commit[:12]} has a different tree from the finalisation record.")
    return problems


def signature_problems(repo: Path, version: str, record: dict, approval: dict) -> list[str]:
    """For a signed finalisation: the commit and (if present) the tag must be signed by the approver, every time."""

    from .local_signing import release_signatures

    return release_signatures(repo, version, record, approval).problems


def finalize_state(repo: Path, store: ReleaseStore, version: str) -> FinalizeState:
    """The proven finalisation state; raises on a conflict."""

    found = load_finalization(store, version)
    if found is None:
        if tag_exists(repo, version):
            raise LocalReleaseError(f"Tag {version} exists but was not made by this local release; refusing.")
        return FinalizeState(version, "approved", "", "approved; not finalised")
    path, record = found
    commit = record["release_commit"]
    ref = _rev(repo, pending_ref(version))
    if ref and ref != commit:
        raise LocalReleaseError(f"{pending_ref(version)} points to {ref[:12]}, not the recorded release commit.")
    tag = _rev(repo, f"refs/tags/{version}")
    if tag:
        if _git(repo, "cat-file", "-t", tag).stdout.strip() != "tag":
            raise LocalReleaseError(f"Tag {version} is not annotated; refusing.")
        if _rev(repo, f"refs/tags/{version}^{{commit}}") != commit:
            raise LocalReleaseError(f"Tag {version} points elsewhere than the release commit {commit[:12]}; refusing.")
        if f"Reviewed-Candidate: {record['candidate_id']}" not in _git(repo, "cat-file", "tag", tag).stdout:
            raise LocalReleaseError(f"Tag {version} does not name the reviewed candidate; refusing.")
        return FinalizeState(version, "tagged", commit, f"tag {version} on {commit[:12]}")
    branch_head = _rev(repo, f"refs/heads/{record['branch']}")
    if branch_head and _is_ancestor(repo, commit, branch_head) and _rev(repo, commit):
        return FinalizeState(version, "advanced", commit, f"{record['branch']} contains {commit[:12]}; not tagged")
    if ref:
        return FinalizeState(version, "imported", commit, f"release commit {commit[:12]} imported; branch not advanced")
    return FinalizeState(version, "prepared", commit, f"release commit {commit[:12]} recorded; not imported")


# -- the transitions ---------------------------------------------------------

def _copy_signing_config(repo: Path, stage: Path) -> None:
    for key in SIGNING_KEYS:
        value = _git(repo, "config", key, check=False).stdout.strip()
        if value:
            _git(stage, "config", key, value)


def _prepare(config, store: ReleaseStore, version: str, candidate_path: Path, record: dict, reviewed: dict,
             approval: dict, *, sign: bool, runner, host_runner, echo) -> tuple[Path, dict]:
    from . import ci_cleanup, release_commit
    from .review_gate import verify_candidate

    repo = Path(config.root)
    source = approval["source_sha"]
    request = ReleaseRequest(**record["request"])
    if request.inputs() != approval["inputs"] or request.release_date != approval["release_date"]:
        raise LocalReleaseError("The candidate's request differs from the approved inputs and date.")
    approval_path = store.entry_path("approvals", version, record["candidate_id"]) / "approval.json"
    approval_bytes = approval_path.read_bytes()
    workdir = (container.default_stage_root() / f"{repo.name}-local-{safe_name(version, 'version')}-final").resolve()
    with stage_lock(workdir, purpose=f"local release finalize {version}"):
        # HEAD on the release branch (proven equal to the approved source by the
        # caller): release preparation checks the branch, as in the approved run.
        container.stage(repo, dest=workdir, purpose=STAGE_PURPOSE)
        if _rev(workdir, "HEAD") != source or _git(workdir, "symbolic-ref", "--quiet", "--short", "HEAD",
                                                   check=False).stdout.strip() != record["branch"]:
            raise LocalReleaseError("The finalisation stage is not the reviewed source commit; nothing was committed.")
        write_local_refs(workdir, repo)
        env = {**request.inputs(), "BOARDWRIGHT_SOURCE_SHA": source,
               "BOARDWRIGHT_REMOTE_REFS": f".git/{container.REMOTE_REFS_FILE}"}
        echo(f"[finalize] rebuilding {version} from {source[:12]} with the reviewed inputs and date {request.release_date}")
        failed = container.run_candidate_steps(workdir, request.step_args(), env, local=True, runner=runner,
                                               host_runner=host_runner)
        if failed:
            raise LocalReleaseError(f"Finalisation step failed: {failed}. Nothing was committed or tagged.")
        problems = verify_candidate(workdir, candidate_path, environ=env)
        if problems:
            raise LocalReleaseError("The rebuild does not match the approved candidate; nothing was committed:\n- "
                                    + "\n- ".join(problems))
        echo("[finalize] the rebuild matches the approved candidate (review gate)")
        _restored, remaining = ci_cleanup.discard_side_effects(workdir, KEEP)
        if remaining:
            raise LocalReleaseError("Build side effects remain outside the release files: " + ", ".join(remaining))
        (workdir / ".boardwright" / "approval.json").write_bytes(approval_bytes)
        name, _, email = git_identity(repo).partition(" <")
        _git(workdir, "config", "user.name", name)
        _git(workdir, "config", "user.email", email.rstrip(">"))
        if sign:
            _copy_signing_config(repo, workdir)
        try:
            commit = release_commit.commit_release(
                workdir, version=version, variant=request.variant, source_sha=source,
                reviewed_candidate=record["candidate_id"], local_approval=True, sign=sign)
        except subprocess.CalledProcessError as exc:
            raise LocalReleaseError("The release commit could not be created"
                                    + (" (signing was requested and failed; nothing was released unsigned)" if sign else "")
                                    + f": {(exc.stderr or '').strip()}") from exc
        if sign:
            from .local_signing import approver_email, verify_object

            _valid, matches, problem = verify_object(workdir, commit, "commit", approver_email(approval))
            if not matches:
                raise LocalReleaseError(f"{problem} Nothing was recorded, imported or tagged.")
        finalization = {
            "schema": 1, "version": version, "candidate_id": record["candidate_id"],
            "approval_sha256": sha256_bytes(approval_bytes), "source_sha": source, "branch": record["branch"],
            "release_commit": commit, "release_tree": _rev(workdir, f"{commit}^{{tree}}"), "signed": sign,
            "review_gate": "match", "image": container.image_for(request.kicad).reference,
            "pending_ref": pending_ref(version),
            # Signed: the commit was verified as signed by this approver (signer binding, audit F-R7).
            "signing": {"format": "ssh", "approver": approval["approver"], "authenticated": False} if sign else None,
        }
        problems = verify_release_commit(workdir, commit, finalization, approval_bytes)
        if problems:
            raise LocalReleaseError("The release commit is not as required: " + "; ".join(problems))
        _git(workdir, "update-ref", BUNDLE_BRANCH, commit)
        bundle = workdir / ".git" / BUNDLE_FILE
        _git(workdir, "bundle", "create", str(bundle), BUNDLE_BRANCH, f"^{source}")
        files = {FINALIZE_FILE: (json.dumps(finalization, indent=2, sort_keys=True) + "\n").encode("utf-8"),
                 BUNDLE_FILE: bundle}
        with_entry = store.freeze("finalizations", version, "", files)
        store.append_event({"event": "prepared", "version": version, "candidate_id": record["candidate_id"],
                            "release_commit": commit, "finalize_sha256": sha256_file(with_entry.path / FINALIZE_FILE),
                            "bundle_sha256": sha256_file(with_entry.path / BUNDLE_FILE)})
    echo(f"[finalize] release commit {commit[:12]} prepared and recorded in the store")
    return with_entry.path, finalization


def import_release_commit(repo: Path, entry: Path, record: dict, version: str) -> bool:
    """Fetch the recorded release commit from the bundle into the pending ref (create-only)."""

    ref = pending_ref(version)
    if _rev(repo, ref):
        return False
    bundle = str(entry / BUNDLE_FILE)
    _git(repo, "bundle", "verify", bundle)
    checkpoint("before-import")
    _git(repo, "fetch", "--no-tags", "--no-write-fetch-head", bundle, f"{BUNDLE_BRANCH}:{ref}")
    checkpoint("after-import")
    if _rev(repo, ref) != record["release_commit"]:
        raise LocalReleaseError(f"The imported release commit is not {record['release_commit'][:12]}.")
    return True


def sync_worktree(repo: Path, source: str, commit: str) -> bool:
    """After the branch moved: bring the index and files from the source tree to the release tree.

    Only when they still hold exactly the source tree (an interrupted
    advance); never a reset, never over local changes.
    """

    index_tree = _git(repo, "write-tree").stdout.strip()
    release_tree = _rev(repo, f"{commit}^{{tree}}")
    head_tree = _rev(repo, "HEAD^{tree}")
    if index_tree == head_tree and not container.host_tree_fingerprint(repo).strip():
        return False
    if index_tree != _rev(repo, f"{source}^{{tree}}") or _git(repo, "diff", "--quiet", check=False).returncode != 0:
        raise LocalReleaseError("The working tree differs from both the source and the release commit; "
                                "it was not changed. Inspect it, then run finalize or recover again.")
    _git(repo, "read-tree", "-m", "-u", source, commit)
    checkpoint("after-worktree")
    return index_tree != release_tree


def advance_branch(repo: Path, record: dict) -> bool:
    branch, source, commit = record["branch"], record["source_sha"], record["release_commit"]
    head = _rev(repo, f"refs/heads/{branch}")
    if head == commit or (head and _is_ancestor(repo, commit, head)):
        if _git(repo, "symbolic-ref", "--quiet", "HEAD", check=False).stdout.strip() == f"refs/heads/{branch}" \
                and head == commit:
            sync_worktree(repo, source, commit)
        return False
    if head != source:
        raise LocalReleaseError(f"{branch} is at {head[:12] or '(none)'}, not the reviewed source {source[:12]}; "
                                "the release branch is never reset or merged. Nothing was changed.")
    if _git(repo, "symbolic-ref", "--quiet", "HEAD", check=False).stdout.strip() != f"refs/heads/{branch}":
        raise LocalReleaseError(f"Check out {branch} to finalise; nothing was changed.")
    if not _clean(repo):
        raise LocalReleaseError("The working tree has changes; nothing was changed.")
    checkpoint("before-branch")
    _git(repo, "update-ref", "-m", f"boardwright: local release {record['version']}", f"refs/heads/{branch}",
         commit, source)
    checkpoint("after-branch")
    sync_worktree(repo, source, commit)
    return True


def create_tag(repo: Path, version: str, record: dict, *, sign: bool, approval: dict | None = None) -> bool:
    commit = record["release_commit"]
    if _rev(repo, f"refs/tags/{version}"):
        return False  # finalize_state verified it
    args = ["tag", "-a", *(["-s"] if sign else []), "-m", tag_message(version, record), version, commit]
    checkpoint("before-tag")
    done = _git(repo, *args, check=False)
    if done.returncode != 0:
        raise LocalReleaseError(f"Tag {version} was not created"
                                + (" (signing was requested and failed; no unsigned tag was made)" if sign else "")
                                + f": {(done.stderr or done.stdout).strip()}")
    checkpoint("after-tag")
    if sign:
        from .local_signing import approver_email, verify_object

        _valid, matches, problem = verify_object(repo, version, "tag", approver_email(approval or {}))
        if not matches:
            raise LocalReleaseError(f"{problem} The tag exists; it is never deleted automatically.")
    return True


def _record_events(store: ReleaseStore, version: str, record: dict, state: FinalizeState) -> None:
    done = {e.get("event") for e in store.events() if e.get("version") == version}
    order = ["imported", "advanced", "tagged"]
    reached = order[: order.index(state.stage) + 1] if state.stage in order else []
    for name in reached:
        if name not in done:
            store.append_event({"event": name, "version": version, "release_commit": record["release_commit"]})


def finalize(config, store: ReleaseStore, version: str, *, sign: bool = False, runner=None, host_runner=None,
             echo=print) -> FinalizeState:
    repo = Path(config.root)
    with store.locked(f"finalize {version}"):
        problems = store.verify_index()
        if problems:
            raise LocalReleaseError("The release-store index is inconsistent: " + "; ".join(problems))
        candidate_path, record, reviewed, approval = verify_approval(store, version)
        if record.get("branch") != config.release_branch:
            raise LocalReleaseError(f"The candidate was built on {record.get('branch')}, not the release branch "
                                    f"{config.release_branch}.")
        if any(store.entries("releases", version)):
            raise LocalReleaseError(f"Release {version} is already published.")
        state = finalize_state(repo, store, version)
        if sign and state.stage != "tagged":
            # Before any build or tag: the configured key must stand for the approver alone (F-R7).
            from .local_signing import preflight

            preflight(repo, approval)
        if state.stage == "approved":
            if not _clean(repo):
                raise LocalReleaseError("The working tree has uncommitted or untracked changes; nothing was done.")
            if _rev(repo, "HEAD") != approval["source_sha"]:
                raise LocalReleaseError(f"HEAD moved from the approved source {approval['source_sha'][:12]}; "
                                        "finalise the approved commit or build a new candidate. Nothing was done.")
            if _rev(repo, pending_ref(version)):
                raise LocalReleaseError(f"{pending_ref(version)} exists without a finalisation record; refusing.")
            entry, finalization = _prepare(config, store, version, candidate_path, record, reviewed, approval,
                                           sign=sign, runner=runner, host_runner=host_runner, echo=echo)
        else:
            entry, finalization = load_finalization(store, version)
            if finalization["signed"] != sign:
                raise LocalReleaseError(f"This finalisation was started {'with' if finalization['signed'] else 'without'} "
                                        "--sign; continue it the same way.")
            echo(f"[finalize] continuing from: {state.detail}")
        approval_bytes = (store.entry_path("approvals", version, record["candidate_id"]) / "approval.json").read_bytes()
        if (finalization["candidate_id"], finalization["approval_sha256"], finalization["source_sha"]) != (
                record["candidate_id"], sha256_bytes(approval_bytes), approval["source_sha"]):
            raise LocalReleaseError("The finalisation record does not match the approval; refusing.")
        import_release_commit(repo, entry, finalization, version)
        problems = verify_release_commit(repo, finalization["release_commit"], finalization, approval_bytes)
        if problems:
            raise LocalReleaseError("The release commit is not as required: " + "; ".join(problems))
        _record_events(store, version, finalization, finalize_state(repo, store, version))
        advance_branch(repo, finalization)
        _record_events(store, version, finalization, finalize_state(repo, store, version))
        create_tag(repo, version, finalization, sign=sign, approval=approval)
        state = finalize_state(repo, store, version)
        if state.stage != "tagged" or _rev(repo, f"refs/heads/{finalization['branch']}") is None:
            raise LocalReleaseError(f"Finalisation did not reach the tagged state ({state.detail}).")
        _record_events(store, version, finalization, state)
        problems = signature_problems(repo, version, finalization, approval)
        if problems:
            raise LocalReleaseError(" ".join(problems))
    signed = (f" (signed by the approver {approval['approver']}; the key mapping is local, not authenticated)" if sign
              else " (unsigned)")
    echo(f"Finalised {version}: release commit {finalization['release_commit'][:12]} on {finalization['branch']}, "
         f"annotated tag {version}{signed}.")
    echo("Local approval: not server-enforced; the approver is asserted, not authenticated. The tag was not pushed.")
    return state
