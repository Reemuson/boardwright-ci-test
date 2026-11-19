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
STAGE_MARKER = "boardwright-stage"


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

    completed = subprocess.run(["git", "config", "--local", "--list"], cwd=workdir, capture_output=True, text=True)
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
    completed = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)
    if check and completed.returncode != 0:
        raise ContainerError(f"git {' '.join(args)} failed: {completed.stderr.strip()}")
    return completed.stdout


def repository_root(start: Path) -> Path:
    return Path(_git(start, "rev-parse", "--show-toplevel").strip())


def default_stage_root() -> Path:
    return Path(os.environ.get("BOARDWRIGHT_STAGE_ROOT") or Path.home() / ".boardwright" / "stages")


def _remove_tree(path: Path) -> None:
    def make_writable(func, target, _exc):
        os.chmod(target, stat.S_IWRITE)
        func(target)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=make_writable)
    else:  # pragma: no cover
        shutil.rmtree(path, onerror=make_writable)


def worktree_changes(repo: Path) -> tuple[list[str], list[str]]:
    """(changed or new paths to copy, deleted paths) relative to HEAD. Ignored files are excluded."""

    changed = [p for p in _git(repo, "ls-files", "-z", "--modified", "--others", "--exclude-standard").split("\0") if p]
    deleted = [p for p in _git(repo, "ls-files", "-z", "--deleted").split("\0") if p]
    return sorted(set(changed) - set(deleted)), sorted(set(deleted))


def stage(repo: Path, *, worktree: bool = False, dest: Path | None = None, label: str = "") -> Path:
    """Clone ``HEAD`` (plus, with ``worktree``, uncommitted changes) into a fresh LF tree.

    The clone has ``core.autocrlf=false`` and ``core.eol=lf``, so text files have
    the same bytes as a Linux CI checkout. ``.boardwright/local.yaml`` and every
    other ignored file stay behind. With ``worktree`` the uncommitted changes
    are committed inside the stage (normalised by Git) and the stage records
    that the build is not of a pushed commit.
    """

    repo = repository_root(repo)
    head = _git(repo, "rev-parse", "HEAD").strip()
    branch = _git(repo, "symbolic-ref", "--quiet", "--short", "HEAD", check=False).strip() or "detached"
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
        changed, deleted = worktree_changes(repo)
        for relative in changed:
            source = repo / relative
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
    marker = {"source": str(repo), "head": head, "worktree": worktree}
    (target / ".git" / STAGE_MARKER).write_text(json.dumps(marker) + "\n", encoding="utf-8")
    return target


def host_tree_fingerprint(repo: Path) -> str:
    """``git status`` of the developer's tree; compared before and after a local build."""

    return _git(repo, "status", "--porcelain=v1", "--untracked-files=all", "--ignored=no")


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
        try:
            record = json.loads((base / "record.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ContainerError(f"No build record in {directory}: {exc}") from exc
        try:
            manifest = load_manifest(base / "manifest.json")
        except ValueError as exc:
            raise ContainerError(str(exc)) from exc
        return record, manifest

    (a_record, a_manifest), (b_record, b_manifest) = load(local), load(remote)
    problems: list[str] = []
    notes: list[str] = []
    for key, label in (("source_commit", "source commit"), ("variant", "variant"), ("image", "container image"), ("normaliser", "manifest normaliser")):
        if a_record.get(key) != b_record.get(key):
            problems.append(f"{label} differs: {a_record.get(key)!r} vs {b_record.get(key)!r}")
    for record, side in ((a_record, "first"), (b_record, "second")):
        if (record.get("stage") or {}).get("worktree"):
            problems.append(f"{side} build was staged from an uncommitted working tree")
        if record.get("source_modified"):
            problems.append(f"{side} build was made from uncommitted changes: {', '.join(record['source_modified'])}")
        if not record.get("image") or not record.get("image_id"):
            problems.append(f"{side} build has no verified container image identity (not built with boardwright container)")
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
    throw-away clone. The remote-tag check queries the local repository the
    stage was cloned from, so fetch tags first.
    """

    repo = repository_root(Path.cwd())
    workdir = Path(args.workdir) if args.workdir else stage(repo, label=f"{repo.name}-candidate-{args.version}")
    env = {
        "IN_VERSION": args.version,
        "IN_VARIANT": args.variant,
        "IN_RELEASE_KIND": args.kind,
        "IN_DRAWING_REVISION": args.drawing_revision,
        "IN_REVISION_DESCRIPTION": args.revision_description,
        "IN_CHANGE_CLASSES": args.change_class,
        "IN_JUSTIFICATION": args.justification,
        "BOARDWRIGHT_SOURCE_SHA": _git(workdir, "rev-parse", "HEAD").strip(),
    }
    for step in candidate_steps(args):
        name, command = step[0], step[1:]
        print(f"[candidate] {name}", flush=True)
        if name == "check-baseline":
            if args.skip_baseline:
                print("[candidate] check-baseline skipped (--skip-baseline); the CI run always performs it.")
                continue
            # On the host: legacy baselines are fetched with the GitHub CLI.
            status = subprocess.run(
                [sys.executable, *command[1:]], cwd=workdir, env={**os.environ, "PYTHONPATH": str(workdir / "src")}
            ).returncode
        else:
            status = run_in_container(args.kicad, workdir, command, environment=env)
        if status != 0:
            print(f"error: candidate step failed: {name}", file=sys.stderr)
            return 1
    print(f"Release candidate {args.version} built in {workdir}")
    print("Nothing was committed, pushed or tagged. Approval, tagging and publication stay in prepare-release.yaml.")
    return 0


def candidate_steps(args: argparse.Namespace) -> list[list[str]]:
    """``[name, *argv]`` in prepare-release order. ``check-baseline`` runs on the host."""

    return [
        ["release-prepare", "python3", "-m", "boardwright", "release", args.version, "--prepare", "--allow-dirty",
         "--release-env", "--variant", args.variant, "--kind", args.kind, "--drawing-revision", args.drawing_revision,
         "--revision-description", args.revision_description, "--change-class", args.change_class,
         "--justification", args.justification],
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
            print(stage(Path.cwd(), worktree=args.worktree, dest=args.dest))
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
    except ContainerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 2


def _build(args: argparse.Namespace) -> int:
    command = ["python3", "-m", "boardwright", "build", "--variant", args.variant, "--kicad", args.kicad,
               "--log-prefix", args.log_prefix]
    if args.keep:
        command += ["--keep", *args.keep]
    if args.workdir:
        return run_in_container(args.kicad, args.workdir, command)
    repo = repository_root(Path.cwd())
    before = host_tree_fingerprint(repo)
    workdir = stage(repo, worktree=args.worktree, label=f"{repo.name}-{args.variant.lower()}-k{args.kicad}{'-worktree' if args.worktree else ''}")
    print(f"[container] staged {workdir}")
    status = run_in_container(args.kicad, workdir, command)
    if host_tree_fingerprint(repo) != before:
        print("error: the developer working tree changed during the build.", file=sys.stderr)
        return 1
    if status == 0:
        print(f"Outputs: {workdir}")
        print(f"Evidence: {workdir / BUILD_DIR}")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
