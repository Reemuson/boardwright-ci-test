# SPDX-License-Identifier: LicenseRef-Boardwright-Source-Available
# Copyright (c) Ryan Hicks
"""Non-publishing integration check of local-first previews (testbench only).

Runs the installed Boardwright CLI with real Docker in a disposable clone of
this checkout (under a path with spaces and non-ASCII characters):

- clean and dirty DRAFT and CHECKED previews: source identity, output
  freshness, evidence markers, DRC/ERC reporting, working tree unchanged;
- a preview refused while another process holds the stage lock;
- the dirty CHECKED preview against a direct ``container build`` of the same
  inputs;
- local preview evidence refused by compare, the review gate and the
  reviewed-preview marker; no workflow reads local preview evidence;
- the existing release preparation (``container candidate``, a local
  rehearsal that never commits, pushes or tags), compared with the held
  0.1.8 candidate.

Nothing is pushed, tagged, approved or published: the workflow token is
read-only and the disposable clone's push URL is disabled.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

results: list[dict] = []
EDIT_FROM, EDIT_TO = b'(text "+??V"', b'(text "+12V"'
SCHEMATIC = "Block Diagram.kicad_sch"


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append({"check": name, "ok": bool(ok), "detail": str(detail)[:600]})
    print(("PASS " if ok else "FAIL ") + name + (f" - {detail}" if detail else ""), flush=True)
    return bool(ok)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, encoding="utf-8", check=True).stdout


def snapshot(root: Path) -> dict[str, str]:
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for name in filenames:
            path = Path(dirpath) / name
            out[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


class Harness:
    def __init__(self, checkout: Path, temp: Path) -> None:
        self.checkout = checkout
        self.temp = temp
        self.project = temp / "preview é project" / "FIC board"
        self.env = {**os.environ, "PYTHONPATH": str(checkout / "src"), "PYTHONIOENCODING": "utf-8",
                    "BOARDWRIGHT_STAGE_ROOT": str(temp / "stages é"),
                    "BOARDWRIGHT_PREVIEW_INDEX": str(temp / "preview index"),
                    "BOARDWRIGHT_3D_CACHE": str(temp / "cache 3d")}
        self.timings: dict[str, float] = {}

    def setup(self) -> None:
        self.project.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "--quiet", "--no-hardlinks", str(self.checkout), str(self.project)], check=True)
        git(self.project, "remote", "set-url", "--push", "origin", "DISABLED-no-push")
        for key, value in (("user.name", "integration"), ("user.email", "integration@example.invalid")):
            git(self.project, "config", key, value)
        check("disposable clone is at the checked-out commit",
              git(self.project, "rev-parse", "HEAD") == git(self.checkout, "rev-parse", "HEAD"))
        check("disposable clone cannot push", git(self.project, "remote", "get-url", "--push", "origin").strip() == "DISABLED-no-push")

    def cli(self, label: str, *args: str) -> subprocess.CompletedProcess:
        start = time.perf_counter()
        done = subprocess.run([sys.executable, "-m", "boardwright", *args], cwd=self.project, env=self.env,
                              capture_output=True, text=True, encoding="utf-8", errors="replace")
        self.timings[label] = round(time.perf_counter() - start, 1)
        (self.temp / f"{label}.log").write_text(done.stdout + "\n--- stderr ---\n" + done.stderr, encoding="utf-8")
        return done

    def plan_digest(self) -> str:
        sys.path.insert(0, str(self.checkout / "src"))
        from boardwright.local_preview import plan_source

        return plan_source(self.project).source_digest

    def index(self, variant: str) -> dict:
        directory = Path(self.env["BOARDWRIGHT_PREVIEW_INDEX"])
        found = list(directory.glob(f"*/{variant}-k9.json"))
        return json.loads(found[0].read_text(encoding="utf-8")) if found else {}

    def preview(self, label: str, variant: str, *, dirty: bool) -> dict:
        before = snapshot(self.project)
        digest = self.plan_digest()
        started = time.time()
        done = self.cli(label, "preview", "-v", variant)
        check(f"[{label}] preview completed (exit 0)", done.returncode == 0, done.stderr.strip()[-300:])
        check(f"[{label}] working tree byte-identical", before == snapshot(self.project), f"{len(before)} files")
        record = self.index(variant)
        source = record.get("source", {})
        stage = Path(record.get("outputs", "/nonexistent"))
        check(f"[{label}] non-release record", (record.get("purpose"), record.get("release_evidence"), record.get("backend"))
              == ("preview", False, "local"))
        check(f"[{label}] dirty flag and identity", source.get("dirty") is dirty and (("dirty" in record.get("identity", "")) is dirty),
              record.get("identity"))
        check(f"[{label}] source digest equals the pre-run plan", source.get("source_digest") == digest, digest[:16])
        check(f"[{label}] staged tree is the stage's HEAD tree",
              stage.is_dir() and source.get("staged_tree") == git(stage, "rev-parse", "HEAD^{tree}").strip(),
              source.get("staged_tree", "")[:12])
        check(f"[{label}] base commit is the project HEAD", source.get("base_commit") == git(self.project, "rev-parse", "HEAD").strip())
        cleared = {item["path"]: item for item in record.get("cleared_outputs", [])}
        check(f"[{label}] tracked generated files cleared from the stage",
              cleared.get("README.md", {}).get("tracked") and cleared.get("assets/renders", {}).get("tracked"), sorted(cleared))
        renders = list((stage / "assets" / "renders").glob("*.png"))
        check(f"[{label}] renders regenerated by this run",
              renders and all(p.stat().st_mtime >= started for p in renders)
              and not git(stage, "ls-files", "assets/renders", "README.md").strip(), f"{len(renders)} render(s)")
        check(f"[{label}] build output newer than the run start",
              (stage / "README.md").is_file() and (stage / "README.md").stat().st_mtime >= started)
        if dirty:
            included = {c["path"]: c["sha256"] for c in source.get("included", [])}
            edited = hashlib.sha256((self.project / SCHEMATIC).read_bytes()).hexdigest()
            check(f"[{label}] only the saved schematic edit is included", list(included) == [SCHEMATIC], sorted(included))
            check(f"[{label}] recorded hash is the edited file's bytes", included.get(SCHEMATIC) == edited)
            check(f"[{label}] staged schematic carries the edit", EDIT_TO in (stage / SCHEMATIC).read_bytes())
            excluded = {e["path"]: e["reason"] for e in source.get("excluded", [])}
            check(f"[{label}] unrelated and credential files excluded",
                  excluded.get("notes.txt") == "untracked and not a KiCad project file"
                  and excluded.get(".env") == "looks like a credential", excluded)
            check(f"[{label}] excluded files absent from the stage",
                  not (stage / "notes.txt").exists() and not (stage / ".env").exists())
        rules = record.get("design_rules", {})
        if variant == "CHECKED":
            drc, erc = rules.get("drc", [{}])[0], rules.get("erc", [{}])[0]
            check(f"[{label}] DRC 28 violations: 1 error, 26 warnings, 1 excluded",
                  (drc.get("violations"), drc.get("errors"), drc.get("warnings"), drc.get("excluded")) == (28, 1, 26, 1), drc)
            check(f"[{label}] ERC 0 errors, 4 warnings", (erc.get("errors"), erc.get("warnings")) == (0, 4), erc)
            check(f"[{label}] KiBot reported 1 DRC error", rules.get("kibot_drc_errors") == 1, rules.get("kibot_drc_errors"))
            check(f"[{label}] CLI separates build and design rules",
                  "Build: completed, outputs generated" in done.stdout and "design-rule errors" in done.stdout)
        else:
            check(f"[{label}] DRAFT design rules unknown, never zero",
                  rules.get("drc", [{}])[0].get("status") == "unknown" and "design rules unknown" in done.stdout, rules)
        self.markers(label, stage)
        return record

    def markers(self, label: str, stage: Path) -> None:
        evidence = stage / "boardwright-build"
        stage_marker = json.loads((stage / ".git" / "boardwright-stage").read_text(encoding="utf-8"))
        built = json.loads((evidence / "record.json").read_text(encoding="utf-8"))
        preview_json = json.loads((evidence / "preview.json").read_text(encoding="utf-8"))
        check(f"[{label}] three preview markers present",
              stage_marker.get("purpose") == "preview" and built.get("stage", {}).get("purpose") == "preview"
              and preview_json.get("release_evidence") is False)
        done = subprocess.run([sys.executable, "-m", "boardwright.container", "compare", str(stage), str(stage)],
                              cwd=self.project, env=self.env, capture_output=True, text=True)
        check(f"[{label}] container compare refuses preview evidence",
              done.returncode != 0 and "local preview evidence" in done.stdout, done.stdout.strip()[-200:])

    def edit(self) -> None:
        path = self.project / SCHEMATIC
        data = path.read_bytes()
        check("schematic edit applies once", data.count(EDIT_FROM) >= 1)
        path.write_bytes(data.replace(EDIT_FROM, EDIT_TO, 1))
        (self.project / "notes.txt").write_text("scratch notes\n", encoding="utf-8")
        (self.project / ".env").write_text("SECRET=not-real\n", encoding="utf-8")

    def revert(self) -> None:
        git(self.project, "checkout", "--", SCHEMATIC)
        (self.project / "notes.txt").unlink()
        (self.project / ".env").unlink()
        check("project clean again", not git(self.project, "status", "--porcelain").strip())

    def lock(self) -> None:
        stage = Path(self.index("CHECKED")["outputs"])
        index_before = json.dumps(self.index("CHECKED"), sort_keys=True)
        holder = subprocess.Popen([sys.executable, "-c",
                                   "import sys,time;from pathlib import Path;from boardwright.stage_lock import stage_lock\n"
                                   f"with stage_lock(Path({str(stage)!r}), purpose='integration holder'):\n"
                                   "    print('locked', flush=True); time.sleep(600)"],
                                  env=self.env, stdout=subprocess.PIPE, text=True)
        try:
            check("second process holds the stage lock", holder.stdout.readline().strip() == "locked")
            done = self.cli("locked-checked", "preview", "-v", "CHECKED")
            check("preview refused while the stage is locked",
                  done.returncode == 1 and "in use by" in done.stderr and "integration holder" in done.stderr,
                  done.stderr.strip()[-250:])
            check("refused preview left the index unchanged", json.dumps(self.index("CHECKED"), sort_keys=True) == index_before)
        finally:
            holder.kill()
            holder.wait()
        done = self.cli("status", "preview", "--status")
        check("status lists both previews as current",
              "CHECKED (KiCad 9): current" in done.stdout and "DRAFT (KiCad 9): current" in done.stdout, done.stdout[:300])

    def direct_build(self, preview_record: dict) -> None:
        sys.path.insert(0, str(self.checkout / "src"))
        from boardwright.output_manifest import blocking, compare_manifests, load_manifest

        done = subprocess.run([sys.executable, "-m", "boardwright.container", "build", "--variant", "CHECKED", "--worktree"],
                              cwd=self.project, env=self.env, capture_output=True, text=True, errors="replace")
        check("direct container build completed", done.returncode == 0, done.stderr.strip()[-300:])
        direct = next(Path(self.env["BOARDWRIGHT_STAGE_ROOT"]).glob("*-checked-k9-worktree"))
        preview = Path(preview_record["outputs"])
        check("direct worktree build excluded .env and notes.txt (C1)",
              not (direct / ".env").exists() and not (direct / "notes.txt").exists()
              and ".env" not in git(direct, "ls-files") and "notes.txt" not in git(direct, "ls-files"))
        check("direct worktree build is marked as a non-release worktree build",
              json.loads((direct / ".git" / "boardwright-stage").read_text(encoding="utf-8")).get("purpose") == "worktree-build"
              and not (direct / "boardwright-build" / "preview.json").exists())
        pm = load_manifest(preview / "boardwright-build" / "manifest.json")
        dm = load_manifest(direct / "boardwright-build" / "manifest.json")
        differences = compare_manifests(dm, pm)
        check("preview vs direct build: controlled outputs equivalent",
              not blocking(differences) and set(pm["files"]) == set(dm["files"]),
              f"{len(pm['files'])} entries, {len(differences)} normalised notes, {len(blocking(differences))} blocking")
        tree = lambda root: {line.split("\t")[1]: line.split()[2] for line in git(root, "ls-tree", "-r", "HEAD").splitlines()}
        a, b = tree(direct), tree(preview)
        only = sorted(set(a) ^ set(b)) + sorted(p for p in set(a) & set(b) if a[p] != b[p])
        generated = ("README.md", "assets/renders/")
        check("staged sources identical apart from cleared generated files",
              all(p == "README.md" or p.startswith(generated[1]) for p in only), only)

    def approval(self, preview_record: dict) -> None:
        sys.path.insert(0, str(self.checkout / "src"))
        from boardwright.errors import BoardwrightError
        from boardwright.preview import PreviewState, mark_preview_reviewed

        # Structural boundary: an approved run accepts only a successful
        # prepare-release review-hold run of the current main, and takes the
        # candidate artifact from that run. Run the approved path's own gate
        # script against the only GitHub run this exercise has: this one.
        import yaml

        prepare_yaml = yaml.safe_load((self.checkout / ".github" / "workflows" / "prepare-release.yaml").read_text(encoding="utf-8"))
        gate = next(s["run"] for j in prepare_yaml["jobs"].values() for s in j["steps"]
                    if s.get("name") == "Check approved review run")
        run_id = os.environ.get("GITHUB_RUN_ID", "")
        if run_id:
            env = {**os.environ, "IN_APPROVED_REVIEW_RUN": run_id,
                   "BOARDWRIGHT_SOURCE_SHA": git(self.checkout, "rev-parse", "HEAD").strip()}
            done = subprocess.run(["bash", "-c", gate], env=env, capture_output=True, text=True)
            check("approval gate refuses this preview run as approved_review_run",
                  done.returncode != 0 and "is not a prepare-release run" in done.stderr, done.stderr.strip()[-200:])
            artifacts = subprocess.run(["gh", "api", f"repos/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{run_id}/artifacts",
                                        "--jq", "[.artifacts[].name]"], env=os.environ, capture_output=True, text=True)
            names = json.loads(artifacts.stdout or "[]")
            check("this run holds no release-candidate artifact", artifacts.returncode == 0
                  and not any(n.startswith("boardwright-release-candidate-") for n in names), names)
        else:
            check("approval gate check needs GITHUB_RUN_ID (GitHub only)", False, "not running on GitHub Actions")
        stage = Path(preview_record["outputs"])
        check("a preview stage has no review candidate of its own",
              not (stage / "boardwright-build" / "review_candidate.json").exists()
              and json.loads((stage / ".git" / "boardwright-stage").read_text(encoding="utf-8")).get("purpose") == "preview")
        try:
            mark_preview_reviewed(type("C", (), {"root": self.project})(),
                                  PreviewState("ready", "boardwright-preview-CHECKED", "x", run=None))
            refused = False
        except BoardwrightError:
            refused = True
        check("reviewed-preview (CHECKED acceptance) marker needs a GitHub run", refused)
        workflows = {p.name: p.read_text(encoding="utf-8") for p in (self.checkout / ".github" / "workflows").glob("*.yaml")}
        readers = [name for name, text in workflows.items() if name not in ("preview-integration.yaml",)
                   and any(k in text for k in ("preview.json", "BOARDWRIGHT_STAGE_ROOT", "BOARDWRIGHT_PREVIEW_INDEX",
                                               "local_preview", ".boardwright/stages", ".boardwright/previews"))]
        check("no GitHub workflow reads local preview evidence", not readers, readers)
        prepare = workflows["prepare-release.yaml"]
        check("approved runs take the candidate from the reviewed GitHub run",
              "approved_review_run" in prepare and "Download reviewed candidate" in prepare)

    def candidate(self, held: Path) -> None:
        sys.path.insert(0, str(self.checkout / "src"))
        from boardwright.output_manifest import blocking, compare_manifests, load_manifest

        heads_before = git(self.project, "for-each-ref", "--format=%(refname) %(objectname)")
        done = subprocess.run([sys.executable, "-m", "boardwright.container", "candidate", "0.1.8", "--variant", "RELEASED",
                               "--kind", "draft", "--drawing-revision", "C",
                               "--revision-description", "Revision note labelled SCH REV", "--change-class", "tooling",
                               "--justification", "Integration rehearsal of release preparation; never committed or tagged."],
                              cwd=self.project, env=self.env, capture_output=True, text=True, errors="replace")
        (self.temp / "candidate.log").write_text(done.stdout + "\n--- stderr ---\n" + done.stderr, encoding="utf-8")
        check("release preparation rehearsal completed", done.returncode == 0, done.stderr.strip()[-300:])
        check("rehearsal reused the 0.1.7 baselines",
              "fabrication baseline A/2 (reused), assembly baseline A (reused)" in done.stdout)
        check("rehearsal changed no refs in the project", git(self.project, "for-each-ref", "--format=%(refname) %(objectname)") == heads_before)
        stage = next(Path(self.env["BOARDWRIGHT_STAGE_ROOT"]).glob("*-candidate-0.1.8"))
        mine = load_manifest(stage / ".boardwright" / "accepted_manifest.json")
        theirs = load_manifest(held / ".boardwright" / "accepted_manifest.json")
        differences = compare_manifests(theirs, mine)
        check("rehearsal outputs equivalent to the held 0.1.8 candidate", not blocking(differences),
              f"{len(differences)} normalised notes, {len(blocking(differences))} blocking")
        a = json.loads((stage / ".boardwright" / "review_candidate.json").read_text(encoding="utf-8"))["ledger_entry"]
        b = json.loads((held / ".boardwright" / "review_candidate.json").read_text(encoding="utf-8"))["ledger_entry"]
        same = {k: a[k] == b[k] for k in ("identities", "fabrication_baseline", "assembly_baseline", "change_classes", "previous_version")}
        check("rehearsal ledger identities equal the held candidate's", all(same.values()), same)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--temp", required=True, type=Path)
    parser.add_argument("--held-candidate", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--quick", action="store_true", help="DRAFT, markers, lock and approval only (local dry run)")
    args = parser.parse_args()
    harness = Harness(Path.cwd(), args.temp)
    harness.setup()
    harness.preview("clean-draft", "DRAFT", dirty=False)
    if not args.quick:
        harness.preview("clean-checked", "CHECKED", dirty=False)
    harness.edit()
    harness.preview("dirty-draft", "DRAFT", dirty=True)
    checked = harness.preview("dirty-checked", "CHECKED", dirty=True) if not args.quick else harness.index("DRAFT")
    if not args.quick:
        harness.lock()
        harness.direct_build(checked)
    harness.approval(checked)
    harness.revert()
    if not args.quick:
        harness.candidate(args.held_candidate)
    args.out.write_text(json.dumps({"timings_s": harness.timings, "results": results}, indent=2), encoding="utf-8")
    failed = [r["check"] for r in results if not r["ok"]]
    print(f"{len(results) - len(failed)}/{len(results)} checks passed; timings {harness.timings}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
