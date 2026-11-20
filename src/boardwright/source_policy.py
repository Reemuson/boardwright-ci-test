# SPDX-License-Identifier: LicenseRef-Boardwright-Source-Available
# Copyright (c) Ryan Hicks

"""Which working-tree files a local build may use (previews and worktree builds).

One policy for ``boardwright preview`` and ``container build --worktree`` /
``container stage --worktree`` (audit C1: the direct worktree build used to
stage every untracked, non-ignored file, including ``.env``).

- Modified and deleted tracked files are included, except the per-user and
  private files in ``ALWAYS_EXCLUDED``.
- Untracked files are included only when they are KiCad project sources
  (``UNTRACKED_PROJECT_PATTERNS``), or images a project file names, and their
  names do not look like credentials (``CREDENTIAL_PATTERNS``). Everything
  else is listed as excluded with a reason.
- Ignored files, including ``.boardwright/local.yaml``, never enter a stage.
- Fail-closed preconditions (``check_preconditions``): a tracked
  ``local.yaml``, Git submodules, and tracked files whose names look like
  credentials (unless listed in ``source_policy.allow_tracked`` in
  ``.boardwright/project.yaml``) are refused before anything is read.

Only paths and hashes are ever recorded or printed, never file contents.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .errors import BoardwrightError

LOCAL_OVERRIDES = ".boardwright/local.yaml"

# Tracked or untracked: per-user state and private configuration.
ALWAYS_EXCLUDED = (
    LOCAL_OVERRIDES,
    "*.kicad_prl",          # KiCad per-user view state
    "*-backups/*",          # KiCad automatic backups
    "*.lck",                # KiCad lock files
    "fp-info-cache",
    "_autosave-*",
    "*.bak",
    "*~",
)
# Untracked files that are project sources and may be included.
UNTRACKED_PROJECT_PATTERNS = (
    "*.kicad_sch", "*.kicad_pcb", "*.kicad_pro", "*.kicad_sym", "*.kicad_mod", "*.kicad_wks", "*.kicad_dru",
    "*.kicad_dbl", "*.kicad_httplib", "fp-lib-table", "sym-lib-table",
    "*.step", "*.stp", "*.stpz", "*.wrl", "*.wrz", "*.igs", "*.iges",
)
# Untracked images are included only when a project file names their path
# (a worksheet or title-block logo, a render, a documentation figure).
IMAGE_PATTERNS = ("*.png", "*.jpg", "*.jpeg", "*.svg", "*.bmp")
REFERENCE_SOURCES = ("*.kicad_sch", "*.kicad_pcb", "*.kicad_pro", "*.kicad_wks", "*.md",
                     ".boardwright/project.yaml", "boardwright_resources/*.yaml")
# Never included when untracked, whatever the extension (matched on the file name, case-insensitively).
CREDENTIAL_PATTERNS = (
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*", "id_ed25519*", "id_ecdsa*",
    ".netrc", ".git-credentials", "*credential*", "*secret*", "*token*", "*.kdbx",
)




@dataclass(frozen=True)
class SourceChange:
    path: str
    kind: str  # "modified" | "untracked"
    sha256: str


@dataclass(frozen=True)
class SourcePlan:
    """What a local preview stages from the working tree."""

    repository: Path
    base_commit: str
    branch: str
    included: tuple[SourceChange, ...]
    deleted: tuple[str, ...]
    excluded: tuple[tuple[str, str], ...]  # (path, reason)

    @property
    def dirty(self) -> bool:
        return bool(self.included or self.deleted)

    @property
    def source_digest(self) -> str:
        """Content identity of the staged source: base commit plus every staged change."""

        digest = hashlib.sha256(f"base {self.base_commit}\n".encode())
        for change in self.included:
            digest.update(f"file {change.path}\0{change.sha256}\n".encode())
        for path in self.deleted:
            digest.update(f"delete {path}\n".encode())
        return digest.hexdigest()


def _git(repo: Path, *args: str, check: bool = True) -> str:
    done = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, encoding="utf-8")
    if check and done.returncode != 0:
        raise BoardwrightError(f"git {' '.join(args)} failed: {done.stderr.strip()}")
    return done.stdout


def _matches(path: str, patterns: tuple[str, ...]) -> bool:
    name = PurePosixPath(path).name.lower()
    lowered = path.lower()
    return any(fnmatch.fnmatchcase(name, p.lower()) or fnmatch.fnmatchcase(lowered, p.lower()) for p in patterns)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_once(repo: Path, path: str, snapshot: Path | None) -> str:
    """Hash a file; with ``snapshot``, also write exactly the hashed bytes there.

    The working tree is read once per file, so the recorded hash and the staged
    bytes cannot differ even if the file is saved again meanwhile.
    """

    if snapshot is None:
        return _sha256(repo / path)
    data = _read_bytes(repo / path)
    target = snapshot / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def _read_bytes(path: Path) -> bytes:
    return path.read_bytes()


def _reference_texts(repo: Path, untracked: set[str]) -> list[tuple[str, str]]:
    """(path, text) of every file that may name an image: tracked or eligible untracked."""

    tracked = [p for p in _git(repo, "ls-files", "-z").split("\0") if p]
    candidates = [p for p in tracked + sorted(untracked)
                  if _matches(p, REFERENCE_SOURCES) and not _matches(p, CREDENTIAL_PATTERNS + ALWAYS_EXCLUDED)]
    texts = []
    for path in candidates:
        try:
            texts.append((path, (repo / path).read_text(encoding="utf-8", errors="replace")))
        except OSError:
            continue
    return texts


def _referenced(repo: Path, image: str, references: list[tuple[str, str]]) -> bool:
    """Whether some project file names ``image`` by its repository path or by a path relative to itself."""

    for source, text in references:
        if source == image:
            continue
        relative = os.path.relpath(image, start=str(PurePosixPath(source).parent) or ".").replace(os.sep, "/")
        for form in {image, relative, "./" + relative, "${KIPRJMOD}/" + image}:
            if form in text:
                return True
    return False


def plan_source(repo: Path, snapshot: Path | None = None) -> SourcePlan:
    """Apply the source policy to the working tree (read-only).

    With ``snapshot``, the included files' bytes are also copied there (read
    once); the stage is then built from the snapshot, not the live tree.
    """

    repo = Path(_git(repo, "rev-parse", "--show-toplevel").strip())
    base = _git(repo, "rev-parse", "HEAD").strip()
    branch = _git(repo, "symbolic-ref", "--quiet", "--short", "HEAD", check=False).strip() or "detached"
    modified = {p for p in _git(repo, "ls-files", "-z", "--modified").split("\0") if p}
    deleted = {p for p in _git(repo, "ls-files", "-z", "--deleted").split("\0") if p}
    untracked = {p for p in _git(repo, "ls-files", "-z", "--others", "--exclude-standard").split("\0") if p}
    included: list[SourceChange] = []
    excluded: list[tuple[str, str]] = []
    references = _reference_texts(repo, untracked)
    for path in sorted(modified - deleted):
        if _matches(path, ALWAYS_EXCLUDED):
            excluded.append((path, "per-user or private file"))
        elif not (repo / path).is_file() or (repo / path).is_symlink():
            excluded.append((path, "not a regular file"))
        else:
            included.append(SourceChange(path, "modified", _read_once(repo, path, snapshot)))
    for path in sorted(untracked):
        if _matches(path, ALWAYS_EXCLUDED):
            excluded.append((path, "per-user or private file"))
        elif _matches(path, CREDENTIAL_PATTERNS):
            excluded.append((path, "looks like a credential"))
        elif _matches(path, IMAGE_PATTERNS):
            if not _referenced(repo, path, references):
                excluded.append((path, "untracked image not referenced by a KiCad or project file"))
            elif (repo / path).is_symlink() or not (repo / path).is_file():
                excluded.append((path, "not a regular file"))
            else:
                included.append(SourceChange(path, "untracked", _read_once(repo, path, snapshot)))
        elif not _matches(path, UNTRACKED_PROJECT_PATTERNS):
            excluded.append((path, "untracked and not a KiCad project file"))
        elif (repo / path).is_symlink() or not (repo / path).is_file():
            excluded.append((path, "not a regular file"))
        else:
            included.append(SourceChange(path, "untracked", _read_once(repo, path, snapshot)))
    kept_deleted = []
    for path in sorted(deleted):
        if _matches(path, ALWAYS_EXCLUDED):
            excluded.append((path, "per-user or private file"))
        else:
            kept_deleted.append(path)
    return SourcePlan(repo, base, branch, tuple(included), tuple(kept_deleted), tuple(excluded))


def worktree_fingerprint(repo: Path) -> str:
    """Hash of every non-ignored file's bytes, ``local.yaml``, HEAD and the index.

    Compared before and after a preview: Boardwright never writes to the
    working tree, so any difference is reported.
    """

    digest = hashlib.sha256()
    digest.update(_git(repo, "rev-parse", "HEAD").encode())
    digest.update(_git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all").encode())
    files = sorted({p for p in _git(repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard").split("\0") if p})
    for path in files + [LOCAL_OVERRIDES]:
        target = repo / path
        digest.update(path.encode() + b"\0")
        if target.is_file():
            digest.update(_sha256(target).encode())
    return digest.hexdigest()


def submodule_paths(repo: Path) -> list[str]:
    """Gitlinks in the index, plus any path declared in .gitmodules."""

    paths = []
    for entry in _git(repo, "ls-files", "-s", "-z").split("\0"):
        if entry.startswith("160000 "):
            paths.append(entry.split("\t", 1)[1])
    if (repo / ".gitmodules").is_file():
        listed = _git(repo, "config", "-f", ".gitmodules", "--get-regexp", r"^submodule\..*\.path$", check=False)
        paths += [line.split(" ", 1)[1] for line in listed.splitlines() if " " in line]
        if not paths:
            paths.append(".gitmodules")
    return sorted(set(paths))


def allowed_tracked(repo: Path) -> set[str]:
    """``source_policy.allow_tracked`` in project.yaml: tracked credential-like names the project vouches for."""

    path = repo / ".boardwright" / "project.yaml"
    if not path.is_file():
        return set()
    try:
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:  # an unreadable project file allows nothing
        return set()
    policy = data.get("source_policy") if isinstance(data, dict) else None
    allowed = policy.get("allow_tracked") if isinstance(policy, dict) else None
    return {str(item) for item in allowed} if isinstance(allowed, list) else set()


def tracked_credentials(repo: Path) -> list[str]:
    """Tracked files whose names look like credentials and are not explicitly allowed."""

    allowed = allowed_tracked(repo)
    tracked = [p for p in _git(repo, "ls-files", "-z").split("\0") if p]
    return sorted(p for p in tracked if _matches(p, CREDENTIAL_PATTERNS) and p not in allowed)


def check_preconditions(repo: Path) -> None:
    """Refuse, before reading anything, sources a local build cannot use safely or completely."""

    if _git(repo, "ls-files", "--", LOCAL_OVERRIDES).strip():
        raise BoardwrightError(f"{LOCAL_OVERRIDES} is tracked by Git; it must stay local (untrack it) before building.")
    submodules = submodule_paths(repo)
    if submodules:
        raise BoardwrightError(
            "This project uses Git submodules (" + ", ".join(submodules) + "). A local build cannot reproduce "
            "submodule content in its stage, and building without it would silently omit those libraries; "
            "nothing was built. Vendor the library into the project, or build where submodules are checked out.")
    credentials = tracked_credentials(repo)
    if credentials:
        # Paths only: the files are never read here.
        raise BoardwrightError(
            "Tracked files whose names look like credentials would be copied into the build stage and visible "
            "to the build container: " + ", ".join(credentials[:20])
            + (f" (and {len(credentials) - 20} more)" if len(credentials) > 20 else "")
            + ". Remove them from the repository, rename them, or list each path under "
            "source_policy.allow_tracked in .boardwright/project.yaml if it is not a secret; nothing was built.")


def plan_summary(plan: SourcePlan) -> dict:
    """Paths, hashes and reasons only: never contents."""

    return {
        "base_commit": plan.base_commit,
        "branch": plan.branch,
        "dirty": plan.dirty,
        "source_digest": plan.source_digest,
        "included": [change.__dict__ for change in plan.included],
        "deleted": list(plan.deleted),
        "excluded": [{"path": p, "reason": r} for p, r in plan.excluded],
    }
