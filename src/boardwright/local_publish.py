# SPDX-License-Identifier: LicenseRef-Boardwright-Source-Available
# Copyright (c) Ryan Hicks

"""Publish a finalised local release into the release store.

The local counterpart of ``release.yaml`` (``docs/LOCAL_FIRST.md`` section
5): a fresh stage of the approved annotated tag, the same output build
(``container.build_command``), the same verification against the tagged
accepted manifest (``output_manifest``), and the same package
(``release_package``: notes, assets, ``RELEASE_IDENTITY.json``,
``ACCEPTED_OUTPUT_MANIFEST.json``, ``MANIFEST.txt``, zip).

Before anything is stored, the package is checked against the tag: its
identity is the tagged ledger entry, its accepted manifest and approval are
the tagged files, and its ``MANIFEST.txt`` matches its contents. The result
is frozen once as ``releases/<version>/`` (``SHA256SUMS``, read-only) with a
``publish.json`` record and an index event; an existing release is never
replaced. The package bytes are the original ones; a later rebuild is not
claimed to give a byte-identical zip.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from . import container
from .local_finalize import checkpoint, finalize_state, load_finalization
from .local_release import LocalReleaseError, ReleaseRequest, verify_approval
from .release_store import ReleaseStore, safe_name, sha256_bytes, sha256_file
from .stage_lock import stage_lock

PUBLISH_FILE = "publish.json"
STAGE_PURPOSE = "local-release-publish"
RELEASE_DIR = "boardwright-release"


def _git_bytes(root: Path, *args: str) -> bytes:
    done = subprocess.run(["git", *args], cwd=root, capture_output=True)
    if done.returncode != 0:
        raise LocalReleaseError(f"git {' '.join(args[:2])} failed: {done.stderr.decode('utf-8', 'replace').strip()}")
    return done.stdout


def _release_env(text: str) -> dict[str, str]:
    values = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values[key.strip()] = value.strip()
    return values


def local_notes_section(approval: dict, finalization: dict) -> str:
    if finalization["signed"]:
        signing = ("signed; the signatures are valid and were made by the key that the developer's allowed-signers "
                   "file lists for the approver alone. That mapping is local configuration, **not authenticated** "
                   "by any independent authority.")
    else:
        signing = "unsigned (the approval is unauthenticated)."
    return (
        "\n## Local release\n\n"
        "This release was prepared and approved locally. The approval is **local and not server-enforced**; "
        f"the approver ({approval['approver']}) is the Git identity, **asserted, not authenticated**. "
        f"Release commit and tag: {signing} "
        f"Reviewed candidate: `{approval['candidate_id']}`. The tag was not pushed and nothing was published "
        "on GitHub.\n"
    )


def load_release(store: ReleaseStore, version: str) -> tuple[Path, dict] | None:
    entries = store.entries("releases", version)
    if not entries:
        return None
    path = entries[0].path
    problems = store.verify_entry(path)
    if problems:
        raise LocalReleaseError("The published release was modified: " + "; ".join(problems))
    record = json.loads((path / PUBLISH_FILE).read_text(encoding="utf-8"))
    if sha256_file(path / record["zip"]) != record["zip_sha256"]:
        raise LocalReleaseError("The published package does not match its recorded checksum.")
    events = [e for e in store.events() if e.get("event") == "published" and e.get("version") == version]
    if len(events) != 1 or events[0].get("publish_sha256") != sha256_file(path / PUBLISH_FILE):
        raise LocalReleaseError("The published release does not match its release-store index event.")
    return path, record


def verify_package(stage: Path, version: str, package_dir: Path, approval_bytes: bytes, source_sha: str) -> list[str]:
    """The package against the tag: identity, accepted manifest, approval, MANIFEST.txt."""

    from .release_ledger import parse_ledger_text
    from .release_package import manifest_text

    problems = []
    ledger = _git_bytes(stage, "show", f"refs/tags/{version}:.boardwright/releases.yaml").decode("utf-8")
    entry = next((r for r in parse_ledger_text(ledger) if r.version == version), None)
    if entry is None:
        return [f"The tag's ledger has no entry for {version}."]
    identity = json.loads((package_dir / "RELEASE_IDENTITY.json").read_text(encoding="utf-8"))
    if identity != entry.as_dict():
        problems.append("RELEASE_IDENTITY.json is not the tagged ledger entry.")
    if entry.source_commit != source_sha:
        problems.append(f"The tagged ledger entry names source {entry.source_commit[:12]}, not {source_sha[:12]}.")
    accepted = _git_bytes(stage, "show", f"refs/tags/{version}:.boardwright/accepted_manifest.json")
    if (package_dir / "ACCEPTED_OUTPUT_MANIFEST.json").read_bytes() != accepted:
        problems.append("ACCEPTED_OUTPUT_MANIFEST.json is not the tagged accepted manifest.")
    if _git_bytes(stage, "show", f"refs/tags/{version}:.boardwright/approval.json") != approval_bytes:
        problems.append("The tagged approval.json is not the store's approval.")
    if (package_dir / "MANIFEST.txt").read_bytes() != manifest_text(package_dir).encode("utf-8", "surrogateescape"):
        problems.append("MANIFEST.txt does not match the package contents.")
    return problems


def publish(config, store: ReleaseStore, version: str, *, runner=None, echo=print) -> dict:
    from .output_manifest import blocking, build_manifest, compare_manifests, load_manifest
    from .release_package import build_package, copy_board_images, release_notes, source_zip_name

    repo = Path(config.root)
    runner = runner or container.run_in_container
    with store.locked(f"publish {version}"):
        problems = store.verify_index()
        if problems:
            raise LocalReleaseError("The release-store index is inconsistent: " + "; ".join(problems))
        existing = load_release(store, version)
        if existing:
            echo(f"Release {version} is already published and intact: {existing[0]}")
            return existing[1]
        _cpath, record, _reviewed, approval = verify_approval(store, version)
        state = finalize_state(repo, store, version)
        if state.stage != "tagged":
            raise LocalReleaseError(f"Release {version} is not finalised ({state.detail}); run finalize first.")
        _fpath, finalization = load_finalization(store, version)
        from .local_signing import release_signatures

        signatures = release_signatures(repo, version, finalization, approval)
        if signatures.problems:
            raise LocalReleaseError("Nothing was published: " + " ".join(signatures.problems))
        commit = finalization["release_commit"]
        approval_bytes = (store.entry_path("approvals", version, record["candidate_id"]) / "approval.json").read_bytes()
        request = ReleaseRequest(**record["request"])
        tag_object = subprocess.run(["git", "rev-parse", f"refs/tags/{version}"], cwd=repo, capture_output=True,
                                    text=True).stdout.strip()
        workdir = (container.default_stage_root() / f"{repo.name}-local-{safe_name(version, 'version')}-pub").resolve()
        with stage_lock(workdir, purpose=f"local release publish {version}"):
            container.stage(repo, dest=workdir, purpose=STAGE_PURPOSE, revision=f"refs/tags/{version}")
            staged_tag = subprocess.run(["git", "rev-parse", f"refs/tags/{version}"], cwd=workdir, capture_output=True,
                                        text=True).stdout.strip()
            if _git_bytes(workdir, "rev-parse", "HEAD").decode().strip() != commit or staged_tag != tag_object:
                raise LocalReleaseError("The publication stage is not the approved tag; nothing was published.")
            env = _release_env((workdir / ".boardwright" / "release.env").read_text(encoding="utf-8"))
            variant = env.get("RELEASE_VARIANT", "RELEASED")
            if variant != approval["inputs"]["IN_VARIANT"]:
                raise LocalReleaseError(f"The tagged release.env variant {variant} is not the approved one.")
            tracked_before = _git_bytes(workdir, "ls-files", "-s", ".boardwright")
            echo(f"[publish] building {version} ({variant}) from tag {tag_object[:12]} in the pinned container")
            status = runner(request.kicad, workdir, container.build_command(variant, request.kicad, "release"))
            if status != 0:
                raise LocalReleaseError(f"The release build failed (exit {status}); nothing was published.")
            accepted_path = workdir / ".boardwright" / "accepted_manifest.json"
            if accepted_path.read_bytes() != _git_bytes(workdir, "show", f"{commit}:.boardwright/accepted_manifest.json") \
                    or _git_bytes(workdir, "ls-files", "-s", ".boardwright") != tracked_before:
                raise LocalReleaseError("The build changed the tagged release metadata; nothing was published.")
            differences = compare_manifests(load_manifest(accepted_path), build_manifest(workdir))
            hard = blocking(differences)
            if hard:
                raise LocalReleaseError("The outputs rebuilt from the tag differ from the accepted manifest; nothing "
                                        "was published:\n- " + "\n- ".join(str(d) for d in hard[:20]))
            echo(f"[publish] outputs match the tagged accepted manifest ({len(differences)} normalised note(s), 0 blocking)")
            if env.get("RELEASE_INCLUDE_SOURCE_ARCHIVE", "false") == "true":
                from .source_package import build_source_package

                build_source_package(workdir.resolve(), workdir / source_zip_name(version), force=True)
            notes = release_notes(workdir, version, variant=variant, repository=f"local/{store.project_id}",
                                  asset_base_url="") + local_notes_section(approval, finalization)
            (workdir / "RELEASE_NOTES.md").write_text(notes, encoding="utf-8", newline="\n")
            images = copy_board_images(workdir, workdir)
            package = build_package(workdir, version, release_dir=workdir / RELEASE_DIR)
            problems = verify_package(workdir, version, package.release_dir, approval_bytes, finalization["source_sha"])
            if problems:
                raise LocalReleaseError("The package does not match the tag; nothing was published:\n- "
                                        + "\n- ".join(problems))
            files: dict[str, Path | bytes] = {
                package.zip_path.name: package.zip_path,
                "MANIFEST.txt": package.manifest,
                "RELEASE_IDENTITY.json": package.release_dir / "RELEASE_IDENTITY.json",
                "ACCEPTED_OUTPUT_MANIFEST.json": package.release_dir / "ACCEPTED_OUTPUT_MANIFEST.json",
                "RELEASE_NOTES.md": workdir / "RELEASE_NOTES.md",
                **{image.name: image for image in images},
            }
            if (workdir / source_zip_name(version)).is_file():
                files[source_zip_name(version)] = workdir / source_zip_name(version)
            publication = {
                "schema": 1, "version": version, "tag_object": tag_object, "release_commit": commit,
                "source_sha": finalization["source_sha"], "candidate_id": record["candidate_id"],
                "approval_sha256": sha256_bytes(approval_bytes), "authority": "local", "server_enforced": False,
                "authenticated": False, "signed": finalization["signed"], "variant": variant,
                "signature": signatures.as_dict(),
                "zip": package.zip_path.name, "zip_sha256": sha256_file(package.zip_path),
                "manifest_txt_sha256": sha256_file(package.manifest), "package_files": package.files,
                "output_notes": len(differences), "output_blocking": 0,
                "image": container.image_for(request.kicad).reference,
            }
            files[PUBLISH_FILE] = (json.dumps(publication, indent=2, sort_keys=True) + "\n").encode("utf-8")
            checkpoint("before-store")
            entry = store.freeze("releases", version, "", files)
            checkpoint("after-store")
            store.append_event({"event": "published", "version": version, "zip_sha256": publication["zip_sha256"],
                                "publish_sha256": sha256_file(entry.path / PUBLISH_FILE),
                                "sums_sha256": sha256_file(entry.path / "SHA256SUMS")})
        verified = load_release(store, version)
        if not verified:
            raise LocalReleaseError("The published release could not be verified after writing.")
    echo(f"Published {version} to {entry.path}")
    echo(f"  {publication['zip']} sha256 {publication['zip_sha256']}")
    echo("  Local release: approval local, not server-enforced, approver not authenticated; not on GitHub.")
    return publication
