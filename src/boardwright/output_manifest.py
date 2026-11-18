# SPDX-License-Identifier: LicenseRef-Boardwright-Source-Available
# Copyright (c) Ryan Hicks

"""Hash manifests for generated outputs.

The prepare-release workflow writes ``.boardwright/accepted_manifest.json`` for
the outputs it generated and commits it with the release state. The publish
workflow regenerates outputs from the tag and verifies the controlled outputs
against that manifest.

Controlled outputs are machine-readable manufacturing data: Gerber, Excellon,
Gerber job and IPC-D-356 files (loose, and inside the fabrication zip), the
rendered fabrication/assembly notes, plus assembly CSV/position files. Each file records a raw SHA-256 (byte identity)
and a normalised SHA-256 with only timestamp/generator attributes and the
title-block revision field of ``TF.ProjectId`` removed (content identity).
Byte-identical and normalised-equivalent are reported separately.

Drawings (PDF), drill maps, renders, reports and ODB++ archives are recorded
as informational only: they embed dates/release text, and KiCad 9 writes
ODB++ symbol tables in a non-deterministic order.

Usage::

    python -m boardwright.output_manifest write  [--root .] [--out PATH]
    python -m boardwright.output_manifest verify [--root .] [--manifest PATH]
    python -m boardwright.output_manifest compare OLD.json NEW.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MANIFEST_SCHEMA = 2
# Bump whenever normalised_bytes() changes. Normalised hashes are only
# comparable between manifests written by the same normaliser version.
NORMALISER_VERSION = 2
DEFAULT_MANIFEST = Path(".boardwright") / "accepted_manifest.json"
OUTPUT_ROOTS = ("Manufacturing", "Schematic", "Reports", "Testing")
CONTROLLED_CATEGORIES = ("fabrication", "assembly")
ZIP_MEMBER_SEPARATOR = "!/"

GERBER_SUFFIXES = {".gbr", ".gtl", ".gbl", ".gto", ".gbo", ".gts", ".gbs", ".gtp", ".gbp", ".gm1", ".gko"}
GERBER_SUFFIXES |= {f".g{n}" for n in range(1, 33)}
DRILL_SUFFIXES = {".drl", ".xln", ".exc"}
JOB_SUFFIXES = {".gbrjob"}
NETLIST_SUFFIXES = {".ipc", ".d356"}
FAB_TEXT_SUFFIXES = GERBER_SUFFIXES | DRILL_SUFFIXES | JOB_SUFFIXES | NETLIST_SUFFIXES
ASSEMBLY_TEXT_SUFFIXES = {".csv", ".pos"}

# Gerber X2 / Excellon attribute lines that carry only a timestamp or the
# generating tool. Matched exactly; geometry, apertures, FileFunction,
# polarity and every other attribute are kept.
_GERBER_VOLATILE = re.compile(
    r"^(%TF\.(CreationDate|GenerationSoftware),.*\*%"
    r"|G04 #@! TF\.(CreationDate|GenerationSoftware),.*\*"
    r"|G04 Created by KiCad .*\*)$"
)
_GERBER_PROJECT_ID = re.compile(r"^(%TF\.ProjectId|G04 #@! TF\.ProjectId),([^,]*),([^,*]*),[^*]*(\*%?)$")
_DRILL_VOLATILE = re.compile(r"^; ?(DRILL file \{.*\} date .*|#@! TF\.(CreationDate|GenerationSoftware),.*)$")
_DRILL_PROJECT_ID = re.compile(r"^(; ?#@! TF\.ProjectId),([^,]*),([^,]*),.*$")
_NETLIST_VOLATILE = re.compile(r"^C\s.*(\d{4}[-/]\d{2}[-/]\d{2}|Pcbnew|KiCad)", re.IGNORECASE)


@dataclass(frozen=True)
class Difference:
    path: str
    kind: str  # missing | unexpected | changed | regenerated | incomparable

    def __str__(self) -> str:
        labels = {
            "missing": "missing from the new outputs",
            "unexpected": "not present in the accepted outputs",
            "changed": "content differs after normalisation",
            "regenerated": "bytes differ but normalised content is identical",
            "incomparable": "bytes differ and the manifests use different normaliser versions",
        }
        return f"{self.path}: {labels[self.kind]}"


def category_for(relative: str) -> str:
    """Classify an output path (or ``archive.zip!/member`` entry).

    Only machine-readable manufacturing data is controlled. Drawings, drill
    maps, PDFs, renders and archives themselves are informational; the
    fabrication archive is verified through its members instead.
    """

    lowered = relative.lower()
    if ZIP_MEMBER_SEPARATOR in lowered:
        archive, member = lowered.split(ZIP_MEMBER_SEPARATOR, 1)
        if archive.startswith("manufacturing/fabrication/") and _is_fabrication_archive(archive):
            return "fabrication" if Path(member).suffix in FAB_TEXT_SUFFIXES else "informational"
        return "informational"
    suffix = Path(lowered).suffix
    name = Path(lowered).name
    if lowered.startswith("manufacturing/fabrication/gerbers/") and suffix in FAB_TEXT_SUFFIXES:
        return "fabrication"
    # Rendered requirement notes are controlled text: the template lives in
    # Boardwright tooling, so only the rendered result shows a real change.
    if lowered.startswith("manufacturing/fabrication/") and "fabrication_notes" in name and suffix == ".txt":
        return "fabrication"
    if lowered.startswith("manufacturing/assembly/") and (
        suffix in ASSEMBLY_TEXT_SUFFIXES or ("assembly_notes" in name and suffix == ".txt")
    ):
        return "assembly"
    return "informational"


def _is_fabrication_archive(lowered: str) -> bool:
    # ODB++ archives are excluded: KiCad 9 writes their symbol tables in a
    # non-deterministic order, so member bytes differ between identical builds.
    return lowered.endswith(".zip") and "odb" not in Path(lowered).name


def normalised_bytes(relative: str, data: bytes) -> bytes:
    """Remove only provably volatile content from controlled text outputs."""

    if category_for(relative) == "informational":
        return data
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return data
    suffix = Path(relative.lower()).suffix
    if suffix in JOB_SUFFIXES:
        return _normalise_gerber_job(text)
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").split("\n")]
    if suffix in GERBER_SUFFIXES:
        kept = []
        for line in lines:
            if _GERBER_VOLATILE.match(line):
                continue
            match = _GERBER_PROJECT_ID.match(line)
            # Keep project name and GUID; drop the title-block revision field,
            # which is release metadata (validated by the ledger), not artwork.
            kept.append(f"{match[1]},{match[2]},{match[3]}{match[4]}" if match else line)
        lines = kept
    elif suffix in DRILL_SUFFIXES:
        kept = []
        for line in lines:
            if _DRILL_VOLATILE.match(line):
                continue
            match = _DRILL_PROJECT_ID.match(line)
            kept.append(f"{match[1]},{match[2]},{match[3]}" if match else line)
        lines = kept
    elif suffix in NETLIST_SUFFIXES:
        lines = [line for line in lines if not _NETLIST_VOLATILE.match(line)]
    return "\n".join(lines).encode("utf-8")


def _normalise_gerber_job(text: str) -> bytes:
    try:
        job = json.loads(text)
    except json.JSONDecodeError:
        return text.replace("\r\n", "\n").encode("utf-8")
    header = job.get("Header") if isinstance(job, dict) else None
    if isinstance(header, dict):
        header.pop("CreationDate", None)
        header.pop("GenerationSoftware", None)
        project = header.get("ProjectId")
        if isinstance(project, dict):
            project.pop("Revision", None)
    return json.dumps(job, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _entry(relative: str, data: bytes) -> dict[str, str]:
    return {
        "category": category_for(relative),
        "sha256": hashlib.sha256(data).hexdigest(),
        "normalized_sha256": hashlib.sha256(normalised_bytes(relative, data)).hexdigest(),
        "size": str(len(data)),
    }


def build_manifest(root: Path, roots: tuple[str, ...] = OUTPUT_ROOTS) -> dict[str, Any]:
    files: dict[str, dict[str, str]] = {}
    for top in roots:
        base = root / top
        if not base.is_dir():
            continue
        for path in sorted(p for p in base.rglob("*") if p.is_file()):
            relative = path.relative_to(root).as_posix()
            data = path.read_bytes()
            files[relative] = _entry(relative, data)
            lowered = relative.lower()
            if lowered.startswith("manufacturing/fabrication/") and _is_fabrication_archive(lowered):
                files.update(_archive_entries(relative, data))
    return {"schema": MANIFEST_SCHEMA, "normaliser": NORMALISER_VERSION, "files": files}


def _archive_entries(relative: str, data: bytes) -> dict[str, dict[str, str]]:
    import io
    import zipfile

    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        # Recorded as a controlled entry that can never match a real archive.
        return {
            f"{relative}{ZIP_MEMBER_SEPARATOR}<unreadable>": {
                "category": "fabrication",
                "sha256": "unreadable",
                "normalized_sha256": "unreadable",
                "size": "0",
            }
        }
    entries: dict[str, dict[str, str]] = {}
    for info in sorted(archive.infolist(), key=lambda item: item.filename):
        if info.is_dir():
            continue
        name = f"{relative}{ZIP_MEMBER_SEPARATOR}{info.filename}"
        entries[name] = _entry(name, archive.read(info))
    return entries


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read manifest {path}: {exc}") from exc
    if (
        not isinstance(data, dict)
        or data.get("schema") not in (1, MANIFEST_SCHEMA)
        or not isinstance(data.get("files"), dict)
    ):
        raise ValueError(f"Manifest {path} is not a Boardwright output manifest (schema 1 or {MANIFEST_SCHEMA}).")
    data.setdefault("normaliser", 1)
    return data


def compare_manifests(
    accepted: dict[str, Any],
    current: dict[str, Any],
    categories: tuple[str, ...] = CONTROLLED_CATEGORIES,
) -> list[Difference]:
    # A path is controlled if either manifest classifies it as controlled, so
    # re-classifying a file between Boardwright versions cannot hide it.
    controlled = {
        path
        for manifest in (accepted, current)
        for path, entry in manifest["files"].items()
        if entry.get("category") in categories
    }
    old = {path: entry for path, entry in accepted["files"].items() if path in controlled}
    new = {path: entry for path, entry in current["files"].items() if path in controlled}
    same_normaliser = accepted.get("normaliser", 1) == current.get("normaliser", 1)
    differences: list[Difference] = []
    for path in sorted(set(old) | set(new)):
        if path not in new:
            differences.append(Difference(path, "missing"))
        elif path not in old:
            differences.append(Difference(path, "unexpected"))
        elif old[path]["sha256"] == new[path]["sha256"]:
            continue
        elif not same_normaliser:
            differences.append(Difference(path, "incomparable"))
        elif old[path]["normalized_sha256"] != new[path]["normalized_sha256"]:
            differences.append(Difference(path, "changed"))
        else:
            differences.append(Difference(path, "regenerated"))
    return differences


def blocking(differences: list[Difference]) -> list[Difference]:
    return [item for item in differences if item.kind != "regenerated"]


INCOMPARABLE_HINT = (
    "The accepted manifest was written by a different Boardwright normaliser, so only byte "
    "identity can be checked. Rebuild the accepted state with the current Boardwright (new "
    "release version), or compare the outputs manually and record the result."
)


def _print_hints(differences: list[Difference]) -> None:
    if any(item.kind == "incomparable" for item in differences):
        print(INCOMPARABLE_HINT, file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m boardwright.output_manifest")
    sub = parser.add_subparsers(dest="command", required=True)
    write = sub.add_parser("write", help="Write a manifest for the current outputs.")
    write.add_argument("--root", default=".")
    write.add_argument("--out", default=str(DEFAULT_MANIFEST))
    verify = sub.add_parser("verify", help="Verify current outputs against an accepted manifest.")
    verify.add_argument("--root", default=".")
    verify.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    compare = sub.add_parser("compare", help="Compare two manifests.")
    compare.add_argument("old")
    compare.add_argument("new")
    check = sub.add_parser(
        "check-baseline",
        help="For reused baselines in the newest ledger entry, compare outputs with the previous release tag.",
    )
    check.add_argument("--root", default=".")
    check.add_argument(
        "--repo",
        default=os.environ.get("GITHUB_REPOSITORY", ""),
        help="owner/name of the GitHub repository (legacy baselines are fetched from its releases).",
    )
    args = parser.parse_args(argv)

    if args.command == "check-baseline":
        return _check_baseline(Path(args.root), args.repo)

    if args.command == "write":
        root = Path(args.root)
        manifest = build_manifest(root)
        out = Path(args.out)
        out = out if out.is_absolute() else root / out
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
        counts: dict[str, int] = {}
        for entry in manifest["files"].values():
            counts[entry["category"]] = counts.get(entry["category"], 0) + 1
        print(f"wrote {out} ({', '.join(f'{k}: {v}' for k, v in sorted(counts.items())) or 'no files'})")
        if not counts.get("fabrication"):
            print("warning: no fabrication outputs found under Manufacturing/Fabrication/Gerbers", file=sys.stderr)
        return 0

    try:
        if args.command == "verify":
            root = Path(args.root)
            path = Path(args.manifest)
            accepted = load_manifest(path if path.is_absolute() else root / path)
            current = build_manifest(root)
        else:
            accepted = load_manifest(Path(args.old))
            current = load_manifest(Path(args.new))
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    differences = compare_manifests(accepted, current)
    hard = blocking(differences)
    for item in differences:
        print(("ERROR " if item in hard else "note  ") + str(item))
    if hard:
        print(f"{len(hard)} controlled output(s) differ.", file=sys.stderr)
        _print_hints(hard)
        return 1
    soft = len(differences)
    print(
        "Controlled outputs are byte-identical."
        if not soft
        else f"Controlled outputs are equivalent after normalisation ({soft} regenerated file(s))."
    )
    return 0


def _check_baseline(root: Path, repository: str = "") -> int:
    import subprocess
    import tempfile

    from .legacy_baseline import LegacyBaselineError, fetch_published_package

    from .release_ledger import load_ledger

    records = load_ledger(root)
    if not records:
        print("No release ledger entries; nothing to compare.")
        return 0
    record = records[-1]
    categories = tuple(
        name
        for name, baseline in (("fabrication", record.fabrication_baseline), ("assembly", record.assembly_baseline))
        if baseline.status == "reused"
    )
    print(
        f"Release {record.version}: fabrication baseline {record.fabrication_baseline.id} "
        f"({record.fabrication_baseline.status}), assembly baseline {record.assembly_baseline.id} "
        f"({record.assembly_baseline.status})."
    )
    # A re-issued fabrication baseline (same PCB revision, e.g. A/1 -> A/2)
    # is not claimed to be identical, but reviewers must see exactly which
    # controlled fabrication files changed against the previous release.
    reissue = ("fabrication",) if record.fabrication_baseline.status == "reissued" else ()
    if not categories and not reissue:
        print("No reused baselines; new outputs become the approved baseline.")
        return 0
    if not record.previous_version:
        print("error: reused baseline without a previous release version.", file=sys.stderr)
        return 1
    completed = subprocess.run(
        ["git", "show", f"refs/tags/{record.previous_version}:{DEFAULT_MANIFEST.as_posix()}"],
        cwd=root,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        # Legacy release: compare against the authenticated published package.
        # Never a pass by default: an unobtainable or unauthenticated package fails.
        print(
            f"Release {record.previous_version} has no committed output manifest (legacy release); "
            "comparing with its published release package."
        )
        try:
            with tempfile.TemporaryDirectory(prefix="boardwright-legacy-baseline-") as tmp:
                package_root, provenance = fetch_published_package(
                    root, repository, record.previous_version, Path(tmp)
                )
                print("\n".join(provenance.lines()))
                previous = build_manifest(package_root)
        except LegacyBaselineError as exc:
            print(f"error: {exc}", file=sys.stderr)
            print(
                "The reused baseline cannot be verified. Restore access to the published package, or "
                "re-classify the release (fabrication reissue or new revision).",
                file=sys.stderr,
            )
            return 2
        if not any(entry.get("category") in categories + reissue for entry in previous["files"].values()):
            print(f"error: the published package of {record.previous_version} has no controlled "
                  f"{'/'.join(categories + reissue)} outputs to compare.", file=sys.stderr)
            return 2
    else:
        try:
            previous = json.loads(completed.stdout.decode("utf-8"))
        except json.JSONDecodeError as exc:
            print(f"error: previous manifest is invalid: {exc}", file=sys.stderr)
            return 2
    current = build_manifest(root)
    if reissue:
        _report_reissue(record, compare_manifests(previous, current, reissue))
        if not categories:
            return 0
    differences = compare_manifests(previous, current, categories)
    hard = blocking(differences)
    for item in differences:
        print(("ERROR " if item in hard else "note  ") + str(item))
    if hard:
        print(
            f"{len(hard)} output(s) differ from approved release {record.previous_version} although the "
            f"ledger claims the {'/'.join(categories)} baseline is reused. Investigate; if the change is "
            "real, re-classify the release (fabrication reissue or new revision).",
            file=sys.stderr,
        )
        _print_hints(hard)
        return 1
    print(
        f"{'/'.join(categories).capitalize()} outputs match approved release {record.previous_version}"
        + (" byte-for-byte." if not differences else f" after normalisation ({len(differences)} regenerated file(s)).")
    )
    return 0


def _report_reissue(record: Any, differences: list[Difference]) -> None:
    """Informational: what the fabrication re-issue changes against the previous release."""
    changed = [item for item in differences if item.kind != "regenerated"]
    print(
        f"Fabrication baseline re-issued as {record.fabrication_baseline.id} under PCB revision "
        f"{record.pcb_revision}. Controlled fabrication files changed since {record.previous_version}:"
    )
    for item in changed:
        print(f"reissue {item}")
    if not changed:
        print(
            f"warning: no controlled fabrication file differs from {record.previous_version}; "
            "the re-issue changes nothing in the fabrication data. Confirm the classification."
        )
    unchanged = len(differences) - len(changed)
    if unchanged:
        print(f"{unchanged} other controlled fabrication file(s) are identical after normalisation.")


if __name__ == "__main__":
    raise SystemExit(main())
