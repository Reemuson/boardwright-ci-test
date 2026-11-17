# SPDX-License-Identifier: LicenseRef-Boardwright-Source-Available
# Copyright (c) Ryan Hicks

"""Append-only release ledger (``.boardwright/releases.yaml``).

Each entry records the identities a published package claims (project release
version, PCB revision, schematic/assembly/fabrication drawing revisions), the
declared change classes and justification, the reviewed source commit, and the
design fingerprints that back the claims. Entries are never rewritten: a
release whose tag exists must keep the entry it was tagged with.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import BoardwrightError
from .fingerprint import FINGERPRINT_SCHEMA, DesignFingerprint, GitRefReader, compute_fingerprint

LEDGER_RELATIVE_PATH = Path(".boardwright") / "releases.yaml"
LEDGER_SCHEMA = 1
SEMVER_TAG_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")

BASELINE_STATUSES = ("initial", "new", "reissued", "reused")


@dataclass(frozen=True)
class BaselineRef:
    id: str
    status: str

    def as_dict(self) -> dict[str, str]:
        return {"id": self.id, "status": self.status}


@dataclass(frozen=True)
class ReleaseRecord:
    version: str
    date: str
    source_commit: str
    pcb_revision: str
    schematic_revision: str
    assembly_revision: str
    fabrication_revision: str
    change_classes: tuple[str, ...]
    justification: str
    fingerprint: DesignFingerprint
    fabrication_baseline: BaselineRef
    assembly_baseline: BaselineRef
    previous_version: str = ""
    variant: str = ""
    legacy: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "date": self.date,
            "source_commit": self.source_commit,
            "previous_version": self.previous_version,
            "variant": self.variant,
            "identities": {
                "pcb_revision": self.pcb_revision,
                "schematic_revision": self.schematic_revision,
                "assembly_revision": self.assembly_revision,
                "fabrication_revision": self.fabrication_revision,
            },
            "change_classes": list(self.change_classes),
            "justification": self.justification,
            "fabrication_baseline": self.fabrication_baseline.as_dict(),
            "assembly_baseline": self.assembly_baseline.as_dict(),
            "fingerprint": self.fingerprint.as_dict(),
        }

    @classmethod
    def from_dict(cls, data: Any, *, index: int = 0) -> "ReleaseRecord":
        where = f"releases.yaml entry {index + 1}"
        if not isinstance(data, dict):
            raise BoardwrightError(f"{where} must be a mapping.")
        identities = data.get("identities")
        if not isinstance(identities, dict):
            raise BoardwrightError(f"{where} is missing `identities`.")
        version = str(data.get("version") or "").strip()
        if not SEMVER_TAG_RE.match(version):
            raise BoardwrightError(f"{where} has invalid version {version!r}.")
        pcb_revision = str(identities.get("pcb_revision") or "").strip()
        if not pcb_revision:
            raise BoardwrightError(f"{where} ({version}) is missing `identities.pcb_revision`.")
        classes = data.get("change_classes") or []
        if not isinstance(classes, list):
            raise BoardwrightError(f"{where} ({version}) `change_classes` must be a list.")
        return cls(
            version=version,
            date=str(data.get("date") or ""),
            source_commit=str(data.get("source_commit") or ""),
            previous_version=str(data.get("previous_version") or ""),
            variant=str(data.get("variant") or ""),
            pcb_revision=pcb_revision,
            schematic_revision=str(identities.get("schematic_revision") or ""),
            assembly_revision=str(identities.get("assembly_revision") or ""),
            fabrication_revision=str(identities.get("fabrication_revision") or ""),
            change_classes=tuple(str(item) for item in classes),
            justification=str(data.get("justification") or ""),
            fingerprint=DesignFingerprint.from_dict(data.get("fingerprint") or {}),
            fabrication_baseline=_baseline(data.get("fabrication_baseline"), where, "fabrication_baseline"),
            assembly_baseline=_baseline(data.get("assembly_baseline"), where, "assembly_baseline"),
        )


def _baseline(value: Any, where: str, name: str) -> BaselineRef:
    if not isinstance(value, dict):
        raise BoardwrightError(f"{where} is missing `{name}`.")
    status = str(value.get("status") or "")
    if status not in BASELINE_STATUSES:
        raise BoardwrightError(f"{where} `{name}.status` must be one of {', '.join(BASELINE_STATUSES)}.")
    identifier = str(value.get("id") or "").strip()
    if not identifier:
        raise BoardwrightError(f"{where} `{name}.id` is required.")
    return BaselineRef(identifier, status)


def ledger_path(root: Path) -> Path:
    return root / LEDGER_RELATIVE_PATH


def parse_ledger_text(text: str | None) -> list[ReleaseRecord]:
    if not text or not text.strip():
        return []
    import yaml

    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise BoardwrightError("releases.yaml must contain a mapping.")
    schema = data.get("schema", LEDGER_SCHEMA)
    if schema != LEDGER_SCHEMA:
        raise BoardwrightError(f"Unsupported releases.yaml schema {schema!r}; expected {LEDGER_SCHEMA}.")
    entries = data.get("releases") or []
    if not isinstance(entries, list):
        raise BoardwrightError("releases.yaml field `releases` must be a list.")
    records = [ReleaseRecord.from_dict(entry, index=index) for index, entry in enumerate(entries)]
    seen: set[str] = set()
    for record in records:
        if record.version in seen:
            raise BoardwrightError(f"releases.yaml lists version {record.version} more than once.")
        seen.add(record.version)
    return records


def load_ledger(root: Path) -> list[ReleaseRecord]:
    path = ledger_path(root)
    if not path.exists():
        return []
    return parse_ledger_text(path.read_text(encoding="utf-8"))


def append_record(root: Path, record: ReleaseRecord) -> Path:
    """Append ``record``; existing entries are preserved byte-for-byte."""
    import yaml

    existing = load_ledger(root)
    if any(item.version == record.version for item in existing):
        raise BoardwrightError(f"Release {record.version} is already recorded in releases.yaml.")
    path = ledger_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = yaml.safe_dump([record.as_dict()], sort_keys=False, allow_unicode=True)
    entry = "\n".join(("  " + line) if line else line for line in entry.splitlines()) + "\n"
    if path.exists() and path.read_text(encoding="utf-8").strip():
        text = path.read_text(encoding="utf-8")
        if not text.endswith("\n"):
            text += "\n"
        if re.search(r"^releases:\s*\[\]\s*$", text, flags=re.MULTILINE):
            text = re.sub(r"^releases:\s*\[\]\s*$", "releases:", text, flags=re.MULTILINE)
    else:
        text = (
            "# SPDX-License-Identifier: CC0-1.0\n"
            "# Boardwright release ledger. Append-only: never edit published entries.\n"
            f"schema: {LEDGER_SCHEMA}\n"
            "releases:\n"
        )
    path.write_text(text + entry, encoding="utf-8", newline="\n")
    # Re-read to guarantee the file is still valid after the append.
    parse_ledger_text(path.read_text(encoding="utf-8"))
    return path


def semver_tags(root: Path) -> list[str]:
    completed = _run_git(root, "tag", "--list")
    tags = [line.strip() for line in completed.splitlines() if SEMVER_TAG_RE.match(line.strip())]
    return sorted(tags, key=lambda tag: tuple(int(part) for part in tag.split(".")))


def verify_ledger_history(root: Path, records: list[ReleaseRecord] | None = None) -> list[str]:
    """Check that ledger entries present at each release tag are unchanged."""
    current = {record.version: record for record in (records if records is not None else load_ledger(root))}
    problems: list[str] = []
    for tag in semver_tags(root):
        try:
            text = _run_git(root, "show", f"refs/tags/{tag}:{LEDGER_RELATIVE_PATH.as_posix()}")
        except BoardwrightError:
            continue
        for record in parse_ledger_text(text):
            now = current.get(record.version)
            if now is None:
                problems.append(f"Release {record.version} was removed from releases.yaml after tag {tag}.")
            elif now.as_dict() != record.as_dict():
                problems.append(f"Release {record.version} entry was modified after tag {tag}.")
    return problems


@dataclass(frozen=True)
class Baseline:
    """The most recent approved release used for comparison."""

    version: str
    pcb_revision: str
    schematic_revision: str
    assembly_revision: str
    fabrication_revision: str
    fingerprint: DesignFingerprint
    fabrication_baseline: BaselineRef
    assembly_baseline: BaselineRef
    legacy: bool


def latest_baseline(root: Path, records: list[ReleaseRecord] | None = None) -> Baseline | None:
    """Return the latest ledgered release, or derive one from the latest tag.

    Projects released before the ledger existed have tags but no entries. For
    them the baseline is reconstructed read-only from the tagged sources.
    """
    ledger = records if records is not None else load_ledger(root)
    tags = semver_tags(root) if _is_git_repo(root) else []
    newest_tag = tags[-1] if tags else ""
    if ledger:
        record = max(ledger, key=lambda item: _version_key(item.version))
        if not newest_tag or _version_key(record.version) >= _version_key(newest_tag):
            return Baseline(
                version=record.version,
                pcb_revision=record.pcb_revision,
                schematic_revision=record.schematic_revision,
                assembly_revision=record.assembly_revision,
                fabrication_revision=record.fabrication_revision,
                fingerprint=comparable_fingerprint(root, record),
                fabrication_baseline=record.fabrication_baseline,
                assembly_baseline=record.assembly_baseline,
                legacy=False,
            )
    if not newest_tag:
        return None
    return legacy_baseline_from_tag(root, newest_tag)


def comparable_fingerprint(root: Path, record: ReleaseRecord) -> DesignFingerprint:
    """Return the record's fingerprint in the current schema.

    Fingerprints are only comparable within one schema. When the stored one is
    older, it is recomputed read-only from the release tag (or, failing that,
    the recorded source commit) with the current rules. If neither source is
    available the stored fingerprint is returned and the caller reports the
    schema mismatch.
    """

    stored = record.fingerprint
    if stored.schema == FINGERPRINT_SCHEMA or not _is_git_repo(root):
        return stored
    for ref, label in (
        (f"refs/tags/{record.version}", f"tag {record.version} (recomputed)"),
        (record.source_commit, f"commit {record.source_commit[:12]} (recomputed)"),
    ):
        if not ref:
            continue
        try:
            _run_git(root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
        except BoardwrightError:
            continue
        return compute_fingerprint(GitRefReader(root, ref, label=label))
    return stored


def legacy_baseline_from_tag(root: Path, tag: str) -> Baseline:
    import yaml

    reader = GitRefReader(root, f"refs/tags/{tag}", label=f"tag {tag}")
    fingerprint = compute_fingerprint(reader)
    project = yaml.safe_load(reader.read_text(".boardwright/project.yaml") or "") or {}
    project_section = project.get("project", {}) if isinstance(project, dict) else {}
    documents = project.get("documents", {}) if isinstance(project, dict) else {}

    def doc_rev(key: str) -> str:
        document = documents.get(key, {}) if isinstance(documents, dict) else {}
        return str(document.get("revision") or "") if isinstance(document, dict) else ""

    pcb_revision = str(project_section.get("board_revision") or "A")
    return Baseline(
        version=tag,
        pcb_revision=pcb_revision,
        schematic_revision=doc_rev("schematic"),
        assembly_revision=doc_rev("assembly"),
        fabrication_revision=doc_rev("fabrication"),
        fingerprint=fingerprint,
        fabrication_baseline=BaselineRef(f"{pcb_revision}/1", "initial"),
        assembly_baseline=BaselineRef(doc_rev("assembly") or pcb_revision, "initial"),
        legacy=True,
    )


def _version_key(version: str) -> tuple[int, int, int]:
    match = SEMVER_TAG_RE.match(version)
    if not match:
        return (-1, -1, -1)
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def _is_git_repo(root: Path) -> bool:
    try:
        _run_git(root, "rev-parse", "--git-dir")
    except BoardwrightError:
        return False
    return True


def _run_git(root: Path, *args: str) -> str:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=root,
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BoardwrightError(f"git {' '.join(args)} failed: {exc}") from exc
    if completed.returncode != 0:
        raise BoardwrightError(
            f"git {' '.join(args)} failed: {completed.stderr.decode('utf-8', 'replace').strip()}"
        )
    return completed.stdout.decode("utf-8")
