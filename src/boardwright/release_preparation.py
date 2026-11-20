# SPDX-License-Identifier: LicenseRef-Boardwright-Source-Available
# Copyright (c) Ryan Hicks

"""Provenance of the source edits made by ``release --prepare``.

A prepare-release build runs on a tree that ``release --prepare`` has just
edited (ledger, CHANGELOG, revision tables, ``release.env``). Those edits are
expected, but the build record lists them as ``source_modified`` like any
other uncommitted change. To tell them apart without trusting file names
alone, the prepare command records, inside the Git directory (never
committed):

- the commit it prepared and the release version and date;
- the SHA-256 of every file it is allowed to write.

Preparation also syncs the drawing text variables into the root
``*.kicad_pro`` (scratch: KiBot sets them again, and CI restores the file
before committing). Such a file is accepted only if it differs from ``HEAD``
in nothing but its ``text_variables``; it is listed under ``project_files``.

The build checks that every modified tracked file is one of those files and
still has the recorded content (``verify_preparation``). ``container compare``
accepts ``source_modified`` only when both builds carry a verified record with
identical content; any other modification remains an error.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from .errors import BoardwrightError

RECORD_NAME = "boardwright-release-preparation.json"
RECORD_SCHEMA = 1
# Every file release --prepare (with --release-env) may write.
AUTHORISED_PATHS = (
    "CHANGELOG.md",
    ".boardwright/releases.yaml",
    ".boardwright/document_revisions.yaml",
    ".boardwright/project.yaml",
    ".boardwright/revision_history_variables.env",
    ".boardwright/revision_history_schematic.csv",
    ".boardwright/revision_history_fabrication.csv",
    ".boardwright/revision_history_assembly.csv",
    ".boardwright/release.env",
)


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=False, timeout=30)
    if completed.returncode != 0:
        raise BoardwrightError(f"git {' '.join(args)} failed: {completed.stderr.strip()}")
    return completed.stdout


def record_path(root: Path) -> Path:
    git_dir = Path(_git(root, "rev-parse", "--git-dir").strip())
    return (git_dir if git_dir.is_absolute() else root / git_dir) / RECORD_NAME


def modified_tracked(root: Path) -> list[str]:
    """Tracked files that differ from HEAD (the build's ``source_modified``)."""

    output = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=no")
    return sorted(entry[3:] for entry in output.split("\0") if len(entry) > 3)


def file_digests(root: Path, paths: tuple[str, ...] | list[str]) -> dict[str, str]:
    return {
        path: hashlib.sha256((root / path).read_bytes()).hexdigest()
        for path in paths
        if (root / path).is_file()
    }


def text_variables_only(root: Path, path: str) -> bool:
    """True if a root ``*.kicad_pro`` differs from ``HEAD`` only in ``text_variables``."""

    if "/" in path or not path.endswith(".kicad_pro"):
        return False
    try:
        before = json.loads(_git(root, "show", f"HEAD:{path}"))
        after = json.loads((root / path).read_text(encoding="utf-8"))
    except (BoardwrightError, OSError, json.JSONDecodeError):
        return False
    if not isinstance(before, dict) or not isinstance(after, dict):
        return False
    before.pop("text_variables", None)
    after.pop("text_variables", None)
    return before == after


def record_preparation(root: Path, *, version: str, release_date: str) -> Path:
    """Record what ``release --prepare`` wrote; refuse if anything else is modified."""

    modified = modified_tracked(root)
    project_files = sorted(path for path in modified if text_variables_only(root, path))
    unexpected = [path for path in modified if path not in AUTHORISED_PATHS and path not in project_files]
    if unexpected:
        raise BoardwrightError(
            "Release preparation found tracked changes it did not make: " + ", ".join(unexpected)
        )
    record = {
        "schema": RECORD_SCHEMA,
        "source_commit": _git(root, "rev-parse", "HEAD").strip(),
        "version": version,
        "release_date": release_date,
        "files": file_digests(root, AUTHORISED_PATHS),
        "project_files": project_files,
    }
    path = record_path(root)
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    return path


def load_preparation(root: Path) -> dict[str, Any] | None:
    try:
        path = record_path(root)
    except BoardwrightError:
        return None
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BoardwrightError(f"Unreadable release-preparation record {path}: {exc}") from exc
    return data if isinstance(data, dict) else None


def verify_preparation(root: Path, modified: list[str]) -> dict[str, Any] | None:
    """The build record's ``release_preparation`` entry, or None without a record.

    ``problems`` lists every modified file that the record does not authorise or
    whose content changed after preparation, and a record made for another
    commit.
    """

    record = load_preparation(root)
    if record is None:
        return None
    problems: list[str] = []
    if record.get("schema") != RECORD_SCHEMA:
        problems.append(f"unsupported release-preparation record schema {record.get('schema')!r}")
    head = _git(root, "rev-parse", "HEAD").strip()
    if record.get("source_commit") != head:
        problems.append(f"release preparation was recorded for {record.get('source_commit')!r}, not {head}")
    files = record.get("files") if isinstance(record.get("files"), dict) else {}
    project_files = record.get("project_files") if isinstance(record.get("project_files"), list) else []
    current = file_digests(root, list(files))
    for path in modified:
        if path in project_files:
            if not text_variables_only(root, path):
                problems.append(f"{path} changed outside its text variables")
        elif path not in AUTHORISED_PATHS or path not in files:
            problems.append(f"{path} was modified but is not a release-preparation file")
        elif current.get(path) != files[path]:
            problems.append(f"{path} changed after release preparation")
    for path, digest in files.items():
        if current.get(path) != digest and path not in modified:
            problems.append(f"{path} changed after release preparation")
    return {
        "version": record.get("version", ""),
        "release_date": record.get("release_date", ""),
        "source_commit": record.get("source_commit", ""),
        "files": dict(sorted(files.items())),
        "project_files": sorted(project_files),
        "problems": sorted(set(problems)),
    }
