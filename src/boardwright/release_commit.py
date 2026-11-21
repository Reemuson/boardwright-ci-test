# SPDX-License-Identifier: LicenseRef-Boardwright-Source-Available
# Copyright (c) Ryan Hicks

"""Stage and create the release commit (``docs/LOCAL_FIRST.md`` section 6).

Shared by ``prepare-release.yaml`` and the local release backend. Replaces
the staging, message and trailer shell of "Commit accepted release state";
pushing stays with the caller (the workflow pushes to ``main``; a local
release never pushes). The reference shell is frozen in
``tests/fixtures/legacy_release_steps/commit_release.sh``.

Trailers (``docs/COMMITS.md``): ``Source-Commit`` always; ``Reviewed-Run``
for a GitHub approved run; a local approval adds ``Reviewed-Candidate`` and
``Approval-Authority: local`` instead, so a local release can never be read
as a server-approved one.

Command line::

    python -m boardwright.release_commit --version 1.2.0 --variant RELEASED \\
        --source-sha <sha> [--reviewed-run-url URL]

Exit status 1 with "No release changes to commit." when nothing is staged.
"""

from __future__ import annotations

import argparse
import glob
import subprocess
import sys
from pathlib import Path
from typing import Sequence

GENERATED_TREES = ("HTML", "KiRI", "Manufacturing", "Reports", "Schematic", "Testing")
TRACKED = ("CHANGELOG.md", ".boardwright/project.yaml", ".boardwright/document_revisions.yaml")
FORCED = (
    (".boardwright/revision_history_variables.env", ".boardwright/release.env"),
    (".boardwright/releases.yaml", ".boardwright/accepted_manifest.json", ".boardwright/review_candidate.json"),
)
RENDERS = "assets/renders/*.png"
# A local release commits its approval provenance (LOCAL_FIRST.md section 5.2).
LOCAL_APPROVAL = ".boardwright/approval.json"


class NothingToCommit(RuntimeError):
    pass


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    done = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)
    if check and done.returncode != 0:
        raise subprocess.CalledProcessError(done.returncode, ["git", *args], done.stdout, done.stderr)
    return done


def stage_release(root: Path, *, local_approval: bool = False) -> None:
    """Stage exactly what the workflow staged; a local approval adds ``approval.json``."""

    _git(root, "rm", "-r", "--ignore-unmatch", "--quiet", *GENERATED_TREES)
    _git(root, "add", *TRACKED)
    for group in FORCED:
        _git(root, "add", "-f", *group)
    # README.md and the renders are staged independently (audit E1): the
    # legacy "git add README.md assets/renders/*.png || true" staged nothing,
    # README included, when there were no renders. Either may be absent; a
    # real staging failure is no longer swallowed.
    if (root / "README.md").is_file():
        _git(root, "add", "README.md")
    renders = sorted(Path(m).relative_to(root).as_posix() for m in glob.glob(str(root / RENDERS)))
    if renders:
        _git(root, "add", "--", *renders)
    if local_approval:
        if not (root / LOCAL_APPROVAL).is_file():
            raise FileNotFoundError(f"A local release commit needs {LOCAL_APPROVAL}.")
        _git(root, "add", "-f", LOCAL_APPROVAL)


def trailers(
    source_sha: str,
    *,
    reviewed_run_url: str = "",
    reviewed_candidate: str = "",
    local_approval: bool = False,
) -> list[str]:
    if reviewed_run_url and local_approval:
        raise ValueError("a local approval cannot carry a GitHub Reviewed-Run")
    lines = [f"Source-Commit: {source_sha}"]
    if reviewed_run_url:
        lines.append(f"Reviewed-Run: {reviewed_run_url}")
    if local_approval:
        if not reviewed_candidate:
            raise ValueError("a local approval must name the reviewed candidate")
        lines += [f"Reviewed-Candidate: {reviewed_candidate}", "Approval-Authority: local"]
    return lines


def subject(version: str, variant: str) -> str:
    return f"release: prepare {version} ({variant})"


def message(version: str, variant: str, trailer_lines: Sequence[str]) -> str:
    """The message ``git commit -m subject -m trailers`` records."""

    return f"{subject(version, variant)}\n\n" + "\n".join(trailer_lines) + "\n"


def commit_release(
    root: Path,
    *,
    version: str,
    variant: str,
    source_sha: str,
    reviewed_run_url: str = "",
    reviewed_candidate: str = "",
    local_approval: bool = False,
    sign: bool = False,
) -> str:
    """Stage, refuse an empty commit, commit; return the new commit SHA."""

    lines = trailers(source_sha, reviewed_run_url=reviewed_run_url,
                     reviewed_candidate=reviewed_candidate, local_approval=local_approval)
    stage_release(root, local_approval=local_approval)
    if _git(root, "diff", "--cached", "--quiet", check=False).returncode == 0:
        raise NothingToCommit("No release changes to commit.")
    args = ["commit", "-m", subject(version, variant), "-m", "\n".join(lines)]
    if sign:
        args.insert(1, "-S")
    _git(root, *args)
    return _git(root, "rev-parse", "HEAD").stdout.strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m boardwright.release_commit")
    parser.add_argument("--version", required=True)
    parser.add_argument("--variant", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--reviewed-run-url", default="")
    parser.add_argument("--root", default=".")
    args = parser.parse_args(argv)
    try:
        sha = commit_release(Path(args.root).resolve(), version=args.version, variant=args.variant,
                             source_sha=args.source_sha, reviewed_run_url=args.reviewed_run_url)
    except NothingToCommit as exc:
        print(exc, file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        print((exc.stderr or str(exc)).strip(), file=sys.stderr)
        return 2
    print(sha)
    return 0


if __name__ == "__main__":
    sys.exit(main())
