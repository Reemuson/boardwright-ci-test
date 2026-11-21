# SPDX-License-Identifier: LicenseRef-Boardwright-Source-Available
# Copyright (c) Ryan Hicks

"""The local release store (``docs/LOCAL_FIRST.md`` section 5.2).

Default location ``~/.boardwright/releases/<project-id>/`` (or
``release.local_store`` in ``project.yaml``, or ``BOARDWRIGHT_RELEASE_STORE``).
It sits outside the working tree and is never committed::

    <store>/
      .boardwright-release-store       owner marker (schema, project id)
      index.jsonl                      append-only, hash-chained event log
      candidates/<version>/<id>/       frozen candidate      (write once)
      approvals/<version>/<id>/        approval + review     (write once)
      releases/<version>/              published package     (write once)
      drafts/                          editable review drafts
      .tmp-<random>/                   an entry being written (interrupted if left)

Safety rules:

- every path component is validated (no ``..``, separators or odd names) and
  every existing component from the store base down must be a real directory,
  never a symbolic link or junction;
- a directory is only used as a store if it is empty or carries this store's
  owner marker for the same project id, so unrelated user files are refused;
- entries are written into an owned temporary, checksummed (``SHA256SUMS``),
  made read-only and renamed into place; an existing entry is never replaced;
- ``index.jsonl`` lines carry the SHA-256 of the previous line, so editing,
  removing or reordering a line is detected (not prevented).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .errors import BoardwrightError
from .release_package import is_link
from .stage_lock import stage_lock
from .staging import remove_tree

STORE_MARKER = ".boardwright-release-store"
INDEX = "index.jsonl"
INDEX_HEAD = "index.head"  # count and SHA-256 of the last line: anchors the chain's tail
SUMS = "SHA256SUMS"
STORE_SCHEMA = 1
GENESIS = "0" * 64
KINDS = ("candidates", "approvals", "finalizations", "releases")
PER_VERSION = ("finalizations", "releases")  # one entry per version: <kind>/<version>/
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")


class StoreError(BoardwrightError):
    """The release store is unsafe, inconsistent or would be overwritten."""


def safe_name(value: str, what: str = "name") -> str:
    """A single path component: no separators, no ``..``, no leading dot."""

    if not isinstance(value, str) or not _NAME.match(value) or ".." in value:
        raise StoreError(f"Invalid {what} {value!r} for the release store.")
    return value


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _git(root: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8")
    if done.returncode != 0:
        raise StoreError(f"git {' '.join(args)} failed: {done.stderr.strip()}")
    return done.stdout


def project_identity(root: Path, project: dict) -> str:
    """``<slug>-<16 hex>``: the project id and the repository's root commit(s).

    Two projects with the same name (or the same template id) in different
    repositories get different stores; clones of one repository share one.
    """

    meta = project.get("project") if isinstance(project.get("project"), dict) else {}
    label = str(meta.get("id") or meta.get("name") or "project")
    roots = sorted(_git(root, "rev-list", "--max-parents=0", "HEAD").split())
    if not roots:
        raise StoreError("The repository has no commits; a local release needs a committed project.")
    digest = sha256_bytes(("\0".join([label, *roots])).encode("utf-8"))[:16]
    slug = re.sub(r"[^A-Za-z0-9]+", "-", label).strip("-").lower()[:40] or "project"
    return f"{slug}-{digest}"


def store_base(project: dict) -> Path:
    configured = os.environ.get("BOARDWRIGHT_RELEASE_STORE", "").strip()
    if not configured:
        release = project.get("release") if isinstance(project.get("release"), dict) else {}
        configured = str(release.get("local_store") or "").strip()
    return Path(configured).expanduser() if configured else Path.home() / ".boardwright" / "releases"


def _check_no_links(path: Path, base: Path) -> None:
    """Every existing component of ``path`` from ``base`` down is real, never a link or junction."""

    try:
        relative = path.relative_to(base)
    except ValueError as exc:
        raise StoreError(f"{path} is outside the release store base {base}.") from exc
    current = base
    for part in ("",) + relative.parts:
        current = current / part if part else current
        if is_link(current):
            raise StoreError(f"{current} is a symbolic link or junction; refusing to use it in the release store.")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class Entry:
    kind: str
    version: str
    name: str        # entry id (candidates/approvals) or the version (releases)
    path: Path


class ReleaseStore:
    def __init__(self, root: Path, project_id: str) -> None:
        self.project_id = safe_name(project_id, "project id")
        self.root = Path(os.path.abspath(root))
        self.base = self.root.parent

    def _no_links(self, path: Path) -> None:
        _check_no_links(path, self.base)

    @classmethod
    def for_project(cls, root: Path, project: dict) -> "ReleaseStore":
        project_id = project_identity(root, project)
        base = Path(os.path.abspath(store_base(project)))
        store = cls(base / project_id, project_id)
        repo = Path(os.path.abspath(root))
        if store.root == repo or repo in store.root.parents:
            raise StoreError(f"The release store {store.root} is inside the project; it must be outside the working tree.")
        return store

    # -- opening -----------------------------------------------------------
    def ensure(self) -> "ReleaseStore":
        self._no_links(self.root)
        marker = self.root / STORE_MARKER
        if self.root.exists():
            if not self.root.is_dir():
                raise StoreError(f"{self.root} is not a directory.")
            if marker.is_file():
                self._check_marker(marker)
            elif any(self.root.iterdir()):
                raise StoreError(f"{self.root} holds files that are not a Boardwright release store; refusing to use it.")
        self.root.mkdir(parents=True, exist_ok=True)
        self._no_links(self.root)
        if not marker.exists():
            marker.write_text(json.dumps({"schema": STORE_SCHEMA, "project_id": self.project_id}) + "\n",
                              encoding="utf-8", newline="\n")
        return self

    def exists(self) -> bool:
        return (self.root / STORE_MARKER).is_file()

    def _check_marker(self, marker: Path) -> None:
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StoreError(f"Unreadable release-store marker {marker}: {exc}") from exc
        if data.get("schema") != STORE_SCHEMA or data.get("project_id") != self.project_id:
            raise StoreError(f"{self.root} is the release store of another project ({data.get('project_id')!r}).")

    def check(self) -> None:
        self._no_links(self.root)
        if not self.exists():
            raise StoreError(f"No release store at {self.root}.")
        self._check_marker(self.root / STORE_MARKER)

    @contextlib.contextmanager
    def locked(self, purpose: str) -> Iterator["ReleaseStore"]:
        self.ensure()
        with stage_lock(self.root, purpose=purpose):
            self.check()
            yield self

    # -- paths -------------------------------------------------------------
    def entry_path(self, kind: str, version: str, name: str = "") -> Path:
        if kind not in KINDS:
            raise StoreError(f"Unknown release-store kind {kind!r}.")
        parts = [kind, safe_name(version, "version")] + ([safe_name(name, "entry id")] if name else [])
        path = self.root.joinpath(*parts)
        self._no_links(path)
        return path

    def drafts(self) -> Path:
        path = self.root / "drafts"
        self._no_links(path)
        path.mkdir(exist_ok=True)
        return path

    # -- entries -----------------------------------------------------------
    def freeze(self, kind: str, version: str, name: str, files: dict[str, Path | bytes]) -> Entry:
        """Write a new entry once: temporary, ``SHA256SUMS``, read-only, rename into place.

        ``files`` maps a POSIX relative path inside the entry to a source file
        or to bytes. Refused if the entry exists; an interruption leaves only
        an owned ``.tmp-*`` directory (``abandoned()``), never a partial entry.
        """

        final = self.entry_path(kind, version, name if kind not in PER_VERSION else "")
        if final.exists() or is_link(final):
            raise StoreError(f"{kind}/{version}{'/' + name if name and kind not in PER_VERSION else ''} already exists "
                             "in the release store; it is never overwritten.")
        temporary = self.root / f".tmp-{secrets.token_hex(8)}"
        temporary.mkdir()
        (temporary / ".boardwright-store-temporary").write_text(f"{kind}/{version}/{name}\n", encoding="utf-8")
        try:
            sums = []
            for relative in sorted(files):
                target = _inside(temporary, relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                source = files[relative]
                if isinstance(source, bytes):
                    target.write_bytes(source)
                else:
                    if is_link(source) or not Path(source).is_file():
                        raise StoreError(f"{source} is not a regular file; refusing to store it.")
                    shutil.copyfile(source, target)
                sums.append(f"{sha256_file(target)}  {relative}\n")
            (temporary / ".boardwright-store-temporary").unlink()
            (temporary / SUMS).write_text("".join(sums), encoding="utf-8", newline="\n")
            _read_only(temporary)
            final.parent.mkdir(parents=True, exist_ok=True)
            self._no_links(final.parent)
            os.rename(temporary, final)
        except BaseException:
            remove_tree(temporary)
            raise
        return Entry(kind, version, name or version, final)

    def verify_entry(self, path: Path) -> list[str]:
        """Problems with an entry: missing, extra or changed files, links."""

        problems: list[str] = []
        try:
            self._no_links(path)
        except StoreError as exc:
            return [str(exc)]
        sums = path / SUMS
        if not sums.is_file():
            return [f"{path}: {SUMS} is missing."]
        expected: dict[str, str] = {}
        for line in sums.read_text(encoding="utf-8").splitlines():
            digest, _, relative = line.partition("  ")
            expected[relative] = digest
        present = set()
        for item in path.rglob("*"):
            if is_link(item):
                problems.append(f"{item}: a link inside a release-store entry.")
            elif item.is_file() and item != sums:
                present.add(item.relative_to(path).as_posix())
        for relative in sorted(set(expected) - present):
            problems.append(f"{path.name}/{relative} is missing.")
        for relative in sorted(present - set(expected)):
            problems.append(f"{path.name}/{relative} is not listed in {SUMS}.")
        for relative in sorted(present & set(expected)):
            if sha256_file(path / relative) != expected[relative]:
                problems.append(f"{path.name}/{relative} does not match its checksum.")
        return problems

    def entries(self, kind: str, version: str = "") -> list[Entry]:
        base = self.root / kind
        if not base.is_dir():
            return []
        found = []
        for version_dir in sorted(base.iterdir()):
            if version and version_dir.name != version:
                continue
            if kind in PER_VERSION:
                found.append(Entry(kind, version_dir.name, version_dir.name, version_dir))
                continue
            for entry in sorted(version_dir.iterdir()) if version_dir.is_dir() else []:
                found.append(Entry(kind, version_dir.name, entry.name, entry))
        return found

    def abandoned(self) -> list[Path]:
        return sorted(p for p in self.root.glob(".tmp-*") if p.is_dir() and not is_link(p)) if self.root.is_dir() else []

    def remove_abandoned(self) -> list[Path]:
        removed = []
        for path in self.abandoned():
            if path.parent == self.root and path.name.startswith(".tmp-"):
                remove_tree(path)
                removed.append(path)
        return removed

    # -- index -------------------------------------------------------------
    def events(self) -> list[dict]:
        path = self.root / INDEX
        if not path.is_file():
            return []
        try:
            return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        except json.JSONDecodeError as exc:
            raise StoreError(f"The release-store index {path} is unreadable ({exc}); nothing was changed.") from exc

    def verify_index(self) -> list[str]:
        path = self.root / INDEX
        if not path.is_file():
            return [f"{INDEX_HEAD} exists but {INDEX} is missing."] if (self.root / INDEX_HEAD).exists() else []
        problems = []
        previous = GENESIS
        lines = path.read_bytes().split(b"\n")
        if lines and lines[-1] == b"":
            lines = lines[:-1]
        else:
            problems.append(f"{INDEX}: the last line is incomplete (interrupted write or edit).")
        for number, line in enumerate(lines, 1):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                problems.append(f"{INDEX} line {number} is not valid JSON.")
                break
            if event.get("prev") != previous or event.get("seq") != number:
                problems.append(f"{INDEX} line {number} breaks the hash chain (edited, removed or reordered).")
                break
            previous = sha256_bytes(line)
        head = self.root / INDEX_HEAD
        anchor = f"{len(lines)} {previous}"
        if not problems and (not head.is_file() or head.read_text(encoding="utf-8").strip() != anchor):
            problems.append(f"{INDEX} does not match {INDEX_HEAD} (the last line was edited, removed or added).")
        return problems

    def append_event(self, event: dict) -> dict:
        """Append one event (caller holds ``locked``); refuses to extend a broken chain."""

        problems = self.verify_index()
        if problems:
            raise StoreError("The release-store index is inconsistent: " + "; ".join(problems))
        path = self.root / INDEX
        lines = path.read_bytes().splitlines() if path.is_file() else []
        record = {"seq": len(lines) + 1, "prev": sha256_bytes(lines[-1]) if lines else GENESIS,
                  "at": _now(), **event}
        line = json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")
        with path.open("ab") as handle:
            handle.write(line + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        # Written after the line: an interruption between the two is reported
        # by verify_index (the line exists, the anchor does not name it yet).
        temporary = self.root / f"{INDEX_HEAD}.tmp"
        temporary.write_text(f"{record['seq']} {sha256_bytes(line)}\n", encoding="utf-8", newline="\n")
        os.replace(temporary, self.root / INDEX_HEAD)
        return record


def _inside(base: Path, relative: str) -> Path:
    parts = relative.split("/")
    if relative.startswith("/") or any(part in ("", ".", "..") or "\\" in part or ":" in part for part in parts):
        raise StoreError(f"Invalid path {relative!r} for a release-store entry.")
    return base.joinpath(*parts)


def _read_only(root: Path) -> None:
    for item in root.rglob("*"):
        if item.is_file():
            os.chmod(item, stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)


