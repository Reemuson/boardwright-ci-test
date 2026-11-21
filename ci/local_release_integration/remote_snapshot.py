# SPDX-License-Identifier: LicenseRef-Boardwright-Source-Available
# Copyright (c) Ryan Hicks
"""Read-only snapshot of this GitHub repository's refs, releases and held candidates (testbench only).

    remote_snapshot.py capture OUT.json
    remote_snapshot.py compare BEFORE.json AFTER.json

Runs only in the workflow's snapshot steps, which are the only steps given the
read-only token (contents: read, actions: read); the local-release processes
never see it. ``compare`` fails if any branch, tag, visible release, release
asset or held-candidate artifact changed. Draft releases are not visible to a
read-only token; their tags and the held runs' artifacts are compared instead.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys

HELD_RUNS = ("44670323650",)  # held 0.1.8 candidate: never approved, compared only


def api(path: str) -> list | dict:
    done = subprocess.run(["gh", "api", "--paginate", "--slurp", path], capture_output=True, text=True, check=True)
    pages = json.loads(done.stdout)
    if all(isinstance(page, list) for page in pages):
        return [item for page in pages for item in page]
    return pages


def capture(out: str) -> int:
    repo = os.environ["GITHUB_REPOSITORY"]
    refs = {r["ref"]: r["object"]["sha"] for kind in ("heads", "tags")
            for r in api(f"repos/{repo}/git/matching-refs/{kind}")}
    releases = sorted(({
        "id": r["id"], "tag": r["tag_name"], "name": r["name"], "draft": r["draft"], "prerelease": r["prerelease"],
        "target": r["target_commitish"], "published_at": r["published_at"], "body_sha256": hashlib.sha256((r.get("body") or "").encode("utf-8")).hexdigest(),
        "assets": sorted((a["id"], a["name"], a["size"], a.get("digest"), a["updated_at"]) for a in r["assets"]),
    } for r in api(f"repos/{repo}/releases?per_page=100")), key=lambda r: r["id"])
    held = {}
    for run in HELD_RUNS:
        pages = api(f"repos/{repo}/actions/runs/{run}/artifacts?per_page=100")
        artifacts = [a for page in (pages if isinstance(pages, list) else [pages]) for a in page.get("artifacts", [])]
        held[run] = sorted((a["id"], a["name"], a["size_in_bytes"], a.get("digest"), a["expired"]) for a in artifacts)
    snapshot = {"repository": repo, "refs": refs, "releases": releases, "held_candidates": held}
    with open(out, "w", encoding="utf-8") as handle:
        json.dump(snapshot, handle, indent=2, sort_keys=True, default=list)
    print(f"{len(refs)} refs, {len(releases)} visible releases, "
          f"{sum(len(r['assets']) for r in releases)} assets, held runs {list(held)}")
    return 0


def compare(before: str, after: str) -> int:
    with open(before, encoding="utf-8") as a, open(after, encoding="utf-8") as b:
        old, new = json.load(a), json.load(b)
    changed = [key for key in ("refs", "releases", "held_candidates") if old[key] != new[key]]
    for key in changed:
        print(f"FAIL the repository's {key} changed during the run")
    print("PASS repository refs, releases, assets and held candidates unchanged" if not changed else "")
    return 1 if changed else 0


if __name__ == "__main__":
    command, *paths = sys.argv[1:]
    sys.exit(capture(*paths) if command == "capture" else compare(*paths))
