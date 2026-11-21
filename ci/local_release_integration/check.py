# SPDX-License-Identifier: LicenseRef-Boardwright-Source-Available
# Copyright (c) Ryan Hicks
"""Non-publishing integration check of standalone local releases (testbench only).

Runs the real pinned-Docker local-release lifecycle (``boardwright release
local`` and the TUI, Boardwright ``LOCAL_FIRST.md`` sections 5 and 7.1) in a
disposable clone of this checkout with no remote, under paths with spaces
and non-ASCII characters:

- backend selection without a remote, and a GitHub remote refused without an
  explicit local override; plan refusals; read-only plan, status and TUI views;
- release A ``0.1.8`` (tooling): candidate with baseline reuse, review,
  approval, finalisation with an annotated tag, publication from the tag,
  duplicate operations, comparison with the held GitHub candidate;
- release B ``0.1.9`` (fabrication): a reused PCB revision refused, then a
  re-issued baseline; a lost candidate index event recovered; review and
  approval in the TUI; a signed finalisation interrupted after the branch
  moved, recovered in the TUI and resumed; publication interrupted after the
  store write and recovered;
- tampering of the candidate, review candidate, manifest, approval, review
  record, index and package (on copies), refused before any build;
- signatures: valid; cryptographically valid but untrusted identity;
  cryptographically invalid;
- release-store ownership, links, corruption and interrupted writes;
- this checkout (files, HEAD, branches, tags, status) unchanged.

Nothing is pushed, tagged, approved or published in this repository: the
workflow token is read-only and never reaches this process, the disposable
repositories have no remote and a pre-push hook that refuses, and every
local-release process runs with credentials removed from its environment.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

results: list[dict] = []
identities: dict = {}
comparisons: dict = {}
HERE = Path(__file__).resolve().parent
INTERRUPTED = 70
PCB = "FIC-100A.kicad_pcb"
# The front silkscreen label, with either line ending (a Windows checkout may use CRLF).
SILK = re.compile(rb'(\(gr_text "FIC-100A"\r?\n\t\t)\(at 154 81\.5 0\)')
DATE = "2026-11-20"  # the held 0.1.8 candidate's release date
EMAIL = "integration@example.invalid"
A = dict(version="0.1.8", drawing_revision="C", description="Revision note labelled SCH REV", classes="tooling",
         justification="Tooling-only: shared release engine (packaging and release commit in Python) and bounded "
                       "tag-push retry. PCB A, schematic C, fabrication A/2 and assembly A reused.")
B = dict(version="0.1.9", drawing_revision="C", description="Front silkscreen label moved", classes="fabrication",
         justification="Fabrication change: front silkscreen label moved 1 mm; PCB revision B. New fabrication "
                       "baseline expected.")
SENSITIVE = ("TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "PRIVATE_KEY")


def check(name: str, ok, detail="") -> bool:
    results.append({"check": name, "ok": bool(ok), "detail": str(detail)[:800]})
    print(("PASS " if ok else "FAIL ") + name + (f" - {detail}" if detail != "" else ""), flush=True)
    return bool(ok)


def git(repo: Path, *args: str, ok: bool = False) -> str:
    done = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if done.returncode != 0 and not ok:
        raise RuntimeError(f"git {' '.join(args)} in {repo}: {done.stderr.strip()}")
    return done.stdout


def file_hashes(root: Path, *, skip_git: bool = True) -> dict[str, str]:
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        if skip_git:
            dirnames[:] = [d for d in dirnames if d != ".git"]
        for name in filenames:
            path = Path(dirpath) / name
            if not path.is_symlink():
                out[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def clean_env() -> dict[str, str]:
    """The environment of every local-release process: no tokens or credentials at all."""

    return {k: v for k, v in os.environ.items()
            if not any(word in k.upper() for word in SENSITIVE) and not k.startswith(("ACTIONS_", "GH_", "GITHUB_TOKEN"))}


def writable(path: Path) -> None:
    for item in [path, *path.rglob("*")] if path.is_dir() else [path]:
        os.chmod(item, stat.S_IWRITE | stat.S_IREAD | (stat.S_IEXEC if item.is_dir() else 0))


def rewrite_sums(entry: Path) -> None:
    """Make a tampered entry self-consistent, as a careful attacker would."""

    writable(entry)
    lines = []
    for item in sorted(p for p in entry.rglob("*") if p.is_file() and p.name != "SHA256SUMS"):
        lines.append(f"{hashlib.sha256(item.read_bytes()).hexdigest()}  {item.relative_to(entry).as_posix()}\n")
    (entry / "SHA256SUMS").write_text("".join(lines), encoding="utf-8", newline="\n")


class Harness:
    def __init__(self, checkout: Path, temp: Path, held: Path | None, held_run: str | None = None) -> None:
        self.checkout = checkout
        self.temp = temp
        self.held = held
        self.held_run = held_run
        self.repo = temp / "bw local rélease" / "FIC board"
        self.store_base = temp / "release store é"
        self.env = {**clean_env(), "PYTHONPATH": str(self.repo / "src"), "PYTHONIOENCODING": "utf-8",
                    "PYTHONDONTWRITEBYTECODE": "1", "GIT_TERMINAL_PROMPT": "0",
                    "BOARDWRIGHT_RELEASE_STORE": str(self.store_base),
                    "BOARDWRIGHT_STAGE_ROOT": str(temp / "stages é"),
                    "BOARDWRIGHT_3D_CACHE": str(temp / "cache 3d"),
                    # An empty gh configuration: no stored gh login reaches any local-release process.
                    "GH_CONFIG_DIR": str(temp / "empty gh config")}
        self.timings: dict[str, float] = {}
        self.outputs: dict[str, str] = {}

    # -- helpers ---------------------------------------------------------------
    def cli(self, label: str, *args: str, cwd: Path | None = None, store: Path | None = None,
            interrupt: tuple[str, str] | None = None) -> subprocess.CompletedProcess:
        env = dict(self.env)
        if store is not None:
            env["BOARDWRIGHT_RELEASE_STORE"] = str(store)
        command = [sys.executable, str(HERE / "interrupt.py"), *interrupt, "--"] if interrupt else \
            [sys.executable, "-m", "boardwright", "release", "local"]
        operation = next((a for a in args if a in ("candidate", "finalize", "publish")), "")
        if operation:  # may run a pinned-Docker build: record the free disk space first
            free = round(shutil.disk_usage(self.temp).free / 1e9, 1)
            self.timings[f"free-disk-gb-before-{label}"] = free
            print(f"DISK {free} GB free before {label} ({operation})", flush=True)
        start = time.perf_counter()
        done = subprocess.run([*command, *args], cwd=cwd or self.repo, env=env, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        self.timings[label] = round(time.perf_counter() - start, 1)
        self.outputs[label] = done.stdout + done.stderr
        (self.temp / f"{label}.log").write_text(f"$ {' '.join(args)}\n{done.stdout}\n--- stderr ---\n{done.stderr}",
                                                encoding="utf-8")
        return done

    def api(self):
        sys.path.insert(0, str(self.repo / "src"))
        import boardwright  # noqa: F401  (from the disposable repository's own payload)

        return boardwright

    def store(self, base: Path | None = None):
        import yaml

        self.api()
        from boardwright.release_store import ReleaseStore, project_identity

        project = yaml.safe_load((self.repo / ".boardwright" / "project.yaml").read_text(encoding="utf-8"))
        project_id = project_identity(self.repo, project)
        return ReleaseStore((base or self.store_base) / project_id, project_id)

    def repo_state(self, repo: Path | None = None) -> tuple:
        repo = repo or self.repo
        git_dir = repo / ".git"
        return (sorted(str(p.relative_to(git_dir)) for p in (git_dir / "objects").rglob("*")),
                (git_dir / "index").stat().st_mtime_ns, git(repo, "for-each-ref", "--format=%(refname) %(objectname)"),
                git(repo, "--no-optional-locks", "status", "--porcelain", "--untracked-files=all"))

    def store_state(self) -> list:
        root = self.store_base
        return sorted((str(p.relative_to(root)), p.stat().st_size, p.stat().st_mtime_ns)
                      for p in root.rglob("*")) if root.exists() else []

    def candidate_args(self, spec: dict) -> list[str]:
        return [spec["version"], "--drawing-revision", spec["drawing_revision"], "--revision-description",
                spec["description"], "--change-class", spec["classes"], "--justification", spec["justification"],
                "--kind", "draft", "--release-date", DATE]

    def candidate_dir(self, version: str) -> Path:
        entries = list((self.store().root / "candidates" / version).iterdir())
        return entries[0]

    def ledger_entry(self, root: Path, version: str) -> dict:
        import yaml

        data = yaml.safe_load((root / ".boardwright" / "releases.yaml").read_text(encoding="utf-8"))
        return next(r for r in data["releases"] if str(r["version"]) == version)

    def review(self, version: str, label: str) -> Path:
        done = self.cli(f"{label}-review", "review", version)
        draft = Path(next(line.split(": ", 1)[1] for line in done.stdout.splitlines() if line.startswith("Review draft: ")))
        record = self.temp / f"REVIEW-{version}.md"
        record.write_text(draft.read_text(encoding="utf-8").replace("- [ ]", "- [x]").replace("APPROVED: no", "APPROVED: yes"),
                          encoding="utf-8", newline="\n")
        return record

    def no_rebuild(self, label: str) -> bool:
        out = self.outputs[label]
        return "rebuilding" not in out and "[candidate]" not in out and "[publish] building" not in out

    # -- setup -----------------------------------------------------------------
    def setup(self) -> None:
        self.checkout_before = (file_hashes(self.checkout), git(self.checkout, "rev-parse", "HEAD"),
                                git(self.checkout, "for-each-ref", "--format=%(refname) %(objectname)"),
                                git(self.checkout, "status", "--porcelain", "--untracked-files=all"))
        check("checkout has no persisted credentials",
              not git(self.checkout, "config", "--get-regexp", r"http\..*extraheader", ok=True).strip())
        check("the harness step has no GitHub token", not {"GH_TOKEN", "GITHUB_TOKEN"} & set(os.environ))
        leaked = sorted(k for k in self.env if any(word in k.upper() for word in SENSITIVE) or (k.startswith("GH_") and k != "GH_CONFIG_DIR"))
        check("local-release processes get no token or credential variables", not leaked, leaked)
        if shutil.which("gh"):
            auth = subprocess.run(["gh", "auth", "status"], env=self.env, capture_output=True, text=True)
            check("gh is not authenticated for local-release processes", auth.returncode != 0)
        self.repo.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "--quiet", "--no-hardlinks", str(self.checkout), str(self.repo)], check=True,
                       env=self.env)
        git(self.repo, "remote", "remove", "origin")
        # The release branch at the checked-out commit, whatever the checkout's own branch is called.
        git(self.repo, "checkout", "-q", "-B", "main", "HEAD")
        hook = self.repo / ".git" / "hooks" / "pre-push"
        hook.write_text("#!/bin/sh\necho 'push disabled in the disposable repository' >&2\nexit 1\n", encoding="utf-8")
        hook.chmod(0o755)
        for key, value in (("user.name", "Local Reviewer"), ("user.email", EMAIL), ("push.default", "nothing")):
            git(self.repo, "config", key, value)
        check("disposable repository has no remote and is on main at the checked-out commit",
              git(self.repo, "remote").strip() == "" and git(self.repo, "symbolic-ref", "--short", "HEAD").strip() == "main")
        bare = self.temp / "push target.git"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True, env=self.env)
        pushed = subprocess.run(["git", "push", str(bare), "HEAD:refs/heads/main", "--tags"], cwd=self.repo,
                                env=self.env, capture_output=True, text=True)
        check("disposable repository refuses to push (pre-push hook)",
              pushed.returncode != 0 and "push disabled" in pushed.stderr and not git(bare, "for-each-ref").strip())
        check("disposable repository is at the checkout's commit, with tag 0.1.7",
              git(self.repo, "rev-parse", "HEAD") == git(self.checkout, "rev-parse", "HEAD")
              and "0.1.7" in git(self.repo, "tag").split())
        image = self.image()
        start = time.perf_counter()
        pulled = subprocess.run(["docker", "pull", "-q", image], capture_output=True, text=True)
        self.timings["docker-pull"] = round(time.perf_counter() - start, 1)
        check("pinned KiCad 9 image pulled", pulled.returncode == 0, image)
        # Six builds need about 3 GB here (stages 2.1 GB, repositories, stores and copies; measured locally).
        free = shutil.disk_usage(self.temp).free
        timings_free = round(free / 1e9, 1)
        self.timings["free-disk-gb-after-pull"] = timings_free
        if not check("at least 5 GB free for the builds after the image pull", free >= 5e9, f"{timings_free} GB"):
            raise RuntimeError(f"only {timings_free} GB free; the six builds need about 3 GB (stopping before any build)")
        identities["image"] = image

    def image(self) -> str:
        self.api()
        from boardwright import container

        return container.image_for(container.DEFAULT_KICAD).reference

    # -- 1. backend and plan ------------------------------------------------------
    def backend_and_plan(self) -> None:
        done = self.cli("plan-default", "plan", "0.1.8")
        check("no remote: local backend by default and plan OK",
              done.returncode == 0 and "Release backend: local (the project has no remote" in done.stdout
              and "Plan OK" in done.stdout, done.stdout[-300:])
        done = self.cli("plan-github", "plan", "0.1.8", "--backend", "github")
        check("--backend github refused without a remote; nothing falls back",
              done.returncode == 2 and "no remote" in done.stderr and "Release backend: local" not in done.stdout,
              done.stderr.strip()[-200:])
        other = self.temp / "github remote é"
        subprocess.run(["git", "clone", "-q", "--no-hardlinks", str(self.repo), str(other)], check=True, env=self.env)
        git(other, "remote", "set-url", "origin", "https://github.com/example-owner/example-board.git")
        git(other, "remote", "set-url", "--push", "origin", "DISABLED-no-push")
        done = self.cli("plan-github-remote", "plan", "0.1.8", cwd=other)
        check("GitHub remote without gh login: release refused, no local fallback",
              done.returncode == 2 and "Release backend refused" in done.stderr and "--backend local" in done.stderr,
              done.stderr.strip()[-250:])
        done = self.cli("plan-github-remote-local", "plan", "0.1.8", "--backend", "local", cwd=other)
        check("explicit --backend local on a GitHub project warns local-only",
              done.returncode == 0 and "tag is not pushed" in done.stdout, done.stdout[-250:])
        shutil.rmtree(other, onerror=lambda f, p, _e: (os.chmod(p, 0o700), f(p)))

        before = self.repo_state()
        (self.repo / "README.md").write_bytes((self.repo / "README.md").read_bytes() + b"\nlocal edit\n")
        dirty = self.cli("plan-dirty", "plan", "0.1.8")
        git(self.repo, "checkout", "--", "README.md")
        (self.repo / "notes.txt").write_text("scratch\n", encoding="utf-8")
        untracked = self.cli("plan-untracked", "plan", "0.1.8")
        (self.repo / "notes.txt").unlink()
        git(self.repo, "tag", "0.1.8")
        tagged = self.cli("plan-tag", "plan", "0.1.8")
        git(self.repo, "tag", "-d", "0.1.8")
        check("plan refuses a dirty tree, an untracked file and an existing tag",
              all(d.returncode == 1 for d in (dirty, untracked, tagged)) and "uncommitted or untracked" in dirty.stdout
              and "uncommitted or untracked" in untracked.stdout and "0.1.8" in tagged.stdout,
              [d.returncode for d in (dirty, untracked, tagged)])
        check("refused plans changed no refs", before[2] == self.repo_state()[2])

    # -- read-only views -------------------------------------------------------------
    def read_only(self, label: str) -> None:
        repo_before, store_before = self.repo_state(), self.store_state()
        plan = self.cli(f"{label}-ro-plan", "plan", "9.9.9")
        status = self.cli(f"{label}-ro-status", "status")
        self.api()
        from boardwright import local_release_ops
        from boardwright import tui as tui_module

        cwd = os.getcwd()
        os.chdir(self.repo)
        try:
            with _env(self.env):
                tui_module.collect_dashboard_state()
                local_release_ops.summaries(tui_module.load_config())
                line = local_release_ops.overview_line(tui_module.load_config())
        finally:
            os.chdir(cwd)
        check(f"[{label}] plan, status, TUI summaries and dashboard are read-only (objects, index, refs, store)",
              plan.returncode in (0, 1) and status.returncode == 0
              and self.repo_state() == repo_before and self.store_state() == store_before, line)

    # -- 2. release A ------------------------------------------------------------------
    def release_a(self) -> None:
        source = git(self.repo, "rev-parse", "HEAD").strip()
        done = self.cli("A-candidate", "candidate", *self.candidate_args(A))
        check("[A] candidate built and frozen", done.returncode == 0, done.stderr.strip()[-300:])
        entry = self.candidate_dir("0.1.8")
        ledger = self.ledger_entry(entry, "0.1.8")
        check("[A] fabrication A/2 and assembly A reused",
              (ledger["fabrication_baseline"], ledger["assembly_baseline"])
              == ({"id": "A/2", "status": "reused"}, {"id": "A", "status": "reused"}), ledger.get("fabrication_baseline"))
        record = json.loads((entry / "candidate.json").read_text(encoding="utf-8"))
        check("[A] candidate bound to the committed source, id = digest of review_candidate.json",
              record["source_sha"] == source and entry.name ==
              hashlib.sha256((entry / ".boardwright" / "review_candidate.json").read_bytes()).hexdigest())
        identities["A"] = {"source": source, "candidate_id": entry.name, "manifest_sha256": record["manifest_sha256"]}
        # The held GitHub candidate comparison is required, never skipped silently.
        held_file = self.held / ".boardwright" / "review_candidate.json" if self.held else None
        held_ok = bool(held_file) and held_file.is_file()
        held_source = json.loads(held_file.read_text(encoding="utf-8")).get("source_sha") if held_ok else None
        checkout = git(self.checkout, "rev-parse", "HEAD").strip()
        identities["held"] = {"run_id": self.held_run, "source": held_source, "checkout": checkout}
        if check("[A] the held GitHub 0.1.8 candidate is available and built from this checkout's commit",
                 held_ok and held_source == checkout, identities["held"]):
            self.compare_with_held(entry)

        draft = self.store().root / "drafts"
        self.cli("A-review", "review", "0.1.8")
        untouched = next(draft.glob("REVIEW-0.1.8-*.md"))
        refused = self.cli("A-approve-untouched", "approve", "0.1.8", "--record", str(untouched))
        check("[A] an untouched review draft is refused",
              refused.returncode == 2 and "not ticked" in refused.stderr and "APPROVED: yes" in refused.stderr)
        record_path = self.review("0.1.8", "A")
        done = self.cli("A-approve", "approve", "0.1.8", "--record", str(record_path))
        approval = json.loads((next((self.store().root / "approvals" / "0.1.8").iterdir()) / "approval.json")
                              .read_text(encoding="utf-8"))
        check("[A] approved: local, not server-enforced, not authenticated",
              done.returncode == 0 and (approval["authority"], approval["server_enforced"], approval["authenticated"])
              == ("local", False, False) and "not server-enforced" in done.stdout)
        again = self.cli("A-approve-again", "approve", "0.1.8", "--record", str(record_path))
        check("[A] repeating the approval is a no-op", again.returncode == 0 and "already approved" in again.stdout)
        edited = self.temp / "REVIEW-0.1.8-other.md"
        edited.write_text(record_path.read_text(encoding="utf-8").replace("Notes / deviations:", "Notes / deviations: x"),
                          encoding="utf-8", newline="\n")
        other = self.cli("A-approve-other", "approve", "0.1.8", "--record", str(edited))
        check("[A] a different approval is refused, never replaced",
              other.returncode == 2 and "never replaced" in other.stderr)

        self.tamper_suite()

        tree_before = file_hashes(self.repo)
        done = self.cli("A-finalize", "finalize", "0.1.8")
        check("[A] finalised", done.returncode == 0, done.stderr.strip()[-300:])
        commit = self.verify_finalised("A", "0.1.8", source, signed=False)
        tree_after = file_hashes(self.repo)
        changed = sorted(k for k in set(tree_before) | set(tree_after) if tree_before.get(k) != tree_after.get(k))
        committed = sorted(git(self.repo, "diff", "--name-only", source, commit).split())
        check("[A] the developer checkout changed in exactly the release commit's files", changed == committed, changed)
        refs = git(self.repo, "for-each-ref")
        again = self.cli("A-finalize-again", "finalize", "0.1.8")
        check("[A] duplicate finalize: no rebuild, refs unchanged",
              again.returncode == 0 and self.no_rebuild("A-finalize-again") and git(self.repo, "for-each-ref") == refs,
              f"{self.timings['A-finalize-again']} s")

        done = self.cli("A-publish", "publish", "0.1.8")
        check("[A] published from the tag", done.returncode == 0, done.stderr.strip()[-300:])
        self.verify_published("A", "0.1.8", expect_members=392, expect_manifest=358)
        again = self.cli("A-publish-again", "publish", "0.1.8")
        check("[A] duplicate publish: no rebuild, nothing new",
              again.returncode == 0 and "already published" in again.stdout and self.no_rebuild("A-publish-again"))
        self.package_and_store_suite()

    def compare_with_held(self, entry: Path) -> None:
        self.api()
        from boardwright.output_manifest import blocking, compare_manifests, load_manifest

        mine = load_manifest(entry / ".boardwright" / "accepted_manifest.json")
        theirs = load_manifest(self.held / ".boardwright" / "accepted_manifest.json")
        differences = compare_manifests(theirs, mine)
        comparisons["A_candidate_vs_held_github_0.1.8"] = {"entries": len(mine["files"]), "notes": len(differences),
                                                          "blocking": len(blocking(differences))}
        check("[A] controlled outputs equivalent to the held GitHub 0.1.8 candidate", not blocking(differences),
              comparisons["A_candidate_vs_held_github_0.1.8"])
        a = json.loads((entry / ".boardwright" / "review_candidate.json").read_text(encoding="utf-8"))
        b = json.loads((self.held / ".boardwright" / "review_candidate.json").read_text(encoding="utf-8"))
        keys = ("identities", "fabrication_baseline", "assembly_baseline", "change_classes", "previous_version")
        same = {k: a["ledger_entry"][k] == b["ledger_entry"][k] for k in keys}
        check("[A] ledger identities, baselines and inputs equal the held candidate's",
              all(same.values()) and a["inputs"] == b["inputs"] and a["release_date"] == b["release_date"], same)

    def verify_finalised(self, label: str, version: str, source: str, *, signed: bool) -> str:
        commit = git(self.repo, "rev-parse", "main").strip()
        body = git(self.repo, "log", "-1", "--format=%B", commit)
        tag = git(self.repo, "cat-file", "tag", version)
        candidate = self.candidate_dir(version).name
        check(f"[{label}] release commit: one commit on the reviewed source, local trailers, approval committed",
              git(self.repo, "rev-list", "--parents", "-n", "1", commit).split()[1:] == [source]
              and f"Reviewed-Candidate: {candidate}" in body and "Approval-Authority: local" in body
              and "Reviewed-Run" not in body
              and subprocess.run(["git", "cat-file", "-e", f"{commit}:.boardwright/approval.json"], cwd=self.repo,
                                 capture_output=True).returncode == 0)
        check(f"[{label}] annotated tag on the release commit naming the candidate",
              git(self.repo, "cat-file", "-t", version).strip() == "tag"
              and git(self.repo, "rev-parse", f"{version}^{{commit}}").strip() == commit
              and f"Reviewed-Candidate: {candidate}" in tag and ("SSH SIGNATURE" in tag) == signed)
        check(f"[{label}] HEAD is the release commit and the working tree is clean",
              git(self.repo, "rev-parse", "HEAD").strip() == commit and not git(self.repo, "status", "--porcelain").strip())
        check(f"[{label}] still no remote; the pending ref is a record only",
              git(self.repo, "remote").strip() == "" and
              git(self.repo, "rev-parse", f"refs/boardwright/local-release/{version}").strip() == commit)
        identities[label].update(release_commit=commit, tag_object=git(self.repo, "rev-parse", version).strip(),
                                 release_tree=git(self.repo, "rev-parse", f"{commit}^{{tree}}").strip())
        return commit

    def verify_published(self, label: str, version: str, *, expect_members: int | None = None,
                         expect_manifest: int | None = None) -> dict:
        import zipfile

        entry = self.store().root / "releases" / version
        publication = json.loads((entry / "publish.json").read_text(encoding="utf-8"))
        with zipfile.ZipFile(entry / publication["zip"]) as archive:
            members = len(archive.namelist())
        manifest_lines = len([l for l in (entry / "MANIFEST.txt").read_text(encoding="utf-8").splitlines() if l.strip()])
        identity = json.loads((entry / "RELEASE_IDENTITY.json").read_text(encoding="utf-8"))
        ledger = self.ledger_entry_from_tag(version)
        check(f"[{label}] package identity is the tagged ledger entry; outputs match the tagged manifest",
              identity.get("version") == version and identity.get("source_commit") == ledger.get("source_commit")
              and publication["output_blocking"] == 0
              and publication["tag_object"] == git(self.repo, "rev-parse", version).strip()
              and (entry / "ACCEPTED_OUTPUT_MANIFEST.json").read_bytes()
              == subprocess.run(["git", "show", f"{version}:.boardwright/accepted_manifest.json"], cwd=self.repo,
                                capture_output=True).stdout,
              {"notes": publication["output_notes"], "blocking": publication["output_blocking"]})
        check(f"[{label}] publish.json and notes say local, not server-enforced, not authenticated",
              (publication["authority"], publication["server_enforced"], publication["authenticated"])
              == ("local", False, False) and "not server-enforced" in (entry / "RELEASE_NOTES.md").read_text(encoding="utf-8"))
        signature = publication.get("signature") or {}
        expected = ({"signed": True, "approver": EMAIL, "signature_valid": True, "signer_matches_approver": True,
                     "authenticated": False} if publication["signed"] else
                    {"signed": False, "approver": "", "signature_valid": None, "signer_matches_approver": None,
                     "authenticated": False})
        check(f"[{label}] publish.json records signature validity and approver binding separately (F-R7)",
              signature == expected, signature)
        if expect_members is not None:
            check(f"[{label}] package: {expect_members} zip members, {expect_manifest} MANIFEST.txt entries (as the "
                  "published GitHub 0.1.7 package)", (members, manifest_lines) == (expect_members, expect_manifest),
                  (members, manifest_lines))
        comparisons[f"{label}_publish_vs_tagged_manifest"] = {"notes": publication["output_notes"],
                                                              "blocking": publication["output_blocking"],
                                                              "zip_members": members, "manifest_txt": manifest_lines}
        identities[label].update(zip_sha256=publication["zip_sha256"], signed=publication["signed"])
        return publication

    def ledger_entry_from_tag(self, version: str) -> dict:
        import yaml

        data = yaml.safe_load(git(self.repo, "show", f"{version}:.boardwright/releases.yaml"))
        return next(r for r in data["releases"] if str(r["version"]) == version)

    # -- 7. tampering (copies; always refused before any build) ---------------------------
    def tamper_suite(self) -> None:
        store = self.store()
        cases = {
            "candidate output file": ("candidates", lambda e: _flip(next(p for p in (e / "Manufacturing").rglob("*")
                                                                         if p.is_file())), False, "modified"),
            "review candidate (date) with rewritten checksums": (
                "candidates", lambda e: _replace(e / ".boardwright" / "review_candidate.json", DATE, "2026-11-21"),
                True, "candidate id"),
            "accepted manifest with rewritten checksums": (
                "candidates", lambda e: _flip(e / ".boardwright" / "accepted_manifest.json", text=True), True, "manifest"),
            "approval (server_enforced: true) with rewritten checksums": (
                "approvals", lambda e: _replace(e / "approval.json", '"server_enforced": false', '"server_enforced": true'),
                True, "approval"),
            "review record (an item unticked) with rewritten checksums": (
                "approvals", lambda e: _replace(e / "REVIEW-0.1.8.md", "- [x]", "- [ ]"), True, "review"),
            "index line": ("index", None, False, "inconsistent"),
        }
        for number, (name, (kind, mutate, resum, expect)) in enumerate(cases.items()):
            base = self.temp / f"tamper store {number}"
            shutil.copytree(self.store_base, base)
            copy = store.__class__(base / store.project_id, store.project_id)
            repo = self.temp / f"tamper repo {number}"
            subprocess.run(["git", "clone", "-q", "--no-hardlinks", str(self.repo), str(repo)], check=True, env=self.env)
            git(repo, "remote", "remove", "origin")
            git(repo, "config", "user.name", "Local Reviewer")
            git(repo, "config", "user.email", EMAIL)
            if kind == "index":
                index = copy.root / "index.jsonl"
                writable(index)
                lines = index.read_bytes().split(b"\n")
                lines[0] = lines[0].replace(b'"candidate"', b'"candidatX"')
                index.write_bytes(b"\n".join(lines))
            else:
                entry = next((copy.root / kind / "0.1.8").iterdir())
                writable(entry)
                mutate(entry)
                if resum:
                    rewrite_sums(entry)
            refs = git(repo, "for-each-ref")
            done = self.cli(f"tamper-{number}", "finalize", "0.1.8", cwd=repo, store=base)
            check(f"[tamper] {name}: finalize refused before any build, refs unchanged",
                  done.returncode == 2 and expect in done.stderr.lower() and self.no_rebuild(f"tamper-{number}")
                  and git(repo, "for-each-ref") == refs and not git(repo, "tag", "-l", "0.1.8").strip(),
                  done.stderr.strip()[-220:])
            status = self.cli(f"tamper-{number}-status", "status", cwd=repo, store=base)
            check(f"[tamper] {name}: status reports it", status.returncode == 0 and any(
                word in status.stdout for word in ("MODIFIED", "INCONSISTENT", "PROBLEM")), status.stdout[-200:])
            for path in (base, repo):
                shutil.rmtree(path, onerror=lambda f, p, _e: (os.chmod(p, 0o700), f(p)))

    # -- 9. package, store ownership, links, corruption -----------------------------------
    def package_and_store_suite(self) -> None:
        store = self.store()
        self.api()
        from boardwright.release_store import ReleaseStore, StoreError

        def copy(label: str):
            base = self.temp / f"store copy {label}"
            shutil.copytree(self.store_base, base)
            return base, ReleaseStore(base / store.project_id, store.project_id)

        base, copied = copy("zip")
        zip_path = next((copied.root / "releases" / "0.1.8").glob("*.zip"))
        writable(zip_path)
        _flip(zip_path)
        status = self.cli("store-zip-status", "status", "0.1.8", store=base)
        publish = self.cli("store-zip-publish", "publish", "0.1.8", store=base)
        check("[store] a one-byte change to the published zip: status reports it, publish refuses without building",
              "MODIFIED" in status.stdout and publish.returncode == 2 and self.no_rebuild("store-zip-publish"),
              publish.stderr.strip()[-200:])

        base, copied = copy("link")
        entry = copied.root / "releases" / "0.1.8"
        moved = self.temp / "moved release entry"
        os.rename(entry, moved)
        _link(moved, entry)
        status = self.cli("store-link-status", "status", "0.1.8", store=base)
        publish = self.cli("store-link-publish", "publish", "0.1.8", store=base)
        check("[store] a release entry replaced by a symbolic link is reported and refused",
              "link or junction" in status.stdout and publish.returncode == 2 and self.no_rebuild("store-link-publish"),
              status.stdout[-200:])

        base, copied = copy("index")
        index = copied.root / "index.jsonl"
        writable(index)
        index.write_bytes(index.read_bytes()[:-20])
        status = self.cli("store-index-status", "status", store=base)
        check("[store] a truncated index is reported as inconsistent", "INCONSISTENT" in status.stdout,
              status.stdout[:200])

        base, copied = copy("tmp")
        writable(copied.root)
        abandoned = copied.root / ".tmp-0123456789abcdef"
        abandoned.mkdir()
        (abandoned / "partial.bin").write_bytes(b"x")
        before = file_hashes(copied.root / "releases")
        status = self.cli("store-tmp-status", "status", store=base)
        recover = self.cli("store-tmp-recover", "recover", store=base)
        check("[store] an interrupted write is listed and removed by recover; entries untouched",
              "Interrupted write" in status.stdout and recover.returncode == 0 and not abandoned.exists()
              and file_hashes(copied.root / "releases") == before)

        target = self.temp / "link target"
        target.mkdir()
        linked = self.temp / "linked base"
        linked.mkdir()
        _link(target, linked / store.project_id)
        unrelated = self.temp / "unrelated base"
        (unrelated / store.project_id).mkdir(parents=True)
        (unrelated / store.project_id / "my notes.txt").write_text("personal\n", encoding="utf-8")
        foreign = self.temp / "foreign base"
        (foreign / store.project_id).mkdir(parents=True)
        (foreign / store.project_id / ".boardwright-release-store").write_text(
            json.dumps({"schema": 1, "project_id": "another-project-0000000000000000"}), encoding="utf-8")
        refused = {}
        for name, root in (("linked", linked), ("unrelated", unrelated), ("foreign", foreign)):
            try:
                ReleaseStore(root / store.project_id, store.project_id).ensure()
                refused[name] = False
            except StoreError:
                refused[name] = True
        with _env({**os.environ, "BOARDWRIGHT_RELEASE_STORE": str(self.repo / "inside")}):
            try:
                ReleaseStore.for_project(self.repo, {"project": {"id": "x"}})
                refused["inside"] = False
            except StoreError:
                refused["inside"] = True
        check("[store] a linked store, unrelated files, another project's store and a store inside the project "
              "are refused; nothing written through the link", all(refused.values()) and not any(target.iterdir()),
              refused)

    # -- 4/5/8. release B ------------------------------------------------------------------
    def release_b(self) -> None:
        pcb = self.repo / PCB
        data = pcb.read_bytes()
        check("[B] silkscreen edit applies once", len(SILK.findall(data)) == 1)
        pcb.write_bytes(SILK.sub(rb"\g<1>(at 154 80.5 0)", data, count=1))
        git(self.repo, "commit", "-q", "-am", "fix(pcb): move a front silkscreen label 1 mm up")
        done = self.cli("B-candidate-rev-A", "candidate", *self.candidate_args(B))
        check("[B] a physical PCB change with revision A kept is refused before building; nothing frozen",
              done.returncode == 2 and "cannot be reused" in self.outputs["B-candidate-rev-A"]
              and not (self.store().root / "candidates" / "0.1.9").exists(), done.stderr.strip()[-250:])
        project = self.repo / ".boardwright" / "project.yaml"
        text = project.read_text(encoding="utf-8")
        check("[B] project declares board revision A", "  board_revision: A\n" in text)
        project.write_text(text.replace("  board_revision: A\n", "  board_revision: B\n", 1), encoding="utf-8", newline="\n")
        git(self.repo, "commit", "-q", "-am", "feat(pcb): board revision B for the moved silkscreen label")
        source = git(self.repo, "rev-parse", "HEAD").strip()

        done = self.cli("B-candidate", "candidate", *self.candidate_args(B), interrupt=("event", "candidate"))
        check("[B] candidate built, process killed after the freeze and before its index event",
              done.returncode == INTERRUPTED, done.stderr.strip()[-200:])
        status = self.cli("B-status-lost-event", "status", "0.1.9")
        check("[B] status names recover for the unrecorded candidate (F-R1)",
              "index event was not written" in status.stdout and "recover" in status.stdout, status.stdout[-250:])
        recover = self.cli("B-recover-event", "recover", "0.1.9")
        check("[B] recover records the candidate event without rebuilding",
              recover.returncode == 0 and "Recorded the missing candidate event" in recover.stdout)
        entry = self.candidate_dir("0.1.9")
        ledger = self.ledger_entry(entry, "0.1.9")
        fab, asm = ledger["fabrication_baseline"], ledger["assembly_baseline"]
        check("[B] new baselines: fabrication B/1, assembly B-A (re-issued, not reused)",
              (fab["id"], asm["id"]) == ("B/1", "B-A") and "reused" not in (fab["status"], asm["status"]), (fab, asm))
        identities["B"] = {"source": source, "candidate_id": entry.name}
        self.compare_b_with_a(entry)

        self.tui_session_b()
        self.signing_and_finalize_b(source)
        self.publish_b()

    def compare_b_with_a(self, entry: Path) -> None:
        self.api()
        from boardwright.output_manifest import blocking, compare_manifests, load_manifest

        a = load_manifest(self.candidate_dir("0.1.8") / ".boardwright" / "accepted_manifest.json")
        b = load_manifest(entry / ".boardwright" / "accepted_manifest.json")
        hard = blocking(compare_manifests(a, b))
        paths = sorted({d.path for d in hard})
        comparisons["B_candidate_vs_A_candidate"] = {"blocking": len(hard), "paths": paths}
        check("[B] against 0.1.8 only the front silkscreen Gerber (and the Gerber zip) differ",
              hard and all("Silk" in p or "silk" in p or p.endswith(".zip") for p in paths), paths)

    def tui_session_b(self) -> None:
        """Review and approval of B through the TUI: the same operations, explicitly confirmed."""

        record = None
        self.api()
        from boardwright import local_release_ops
        from boardwright import tui as tui_module

        async def session() -> dict:
            nonlocal record
            app = tui_module._build_textual_app()()
            app._poll_ci_status = lambda notify=False: None  # no GitHub polling in the disposable repository
            seen: dict = {}
            async with app.run_test(size=(120, 45)) as pilot:
                await pilot.pause(0.3)
                app.action_release_ci()
                await pilot.pause(0.3)
                seen["backend_screen"] = type(app.screen).__name__
                seen["github_disabled"] = app.screen.query_one("#release_backend_github").disabled
                seen["local_label"] = str(app.screen.query_one("#release_backend_local").label)
                app.screen.dismiss("local")
                await _wait(pilot, lambda: type(app.screen).__name__ == "LocalReleaseScreen")
                await pilot.pause(1.0)  # let every widget finish mounting before the screen is closed
                seen["summary"] = str(app.screen.query_one("#local_release_summary").render())
                app.screen.query_one("#local_release_close").scroll_visible(animate=False)
                await pilot.pause(0.3)
                seen["close_clicked"] = await pilot.click("#local_release_close")
                await _wait(pilot, lambda: type(app.screen).__name__ != "LocalReleaseScreen")
                await pilot.pause(0.2)
                seen["status"] = await _run(app, pilot, "status", local_release_ops.LocalArgs(version="0.1.9"))
                # The CLI at the same state, for parity (the session approves 0.1.9 below).
                seen["cli_status"] = self.cli("B-cli-status-parity", "status", "0.1.9").stdout
                seen["review"] = await _run(app, pilot, "review", local_release_ops.LocalArgs(version="0.1.9"))
                draft = Path(next(l.split(": ", 1)[1] for l in seen["review"][2].splitlines() if l.startswith("Review draft: ")))
                record = self.temp / "REVIEW-0.1.9.md"
                record.write_text(draft.read_text(encoding="utf-8").replace("- [ ]", "- [x]")
                                  .replace("APPROVED: no", "APPROVED: yes"), encoding="utf-8", newline="\n")
                seen["cancelled"] = await _run(app, pilot, "approve",
                                               local_release_ops.LocalArgs(version="0.1.9", record=record), confirm=False)
                seen["approved_after_cancel"] = (self.store().root / "approvals" / "0.1.9").exists()
                seen["approve"] = await _run(app, pilot, "approve",
                                             local_release_ops.LocalArgs(version="0.1.9", record=record))
            return seen

        cwd = os.getcwd()
        os.chdir(self.repo)
        try:
            with _env(self.env):
                seen = asyncio.run(session())
        finally:
            os.chdir(cwd)
        check("[TUI] backend screen: GitHub unavailable without a remote; local labelled not server-enforced",
              seen["backend_screen"] == "ReleaseBackendScreen" and seen["github_disabled"]
              and "not server-enforced" in seen["local_label"] and "(default)" in seen["local_label"])
        a = identities["A"]
        check("[TUI] the local release screen shows version, candidate id, source, drawing revisions and baselines",
              all(text in seen["summary"] for text in ("0.1.8: published", a["candidate_id"], a["source"],
                                                       "drawing revision C", "fabrication A/2 reused",
                                                       "not server-enforced")), seen["summary"][:400])
        check("[parity] TUI and CLI status give identical output at the same state",
              seen["status"][1] == 0 and seen["status"][2].strip() == seen["cli_status"].strip())
        check("[TUI] cancelling the approval confirmation runs nothing",
              seen["cancelled"] is None and not seen["approved_after_cancel"])
        check("[TUI] approval after explicit confirmation", seen["approve"][1] == 0
              and "not server-enforced" in seen["approve"][2], seen["approve"][2][-200:])

    def signing_and_finalize_b(self, source: str) -> None:
        key = self.temp / "signing key"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", EMAIL, "-f", str(key)], check=True)
        self.allowed = self.temp / "allowed_signers"
        self.trusted = f"{EMAIL} " + Path(str(key) + ".pub").read_text(encoding="utf-8")
        self.allowed.write_text(self.trusted, encoding="utf-8")
        for name, value in (("gpg.format", "ssh"), ("user.signingkey", str(key) + ".pub"),
                            ("gpg.ssh.allowedSignersFile", str(self.allowed))):
            git(self.repo, "config", name, value)

        done = self.cli("B-finalize", "finalize", "0.1.9", "--sign", interrupt=("checkpoint", "after-branch"))
        check("[B] signed finalisation rebuilt, verified and killed right after the branch moved",
              done.returncode == INTERRUPTED and "review gate" in done.stdout, done.stderr.strip()[-250:])
        status = self.cli("B-status-interrupted", "status", "0.1.9")
        check("[B] status: advanced, the working tree step interrupted, next recover",
              "Release 0.1.9: advanced" in status.stdout and "interrupted:" in status.stdout, status.stdout[-300:])
        check("[B] the branch moved but HEAD's files are still the source tree",
              git(self.repo, "rev-parse", "main").strip() != source and git(self.repo, "status", "--porcelain").strip())

        self.api()
        from boardwright import local_release_ops
        from boardwright import tui as tui_module

        async def recover() -> tuple:
            app = tui_module._build_textual_app()()
            app._poll_ci_status = lambda notify=False: None
            async with app.run_test(size=(120, 45)) as pilot:
                await pilot.pause(0.3)
                app._release_overview = local_release_ops.backend_overview(tui_module.load_config())
                return await _run(app, pilot, "recover", local_release_ops.LocalArgs(version="0.1.9"))

        cwd = os.getcwd()
        os.chdir(self.repo)
        try:
            with _env(self.env):
                recovered = asyncio.run(recover())
        finally:
            os.chdir(cwd)
        check("[TUI] recover (confirmed) brings the working tree to the release commit",
              recovered[1] == 0 and "Updated the working tree" in recovered[2]
              and not git(self.repo, "status", "--porcelain").strip(), recovered[2][-250:])
        unsigned = self.cli("B-finalize-unsigned", "finalize", "0.1.9")
        check("[B] resuming without --sign is refused", unsigned.returncode == 2 and "--sign" in unsigned.stderr)
        done = self.cli("B-finalize-resume", "finalize", "0.1.9", "--sign")
        check("[B] finalisation resumed without a rebuild and tagged, signed by the approver (not authenticated)",
              done.returncode == 0 and self.no_rebuild("B-finalize-resume")
              and f"signed by the approver Local Reviewer <{EMAIL}>" in done.stdout and "not authenticated" in done.stdout,
              f"{self.timings['B-finalize-resume']} s")
        commit = self.verify_finalised("B", "0.1.9", source, signed=True)
        self.signatures("0.1.9", commit)

    def signature_facts(self, version: str, repo: Path | None = None, base: Path | None = None) -> dict:
        """Boardwright's structured signature report (F-R7): validity and approver binding kept apart."""

        self.api()
        from boardwright.local_finalize import load_finalization
        from boardwright.local_release import verify_approval
        from boardwright.local_signing import release_signatures

        store = self.store(base)
        with _env(self.env):
            report = release_signatures(repo or self.repo, version, load_finalization(store, version)[1],
                                        verify_approval(store, version)[3])
        return {**report.as_dict(), "problems": report.problems}

    def signatures(self, version: str, commit: str) -> None:
        other_principal = "someone-else@example.invalid"
        facts: dict = {}

        def refused(label: str, expect: str, **kw) -> bool:
            status = self.cli(f"{label}-status", "status", version, **kw)
            final = self.cli(f"{label}-finalize", "finalize", version, "--sign", **kw)
            publish = self.cli(f"{label}-publish", "publish", version, **kw)
            return (expect in status.stdout and "PROBLEM" in status.stdout
                    and final.returncode == 2 and expect in final.stderr
                    and publish.returncode == 2 and "Nothing was published" in publish.stderr
                    and self.no_rebuild(f"{label}-finalize") and self.no_rebuild(f"{label}-publish"))

        keys = ("signed", "signature_valid", "signer_matches_approver", "authenticated")
        facts["approver key"] = self.signature_facts(version)
        check("[sign] approver's key: signature valid, signer matches approver, not authenticated",
              {k: facts["approver key"][k] for k in keys}
              == {"signed": True, "signature_valid": True, "signer_matches_approver": True, "authenticated": False},
              facts["approver key"])

        # Another allowed signer: the same valid key, listed for a different principal (F-R7).
        self.allowed.write_text(self.trusted.replace(EMAIL, other_principal, 1), encoding="utf-8")
        facts["another allowed signer"] = self.signature_facts(version)
        check("[sign] another allowed signer: cryptographically valid, signer does not match the approver",
              (facts["another allowed signer"]["signature_valid"],
               facts["another allowed signer"]["signer_matches_approver"]) == (True, False), facts["another allowed signer"])
        check("[sign] another allowed signer: status reports it; finalize and publish refuse without building",
              refused("sign-other-signer", f"listed for {other_principal}")
              and not (self.store().root / "releases" / version).exists())

        # Untrusted identity: the signing key is not an allowed signer at all.
        other = self.temp / "other key"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "other", "-f", str(other)], check=True)
        self.allowed.write_text(f"{EMAIL} " + Path(str(other) + ".pub").read_text(encoding="utf-8"), encoding="utf-8")
        facts["key not allowed"] = self.signature_facts(version)
        check("[sign] key not allowed: cryptographically valid, signer does not match the approver",
              (facts["key not allowed"]["signature_valid"], facts["key not allowed"]["signer_matches_approver"])
              == (True, False), facts["key not allowed"])
        check("[sign] key not allowed: status reports it; finalize and publish refuse without building",
              refused("sign-untrusted", "listed for no principal")
              and not (self.store().root / "releases" / version).exists())

        self.allowed.write_text(self.trusted, encoding="utf-8")
        check("[sign] the approver's allowed-signers entry is restored and binds again",
              self.signature_facts(version)["signer_matches_approver"] is True)

        # Corrupted signature, on a disposable clone and a copy of the store.
        forged_repo = self.temp / "forged signature repo"
        subprocess.run(["git", "clone", "-q", "--no-hardlinks", str(self.repo), str(forged_repo)], check=True, env=self.env)
        git(forged_repo, "remote", "remove", "origin")
        for name in ("gpg.format", "user.signingkey", "gpg.ssh.allowedSignersFile", "user.name", "user.email"):
            git(forged_repo, "config", name, git(self.repo, "config", name).strip())
        git(forged_repo, "update-ref", f"refs/boardwright/local-release/{version}", commit)
        raw = git(forged_repo, "cat-file", "tag", version)
        head, sep, signature = raw.partition("-----BEGIN SSH SIGNATURE-----")
        lines = signature.split("\n")
        middle = len(lines) // 2
        lines[middle] = lines[middle][:10] + ("A" if lines[middle][10] != "A" else "B") + lines[middle][11:]
        forged = subprocess.run(["git", "mktag"], cwd=forged_repo, input=(head + sep + "\n".join(lines)).encode("utf-8"),
                                capture_output=True)
        old = git(forged_repo, "rev-parse", f"refs/tags/{version}").strip()
        if check("[sign] a tag with a corrupted signature can be constructed", forged.returncode == 0,
                 forged.stderr.decode()[-200:]):
            git(forged_repo, "update-ref", f"refs/tags/{version}", forged.stdout.decode().strip(), old)
            store_copy = self.temp / "forged store"
            shutil.copytree(self.store_base, store_copy)
            facts["corrupted signature"] = self.signature_facts(version, forged_repo, store_copy)
            check("[sign] corrupted signature: not cryptographically valid, signer does not match",
                  (facts["corrupted signature"]["signature_valid"], facts["corrupted signature"]["signer_matches_approver"])
                  == (False, False), facts["corrupted signature"])
            check("[sign] corrupted signature: status reports it; finalize and publish refuse without building",
                  refused("sign-invalid", "not cryptographically valid", cwd=forged_repo, store=store_copy))

        facts["unsigned release A"] = self.signature_facts("0.1.8")
        status = self.cli("sign-unsigned-status", "status", "0.1.8")
        check("[sign] the unsigned release A keeps its unauthenticated labels",
              {k: facts["unsigned release A"][k] for k in keys}
              == {"signed": False, "signature_valid": None, "signer_matches_approver": None, "authenticated": False}
              and "signature: unsigned (local approval, unauthenticated)" in status.stdout, facts["unsigned release A"])
        comparisons["signatures"] = facts

    def publish_b(self) -> None:
        done = self.cli("B-publish", "publish", "0.1.9", interrupt=("checkpoint", "after-store"))
        check("[B] publication built and verified, process killed after the store write and before its event",
              done.returncode == INTERRUPTED and "outputs match the tagged accepted manifest" in done.stdout,
              done.stderr.strip()[-250:])
        status = self.cli("B-status-publish-interrupted", "status", "0.1.9")
        check("[B] status reports the unrecorded publication",
              "published entry is complete but its index event was not written" in status.stdout, status.stdout[-250:])
        recover = self.cli("B-recover-publish", "recover", "0.1.9")
        again = self.cli("B-publish-again", "publish", "0.1.9")
        check("[B] recover records it; publish then reports it published without rebuilding",
              recover.returncode == 0 and again.returncode == 0 and "already published" in again.stdout
              and self.no_rebuild("B-publish-again"))
        self.verify_published("B", "0.1.9")

    # -- final state -------------------------------------------------------------------------
    def final(self) -> None:
        status = self.cli("final-status", "status")
        check("final: index intact, both releases published, nothing modified",
              "Index: intact" in status.stdout and "Release 0.1.8: published" in status.stdout
              and "Release 0.1.9: published" in status.stdout and "MODIFIED" not in status.stdout
              and "PROBLEM" not in status.stdout, status.stdout[-400:])
        events = [json.loads(l) for l in (self.store().root / "index.jsonl").read_text(encoding="utf-8").splitlines()]
        counts = {v: [e["event"] for e in events if e.get("version") == v] for v in ("0.1.8", "0.1.9")}
        lifecycle = ["candidate", "approval", "prepared", "imported", "advanced", "tagged", "published"]
        check("final: each version has each transition exactly once, in order",
              all(c == lifecycle for c in counts.values()), counts)
        comparisons["store_events"] = {"total": len(events), "recovered": sum(1 for e in events if e.get("recovered"))}
        check("final: ledger is append-only (0.1.7, 0.1.8, 0.1.9)",
              [str(r["version"]) for r in self.ledger_entries()][-3:] == ["0.1.7", "0.1.8", "0.1.9"])
        log = git(self.repo, "log", "--format=%s", f"{identities['A']['source']}..main").splitlines()
        check("final: history source <- release 0.1.8 <- two developer commits <- release 0.1.9",
              log == ["release: prepare 0.1.9 (RELEASED)", "feat(pcb): board revision B for the moved silkscreen label",
                      "fix(pcb): move a front silkscreen label 1 mm up", "release: prepare 0.1.8 (RELEASED)"], log)
        self.read_only("final")
        after = (file_hashes(self.checkout), git(self.checkout, "rev-parse", "HEAD"),
                 git(self.checkout, "for-each-ref", "--format=%(refname) %(objectname)"),
                 git(self.checkout, "status", "--porcelain", "--untracked-files=all"))
        names = ("source files", "HEAD", "branches and tags", "status")
        check("this checkout is unchanged: " + ", ".join(names),
              after == self.checkout_before, [n for n, a, b in zip(names, after, self.checkout_before) if a != b])
        check("this checkout has no 0.1.8 or 0.1.9 tag", not git(self.checkout, "tag", "-l", "0.1.8", "0.1.9").strip())

    def ledger_entries(self) -> list:
        import yaml

        return yaml.safe_load((self.repo / ".boardwright" / "releases.yaml").read_text(encoding="utf-8"))["releases"]


# -- small utilities ---------------------------------------------------------------------------

class _env:
    def __init__(self, values: dict) -> None:
        self.values = values

    def __enter__(self):
        self.saved = dict(os.environ)
        os.environ.clear()
        os.environ.update(self.values)

    def __exit__(self, *exc):
        os.environ.clear()
        os.environ.update(self.saved)


def _link(target: Path, link: Path) -> None:
    """A directory symbolic link, or a junction where Windows refuses symbolic links."""

    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            raise
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)


def _flip(path: Path, text: bool = False) -> None:
    data = bytearray(path.read_bytes())
    index = len(data) // 2
    if text:
        while chr(data[index]) not in "0123456789abcdef":
            index += 1
        data[index] = ord("0") if chr(data[index]) != "0" else ord("1")
    else:
        data[index] ^= 0x01
    path.write_bytes(bytes(data))


def _replace(path: Path, old: str, new: str) -> None:
    text = path.read_bytes().decode("utf-8")
    assert old in text, (path, old)
    path.write_bytes(text.replace(old, new, 1).encode("utf-8"))


async def _wait(pilot, condition, seconds: float = 30.0) -> None:
    for _ in range(int(seconds / 0.1)):
        if condition():
            return
        await pilot.pause(0.1)
    raise AssertionError("TUI condition not reached")


async def _run(app, pilot, operation: str, args, *, confirm: bool = True, seconds: float = 900.0):
    """One TUI operation: request it, confirm (or cancel) when asked, wait, return (op, status, output)."""

    app.last_local_release = None
    app._local_release_request((operation, args))
    await pilot.pause(0.2)
    if type(app.screen).__name__ == "ConfirmOperationScreen":
        app.screen.dismiss(confirm)
        await pilot.pause(0.2)
        if not confirm:
            return None
    await _wait(pilot, lambda: app.last_local_release is not None and not getattr(app, "_release_running", False),
                seconds)
    while type(app.screen).__name__ == "LocalReleaseReportScreen":
        app.screen.dismiss(None)
        await pilot.pause(0.1)
    return app.last_local_release


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--temp", required=True, type=Path)
    parser.add_argument("--held-candidate", type=Path, default=None)
    parser.add_argument("--held-run", default=None, help="run ID of the held candidate (recorded only)")
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    sys.dont_write_bytecode = True  # the disposable repository and this checkout stay byte-for-byte unchanged
    args.temp.mkdir(parents=True, exist_ok=True)
    harness = Harness(Path.cwd(), args.temp, args.held_candidate, args.held_run)
    started = time.perf_counter()
    try:
        harness.setup()
        harness.backend_and_plan()
        harness.read_only("before")
        harness.release_a()
        harness.release_b()
        harness.final()
    except Exception as exc:  # recorded as a failure; the results are still written
        import traceback

        traceback.print_exc()
        check(f"harness completed without an exception ({type(exc).__name__})", False, str(exc))
    harness.timings["total"] = round(time.perf_counter() - started, 1)
    args.out.write_text(json.dumps({"timings_s": harness.timings, "identities": identities, "comparisons": comparisons,
                                    "results": results}, indent=2, default=str), encoding="utf-8")
    failed = [r["check"] for r in results if not r["ok"]]
    print(f"{len(results) - len(failed)}/{len(results)} checks passed; timings {harness.timings}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
