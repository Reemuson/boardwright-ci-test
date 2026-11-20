# SPDX-License-Identifier: LicenseRef-Boardwright-Source-Available
# Copyright (c) Ryan Hicks

"""Human review gate between release preparation and tagging.

A *held* prepare run builds the release candidate, writes
``.boardwright/review_candidate.json`` and uploads it with the outputs, then
stops before anything is pushed. After a person has inspected the candidate
(PDFs, revision identities, worksheets), an *approved* prepare run names the
held run. It rebuilds, recomputes the same identity, and refuses to push or tag
unless it matches the reviewed candidate:

- same reviewed source commit, version, variant and release inputs;
- same new ledger entry, including its date;
- same release date: the approved run is prepared with the reviewed date
  (``release-date``), so the drawings show the date that was reviewed;
- same release metadata: byte-identical ledger, CHANGELOG, revision tables,
  project file and ``release.env`` (``release_metadata``);
- no blocking difference in controlled outputs (``output_manifest`` rules).

Usage::

    python -m boardwright.review_gate write  [--root .] [--inputs-env]
    python -m boardwright.review_gate verify --candidate DIR [--root .]
    python -m boardwright.review_gate release-date --candidate DIR
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

from .output_manifest import DEFAULT_MANIFEST, blocking, compare_manifests, load_manifest
from .errors import BoardwrightError
from .release_date import parse_release_date
from .release_ledger import load_ledger
from .release_preparation import AUTHORISED_PATHS, file_digests

CANDIDATE_FILE = Path(".boardwright") / "review_candidate.json"
# Schema 2 binds the release date and the release metadata files.
CANDIDATE_SCHEMA = 2
INPUT_KEYS = (
    "IN_VERSION",
    "IN_VARIANT",
    "IN_RELEASE_KIND",
    "IN_DRAWING_REVISION",
    "IN_REVISION_DESCRIPTION",
    "IN_CHANGE_CLASSES",
    "IN_JUSTIFICATION",
)


def build_candidate(root: Path, environ: dict[str, str] | None = None) -> dict[str, Any]:
    env = dict(os.environ if environ is None else environ)
    records = load_ledger(root)
    if not records:
        raise ValueError("No release ledger entry was recorded; nothing to review.")
    entry = records[-1].as_dict()
    release_date = str(entry.get("date") or "")
    _check_date(release_date)
    env_date = _release_env(root).get("RELEASE_DATE_NUM", "")
    if env_date != release_date:
        raise ValueError(
            f"release.env RELEASE_DATE_NUM {env_date or '(missing)'} does not match the ledger date {release_date}."
        )
    manifest_path = root / DEFAULT_MANIFEST
    manifest_bytes = manifest_path.read_bytes()
    return {
        "schema": CANDIDATE_SCHEMA,
        "source_sha": env.get("BOARDWRIGHT_SOURCE_SHA", ""),
        "inputs": {key: env.get(key, "") for key in INPUT_KEYS},
        "ledger_entry": entry,
        "release_date": release_date,
        "release_metadata": file_digests(root, AUTHORISED_PATHS),
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
    }


def _check_date(value: str) -> None:
    try:
        parse_release_date(value)
    except BoardwrightError as exc:
        raise ValueError(str(exc)) from exc


def _release_env(root: Path) -> dict[str, str]:
    path = root / ".boardwright" / "release.env"
    if not path.is_file():
        return {}
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values[key.strip()] = value.strip()
    return values


def reviewed_release_date(candidate_dir: Path) -> str:
    """The release date fixed by the reviewed candidate (for the approved run's preparation)."""

    reviewed = json.loads((candidate_dir / CANDIDATE_FILE).read_text(encoding="utf-8"))
    if reviewed.get("schema") != CANDIDATE_SCHEMA:
        raise ValueError(f"Unsupported review candidate schema {reviewed.get('schema')!r}.")
    value = str(reviewed.get("release_date") or "")
    _check_date(value)
    if (reviewed.get("ledger_entry") or {}).get("date") != value:
        raise ValueError("The reviewed candidate's release date does not match its ledger entry.")
    return value


def verify_candidate(root: Path, candidate_dir: Path, environ: dict[str, str] | None = None) -> list[str]:
    """Return reasons the current run does not match the reviewed candidate."""

    problems: list[str] = []
    try:
        reviewed = json.loads((candidate_dir / CANDIDATE_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"Cannot read reviewed candidate: {exc}"]
    if reviewed.get("schema") != CANDIDATE_SCHEMA:
        return [f"Unsupported review candidate schema {reviewed.get('schema')!r}."]
    try:
        current = build_candidate(root, environ)
    except (OSError, ValueError) as exc:
        return [f"Cannot build current candidate: {exc}"]

    if not reviewed.get("source_sha") or reviewed["source_sha"] != current["source_sha"]:
        problems.append(
            f"Source commit differs: reviewed {reviewed.get('source_sha') or '(none)'}, "
            f"now {current['source_sha'] or '(none)'}."
        )
    for key in INPUT_KEYS:
        before, after = reviewed.get("inputs", {}).get(key, ""), current["inputs"][key]
        if before != after:
            problems.append(f"Input {key} differs: reviewed {before!r}, now {after!r}.")
    if reviewed.get("release_date") != current["release_date"]:
        problems.append(
            f"Release date differs: reviewed {reviewed.get('release_date')!r}, now {current['release_date']!r}."
        )
    if reviewed.get("ledger_entry") != current["ledger_entry"]:
        problems.append("The new ledger entry (identities, baselines, fingerprints, date) differs from the reviewed one.")
    before, after = reviewed.get("release_metadata") or {}, current["release_metadata"]
    for path in sorted(set(before) | set(after)):
        if before.get(path) != after.get(path):
            problems.append(f"Release metadata {path} differs from the reviewed candidate.")
    try:
        accepted = load_manifest(candidate_dir / DEFAULT_MANIFEST)
        rebuilt = load_manifest(root / DEFAULT_MANIFEST)
    except ValueError as exc:
        problems.append(str(exc))
    else:
        for difference in blocking(compare_manifests(accepted, rebuilt)):
            problems.append(f"Controlled output differs from the reviewed candidate: {difference}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m boardwright.review_gate")
    sub = parser.add_subparsers(dest="command", required=True)
    write = sub.add_parser("write", help="Record the release candidate identity for review.")
    write.add_argument("--root", default=".")
    verify = sub.add_parser("verify", help="Check this run against a reviewed candidate.")
    verify.add_argument("--root", default=".")
    verify.add_argument("--candidate", required=True, help="Directory holding the reviewed candidate artifact.")
    dated = sub.add_parser("release-date", help="Print the reviewed candidate's release date (YYYY-MM-DD).")
    dated.add_argument("--candidate", required=True, help="Directory holding the reviewed candidate artifact.")
    args = parser.parse_args(argv)
    if args.command == "release-date":
        try:
            print(reviewed_release_date(Path(args.candidate)))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0
    root = Path(args.root)

    if args.command == "write":
        try:
            candidate = build_candidate(root)
        except (OSError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        path = root / CANDIDATE_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(candidate, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
        print(f"wrote {path} (source {candidate['source_sha'][:12]}, manifest {candidate['manifest_sha256'][:12]})")
        return 0

    problems = verify_candidate(root, Path(args.candidate))
    if problems:
        for problem in problems:
            print(f"ERROR {problem}")
        print("Release does not match the reviewed candidate; nothing will be pushed.", file=sys.stderr)
        return 1
    print("Release matches the reviewed candidate.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
