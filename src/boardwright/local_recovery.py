# SPDX-License-Identifier: LicenseRef-Boardwright-Source-Available
# Copyright (c) Ryan Hicks

"""Status and recovery of local releases (``docs/LOCAL_FIRST.md`` section 5).

``version_state`` proves where a version is in
``candidate -> approved -> prepared -> imported -> advanced -> tagged ->
published`` from its objects (store entries, checksums, index events, refs
and tags) and names the next action. ``recover`` performs only steps that
need no build and change nothing already valid:

- remove interrupted store writes (owned ``.tmp-*`` directories);
- append the index event of a complete, checksummed entry whose event was
  not written (an interruption between the rename and the append);
- import the recorded release commit from its frozen bundle;
- bring the working tree to the release commit after an interrupted branch
  advance (only when it still holds exactly the source tree).

It never builds, commits, tags, pushes, forces, resets or deletes a ref; a
missing tag is created by ``finalize`` (with the same signing choice).
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .errors import BoardwrightError
from .local_finalize import (
    FINALIZE_FILE,
    _rev,
    finalize_state,
    import_release_commit,
    load_finalization,
    pending_ref,
    sync_worktree,
)
from .local_publish import PUBLISH_FILE, load_release
from .local_release import APPROVAL_FILE, CANDIDATE_FILE, CANDIDATE_RECORD, LocalReleaseError, verify_approval
from .release_store import ReleaseStore, sha256_file

NEXT = {
    "candidate": "review the candidate, then `boardwright release local approve`",
    "approved": "`boardwright release local finalize`",
    "prepared": "`boardwright release local recover` (imports the release commit) or finalize",
    "imported": "`boardwright release local finalize` (advances the branch and tags)",
    "advanced": "`boardwright release local finalize` (creates the tag)",
    "tagged": "`boardwright release local publish`",
    "published": "nothing: the release is published (the tag was not pushed)",
}


@dataclass
class VersionState:
    version: str
    stage: str = "none"
    detail: str = ""
    problems: list[str] = field(default_factory=list)
    recoverable: list[str] = field(default_factory=list)
    signature: str = ""  # the signature facts of a finalised release (SignatureReport.line)

    @property
    def next_action(self) -> str:
        if self.problems:
            return "resolve the problems above; nothing is changed automatically"
        if self.recoverable:
            return "`boardwright release local recover`"
        return NEXT.get(self.stage, "")


def versions(store: ReleaseStore) -> list[str]:
    names = set()
    for kind in ("candidates", "approvals", "finalizations", "releases"):
        names.update(entry.version for entry in store.entries(kind))
    return sorted(names)


def _missing_events(store: ReleaseStore, version: str) -> list[dict]:
    """The index events of complete, checksummed entries whose event was not written.

    An interruption between an entry's rename and its index append. Returned
    in lifecycle order (candidate, approval, prepared, published) as the
    events to append; an entry that is incomplete or inconsistent with its own
    id is not offered (``status`` reports it as modified).
    """

    events = store.events()
    have = {(e.get("event"), e.get("version"), e.get("candidate_id")) for e in events}
    have_version = {(e.get("event"), e.get("version")) for e in events}
    missing: list[dict] = []
    for entry in store.entries("candidates", version):
        if ("candidate", entry.version, entry.name) in have or store.verify_entry(entry.path):
            continue
        record = json.loads((entry.path / CANDIDATE_RECORD).read_text(encoding="utf-8"))
        if sha256_file(entry.path / CANDIDATE_FILE) != entry.name or record.get("candidate_id") != entry.name \
                or record.get("version") != entry.version:
            continue
        missing.append({"event": "candidate", "version": entry.version, "candidate_id": entry.name,
                        "source_sha": record["source_sha"], "release_date": record.get("release_date"),
                        "manifest_sha256": record.get("manifest_sha256"),
                        "sums_sha256": sha256_file(entry.path / "SHA256SUMS")})
    for entry in store.entries("approvals", version):
        if ("approval", entry.version, entry.name) in have or store.verify_entry(entry.path):
            continue
        approval = json.loads((entry.path / APPROVAL_FILE).read_text(encoding="utf-8"))
        if approval.get("candidate_id") != entry.name or approval.get("version") != entry.version:
            continue
        missing.append({"event": "approval", "version": entry.version, "candidate_id": entry.name,
                        "review_record_sha256": approval.get("review_record_sha256"),
                        "approver": approval.get("approver"),
                        "approval_sha256": sha256_file(entry.path / APPROVAL_FILE)})
    for entry in store.entries("finalizations", version):
        if ("prepared", entry.version) in have_version or store.verify_entry(entry.path):
            continue
        record = json.loads((entry.path / FINALIZE_FILE).read_text(encoding="utf-8"))
        missing.append({"event": "prepared", "version": entry.version, "candidate_id": record["candidate_id"],
                        "release_commit": record["release_commit"],
                        "finalize_sha256": sha256_file(entry.path / FINALIZE_FILE),
                        "bundle_sha256": sha256_file(entry.path / "release.bundle")})
    for entry in store.entries("releases", version):
        if ("published", entry.version) in have_version or store.verify_entry(entry.path):
            continue
        record = json.loads((entry.path / PUBLISH_FILE).read_text(encoding="utf-8"))
        if sha256_file(entry.path / record["zip"]) != record["zip_sha256"]:
            continue
        missing.append({"event": "published", "version": entry.version, "zip_sha256": record["zip_sha256"],
                        "publish_sha256": sha256_file(entry.path / PUBLISH_FILE),
                        "sums_sha256": sha256_file(entry.path / "SHA256SUMS")})
    return missing


def version_state(repo: Path, store: ReleaseStore, version: str) -> VersionState:
    state = VersionState(version)
    if store.entries("candidates", version):
        state.stage, state.detail = "candidate", "candidate frozen; not approved"
    for event in _missing_events(store, version):
        state.recoverable.append(f"the {event['event']} entry is complete but its index event was not written")
    if state.recoverable:
        return state
    approval: dict = {}
    if store.entries("approvals", version):
        try:
            approval = verify_approval(store, version)[3]
        except BoardwrightError as exc:
            state.problems.append(str(exc))
            return state
        state.stage, state.detail = "approved", "approved; not finalised"
    if state.stage != "approved":
        return state
    try:
        finalized = finalize_state(repo, store, version)
    except BoardwrightError as exc:
        state.problems.append(str(exc))
        return state
    state.stage, state.detail = finalized.stage, finalized.detail
    if finalized.stage in ("advanced", "tagged"):
        from .local_signing import release_signatures

        found = load_finalization(store, version)
        if found:
            report = release_signatures(repo, version, found[1], approval)
            state.signature = report.line()
            state.problems.extend(report.problems)
    if finalized.stage == "prepared":
        state.recoverable.append("the release commit is recorded but not imported")
    if finalized.stage in ("advanced", "tagged") and _rev(repo, "HEAD") == finalized.release_commit:
        # Read-only: compare the index with the release tree (write-tree would write an object).
        if subprocess.run(["git", "diff-index", "--cached", "--quiet", finalized.release_commit, "--"],
                          cwd=repo, capture_output=True).returncode != 0:
            state.recoverable.append("the branch advanced but the working tree still holds the source tree")
    if finalized.stage == "tagged" and store.entries("releases", version):
        try:
            load_release(store, version)
        except BoardwrightError as exc:
            state.problems.append(str(exc))
            return state
        state.stage, state.detail = "published", f"published; {finalized.detail}"
    return state


def status_lines(repo: Path, store: ReleaseStore, version: str = "") -> list[str]:
    from .local_release import status_lines as store_lines

    lines = store_lines(store, version)
    if not store.exists():
        return lines
    if store.verify_index():
        # Reported above as INCONSISTENT; no state is derived from an index that cannot be trusted.
        return lines + ["Releases not evaluated: the release-store index is inconsistent. Nothing is changed "
                        "automatically; restore the index from a backup or inspect it."]
    for name in [version] if version else versions(store):
        state = version_state(repo, store, name)
        lines.append(f"Release {name}: {state.stage} ({state.detail})")
        if state.signature:
            lines.append(f"  {state.signature}")
        lines += [f"  PROBLEM: {p}" for p in state.problems]
        lines += [f"  interrupted: {r}" for r in state.recoverable]
        lines.append(f"  next: {state.next_action}")
    return lines


def recover(config, store: ReleaseStore, version: str = "", *, echo=print) -> int:
    repo = Path(config.root)
    with store.locked("recover"):
        for path in store.remove_abandoned():
            echo(f"Removed interrupted store write {path.name}.")
        if store.verify_index():
            raise LocalReleaseError("The release-store index is inconsistent: " + "; ".join(store.verify_index()))
        for name in [version] if version else versions(store):
            for event in _missing_events(store, name):
                store.append_event({**event, "recovered": True})
                echo(f"Recorded the missing {event['event']} event for {name} (entry complete and checksummed).")
            state = version_state(repo, store, name)
            if state.problems:
                echo(f"{name}: not recovered: " + "; ".join(state.problems))
                continue
            if state.stage in ("prepared", "imported", "advanced", "tagged"):
                entry = store.entries("finalizations", name)[0].path
                record = json.loads((entry / FINALIZE_FILE).read_text(encoding="utf-8"))
                if import_release_commit(repo, entry, record, name):
                    echo(f"Imported release commit {record['release_commit'][:12]} into {pending_ref(name)}.")
                if _rev(repo, f"refs/heads/{record['branch']}") == record["release_commit"] \
                        and _rev(repo, "HEAD") == record["release_commit"]:
                    if sync_worktree(repo, record["source_sha"], record["release_commit"]):
                        echo(f"Updated the working tree to release commit {record['release_commit'][:12]}.")
            state = version_state(repo, store, name)
            echo(f"{name}: {state.stage}; next: {state.next_action}")
    return 0
