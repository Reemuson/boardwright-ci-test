# SPDX-License-Identifier: LicenseRef-Boardwright-Source-Available
# Copyright (c) Ryan Hicks

"""Run Boardwright commands in the pinned KiBot container, locally or in CI.

This is the only place that names the container image. GitHub Actions and a
Windows workstation both call ``python -m boardwright container ...``, so they
use the same image digest and the same in-container commands
(``python -m boardwright build`` and the release-model modules).

Local builds never mount the developer's working tree. ``stage`` makes a fresh
clone of a commit with LF line endings, without ``.boardwright/local.yaml`` or
any other ignored file, and the build runs there. CI mounts its own disposable
checkout instead (``--workdir .``).

Commands::

    python -m boardwright container image   [--kicad 9|10] [--check]
    python -m boardwright container stage   [--worktree] [--dest DIR]
    python -m boardwright container run     [--kicad N] [--workdir DIR] -- COMMAND ...
    python -m boardwright container build   --variant V [--kicad N] [--worktree | --workdir DIR]
    python -m boardwright container candidate VERSION --drawing-revision R ... [--kicad N]
    python -m boardwright container compare LOCAL_DIR GITHUB_DIR
    python -m boardwright container shell   [--kicad N] [--workdir DIR]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .build import BUILD_DIR, SUPPORTED_KICAD, VARIANTS
from .errors import BoardwrightError
from .stage_lock import stage_lock


@dataclass(frozen=True)
class Image:
    kicad: str
    repository: str
    tag: str
    digest: str

    @property
    def reference(self) -> str:
        # Docker resolves by digest; the tag is kept only for readability.
        return f"{self.repository}:{self.tag}@{self.digest}"


# Stable KiBot releases (not the moving ``dev``/``v2_dk*`` tags). To change an
# image, update the tag and the digest together, run
# ``python -m boardwright container image --check``, and treat the change as a
# ``tooling`` change: rebuild and compare the controlled outputs first.
IMAGES = {
    "9": Image(
        "9",
        "ghcr.io/inti-cmnb/kicad9_auto_full",
        "1.9.1-2_k9.0.7_d13.2_b4.2.4LTS",
        "sha256:4d47f3b808e018d0a45afa597b4ffcd2586128bfa9801e5ebedd64cc45758092",
    ),
    "10": Image(
        "10",
        "ghcr.io/inti-cmnb/kicad10_auto_full",
        "1.9.1-2_k10.0.5_d13.2_b4.2.4LTS",
        "sha256:96e75f885b15617c6b38aef11db7856a93f4367cab7a0b6c2b30465557619330",
    ),
}
DEFAULT_KICAD = "9"
WORKDIR = "/work"
CACHE_3D = "/cache_3d"
from .staging import STAGE_MARKER, remove_tree as _remove_tree  # noqa: E402,F401


class ContainerError(RuntimeError):
    pass


def image_for(kicad: str) -> Image:
    try:
        return IMAGES[kicad]
    except KeyError:
        raise ContainerError(f"No pinned image for KiCad {kicad!r}; supported: {', '.join(IMAGES)}.") from None


# --- docker invocation -------------------------------------------------------


def docker_command(
    image: Image,
    workdir: Path,
    command: list[str],
    *,
    cache_dir: Path | None = None,
    environment: dict[str, str] | None = None,
    interactive: bool = False,
    user: str | None = None,
) -> list[str]:
    """The ``docker run`` argv. Pure, so the mount and identity rules are testable.

    - The work tree is the only read-write project mount.
    - On a POSIX host the container runs as the calling user, so outputs are
      owned by that user (no ``sudo chown`` steps). Docker Desktop on Windows
      maps ownership itself.
    - ``HOME`` is a container-private directory: fonts and KiCad settings never
      come from, or leak to, the host.
    - Git inside the container trusts the mounted tree (its owner differs from
      the container user on bind mounts).
    """

    env = {
        "HOME": "/tmp/boardwright-home",
        "PYTHONPATH": f"{WORKDIR}/src",
        "PYTHONDONTWRITEBYTECODE": "1",
        "BOARDWRIGHT_IMAGE": image.reference,
        "BOARDWRIGHT_CONTAINER": "1",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "safe.directory",
        "GIT_CONFIG_VALUE_0": "*",
    }
    argv = ["docker", "run", "--rm", "--init", "--pull", "missing"]
    argv += ["-it"] if interactive else []
    if user is None:
        # BOARDWRIGHT_CONTAINER_USER=1001:1001 reproduces the GitHub runner uid:gid on Docker Desktop.
        user = os.environ.get("BOARDWRIGHT_CONTAINER_USER")
    if user is None and hasattr(os, "getuid"):
        user = f"{os.getuid()}:{os.getgid()}"
    if user:
        argv += ["--user", user]
    argv += ["--volume", f"{workdir.resolve()}:{WORKDIR}", "--workdir", WORKDIR]
    if cache_dir is not None:
        argv += ["--volume", f"{cache_dir}:{CACHE_3D}"]
        env["KIBOT_3D_MODELS"] = CACHE_3D
    if os.environ.get("GITHUB_ACTIONS") == "true":
        for name in ("GITHUB_ACTIONS", "GITHUB_REPOSITORY", "GITHUB_RUN_ID", "GITHUB_SHA", "GITHUB_REF_NAME"):
            if name in os.environ:
                env[name] = os.environ[name]
    env.update(environment or {})
    for key in sorted(env):
        argv += ["--env", f"{key}={env[key]}"]
    argv += ["--entrypoint", command[0], image.reference, *command[1:]]
    return argv


def default_cache_dir(kicad: str) -> Path | str:
    """3D model cache: ``$BOARDWRIGHT_3D_CACHE`` (CI) or a named Docker volume."""

    configured = os.environ.get("BOARDWRIGHT_3D_CACHE")
    if configured:
        path = Path(configured).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        return path.resolve()
    return f"boardwright-3d-k{kicad}"


def resolve_image(image: Image) -> str:
    """Pull the pinned image if needed and return the local image ID.

    Fails unless Docker lists the pinned repository digest for that image, so
    the ID recorded as evidence is the image that actually runs.
    """

    def inspect() -> subprocess.CompletedProcess:
        return subprocess.run(
            ["docker", "image", "inspect", "--format", "{{.Id}} {{json .RepoDigests}}", image.reference],
            capture_output=True, text=True,
        )

    completed = inspect()
    if completed.returncode != 0:
        if subprocess.run(["docker", "pull", image.reference]).returncode != 0:
            raise ContainerError(f"Could not pull {image.reference}.")
        completed = inspect()
    if completed.returncode != 0:
        raise ContainerError(f"Could not inspect {image.reference}: {completed.stderr.strip()}")
    image_id, _, digests = completed.stdout.strip().partition(" ")
    pinned = f"{image.repository}@{image.digest}"
    if pinned not in json.loads(digests or "[]"):
        raise ContainerError(f"Local image {image_id} does not carry the pinned digest {pinned}.")
    return image_id


_SECRET_NAME = re.compile(r"TOKEN|SECRET|PASSWORD|CREDENTIAL", re.IGNORECASE)
_URL_USERINFO = re.compile(r"://[^/\s]+@")


def repository_credentials(workdir: Path) -> list[str]:
    """Credentials stored in the mounted repository's local Git config.

    ``actions/checkout`` persists the job token as an ``extraheader`` in
    ``.git/config`` unless ``persist-credentials: false``. The work tree is
    mounted into the container, so such a token would be readable by every
    build step. Returns the offending config keys (never their values).
    """

    completed = subprocess.run(["git", "config", "--local", "--list"], cwd=workdir, capture_output=True, text=True, encoding="utf-8", errors="surrogateescape")
    found = []
    for line in completed.stdout.splitlines():
        key, _, value = line.partition("=")
        if "extraheader" in key.lower() or _URL_USERINFO.search(value) or key.lower().startswith("credential."):
            found.append(key)
    return found


def run_in_container(kicad: str, workdir: Path, command: list[str], *, environment=None, interactive=False) -> int:
    if shutil.which("docker") is None:
        raise ContainerError("Docker is not installed or not on PATH (Docker Desktop on Windows).")
    stored = repository_credentials(workdir)
    if stored:
        raise ContainerError(
            "The repository to be mounted stores Git credentials (" + ", ".join(stored) + "). The build container "
            "must not see them: check out with persist-credentials: false and authenticate pushes per step."
        )
    secrets = sorted(name for name in (environment or {}) if _SECRET_NAME.search(name))
    if secrets:
        raise ContainerError("Refusing to pass secret-like variables into the build container: " + ", ".join(secrets))
    image = image_for(kicad)
    environment = {"BOARDWRIGHT_IMAGE_ID": resolve_image(image), **(environment or {})}
    argv = docker_command(image, workdir, command, cache_dir=default_cache_dir(kicad), environment=environment, interactive=interactive)
    print(f"[container] KiCad {kicad} image {image.reference} ({environment['BOARDWRIGHT_IMAGE_ID']})", flush=True)
    status = subprocess.run(argv).returncode
    foreign = foreign_owned(workdir)
    if foreign:
        shown = "\n  ".join(foreign[:10])
        print(f"error: the container left files not owned by this user ({len(foreign)}):\n  {shown}", file=sys.stderr)
        return status or 1
    return status


def foreign_owned(workdir: Path) -> list[str]:
    """Files in the work tree not owned by the calling user (POSIX hosts only).

    The container runs as the caller's uid:gid, so every output must belong to
    the caller; anything else would need the ``sudo chown`` the workflows no
    longer have. Docker Desktop on Windows maps ownership itself.
    """

    if not hasattr(os, "getuid"):
        return []
    uid = os.getuid()
    found = []
    for path in [workdir, *workdir.rglob("*")]:
        try:
            if path.lstat().st_uid != uid:
                found.append(path.relative_to(workdir).as_posix() or ".")
        except OSError:
            continue
    return found


# --- staging -----------------------------------------------------------------


def _git(root: Path, *args: str, check: bool = True) -> str:
    completed = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8", errors="surrogateescape")
    if check and completed.returncode != 0:
        raise ContainerError(f"git {' '.join(args)} failed: {completed.stderr.strip()}")
    return completed.stdout


def repository_root(start: Path) -> Path:
    return Path(_git(start, "rev-parse", "--show-toplevel").strip())


def default_stage_root() -> Path:
    return Path(os.environ.get("BOARDWRIGHT_STAGE_ROOT") or Path.home() / ".boardwright" / "stages")


def stage(
    repo: Path,
    *,
    worktree: bool = False,
    dest: Path | None = None,
    label: str = "",
    changes: tuple[list[str], list[str]] | None = None,
    purpose: str = "build",
    copy_from: Path | None = None,
    revision: str = "",
) -> Path:
    """Clone ``HEAD`` (plus, with ``worktree``, uncommitted changes) into a fresh LF tree.

    The clone has ``core.autocrlf=false`` and ``core.eol=lf``, so text files have
    the same bytes as a Linux CI checkout. ``.boardwright/local.yaml`` and every
    other ignored file stay behind. With ``worktree`` the uncommitted changes
    are committed inside the stage (normalised by Git) and the stage records
    that the build is not of a pushed commit.

    ``changes`` replaces the default change set (every modified and
    untracked, non-ignored file) with an explicit ``(copy, delete)`` list, as
    chosen by the local-preview source policy. ``purpose`` is written to the
    stage marker and from there into the build record; ``"preview"`` marks
    the build as never release evidence. ``copy_from`` reads the changed
    files from a snapshot directory instead of the working tree, so the
    staged bytes are exactly the ones the caller hashed. ``revision`` stages
    that commit (detached) instead of ``HEAD``, as a tag build does.
    """

    repo = repository_root(repo)
    if revision and worktree:
        raise ContainerError("A worktree stage is always of HEAD.")
    head = _git(repo, "rev-parse", "--verify", f"{revision or 'HEAD'}^{{commit}}").strip()
    branch = "detached" if revision else (
        _git(repo, "symbolic-ref", "--quiet", "--short", "HEAD", check=False).strip() or "detached")
    name = label or f"{repo.name}-{head[:12]}{'-worktree' if worktree else ''}"
    target = (dest or default_stage_root() / name).resolve()
    if target.exists():
        if not (target / ".git" / STAGE_MARKER).is_file():
            raise ContainerError(f"{target} exists and is not a Boardwright stage; refusing to replace it.")
        _remove_tree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", "--quiet", "--no-hardlinks", "--no-checkout",
         "--config", "core.autocrlf=false", "--config", "core.eol=lf", str(repo), str(target)],
        check=True,
    )
    checkout = ["checkout", "--quiet", "-B", branch, head] if branch != "detached" else ["checkout", "--quiet", "--detach", head]
    _git(target, *checkout)
    if worktree:
        if changes is None:
            # Audit C1: the change set must come from the source policy
            # (stage_worktree), never from "every untracked file".
            raise ContainerError("A worktree stage needs an explicit change set; use stage_worktree().")
        changed, deleted = changes
        for relative in changed:
            source = (copy_from or repo) / relative
            if source.is_file():
                (target / relative).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target / relative)
        for relative in deleted:
            (target / relative).unlink(missing_ok=True)
        if changed or deleted:
            _git(target, "add", "--all")
            _git(target, "-c", "user.name=Boardwright stage", "-c", "user.email=stage@boardwright.invalid",
                 "commit", "--quiet", "--no-verify", "-m", f"Uncommitted working tree of {head[:12]} (local build only)")
            # Re-checkout so every file is written from the normalised index (LF).
            _git(target, "rm", "--cached", "-r", "--quiet", ".")
            _git(target, "reset", "--hard", "--quiet")
    # Read by the build and copied into its record: a worktree build is never
    # evidence for a commit. Also identifies the directory as a stage.
    marker = {"source": str(repo), "head": head, "worktree": worktree, "purpose": purpose}
    (target / ".git" / STAGE_MARKER).write_text(json.dumps(marker) + "\n", encoding="utf-8")
    return target


SOURCE_RECORD = "boardwright-source.json"  # in a worktree stage's .git: paths and hashes only


@dataclass(frozen=True)
class StagedWorktree:
    plan: object          # source_policy.SourcePlan
    fingerprint: str      # working-tree fingerprint the snapshot was verified against
    prepared: object      # the caller's prepare() result, if any


def stage_worktree(repo: Path, workdir: Path, *, purpose: str, prepare=None) -> StagedWorktree:
    """Stage the working tree under the shared source policy (previews and worktree builds).

    The caller holds ``stage_lock(workdir)``. In order: fail-closed
    preconditions; read every included file once into an owned snapshot,
    hashing those bytes, between two working-tree fingerprints (refused if the
    tree changed); stage from the snapshot into an owned temporary; check HEAD
    did not move; run ``prepare(new_stage)``; swap into place. Any failure
    before the swap leaves the previous stage untouched. Records only paths
    and hashes (``.git/boardwright-source.json``).
    """

    from . import source_policy, staging

    source_policy.check_preconditions(repo)
    staging.remove_abandoned_temporaries(workdir)
    snapshot = staging.owned_temporary(workdir, "snapshot")
    temporary = staging.owned_temporary(workdir, "staging")
    try:
        before = source_policy.worktree_fingerprint(repo)
        plan = source_policy.plan_source(repo, snapshot=snapshot)
        if source_policy.worktree_fingerprint(repo) != before:
            raise BoardwrightError("The working tree changed while it was being read (a save in KiCad?). "
                                   "Nothing was built and the previous stage is unchanged; run again.")
        new_stage = temporary / "s"
        try:
            stage(repo, worktree=plan.dirty, dest=new_stage, purpose=purpose,
                  changes=([c.path for c in plan.included], list(plan.deleted)), copy_from=snapshot)
        except (ContainerError, subprocess.CalledProcessError, OSError) as exc:
            raise BoardwrightError(f"Staging failed: {exc}. Nothing was built and the previous stage is unchanged.") from exc
        marker = json.loads((new_stage / ".git" / STAGE_MARKER).read_text(encoding="utf-8"))
        if marker.get("head") != plan.base_commit:
            raise BoardwrightError(f"HEAD moved while staging ({plan.base_commit[:12]} -> {str(marker.get('head'))[:12]}); "
                                   "nothing was built and the previous stage is unchanged; run again.")
        record = {"purpose": purpose, "release_evidence": False, **source_policy.plan_summary(plan)}
        (new_stage / ".git" / SOURCE_RECORD).write_text(json.dumps(record, indent=2, sort_keys=True) + "\n",
                                                        encoding="utf-8", newline="\n")
        prepared = prepare(new_stage) if prepare else None
        staging.swap_into_place(new_stage, workdir)
    finally:
        staging.remove_owned(snapshot, workdir)
        staging.remove_owned(temporary, workdir)
    return StagedWorktree(plan, before, prepared)


REMOTE_REFS_FILE = "boardwright-remote-refs.json"
# Host-written beside record.json by a local preview (local_preview.py).
PREVIEW_EVIDENCE_MARKER = "preview.json"


def pin_release_refs(repo: Path, target: Path) -> Path:
    """Give a stage the release references of the source repository's upstream.

    Runs on the host, where the developer's Git credentials and paths work. A
    stage is cloned from the local repository, so its ``origin`` would be a
    host path (``C:\\...`` on Windows) that the Linux container cannot open.
    Instead, on the host:

    - the stage's ``origin`` is set to the source repository's own ``origin``
      URL (refused if that URL embeds credentials);
    - the upstream's tags are fetched into the stage without ``--force``, so a
      local tag that differs from the upstream one fails the rehearsal;
    - ``git ls-remote --tags`` of the upstream is written to
      ``.git/boardwright-remote-refs.json``. Inside the container, the release
      plan reads that snapshot for the remote-tag check
      (``BOARDWRIGHT_REMOTE_REFS``) instead of contacting the remote.
    """

    url = _git(repo, "remote", "get-url", "origin", check=False).strip()
    if not url:
        raise ContainerError(
            f"{repo} has no `origin` remote; a release candidate needs the upstream's tags to check the version is free."
        )
    if _URL_USERINFO.search(url):
        raise ContainerError("The `origin` URL embeds credentials; refusing to copy it into the stage.")
    listing = subprocess.run(["git", "ls-remote", "--tags", url], cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="surrogateescape", timeout=120)
    if listing.returncode != 0:
        raise ContainerError(f"Cannot list the upstream's tags ({url}): {listing.stderr.strip()}")
    tags = {}
    for line in listing.stdout.splitlines():
        sha, _, ref = line.partition("\t")
        if ref.startswith("refs/tags/") and not ref.endswith("^{}"):
            tags[ref[len("refs/tags/"):]] = sha
    fetched = subprocess.run(
        ["git", "fetch", "--no-tags", url, "refs/tags/*:refs/tags/*"],
        cwd=target, capture_output=True, text=True, encoding="utf-8", errors="surrogateescape", timeout=600,
    )
    if fetched.returncode != 0:
        detail = [line.strip() for line in fetched.stderr.splitlines() if "rejected" in line or line.startswith(("error", "fatal"))]
        raise ContainerError(
            "Cannot fetch the upstream's tags into the stage; a local tag differs from the upstream or the "
            "upstream is unreachable: " + ("; ".join(detail) or fetched.stderr.strip())
        )
    _git(target, "remote", "set-url", "origin", url)
    snapshot = {"schema": 1, "url": url, "tags": dict(sorted(tags.items()))}
    path = target / ".git" / REMOTE_REFS_FILE
    path.write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")
    return path


def host_tree_fingerprint(repo: Path) -> str:
    """``git status`` of the developer's tree; compared before and after a local build."""

    # --no-optional-locks: never refresh or lock the developer's index (read-only)
    return _git(repo, "--no-optional-locks", "status", "--porcelain=v1", "--untracked-files=all", "--ignored=no")


# --- evidence comparison -------------------------------------------------------


def compare_builds(local: Path, remote: Path) -> tuple[list[str], list[str]]:
    """Compare two build-evidence directories (``record.json`` + ``manifest.json``).

    Returns (problems, notes). A problem is any identity mismatch (source,
    variant, image, normaliser) or a blocking controlled-output difference.
    Normalisation is exactly ``output_manifest``'s: only timestamps, generator
    banners and the ``TF.ProjectId`` revision field of controlled files.
    """

    from .output_manifest import blocking, compare_manifests, load_manifest

    def load(directory: Path) -> tuple[dict, dict]:
        base = directory / BUILD_DIR if (directory / BUILD_DIR / "record.json").is_file() else directory
        # Three independent preview markers, any of which refuses the side:
        # the host-written evidence marker (before Docker starts), the
        # host-written stage marker in the stage's .git (written when the
        # stage is created), and the record's stage purpose (newer payloads).
        stage_root = base.parent if base.name == Path(BUILD_DIR).name else base
        stage_marker = stage_root / ".git" / STAGE_MARKER
        try:
            stage_purpose = json.loads(stage_marker.read_text(encoding="utf-8")).get("purpose") if stage_marker.is_file() else None
        except (OSError, ValueError):
            stage_purpose = "unreadable"
        if (base / PREVIEW_EVIDENCE_MARKER).is_file() or stage_purpose in ("preview", "unreadable"):
            preview_sides.append(str(directory))
        try:
            record = json.loads((base / "record.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ContainerError(f"No build record in {directory}: {exc}") from exc
        try:
            manifest = load_manifest(base / "manifest.json")
        except ValueError as exc:
            raise ContainerError(str(exc)) from exc
        return record, manifest

    preview_sides: list[str] = []
    (a_record, a_manifest), (b_record, b_manifest) = load(local), load(remote)
    problems: list[str] = [f"{side} is local preview evidence (never release evidence)" for side in preview_sides]
    notes: list[str] = []
    for key, label in (("source_commit", "source commit"), ("variant", "variant"), ("image", "container image"), ("normaliser", "manifest normaliser")):
        if a_record.get(key) != b_record.get(key):
            problems.append(f"{label} differs: {a_record.get(key)!r} vs {b_record.get(key)!r}")
    for record, side in ((a_record, "first"), (b_record, "second")):
        if (record.get("stage") or {}).get("worktree"):
            problems.append(f"{side} build was staged from an uncommitted working tree")
        if (record.get("stage") or {}).get("purpose") == "preview":
            problems.append(f"{side} build is a local preview (never release evidence)")
        if record.get("source_modified"):
            unexplained = _unprepared_modifications(record)
            if unexplained:
                problems.append(f"{side} build was made from uncommitted changes: {', '.join(unexplained)}")
        if not record.get("image") or not record.get("image_id"):
            problems.append(f"{side} build has no verified container image identity (not built with boardwright container)")
    if a_record.get("source_modified") or b_record.get("source_modified"):
        # Reached only when both sides' modifications are fully covered by a
        # verified release-preparation record; then the preparations must be
        # the same: same commit, version, date and file contents.
        a_prep, b_prep = a_record.get("release_preparation") or {}, b_record.get("release_preparation") or {}
        if not _unprepared_modifications(a_record) and not _unprepared_modifications(b_record):
            mismatched = []
            if sorted(a_record.get("source_modified") or []) != sorted(b_record.get("source_modified") or []):
                mismatched.append("modified files")
            mismatched += [key.replace("_", " ") for key in ("source_commit", "version", "release_date", "files", "project_files") if a_prep.get(key) != b_prep.get(key)]
            if mismatched:
                problems.append("release preparation differs between the builds: " + ", ".join(mismatched))
            else:
                notes.append(
                    f"Both builds include the same verified release preparation ({a_prep.get('version')}, "
                    f"{a_prep.get('release_date')}; {len(a_prep.get('files') or {})} file(s) with identical content)."
                )
    differences = compare_manifests(a_manifest, b_manifest)
    hard = blocking(differences)
    for item in differences:
        (problems if item in hard else notes).append(str(item))
    informational = sorted(
        path for path in set(a_manifest["files"]) | set(b_manifest["files"])
        if a_manifest["files"].get(path, {}).get("sha256") != b_manifest["files"].get(path, {}).get("sha256")
        and path not in {item.path for item in differences}
    )
    if informational:
        notes.append(f"{len(informational)} informational file(s) differ in bytes (PDFs, renders, reports, ODB++); not compared.")
    return problems, notes


def _unprepared_modifications(record: dict) -> list[str]:
    """``source_modified`` entries not covered by a verified release-preparation record."""

    modified = list(record.get("source_modified") or [])
    preparation = record.get("release_preparation")
    if not isinstance(preparation, dict) or preparation.get("problems"):
        return modified
    files = preparation.get("files") if isinstance(preparation.get("files"), dict) else {}
    covered = set(files) | set(preparation.get("project_files") or [])
    return [path for path in modified if path not in covered]


# --- CLI -----------------------------------------------------------------------


def _check_image(kicad: str) -> int:
    image = image_for(kicad)
    print(image.reference)
    if subprocess.run(["docker", "pull", "--quiet", image.reference]).returncode != 0:
        return 1
    probe = ["sh", "-c", "kicad-cli --version; kibot --version; python3 --version; git --version"]
    completed = subprocess.run(["docker", "run", "--rm", "--entrypoint", probe[0], image.reference, *probe[1:]], capture_output=True, text=True)
    print(completed.stdout.strip())
    return completed.returncode


def _candidate(args: argparse.Namespace) -> int:
    """Local release-candidate rehearsal: the same commands prepare-release.yaml runs, in a stage.

    Nothing is committed, pushed, tagged or published: the stage is a
    throw-away clone. Its release references come from the upstream of the
    current repository, pinned on the host (``pin_release_refs``); CI
    (``--workdir``) checks GitHub directly.
    """

    extra: dict[str, str] = {}
    if args.workdir:
        workdir = Path(args.workdir)
    else:
        repo = repository_root(Path.cwd())
        workdir = stage(repo, label=f"{repo.name}-candidate-{args.version}")
        snapshot = pin_release_refs(repo, workdir)
        print(f"[candidate] release references pinned from {json.loads(snapshot.read_text(encoding='utf-8'))['url']}")
        extra["BOARDWRIGHT_REMOTE_REFS"] = f".git/{REMOTE_REFS_FILE}"
    env = {
        "IN_VERSION": args.version,
        "IN_VARIANT": args.variant,
        "IN_RELEASE_KIND": args.kind,
        "IN_DRAWING_REVISION": args.drawing_revision,
        "IN_REVISION_DESCRIPTION": args.revision_description,
        "IN_CHANGE_CLASSES": args.change_class,
        "IN_JUSTIFICATION": args.justification,
        "BOARDWRIGHT_SOURCE_SHA": _git(workdir, "rev-parse", "HEAD").strip(),
        **extra,
    }
    failed = run_candidate_steps(workdir, args, env, skip_baseline=args.skip_baseline)
    if failed:
        print(f"error: candidate step failed: {failed}", file=sys.stderr)
        return 1
    print(f"Release candidate {args.version} built in {workdir}")
    print("Nothing was committed, pushed or tagged. Approval, tagging and publication stay in prepare-release.yaml.")
    return 0


def run_candidate_steps(workdir: Path, args: argparse.Namespace, env: dict[str, str], *, skip_baseline: bool = False,
                        local: bool = False, runner=None, host_runner=None) -> str:
    """Run ``candidate_steps`` in order; return the failed step's name, or "".

    Shared by the rehearsal (``container candidate``) and the local release
    backend (``local_release``), so both run exactly prepare-release's steps.
    ``check-baseline`` runs on the host; with ``local`` it never falls back to
    a published GitHub package (``--local``: fail closed).
    """

    runner = runner or run_in_container
    host_runner = host_runner or (lambda command: subprocess.run(
        command, cwd=workdir, env={**os.environ, "PYTHONPATH": str(workdir / "src")}).returncode)
    for step in candidate_steps(args):
        name, command = step[0], step[1:]
        print(f"[candidate] {name}", flush=True)
        if name == "check-baseline":
            if skip_baseline:
                print("[candidate] check-baseline skipped (--skip-baseline); the CI run always performs it.")
                continue
            status = host_runner([sys.executable, *command[1:], *(["--local"] if local else [])])
        else:
            status = runner(args.kicad, workdir, command, environment=env)
        if status != 0:
            return name
    return ""


def candidate_steps(args: argparse.Namespace) -> list[list[str]]:
    """``[name, *argv]`` in prepare-release order. ``check-baseline`` runs on the host."""

    return [
        ["release-prepare", "python3", "-m", "boardwright", "release", args.version, "--prepare", "--allow-dirty",
         "--release-env", "--variant", args.variant, "--kind", args.kind, "--drawing-revision", args.drawing_revision,
         "--revision-description", args.revision_description, "--change-class", args.change_class,
         "--justification", args.justification,
         *(["--release-date", args.release_date] if args.release_date else [])],
        ["build", "python3", "-m", "boardwright", "build", "--variant", args.variant, "--kicad", args.kicad,
         "--log-prefix", "prepare", "--keep", "CHANGELOG.md", ".boardwright"],
        ["manifest", "python3", "-m", "boardwright.output_manifest", "write"],
        ["check-baseline", "python3", "-m", "boardwright.output_manifest", "check-baseline"],
        ["review-candidate", "python3", "-m", "boardwright.review_gate", "write"],
    ]


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(line_buffering=True)
    parser = argparse.ArgumentParser(prog="python -m boardwright container", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    def kicad_option(p: argparse.ArgumentParser) -> None:
        p.add_argument("--kicad", choices=SUPPORTED_KICAD, default=DEFAULT_KICAD, help="KiCad major version (default 9).")

    p = sub.add_parser("image", help="Print the pinned image; --check pulls it and prints tool versions.")
    kicad_option(p)
    p.add_argument("--check", action="store_true")

    p = sub.add_parser("stage", help="Create a clean LF clone of HEAD to build in; prints its path.")
    p.add_argument("--worktree", action="store_true", help="Include uncommitted (non-ignored) changes.")
    p.add_argument("--dest", type=Path)

    p = sub.add_parser("run", help="Run a command in the container (after --).")
    kicad_option(p)
    p.add_argument("--workdir", type=Path, default=Path("."))
    p.add_argument("cmd", nargs=argparse.REMAINDER)

    p = sub.add_parser("build", help="Stage (unless --workdir) and run the output build in the container.")
    kicad_option(p)
    p.add_argument("--variant", required=True, choices=VARIANTS)
    p.add_argument("--worktree", action="store_true", help="Build uncommitted changes (marked as such in the record).")
    p.add_argument("--workdir", type=Path, help="Build in this disposable checkout instead of a new stage (CI).")
    p.add_argument("--log-prefix", default="build")
    p.add_argument("--keep", nargs="*", default=[])

    p = sub.add_parser("candidate", help="Rehearse a release candidate locally (never commits, pushes or tags).")
    kicad_option(p)
    p.add_argument("version")
    p.add_argument("--variant", default="RELEASED", choices=VARIANTS)
    p.add_argument("--kind", default="prerelease", choices=("draft", "prerelease", "release"))
    p.add_argument("--drawing-revision", required=True)
    p.add_argument("--revision-description", required=True)
    p.add_argument("--change-class", default="")
    p.add_argument("--justification", default="")
    p.add_argument("--workdir", type=Path)
    p.add_argument("--release-date", default="", metavar="YYYY-MM-DD",
                   help="Release date (default: today's UTC date). An approved run passes the reviewed candidate's date.")
    p.add_argument("--skip-baseline", action="store_true", help="Skip check-baseline (needs gh for legacy releases).")

    p = sub.add_parser("compare", help="Compare two build-evidence directories (local vs GitHub).")
    p.add_argument("first", type=Path)
    p.add_argument("second", type=Path)

    p = sub.add_parser("shell", help="Interactive shell in the container.")
    kicad_option(p)
    p.add_argument("--workdir", type=Path, default=Path("."))

    args = parser.parse_args(argv)
    try:
        if args.command == "image":
            if args.check:
                return _check_image(args.kicad)
            print(image_for(args.kicad).reference)
            return 0
        if args.command == "stage":
            if not args.worktree:
                print(stage(Path.cwd(), dest=args.dest))
                return 0
            repo = repository_root(Path.cwd())
            target = (args.dest or default_stage_root() / f"{repo.name}-worktree").resolve()
            with stage_lock(target, purpose="container stage --worktree"):
                staged = stage_worktree(repo, target, purpose="worktree-build")
            _print_source(staged.plan)
            print(target)
            return 0
        if args.command == "run":
            cmd = args.cmd[1:] if args.cmd[:1] == ["--"] else args.cmd
            if not cmd:
                parser.error("run needs a command after --")
            return run_in_container(args.kicad, args.workdir, cmd)
        if args.command == "shell":
            return run_in_container(args.kicad, args.workdir, ["bash"], interactive=True)
        if args.command == "build":
            return _build(args)
        if args.command == "candidate":
            return _candidate(args)
        if args.command == "compare":
            problems, notes = compare_builds(args.first, args.second)
            for line in notes:
                print(f"note  {line}")
            for line in problems:
                print(f"ERROR {line}")
            print("Builds match." if not problems else f"{len(problems)} problem(s).")
            return 1 if problems else 0
    except (ContainerError, BoardwrightError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 2


def build_command(variant: str, kicad: str, log_prefix: str, keep: list[str] | tuple[str, ...] = ()) -> list[str]:
    """The output build inside the container; shared by ``container build`` and local publication."""

    command = ["python3", "-m", "boardwright", "build", "--variant", variant, "--kicad", kicad,
               "--log-prefix", log_prefix]
    return command + (["--keep", *keep] if keep else [])


def _build(args: argparse.Namespace) -> int:
    command = build_command(args.variant, args.kicad, args.log_prefix, args.keep)
    if args.workdir:
        return run_in_container(args.kicad, args.workdir, command)
    repo = repository_root(Path.cwd())
    if not args.worktree:
        before = host_tree_fingerprint(repo)
        workdir = stage(repo, label=f"{repo.name}-{args.variant.lower()}-k{args.kicad}")
        print(f"[container] staged {workdir}")
        status = run_in_container(args.kicad, workdir, command)
        if host_tree_fingerprint(repo) != before:
            print("error: the developer working tree changed during the build.", file=sys.stderr)
            return 1
    else:
        # A non-release worktree build: the shared source policy, a verified
        # snapshot and the stage lock, as for previews (audit C1). Marked
        # worktree=True and purpose "worktree-build"; container compare and
        # the review gate refuse it.
        from . import source_policy

        workdir = (default_stage_root() / f"{repo.name}-{args.variant.lower()}-k{args.kicad}-worktree").resolve()
        with stage_lock(workdir, purpose=f"container build --worktree {args.variant}"):
            staged = stage_worktree(repo, workdir, purpose="worktree-build")
            _print_source(staged.plan)
            print(f"[container] staged {workdir} (non-release worktree build)")
            status = run_in_container(args.kicad, workdir, command)
            if source_policy.worktree_fingerprint(repo) != staged.fingerprint:
                print("error: the developer working tree changed during the build; the outputs show the staged "
                      "snapshot.", file=sys.stderr)
                return 1
    if status == 0:
        print(f"Outputs: {workdir}")
        print(f"Evidence: {workdir / BUILD_DIR}")
    return status


def _print_source(plan) -> None:
    """Paths and hashes only; file contents are never printed."""

    print(f"[container] worktree source {plan.source_digest[:16]} from {plan.base_commit[:12]}"
          f" ({len(plan.included)} changed, {len(plan.deleted)} deleted)")
    for change in plan.included:
        print(f"[container]   include {change.kind}: {change.path}")
    for path in plan.deleted:
        print(f"[container]   delete: {path}")
    for path, reason in plan.excluded:
        print(f"[container]   exclude: {path} ({reason})")


if __name__ == "__main__":
    raise SystemExit(main())
