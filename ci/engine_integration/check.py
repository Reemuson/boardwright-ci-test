# SPDX-License-Identifier: LicenseRef-Boardwright-Source-Available
# Copyright (c) Ryan Hicks
"""Non-publishing integration check of the shared release engine (testbench only).

1. Packaging: rebuild the published 0.1.7 package with the installed
   ``python -m boardwright.release_package notes|collect`` (the exact commands
   ``release.yaml`` runs) from the authenticated tag source and published
   outputs, and compare identities, manifests, notes, zip member names and
   member contents.
2. Release commit: run ``python -m boardwright.release_commit`` in disposable
   repositories with no remote, and verify staged files, the commit object,
   the subject and the provenance trailers.

It never pushes, tags or publishes: the workflow token is read-only, the
commit repositories have no remote, and the script refuses to run against a
repository that has one.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import io
import zipfile
from pathlib import Path

TAG = "0.1.7"
TAG_OBJECT = "19fae63115299e91d3faa3fcf5a566591d456bd9"
TAG_COMMIT = "a2063926dcfe5ec5a58b36fc2115f260d08299e2"
ZIP_SHA256 = "47bd87c34da2446efab04032c5d7ad46a2dd534404ec29b2ed60965b6d51dc4c"  # release asset digest
BODY_SHA256 = "1b50dfd9dfb036a323a2e7c82e6db4726498ffa2b629fbabf57d37017994f385"  # published release body
COPY_SET = ("Schematic", "Manufacturing", "assets", "HTML", "KiRI", "Testing", "Reports",
            "README.md", "NOTICE.md", "THIRD_PARTY_NOTICES.md", "LICENSE")

results: list[dict] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append({"check": name, "ok": bool(ok), "detail": detail})
    print(("PASS " if ok else "FAIL ") + name + (f" - {detail}" if detail else ""), flush=True)


def run(argv: list[str], cwd: Path, env: dict | None = None, ok=(0,)) -> subprocess.CompletedProcess:
    done = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, env={**os.environ, **(env or {})})
    if done.returncode not in ok:
        raise SystemExit(f"{' '.join(argv)} exited {done.returncode}: {done.stderr.strip()}")
    return done


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def packaging(repo: Path, published_zip: Path, body: Path, work: Path, repository: str) -> None:
    check("tag object is pinned", run(["git", "rev-parse", f"{TAG}^{{tag}}"], repo).stdout.strip() == TAG_OBJECT)
    check("tag commit is pinned", run(["git", "rev-parse", f"{TAG}^{{commit}}"], repo).stdout.strip() == TAG_COMMIT)
    check("published zip digest matches the release asset", sha256(published_zip) == ZIP_SHA256, sha256(published_zip))
    check("release body fixture matches the published body", sha256(body) == BODY_SHA256)

    ws, pub = work / "workspace", work / "published"
    for path in (ws, pub):
        shutil.rmtree(path, ignore_errors=True)
        path.mkdir(parents=True)
    archive = subprocess.run(["git", "archive", "--format=tar", TAG], cwd=repo, capture_output=True, check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(ws, filter="data")
    with zipfile.ZipFile(published_zip) as z:
        published_names = set(z.namelist())
        z.extractall(pub)
    rel = pub / "boardwright-release"
    # The published outputs stand in for the build; the tag supplies the rest.
    for name in COPY_SET:
        target, source = ws / name, rel / name
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
        if source.is_dir():
            shutil.copytree(source, target)
        elif source.exists():
            shutil.copy2(source, target)
    check("tagged accepted manifest equals the packaged one",
          (ws / ".boardwright" / "accepted_manifest.json").read_bytes() == (rel / "ACCEPTED_OUTPUT_MANIFEST.json").read_bytes())

    variant = "RELEASED"
    for line in (ws / ".boardwright" / "release.env").read_text(encoding="utf-8").splitlines():
        if line.startswith("RELEASE_VARIANT="):
            variant = line.split("=", 1)[1].strip()
    # Exactly the release.yaml commands, run with the installed engine.
    base_url = f"https://github.com/{repository}/releases/download/{TAG}"
    run([sys.executable, "-m", "boardwright.release_package", "--root", str(ws), "notes", "--tag", TAG,
         "--variant", variant, "--repository", repository, "--asset-base-url", base_url], repo)
    run([sys.executable, "-m", "boardwright.release_package", "--root", str(ws), "collect", "--tag", TAG,
         "--release-dir", "boardwright-release"], repo)

    new = ws / "boardwright-release"
    for name in ("MANIFEST.txt", "RELEASE_IDENTITY.json", "ACCEPTED_OUTPUT_MANIFEST.json"):
        check(f"{name} byte-identical", (new / name).read_bytes() == (rel / name).read_bytes(), sha256(new / name)[:12])
    check("MANIFEST.txt verifies against the new package",
          subprocess.run(["sha256sum", "-c", "--quiet", "MANIFEST.txt"], cwd=new, capture_output=True).returncode == 0)
    check("release notes identical to the published body",
          (ws / "RELEASE_NOTES.md").read_bytes() == body.read_bytes())
    new_zip = ws / f"boardwright-{TAG}-release.zip"
    with zipfile.ZipFile(new_zip) as z:
        new_names = set(z.namelist())
        check("zip member names identical", new_names == published_names,
              f"{len(new_names)} members; only new {sorted(new_names - published_names)[:3]}, "
              f"only published {sorted(published_names - new_names)[:3]}")
        with zipfile.ZipFile(published_zip) as p:
            differing = [n for n in published_names & new_names if not n.endswith("/") and z.read(n) != p.read(n)]
        check("zip member contents identical", not differing, f"differing: {differing[:5]}")
        check("zip testzip clean", z.testzip() is None)
    check("zip bytes differ (expected, not claimed)", sha256(new_zip) != ZIP_SHA256, sha256(new_zip)[:12])
    images = sorted(p.name for p in ws.glob("*.png"))
    check("board renders copied for upload", images == ["FIC-100A-angled_bottom.png", "FIC-100A-angled_top.png"], str(images))


def _git(repo: Path, *args: str) -> str:
    return run(["git", *args], repo).stdout


def _seed(repo: Path, renders: bool) -> str:
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    for key, value in (("user.name", "boardwright[bot]"), ("user.email", "boardwright[bot]@users.noreply.github.com"),
                       ("commit.gpgsign", "false")):
        _git(repo, "config", key, value)
    files = {".gitignore": ".boardwright/*.env\n.boardwright/releases.yaml\n.boardwright/accepted_manifest.json\n"
                           ".boardwright/review_candidate.json\n",
             "CHANGELOG.md": "# Changelog\n", "README.md": "# Board\n", ".boardwright/project.yaml": "name: b\n",
             ".boardwright/document_revisions.yaml": "schema: 1\n", "Manufacturing/old.gbr": "old\n",
             "board.kicad_pcb": "(kicad_pcb)\n"}
    if renders:
        files["assets/renders/top.png"] = "png1\n"
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "source")
    for name, text in {"CHANGELOG.md": "# Changelog\n\n## [0.1.8] - 2026-11-20\n", "README.md": "# Board\n\n0.1.8\n",
                       ".boardwright/project.yaml": "name: b\nversion: 0.1.8\n",
                       ".boardwright/release.env": "RELEASE_VARIANT=RELEASED\n",
                       ".boardwright/revision_history_variables.env": "R=C\n", ".boardwright/releases.yaml": "schema: 1\n",
                       ".boardwright/accepted_manifest.json": "{}\n", ".boardwright/review_candidate.json": "{}\n",
                       "untracked.txt": "not released\n"}.items():
        (repo / name).write_text(text, encoding="utf-8")
    if renders:
        (repo / "assets/renders/top.png").write_text("png2\n", encoding="utf-8")
        (repo / "assets/renders/bottom.png").write_text("png3\n", encoding="utf-8")
    return _git(repo, "rev-parse", "HEAD").strip()


def release_commit(work: Path, source_sha: str, run_url: str, repo_root: Path) -> None:
    expected_base = {".boardwright/accepted_manifest.json", ".boardwright/project.yaml", ".boardwright/release.env",
                     ".boardwright/releases.yaml", ".boardwright/review_candidate.json",
                     ".boardwright/revision_history_variables.env", "CHANGELOG.md", "Manufacturing/old.gbr", "README.md"}
    for label, renders, url in (("approved, with renders", True, run_url), ("unreviewed, no renders", False, "")):
        repo = work / f"commit-{'renders' if renders else 'plain'}"
        shutil.rmtree(repo, ignore_errors=True)
        parent = _seed(repo, renders)
        if _git(repo, "remote").strip():
            raise SystemExit("refusing: the disposable repository has a remote")
        done = run([sys.executable, "-m", "boardwright.release_commit", "--root", str(repo), "--version", "0.1.8",
                    "--variant", "RELEASED", "--source-sha", source_sha, "--reviewed-run-url", url], repo_root)
        head = done.stdout.strip()
        check(f"[{label}] commit created", head == _git(repo, "rev-parse", "HEAD").strip() and head != parent)
        check(f"[{label}] single parent is the source commit", _git(repo, "rev-list", "--parents", "-n1", "HEAD").split()[1:] == [parent])
        changed = set(_git(repo, "diff", "--name-only", "HEAD~1", "HEAD").split())
        expected = expected_base | ({"assets/renders/top.png", "assets/renders/bottom.png"} if renders else set())
        check(f"[{label}] staged files", changed == expected, f"unexpected {sorted(changed ^ expected)}")
        check(f"[{label}] untracked file left out", "untracked.txt" not in changed)
        message = _git(repo, "log", "-1", "--format=%B")
        check(f"[{label}] subject", message.splitlines()[0] == "release: prepare 0.1.8 (RELEASED)", message.splitlines()[0])
        trailers = subprocess.run(["git", "interpret-trailers", "--parse"], input=message, capture_output=True, text=True).stdout
        want = f"Source-Commit: {source_sha}\n" + (f"Reviewed-Run: {url}\n" if url else "")
        check(f"[{label}] provenance trailers", trailers == want, trailers.strip().replace("\n", " | "))
        check(f"[{label}] commit identity", _git(repo, "log", "-1", "--format=%an <%ae>").strip()
              == "boardwright[bot] <boardwright[bot]@users.noreply.github.com>")
        check(f"[{label}] no tags and no remote", not _git(repo, "tag").strip() and not _git(repo, "remote").strip())
        check(f"[{label}] commit object recorded", True, _git(repo, "cat-file", "-p", "HEAD").replace("\n", " | ")[:300])
        again = run([sys.executable, "-m", "boardwright.release_commit", "--root", str(repo), "--version", "0.1.8",
                     "--variant", "RELEASED", "--source-sha", source_sha], repo_root, ok=(0, 1, 2))
        check(f"[{label}] empty second commit refused (exit 1)", again.returncode == 1, again.stderr.strip())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--published-zip", required=True, type=Path)
    parser.add_argument("--body", required=True, type=Path)
    parser.add_argument("--work", required=True, type=Path)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--run-url", required=True)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    repo = Path.cwd()
    source_sha = run(["git", "rev-parse", "HEAD"], repo).stdout.strip()
    packaging(repo, args.published_zip, args.body, args.work, args.repository)
    release_commit(args.work, source_sha, args.run_url, repo)
    args.out.write_text(json.dumps({"source_sha": source_sha, "results": results}, indent=2), encoding="utf-8")
    failed = [r["check"] for r in results if not r["ok"]]
    print(f"{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
