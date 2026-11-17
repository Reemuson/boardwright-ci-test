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
- same new ledger entry (except its date);
- no blocking difference in controlled outputs (``output_manifest`` rules).

Usage::

    python -m boardwright.review_gate write  [--root .] [--inputs-env]
    python -m boardwright.review_gate verify --candidate DIR [--root .]
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
from .release_ledger import load_ledger

CANDIDATE_FILE = Path(".boardwright") / "review_candidate.json"
CANDIDATE_SCHEMA = 1
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
    entry.pop("date", None)
    manifest_path = root / DEFAULT_MANIFEST
    manifest_bytes = manifest_path.read_bytes()
    return {
        "schema": CANDIDATE_SCHEMA,
        "source_sha": env.get("BOARDWRIGHT_SOURCE_SHA", ""),
        "inputs": {key: env.get(key, "") for key in INPUT_KEYS},
        "ledger_entry": entry,
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
    }


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
    if reviewed.get("ledger_entry") != current["ledger_entry"]:
        problems.append("The new ledger entry (identities, baselines, fingerprints) differs from the reviewed one.")
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
    args = parser.parse_args(argv)
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
