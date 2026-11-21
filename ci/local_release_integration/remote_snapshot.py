# SPDX-License-Identifier: LicenseRef-Boardwright-Source-Available
# Copyright (c) Ryan Hicks
"""Read-only snapshot of this GitHub repository's refs, releases and the held candidate (testbench only).

    remote_snapshot.py capture OUT.json HELD_RUN_ID
    remote_snapshot.py fetch-held HELD_RUN_ID DEST
    remote_snapshot.py compare BEFORE.json AFTER.json

Runs only in the workflow's snapshot steps, which are the only steps given the
read-only token (contents: read, actions: read); the local-release processes
never see it. ``capture`` and ``fetch-held`` fail unless HELD_RUN_ID is a
successful held 0.1.8 prepare-release run on this workflow's commit: dispatched
on main with review_hold (its commit, tag, dispatch and approved-run steps were
skipped) and exactly one unexpired candidate artifact. ``fetch-held`` downloads
that artifact, verifies its SHA-256 against GitHub's digest, extracts it and
writes SHA256SUMS. ``compare`` fails if any branch, tag, visible release,
release asset or the held candidate changed. Draft releases are not visible to
a read-only token; their tags are compared instead.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

WORKFLOW = ".github/workflows/prepare-release.yaml"
VERSION = "0.1.8"
TITLE = f"Prepare {VERSION} (RELEASED, draft)"
ARTIFACT = f"boardwright-release-candidate-{VERSION}"
HELD_STEPS = {  # step name -> required conclusion in a review_hold run without approved_review_run
    "Check approved review run": "skipped",
    "Download reviewed candidate": "skipped",
    "Build release candidate in the pinned container": "success",
    "Upload release candidate for review": "success",
    "Stop for review": "success",
    "Commit accepted release state": "skipped",
    "Create and push tag": "skipped",
    "Dispatch tag release workflow": "skipped",
}
FORBIDDEN_TAGS = ("refs/tags/0.1.8", "refs/tags/0.1.9")


def gh(path: str, *, raw: bool = False):
    done = subprocess.run(["gh", "api", path], capture_output=True, check=True)
    return done.stdout if raw else json.loads(done.stdout.decode("utf-8"))


def api(path: str) -> list | dict:
    done = subprocess.run(["gh", "api", "--paginate", "--slurp", path], capture_output=True, text=True, check=True)
    pages = json.loads(done.stdout)
    if all(isinstance(page, list) for page in pages):
        return [item for page in pages for item in page]
    return pages


def held_candidate(repo: str, run_id: str) -> dict:
    """Verify the held run (raises SystemExit) and return its identity and artifact."""
    if not run_id.isdigit():
        raise SystemExit(f"FAIL held run id must be numeric: {run_id!r}")
    run = gh(f"repos/{repo}/actions/runs/{run_id}")
    expected = {"path": WORKFLOW, "event": "workflow_dispatch", "head_branch": "main", "status": "completed",
                "conclusion": "success", "head_sha": os.environ["GITHUB_SHA"], "display_title": TITLE}
    wrong = {k: run.get(k) for k, v in expected.items() if run.get(k) != v}
    if wrong:
        raise SystemExit(f"FAIL held run {run_id} is not a held {VERSION} candidate on this commit: {wrong}")
    steps = {s["name"]: s["conclusion"] for job in gh(f"repos/{repo}/actions/runs/{run_id}/jobs")["jobs"]
             for s in job["steps"]}
    wrong = {k: steps.get(k) for k, v in HELD_STEPS.items() if steps.get(k) != v}
    if wrong:
        raise SystemExit(f"FAIL held run {run_id} was not a review_hold run without approval: {wrong}")
    artifacts = [a for a in gh(f"repos/{repo}/actions/runs/{run_id}/artifacts?per_page=100")["artifacts"]
                 if a["name"] == ARTIFACT]
    if len(artifacts) != 1 or artifacts[0]["expired"] or not (artifacts[0].get("digest") or "").startswith("sha256:"):
        raise SystemExit(f"FAIL held run {run_id} must have one unexpired {ARTIFACT} with a digest: {artifacts}")
    a = artifacts[0]
    return {"run_id": int(run_id), "head_sha": run["head_sha"], "run_attempt": run["run_attempt"],
            "artifact": {"id": a["id"], "name": a["name"], "size": a["size_in_bytes"], "digest": a["digest"],
                         "expires_at": a["expires_at"]}}


def capture(out: str, run_id: str) -> int:
    repo = os.environ["GITHUB_REPOSITORY"]
    refs = {r["ref"]: r["object"]["sha"] for kind in ("heads", "tags")
            for r in api(f"repos/{repo}/git/matching-refs/{kind}")}
    present = [t for t in FORBIDDEN_TAGS if t in refs]
    if present:
        raise SystemExit(f"FAIL the repository already has {present}")
    releases = sorted(({
        "id": r["id"], "tag": r["tag_name"], "name": r["name"], "draft": r["draft"], "prerelease": r["prerelease"],
        "target": r["target_commitish"], "published_at": r["published_at"], "body_sha256": hashlib.sha256((r.get("body") or "").encode("utf-8")).hexdigest(),
        "assets": sorted((a["id"], a["name"], a["size"], a.get("digest"), a["updated_at"]) for a in r["assets"]),
    } for r in api(f"repos/{repo}/releases?per_page=100")), key=lambda r: r["id"])
    held = held_candidate(repo, run_id)
    snapshot = {"repository": repo, "refs": refs, "releases": releases, "held_candidate": held}
    with open(out, "w", encoding="utf-8") as handle:
        json.dump(snapshot, handle, indent=2, sort_keys=True, default=list)
    print(f"{len(refs)} refs, {len(releases)} visible releases, "
          f"{sum(len(r['assets']) for r in releases)} assets, held run {run_id} {held['artifact']['digest']}")
    return 0


def fetch_held(run_id: str, dest: str) -> int:
    repo = os.environ["GITHUB_REPOSITORY"]
    held = held_candidate(repo, run_id)
    data = gh(f"repos/{repo}/actions/artifacts/{held['artifact']['id']}/zip", raw=True)
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    if digest != held["artifact"]["digest"]:
        raise SystemExit(f"FAIL downloaded artifact digest {digest} != GitHub digest {held['artifact']['digest']}")
    root = Path(dest).resolve()
    root.mkdir(parents=True, exist_ok=False)
    sums = []
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for member in archive.infolist():
            target = (root / member.filename).resolve()
            if root not in target.parents:
                raise SystemExit(f"FAIL artifact member escapes the destination: {member.filename}")
            if member.is_dir():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            body = archive.read(member)
            target.write_bytes(body)
            sums.append((member.filename, hashlib.sha256(body).hexdigest()))
    (root.parent / f"{root.name}.SHA256SUMS").write_text(
        "".join(f"{digest_}  {name}\n" for name, digest_ in sorted(sums)), encoding="utf-8")
    (root.parent / f"{root.name}.json").write_text(
        json.dumps({**held, "verified_zip_digest": digest, "files": len(sums)}, indent=2), encoding="utf-8")
    print(f"PASS held run {run_id} artifact {digest} verified; {len(sums)} files extracted")
    return 0


def compare(before: str, after: str) -> int:
    with open(before, encoding="utf-8") as a, open(after, encoding="utf-8") as b:
        old, new = json.load(a), json.load(b)
    changed = [key for key in ("refs", "releases", "held_candidate") if old[key] != new[key]]
    for key in changed:
        print(f"FAIL the repository's {key} changed during the run")
    print("PASS repository refs, releases, assets and held candidate unchanged" if not changed else "")
    return 1 if changed else 0


if __name__ == "__main__":
    command, *arguments = sys.argv[1:]
    sys.exit({"capture": capture, "fetch-held": fetch_held, "compare": compare}[command](*arguments))
