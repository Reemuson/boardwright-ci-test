# SPDX-License-Identifier: LicenseRef-Boardwright-Source-Available
# Copyright (c) Ryan Hicks

"""Local-first previews: one implementation for the CLI and the TUI.

``run_preview`` selects the backend once (``backends.select_backend``; every
preview variant defaults to local Docker), then either builds locally in the
pinned container or dispatches the existing GitHub preview workflow. It never
switches backend after a failure; it names the override instead.

Local previews (``docs/LOCAL_FIRST.md`` section 4):

- build in an isolated stage (``container.stage``), never in the developer's
  working tree, which is fingerprinted before and after;
- need no remote, no GitHub authentication and no pushed commit;
- include saved-to-disk changes according to the source policy below;
- are identified by their staged content, not only by the last commit;
- are marked ``purpose: preview`` and ``release_evidence: false`` in the
  stage marker, the build record and the preview record.

Source policy for a dirty working tree:

- Modified and deleted tracked files are included, except the per-user and
  private files in ``ALWAYS_EXCLUDED``.
- Untracked files are included only when they are KiCad project sources
  (``UNTRACKED_PROJECT_PATTERNS``) and not credentials or private
  configuration (``CREDENTIAL_PATTERNS``). Everything else is listed as
  excluded with its reason; commit or ``git add -N`` a file to include it.
- Ignored files, including ``.boardwright/local.yaml``, never enter the stage.
  The build refuses a tree containing ``local.yaml``, so local overrides
  cannot reach outputs, fingerprints or release packages.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable

from . import backends
from .backends import Backend, Operation, Selection
from .errors import BoardwrightError

PREVIEW_SCHEMA = 1
PREVIEW_RECORD = "boardwright-preview.json"  # in the stage's .git directory
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


# ---------------------------------------------------------------- source plan


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


# ---------------------------------------------------------------- locations


def project_key(repo: Path) -> str:
    """Collision-resistant per-checkout key: name plus a hash of the absolute path."""

    path = str(Path(repo).resolve())
    if os.name == "nt":
        path = path.lower()
    return f"{Path(repo).resolve().name}-{hashlib.sha256(path.encode()).hexdigest()[:10]}"


def stage_root() -> Path:
    from .container import default_stage_root

    return default_stage_root()


def preview_stage_dir(repo: Path, variant: str, kicad: str) -> Path:
    return stage_root() / f"{project_key(repo)}-preview-{variant.lower()}-k{kicad}"


def index_dir(repo: Path) -> Path:
    base = Path(os.environ.get("BOARDWRIGHT_PREVIEW_INDEX") or Path.home() / ".boardwright" / "previews")
    return base / project_key(repo)


def index_path(repo: Path, variant: str, kicad: str) -> Path:
    return index_dir(repo) / f"{variant.upper()}-k{kicad}.json"


# ---------------------------------------------------------------- local run

Runner = Callable[[str, Path, list[str]], int]


def _container_runner(kicad: str, workdir: Path, command: list[str]) -> int:
    from .container import run_in_container

    return run_in_container(kicad, workdir, command)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def build_command(variant: str, kicad: str) -> list[str]:
    """The same in-container command as ``container build`` (``container._build``)."""

    return ["python3", "-m", "boardwright", "build", "--variant", variant, "--kicad", kicad, "--log-prefix", "preview"]


def run_local_preview(
    repo: Path,
    variant: str,
    *,
    kicad: str = "9",
    runner: Runner | None = None,
    echo: Callable[[str], None] = print,
    clock: Callable[[], float] = time.monotonic,
) -> dict:
    """Stage, build in the pinned container, record. Returns the preview record."""

    from .stage_lock import stage_lock

    runner = runner or _container_runner
    variant = variant.upper()
    repo = Path(_git(repo, "rev-parse", "--show-toplevel").strip())
    workdir = preview_stage_dir(repo, variant, kicad)
    # Held from before anything in the stage or the index can change until the
    # outputs and the index are recorded (StageBusy if another run holds it).
    with stage_lock(workdir, purpose=f"preview {variant}"):
        return _run_locked(repo, variant, kicad, workdir, runner, echo, clock)


def _run_locked(repo: Path, variant: str, kicad: str, workdir: Path, runner: Runner,
                echo: Callable[[str], None], clock: Callable[[], float]) -> dict:
    from .build import BUILD_DIR
    from .container import ContainerError, image_for, stage

    if _git(repo, "ls-files", "--", LOCAL_OVERRIDES).strip():
        raise BoardwrightError(f"{LOCAL_OVERRIDES} is tracked by Git; it must stay local (untrack it) before previewing.")
    submodules = submodule_paths(repo)
    if submodules:
        raise BoardwrightError(
            "This project uses Git submodules (" + ", ".join(submodules) + "). A local preview cannot reproduce "
            "submodule content in its stage, and building without it would silently omit those libraries; "
            "nothing was built. Vendor the library into the project, or build where submodules are checked out.")
    started, start = _now(), clock()
    index = index_path(repo, variant, kicad)
    # Everything up to the swap works in Boardwright-owned temporary
    # directories beside the stage; a failure leaves the previous completed
    # preview (stage and index entry) exactly as it was.
    _remove_abandoned_temporaries(workdir)
    snapshot = _owned_temporary(workdir, "snapshot")
    staging = _owned_temporary(workdir, "staging")
    try:
        # Snapshot integrity (the stage is the source of truth): 1. fingerprint
        # the tree; 2. read every included file once into the snapshot,
        # hashing those bytes; 3. fingerprint again. A difference means the
        # tree changed while it was read (for example a KiCad save writing
        # several files), so nothing is staged.
        before = worktree_fingerprint(repo)
        plan = plan_source(repo, snapshot=snapshot)
        if worktree_fingerprint(repo) != before:
            raise BoardwrightError("The working tree changed while the preview was reading it (a save in KiCad?). "
                                   "Nothing was built and the previous preview is unchanged; run the preview again.")
        new_stage = staging / "s"
        stage(repo, worktree=plan.dirty, dest=new_stage, purpose="preview",
              changes=([c.path for c in plan.included], list(plan.deleted)), copy_from=snapshot)
        marker = json.loads((new_stage / ".git" / "boardwright-stage").read_text(encoding="utf-8"))
        if marker.get("head") != plan.base_commit:
            # A commit or checkout happened while staging: the stage would not
            # be the planned base plus the snapshot.
            raise BoardwrightError(f"HEAD moved while staging ({plan.base_commit[:12]} -> "
                                   f"{str(marker.get('head'))[:12]}); nothing was built and the previous preview "
                                   "is unchanged; run the preview again.")
        cleared = clear_generated_outputs(new_stage)
        dependencies = scan_dependencies(new_stage, kicad)
        _swap_into_place(new_stage, workdir)
    except (ContainerError, subprocess.CalledProcessError, OSError) as exc:
        raise BoardwrightError(f"Staging failed: {exc}. Nothing was built and the previous preview is unchanged.") from exc
    finally:
        _remove_owned(snapshot, workdir)
        _remove_owned(staging, workdir)
    record: dict = {
        "schema": PREVIEW_SCHEMA,
        "purpose": "preview",
        "release_evidence": False,
        "backend": "local",
        "variant": variant,
        "kicad": kicad,
        "experimental_toolchain": kicad != "9",
        "status": "running",
        "started_at": started,
        "source": {
            "repository": str(repo),
            "base_commit": plan.base_commit,
            "branch": plan.branch,
            "dirty": plan.dirty,
            "source_digest": plan.source_digest,
            "included": [change.__dict__ for change in plan.included],
            "deleted": list(plan.deleted),
            "excluded": [{"path": p, "reason": r} for p, r in plan.excluded],
        },
        "toolchain": {"image": image_for(kicad).reference},
        "outputs": str(workdir),
        "evidence": str(workdir / BUILD_DIR),
        "cleared_outputs": cleared,
        "dependencies": dependencies,
    }
    echo(f"[preview] {variant} local (KiCad {kicad}) from {plan.base_commit[:12]}"
         + (f" + {len(plan.included)} changed, {len(plan.deleted)} deleted file(s)" if plan.dirty else " (clean)"))
    for path, reason in plan.excluded:
        echo(f"[preview] excluded {path}: {reason}")
    for item in dependencies["unresolved"]:
        echo(f"[preview] warning: unresolved {item['kind']} {item['reference']} in {item['file']}: {item['reason']}")
    for item in cleared:
        echo(f"[preview] cleared {item['path']} from the stage ({item['files']} file(s), "
             f"{'tracked' if item['tracked'] else 'untracked'}); the build regenerates it")
    try:
        staged_commit = _git(workdir, "rev-parse", "HEAD").strip()
        record["source"]["staged_commit"] = staged_commit
        record["source"]["staged_tree"] = _git(workdir, "rev-parse", "HEAD^{tree}").strip()
        record["identity"] = preview_identity(record)
        # Before Docker starts: an interrupted build's evidence is already
        # marked as preview evidence (the post-build write repeats it).
        _write_evidence_marker(record)
        _write_json(workdir / ".git" / PREVIEW_RECORD, record)
        _write_json(index, record)
        echo(f"[preview] staged {workdir}")
        status = runner(kicad, workdir, build_command(variant, kicad))
        error = "" if status == 0 else f"the container build exited with status {status}"
    except (ContainerError, BoardwrightError, OSError, subprocess.CalledProcessError) as exc:
        status, error = 1, str(exc)
    record["exit_status"] = status
    record["finished_at"] = _now()
    record["elapsed_seconds"] = round(clock() - start, 1)
    evidence = Path(record["evidence"]) / "record.json"
    if Path(record["evidence"]).parent.is_dir():
        _write_evidence_marker(record)
    if evidence.is_file():
        built = json.loads(evidence.read_text(encoding="utf-8"))
        record["toolchain"].update({
            "image": built.get("image") or record["toolchain"]["image"],
            "image_id": built.get("image_id", ""),
            "boardwright_tree": (built.get("boardwright_source") or {}).get("tree", ""),
            "kibot_config_tree": (built.get("configuration") or {}).get("kibot_config_tree", ""),
            "tools": built.get("tools", {}),
        })
        record["build_record_stage"] = built.get("stage", {})
    if worktree_fingerprint(repo) != before:
        record["worktree_changed_during_preview"] = True
        echo("[preview] warning: the working tree changed while the preview ran; "
             "the outputs show the staged snapshot and the preview is stale.")
    if (workdir / ".git").is_dir() and "staged_tree" in record["source"]:
        # Reports present now were generated by this run: the stage was
        # recreated and its Reports/ cleared before the build.
        from . import design_rules

        record["design_rules"] = design_rules.collect(workdir)
    record["status"] = "succeeded" if status == 0 and not error else "failed"
    if error:
        record["error"] = error
    record.setdefault("identity", preview_identity(record))
    if (workdir / ".git").is_dir():
        _write_json(workdir / ".git" / PREVIEW_RECORD, record)
    _write_json(index, record)
    return record


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


_MODEL = re.compile(r'\(model\s+"((?:[^"\\]|\\.)*)"')
_URI = re.compile(r'\(uri\s+"((?:[^"\\]|\\.)*)"')
_VAR = re.compile(r"^\$\{([A-Za-z0-9_]+)\}")


def _classify(reference: str, kind: str, source: str, stage_dir: Path, kicad: str) -> dict | None:
    """None when the reference resolves in the stage or the pinned image; else why it may not."""

    stock = {f"KICAD{kicad}_3DMODEL_DIR", f"KICAD{kicad}_FOOTPRINT_DIR", f"KICAD{kicad}_SYMBOL_DIR"}
    value = reference.replace("\\", "/")
    match = _VAR.match(value)
    if match:
        name = match.group(1)
        if name in stock:
            return None  # provided by the pinned image's stock libraries
        if name == "KIPRJMOD":
            relative = value[match.end():].lstrip("/")
            if (stage_dir / relative).exists():
                return None
            return {"kind": kind, "reference": reference, "file": source, "reason": "missing from the staged project"}
        if name.startswith("KICAD") and name.endswith(("_3DMODEL_DIR", "_FOOTPRINT_DIR", "_SYMBOL_DIR")):
            return {"kind": kind, "reference": reference, "file": source,
                    "reason": f"stock library of another KiCad version; may not resolve with KiCad {kicad}"}
        return {"kind": kind, "reference": reference, "file": source,
                "reason": f"environment variable {name} is not defined in the build container"}
    if re.match(r"^[A-Za-z]:/", value) or value.startswith("/") or value.startswith("~"):
        return {"kind": kind, "reference": reference, "file": source, "reason": "absolute path outside the project"}
    if (stage_dir / PurePosixPath(source).parent / value).exists() or (stage_dir / value).exists():
        return None
    return {"kind": kind, "reference": reference, "file": source, "reason": "missing from the staged project"}


def scan_dependencies(stage_dir: Path, kicad: str) -> dict:
    """3D models and library tables the staged project names, and those that may not resolve.

    Only what can be detected reliably from the staged files: ``(model ...)``
    paths in boards and ``(uri ...)`` entries in project library tables.
    Libraries named only by nickname (resolved through a user's global
    tables) are not visible here; ERC reports those.
    """

    checked = {"3d-model": 0, "footprint-library": 0, "symbol-library": 0}
    unresolved: list[dict] = []
    seen = set()
    sources = [(p, "3d-model", _MODEL) for p in stage_dir.rglob("*.kicad_pcb")]
    sources += [(p, "footprint-library", _URI) for p in stage_dir.rglob("fp-lib-table")]
    sources += [(p, "symbol-library", _URI) for p in stage_dir.rglob("sym-lib-table")]
    for path, kind, pattern in sources:
        if ".git" in path.relative_to(stage_dir).parts:
            continue
        source = path.relative_to(stage_dir).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        for reference in pattern.findall(text):
            if (kind, reference) in seen:
                continue
            seen.add((kind, reference))
            checked[kind] += 1
            problem = _classify(reference, kind, source, stage_dir, kicad)
            if problem:
                unresolved.append(problem)
    return {"checked": checked, "unresolved": unresolved[:100], "unresolved_count": len(unresolved),
            "complete": not unresolved}


def generated_paths() -> tuple[str, ...]:
    """Output trees and generated tracked files a build writes (never sources)."""

    from .build import GENERATED_TRACKED
    from .ci_cleanup import OUTPUT_DIRS

    return tuple(OUTPUT_DIRS) + tuple(GENERATED_TRACKED)


def clear_generated_outputs(stage_dir: Path) -> list[dict]:
    """Remove previously generated outputs from the stage copy before the build.

    A branch may track generated outputs (accepted outputs, README.md,
    renders). Left in the stage, a failed build or a variant that does not
    regenerate one of them would present the committed files as this
    preview's outputs. Only the stage is changed, never the working tree; the
    removal is committed in the stage so the staged tree records it.
    """

    from .container import _remove_tree

    cleared = []
    tracked_paths = []
    for path in generated_paths():
        target = stage_dir / path
        if not (target.exists() or target.is_symlink()):
            continue
        files = sum(1 for p in target.rglob("*") if p.is_file()) if target.is_dir() else 1
        tracked = bool(_git(stage_dir, "ls-files", "--", path).strip())
        cleared.append({"path": path, "files": files, "tracked": tracked})
        if tracked:
            tracked_paths.append(path)
        if target.is_dir() and not target.is_symlink():
            _remove_tree(target)
        else:
            target.unlink()
    if tracked_paths:
        _git(stage_dir, "rm", "-r", "-q", "--cached", "--ignore-unmatch", "--", *tracked_paths)
        _git(stage_dir, "-c", "user.name=Boardwright stage", "-c", "user.email=stage@boardwright.invalid",
             "commit", "--quiet", "--no-verify", "-m", "Clear generated outputs before the local preview build")
    return cleared


def _write_evidence_marker(record: dict) -> None:
    """Mark the evidence directory itself, whatever the project's payload version.

    An older payload's ``record.json`` has no stage purpose, and a copied
    ``boardwright-build`` directory loses the stage's ``.git`` marker.
    """

    from .container import PREVIEW_EVIDENCE_MARKER

    _write_json(Path(record["evidence"]) / PREVIEW_EVIDENCE_MARKER, {
        "purpose": "preview", "release_evidence": False, "backend": "local",
        "variant": record["variant"], "source_digest": record["source"].get("source_digest", ""),
        "staged_tree": record["source"].get("staged_tree", ""),
    })


TEMP_OWNER = ".boardwright-temporary"


def _owned_temporary(workdir: Path, kind: str) -> Path:
    """A new, uniquely named directory beside the stage, marked as Boardwright's.

    ``mkdtemp`` creates it atomically and never reuses an existing path, so a
    run never adopts another run's (or anyone's) directory.
    """

    import tempfile

    workdir.parent.mkdir(parents=True, exist_ok=True)
    # Short names: a nested stage under a name repeating the stage name
    # doubled the path length and broke clones on Windows (MAX_PATH).
    # Ownership comes from the marker's stage name, never from the name.
    path = Path(tempfile.mkdtemp(prefix=f".bw-{kind}-", dir=workdir.parent))
    (path / TEMP_OWNER).write_text(json.dumps({"stage": workdir.name, "kind": kind, "pid": os.getpid()}) + "\n",
                                   encoding="utf-8")
    return path


def _is_owned_temporary(path: Path, workdir: Path) -> bool:
    marker = path / TEMP_OWNER
    if path.is_symlink() or not marker.is_file() or marker.is_symlink():
        return False
    try:
        return json.loads(marker.read_text(encoding="utf-8")).get("stage") == workdir.name
    except (OSError, ValueError):
        return False


def _remove_owned(path: Path, workdir: Path) -> None:
    """Remove a temporary directory only when it carries this stage's owner marker."""

    if path.exists() and _is_owned_temporary(path, workdir):
        from .container import _remove_tree

        _remove_tree(path)


def _remove_abandoned_temporaries(workdir: Path) -> None:
    """Temporaries left by a killed run of this stage (the stage lock is held)."""

    from .container import _remove_tree

    for kind in ("snapshot", "staging", "retired"):
        for path in workdir.parent.glob(f".bw-{kind}-*"):
            if _is_owned_temporary(path, workdir):
                _remove_tree(path)


def _swap_into_place(new_stage: Path, workdir: Path) -> None:
    """Replace the previous stage with the new one; refuse anything not a stage."""

    from .container import STAGE_MARKER, _remove_tree

    if workdir.exists() or workdir.is_symlink():
        if workdir.is_symlink() or not (workdir / ".git" / STAGE_MARKER).is_file():
            raise BoardwrightError(f"{workdir} exists and is not a Boardwright stage; refusing to replace it.")
        retired = _owned_temporary(workdir, "retired")
        old = retired / "s"
        try:
            os.replace(workdir, old)
        except OSError as exc:
            _remove_tree(retired)
            raise BoardwrightError(f"Cannot replace the previous preview in {workdir} ({exc}); a file in it may be "
                                   "open in another program. The previous preview is unchanged.") from exc
        try:
            os.replace(new_stage, workdir)
        except OSError:
            os.replace(old, workdir)  # put the previous preview back
            raise
        _remove_tree(retired)
    else:
        os.replace(new_stage, workdir)


def preview_identity(record: dict) -> str:
    """``VARIANT@<staged tree>`` plus ``+dirty`` and the base commit when not clean."""

    source = record.get("source", {})
    tree = (source.get("staged_tree") or source.get("source_digest") or "")[:12]
    text = f"{record.get('variant')}@{tree}"
    if source.get("dirty"):
        text += f" (dirty, base {source.get('base_commit', '')[:12]})"
    return text


# ---------------------------------------------------------------- selection and dispatch


@dataclass
class PreviewOutcome:
    selection: Selection
    ok: bool
    message: str
    record: dict | None = None
    lines: list[str] = field(default_factory=list)
    # Design-rule state, separate from build success: True errors, False
    # clean, None unknown (or not a local build).
    rule_errors: bool | None = None


def select_preview_backend(
    repo: Path,
    variant: str,
    *,
    project_setting: str = "auto",
    override: str = "",
    inspect_remote=backends.inspect_remote,
    inspect_docker=backends.inspect_docker,
) -> Selection:
    """The shared selection. GitHub access is inspected only when GitHub is requested."""

    wanted = (override if override and override != "auto" else project_setting) or "auto"
    remote = inspect_remote(repo, check_access=(wanted == "github"))
    docker = inspect_docker(root=repo)
    return backends.select_backend(Operation.PREVIEW, remote=remote, docker=docker, variant=variant,
                                   project_setting=project_setting, override=override)


def github_preview_problems(config) -> list[str]:
    """Preconditions of the GitHub preview workflow (it builds a pushed commit)."""

    from .status import collect_status

    status = collect_status(config)
    problems = []
    if status.branch != config.dev_branch:
        problems.append(f"switch to {config.dev_branch} to preview on GitHub")
    if status.dirty_count:
        problems.append("commit local changes before a GitHub preview (a local preview includes them)")
    if status.ahead:
        problems.append("push commits before a GitHub preview")
    return problems


def run_preview(
    config,
    variant: str | None,
    *,
    override: str = "",
    kicad: str = "9",
    echo: Callable[[str], None] = print,
    runner: Runner | None = None,
    inspect_remote=backends.inspect_remote,
    inspect_docker=backends.inspect_docker,
    dispatch: Callable | None = None,
) -> PreviewOutcome:
    """Select once, run the selected backend, never switch."""

    from .preview import build_preview_plan, dispatch_preview
    from .variants import normalize_variant

    try:
        chosen = normalize_variant(variant or config.preview_variant)
    except BoardwrightError as exc:
        refused = Selection(Operation.PREVIEW, None, "override", "invalid variant", (str(exc),))
        return PreviewOutcome(refused, False, str(exc))
    selection = select_preview_backend(config.root, chosen, project_setting=config.preview_backend, override=override,
                                       inspect_remote=inspect_remote, inspect_docker=inspect_docker)
    echo(backends.format_selection(selection))
    if not selection.ok:
        return PreviewOutcome(selection, False, "; ".join(selection.problems))
    if selection.backend is Backend.GITHUB:
        problems = github_preview_problems(config)
        if problems:
            return PreviewOutcome(selection, False, "; ".join(problems))
        plan = build_preview_plan(config, chosen)
        try:
            (dispatch or dispatch_preview)(plan, config.root)
        except BoardwrightError as exc:
            return PreviewOutcome(selection, False, f"GitHub preview failed: {exc} (not retried locally; "
                                  "run with --backend local to build locally)")
        return PreviewOutcome(selection, True, f"GitHub preview {chosen} dispatched ({plan.workflow} on {plan.branch}).")
    try:
        record = run_local_preview(config.root, chosen, kicad=kicad, runner=runner, echo=echo)
    except BoardwrightError as exc:
        return PreviewOutcome(selection, False, str(exc))
    if record["status"] != "succeeded":
        hint = " (not retried on GitHub; run with --backend github to build there)"
        return PreviewOutcome(selection, False, f"Local preview {chosen} failed: {record.get('error', '')}{hint}", record)
    from . import design_rules

    rules = record.get("design_rules")
    message = (f"Local preview {record['identity']}: build completed, outputs generated; "
               f"{design_rules.headline(rules)}. Outputs: {record['outputs']}")
    return PreviewOutcome(selection, True, message, record, rule_errors=design_rules.has_errors(rules or {}))


# ---------------------------------------------------------------- status


@dataclass(frozen=True)
class PreviewStatus:
    variant: str
    kicad: str
    state: str  # "current" | "stale" | "failed" | "incomplete" | "missing outputs"
    reasons: tuple[str, ...]
    record: dict


def preview_status(repo: Path, *, plan: SourcePlan | None = None) -> list[PreviewStatus]:
    """Every recorded local preview of this checkout, with staleness."""

    from .container import image_for

    directory = index_dir(repo)
    if not directory.is_dir():
        return []
    plan = plan or plan_source(repo)
    found = []
    for path in sorted(directory.glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        reasons = []
        state = "current"
        if record.get("status") == "running":
            state, reasons = "incomplete", ["the preview did not finish (interrupted, or still running)"]
        elif record.get("status") != "succeeded":
            state, reasons = "failed", [record.get("error", "the build failed")]
        else:
            source = record.get("source", {})
            if source.get("source_digest") != plan.source_digest:
                reasons.append("the working tree or HEAD changed since this preview")
            if record.get("worktree_changed_during_preview"):
                reasons.append("the working tree changed while it ran")
            if record.get("toolchain", {}).get("image") != image_for(record.get("kicad", "9")).reference:
                reasons.append("the pinned toolchain changed")
            if not Path(record.get("outputs", "")).is_dir():
                state, reasons = "missing outputs", reasons + ["the stage directory was removed"]
            elif reasons:
                state = "stale"
        found.append(PreviewStatus(record.get("variant", "?"), str(record.get("kicad", "9")), state, tuple(reasons), record))
    return found


def format_record(record: dict) -> list[str]:
    source = record.get("source", {})
    toolchain = record.get("toolchain", {})
    from . import design_rules

    build_line = {"succeeded": "completed, outputs generated", "failed": "FAILED, no usable outputs",
                  "running": "did not finish"}.get(record.get("status"), str(record.get("status")))
    lines = [
        f"Preview: {record.get('identity', '?')} (local, never release evidence)",
        f"  Build: {build_line}",
        *(f"  {line}" for line in (design_rules.summary_lines(record.get("design_rules"))
                                   if record.get("status") == "succeeded" else
                                   ["Design rules: not evaluated (the build did not complete)"])),
        f"  Base commit: {source.get('base_commit', '')}" + (" + uncommitted changes" if source.get("dirty") else " (clean)"),
        f"  Staged source: tree {source.get('staged_tree', '?')}, digest {source.get('source_digest', '')[:16]}",
        f"  Toolchain: {toolchain.get('image', '')}" + (f" ({toolchain['image_id']})" if toolchain.get("image_id") else ""),
        (f"  Outputs: {record.get('outputs', '')}" if record.get("status") == "succeeded" else
         f"  Outputs: none usable ({record.get('status')}; the stage holds only this run's partial output): "
         f"{record.get('outputs', '')}"),
        f"  Evidence: {record.get('evidence', '')}",
    ]
    if record.get("elapsed_seconds") is not None:
        lines.append(f"  Elapsed: {record['elapsed_seconds']} s")
    if source.get("included"):
        lines.append(f"  Included changes: {', '.join(c['path'] for c in source['included'])}")
    if source.get("deleted"):
        lines.append(f"  Deleted: {', '.join(source['deleted'])}")
    if source.get("excluded"):
        lines.append(f"  Excluded: {', '.join(e['path'] + ' (' + e['reason'] + ')' for e in source['excluded'])}")
    dependencies = record.get("dependencies")
    if dependencies and not dependencies.get("complete", True):
        lines.append(f"  Dependencies: {dependencies.get('unresolved_count')} unresolved reference(s); "
                     "outputs may be incomplete and this is not a complete reproduction")
        lines += [f"    - {d['kind']} {d['reference']} ({d['file']}): {d['reason']}"
                  for d in dependencies.get("unresolved", [])[:10]]
    if record.get("experimental_toolchain"):
        lines.append("  KiCad 10 is experimental.")
    if record.get("error"):
        lines.append(f"  Error: {record['error']}")
    return lines


def format_status(statuses: list[PreviewStatus]) -> str:
    if not statuses:
        return "No local previews recorded for this checkout. Run: boardwright preview"
    lines = []
    for status in statuses:
        lines.append(f"{status.variant} (KiCad {status.kicad}): {status.state}")
        lines += [f"  - {reason}" for reason in status.reasons]
        lines += ["  " + line for line in format_record(status.record)]
    return "\n".join(lines)


def latest_outputs(repo: Path) -> Path | None:
    """Outputs of the most recent successful local preview (for "open folder")."""

    best = None
    for status in preview_status(repo):
        if status.record.get("status") == "succeeded" and Path(status.record.get("outputs", "")).is_dir():
            if best is None or status.record.get("finished_at", "") > best.record.get("finished_at", ""):
                best = status
    return Path(best.record["outputs"]) if best else None


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - thin wrapper for debugging
    print(format_status(preview_status(Path.cwd())))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
