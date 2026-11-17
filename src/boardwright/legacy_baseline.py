# SPDX-License-Identifier: LicenseRef-Boardwright-Source-Available
# Copyright (c) Ryan Hicks

"""Authenticated baseline from a published legacy release package (audit F19).

Releases made before the output manifest existed have no committed
``accepted_manifest.json``. For those, the approved manufacturing data is the
package the release workflow published. This module downloads that package
and accepts it only if its provenance checks out; any failure raises
:class:`LegacyBaselineError` so the caller fails closed.

Provenance requirements:

* the GitHub release for the tag exists, is published (not a draft) and was
  created by the release workflow (``github-actions[bot]``);
* exactly one ``boardwright-<tag>-release.zip`` asset, fully uploaded by the
  release workflow, with a server-side SHA-256 digest;
* the downloaded bytes match that digest;
* the remote tag resolves to the same commit as the local tag, and that commit
  is an ancestor of the commit being released;
* the archive is a well-formed package (single ``boardwright-release/`` root,
  no unsafe paths) containing ``Manufacturing/Fabrication``.
"""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable

from .errors import BoardwrightError

RELEASE_WORKFLOW_ACTOR = "github-actions[bot]"
PACKAGE_ROOT = "boardwright-release"


class LegacyBaselineError(BoardwrightError):
    """The published package cannot be obtained or authenticated."""


@dataclass(frozen=True)
class Provenance:
    repository: str
    tag: str
    tag_commit: str
    asset: str
    digest: str
    size: int

    def lines(self) -> list[str]:
        return [
            f"Legacy baseline: published package of {self.repository}@{self.tag}",
            f"- tag commit: {self.tag_commit} (remote and local agree; ancestor of HEAD)",
            f"- asset: {self.asset} ({self.size} bytes), published by {RELEASE_WORKFLOW_ACTOR}",
            f"- sha256 verified against GitHub digest: {self.digest}",
        ]


Runner = Callable[[list[str], Path], bytes]


def _run(command: list[str], cwd: Path) -> bytes:
    completed = subprocess.run(command, cwd=cwd, capture_output=True, check=False)
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip() or f"exit {completed.returncode}"
        raise LegacyBaselineError(f"`{' '.join(command[:3])} …` failed: {detail}")
    return completed.stdout


def fetch_published_package(
    root: Path,
    repository: str,
    tag: str,
    destination: Path,
    run: Runner = _run,
) -> tuple[Path, Provenance]:
    """Download, authenticate and extract the release package; return its output root."""
    if not repository or "/" not in repository:
        raise LegacyBaselineError("Repository (owner/name) is unknown; set GITHUB_REPOSITORY or pass --repo.")
    asset_name = f"boardwright-{tag}-release.zip"
    try:
        release = json.loads(run(["gh", "api", f"repos/{repository}/releases/tags/{tag}"], root))
    except json.JSONDecodeError as error:
        raise LegacyBaselineError(f"GitHub returned an unreadable release record for {tag}.") from error

    problems: list[str] = []
    if release.get("draft"):
        problems.append(f"release {tag} is a draft, not a published release")
    author = (release.get("author") or {}).get("login")
    if author != RELEASE_WORKFLOW_ACTOR:
        problems.append(f"release {tag} was created by {author!r}, not the release workflow")
    assets = [asset for asset in release.get("assets") or [] if asset.get("name") == asset_name]
    if len(assets) != 1:
        problems.append(f"release {tag} has {len(assets)} asset(s) named {asset_name}; expected exactly one")
        raise LegacyBaselineError(_message(tag, problems))
    asset = assets[0]
    if asset.get("state") != "uploaded":
        problems.append(f"{asset_name} upload state is {asset.get('state')!r}")
    uploader = (asset.get("uploader") or {}).get("login")
    if uploader != RELEASE_WORKFLOW_ACTOR:
        problems.append(f"{asset_name} was uploaded by {uploader!r}, not the release workflow")
    digest = str(asset.get("digest") or "")
    if not digest.startswith("sha256:") or len(digest) != len("sha256:") + 64:
        problems.append(f"{asset_name} has no GitHub SHA-256 digest, so its content cannot be authenticated")

    tag_commit = _verified_tag_commit(root, repository, tag, run, problems)
    if problems:
        raise LegacyBaselineError(_message(tag, problems))

    destination.mkdir(parents=True, exist_ok=True)
    run(["gh", "release", "download", tag, "--repo", repository, "--pattern", asset_name,
         "--dir", str(destination), "--clobber"], root)
    archive_path = destination / asset_name
    if not archive_path.is_file():
        raise LegacyBaselineError(f"{asset_name} was not downloaded.")
    data = archive_path.read_bytes()
    actual = "sha256:" + hashlib.sha256(data).hexdigest()
    if actual != digest:
        raise LegacyBaselineError(_message(tag, [f"{asset_name} sha256 {actual} does not match GitHub digest {digest}"]))

    output_root = _extract(data, destination / "package", asset_name)
    return output_root, Provenance(repository, tag, tag_commit, asset_name, digest, len(data))


def _verified_tag_commit(root: Path, repository: str, tag: str, run: Runner, problems: list[str]) -> str:
    try:
        local = run(["git", "rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}"], root).decode().strip()
    except LegacyBaselineError:
        problems.append(f"tag {tag} is not available locally (fetch tags with full history)")
        return ""
    try:
        ref = json.loads(run(["gh", "api", f"repos/{repository}/git/ref/tags/{tag}"], root))
        obj = ref.get("object") or {}
        remote = obj.get("sha", "")
        if obj.get("type") == "tag":  # annotated tag: dereference to its commit
            remote = (json.loads(run(["gh", "api", f"repos/{repository}/git/tags/{remote}"], root)).get("object") or {}).get("sha", "")
    except (LegacyBaselineError, json.JSONDecodeError) as error:
        problems.append(f"remote tag {tag} cannot be resolved: {error}")
        return local
    if remote != local:
        problems.append(f"remote tag {tag} is {remote or 'missing'} but the local tag is {local}; the tag moved")
    try:
        run(["git", "merge-base", "--is-ancestor", local, "HEAD"], root)
    except LegacyBaselineError:
        problems.append(f"tag {tag} ({local}) is not an ancestor of HEAD")
    return local


def _extract(data: bytes, target: Path, name: str) -> Path:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as error:
        raise LegacyBaselineError(f"{name} is not a readable zip archive.") from error
    members = [info for info in archive.infolist() if not info.is_dir()]
    for info in members:
        parts = PurePosixPath(info.filename).parts
        if info.filename.startswith(("/", "\\")) or ".." in parts or ":" in info.filename or not parts:
            raise LegacyBaselineError(f"{name} contains an unsafe path: {info.filename!r}")
        if parts[0] != PACKAGE_ROOT:
            raise LegacyBaselineError(f"{name} member {info.filename!r} is outside {PACKAGE_ROOT}/")
    if not any(info.filename.startswith(f"{PACKAGE_ROOT}/Manufacturing/Fabrication/") for info in members):
        raise LegacyBaselineError(f"{name} contains no {PACKAGE_ROOT}/Manufacturing/Fabrication outputs.")
    archive.extractall(target)
    return target / PACKAGE_ROOT


def _message(tag: str, problems: list[str]) -> str:
    return (
        f"Cannot authenticate the published package of legacy release {tag}:\n- "
        + "\n- ".join(problems)
    )
