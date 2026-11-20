# SPDX-License-Identifier: LicenseRef-Boardwright-Source-Available
# Copyright (c) Ryan Hicks

"""The output build: one pipeline for local Docker and GitHub Actions.

Every workflow and every local container build runs this module instead of a
copy of the steps in YAML. It runs in the working tree it is given, which must
be a disposable checkout (a CI checkout or a ``boardwright container stage``
copy), never the developer's tree.

Stages, in order:

1. Preflight: refuse ``.boardwright/local.yaml`` (local overrides never reach a
   controlled build), refuse a working tree whose text files were checked out
   with CRLF, record the source state, install the bundled fonts, and run the
   drawing-sheet and logo check. All of this happens before KiBot.
2. KiBot table sources (all variants) and PDF preparation (not ``DRAFT``).
3. The variant's KiBot output set, then the fabrication and assembly drawing
   regeneration with the generated document configs (not ``DRAFT``).
4. Combined review PDF, silkscreen verification, generated-output cleanup.
5. Source side effects: every tracked file the build rewrote is restored, and
   the build fails if anything outside ``--keep`` is still modified.
6. ``boardwright-build/manifest.json`` and ``boardwright-build/record.json``:
   the controlled-output manifest and the build identity (source, image, tool
   versions), used to compare a local build with a GitHub build.

KiBot is called directly. The KiBot GitHub action's entrypoint also runs
``kicad-git-filters.py``, which rewrote ``.gitattributes`` and ``.git/config``
during every CI build (audit F16); calling ``kibot`` avoids it.

Usage::

    python -m boardwright build --variant CHECKED [--log-prefix main] [--keep PATH ...]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

VARIANTS = ("DRAFT", "PRELIMINARY", "CHECKED", "RELEASED")
KIBOT_CONFIG = "boardwright_resources/kibot/yaml/kibot_main.yaml"
FABRICATION_DOCUMENT_CONFIG = "boardwright_resources/kibot/yaml/kibot_document_fabrication.generated.yaml"
ASSEMBLY_DOCUMENT_CONFIG = "boardwright_resources/kibot/yaml/kibot_document_assembly.generated.yaml"
SCRIPTS = "boardwright_resources/kibot/resources/scripts"
FONTS = "boardwright_resources/kibot/resources/fonts"
SKIP_CHECKS = "draw_fancy_stackup,erc,drc"
SUPPORTED_KICAD = ("9", "10")
BUILD_DIR = Path("boardwright-build")
LOCAL_OVERRIDES = Path(".boardwright") / "local.yaml"
# Tracked files that are themselves generated outputs (the README snapshot and
# renders committed by the accepted-outputs workflow). Never restored.
GENERATED_TRACKED = ("README.md", "assets/renders")
# Python packages Boardwright needs that the KiBot image lacks, pinned by hash.
# Installed into the container-private home, never into the image or the host.
CONTAINER_REQUIREMENTS = (
    "pypdf==6.21.4 --hash=sha256:33e4fb41ca352810eb4e9838aa0d82d6888a5f8bf682f43e99cce5faa666255c\n"
)


class BuildError(RuntimeError):
    pass


@dataclass(frozen=True)
class Step:
    name: str
    argv: tuple[str, ...]


@dataclass
class BuildPlan:
    variant: str
    kicad: str
    steps: list[Step] = field(default_factory=list)


def _python(*args: str) -> tuple[str, ...]:
    return (sys.executable, *args)


def _kibot(config: str, variant: str, log: str, targets: tuple[str, ...], skip: str = "") -> tuple[str, ...]:
    argv = ["kibot", "-c", config, "-d", ".", "-g", f"variant={variant}"]
    if skip:
        argv += ["--skip-pre", skip]
    return (*argv, "--log", log, *targets)


def plan_build(variant: str, kicad: str, log_prefix: str = "build") -> BuildPlan:
    """Return the ordered steps for one variant. Pure: no side effects."""

    if variant not in VARIANTS:
        raise BuildError(f"Unsupported variant {variant!r}; expected one of {', '.join(VARIANTS)}.")
    if kicad not in SUPPORTED_KICAD:
        raise BuildError(f"Unsupported KiCad major version {kicad!r}; expected 9 or 10.")
    log = f"kibot_{log_prefix}"
    full = variant != "DRAFT"
    plan = BuildPlan(variant, kicad)
    add = plan.steps.append

    add(Step("Check drawing sheets and branding", _python("-m", "boardwright.drawing_sheets", "check")))
    if full:
        add(Step("Generate revision table variables", _python("-m", "boardwright", "revision-history")))
        add(Step("Prepare PDF table placeholders", _python(f"{SCRIPTS}/prepare_pcb_tables.py", ".")))
        add(Step("KiBot PDF table CSV sources", _kibot(KIBOT_CONFIG, variant, f"{log}_tables.log", ("table_sources",), SKIP_CHECKS)))
        add(Step("Prune empty conditional PDF pages", _python(f"{SCRIPTS}/prune_pdf_pages.py", ".")))
        add(Step("Prepare combined PDF contents", _python(f"{SCRIPTS}/prepare_combined_pdf_native.py", ".")))

    else:
        # KiBot's ``report`` output resolves every output's path for the README
        # ``${..._outpath}`` variables. That configures ``pdf_fabrication``,
        # whose ``include_table`` refuses to load without the drill-table and
        # test-point CSVs, so DRAFT generates them too.
        add(Step("KiBot PDF table CSV sources", _kibot(KIBOT_CONFIG, variant, f"{log}_tables.log", ("table_sources",), SKIP_CHECKS)))

    group = f"all_group_k{kicad}"
    if variant == "DRAFT":
        main = _kibot(KIBOT_CONFIG, variant, f"{log}.log", ("draft_group", "md_readme"), SKIP_CHECKS)
    elif variant == "PRELIMINARY":
        main = _kibot(KIBOT_CONFIG, variant, f"{log}.log", (group,), "erc,drc")
    else:
        main = _kibot(KIBOT_CONFIG, variant, f"{log}.log", (group,))
    add(Step(f"KiBot {variant} outputs", main))

    if full:
        add(Step(
            "Regenerate fabrication drawing",
            _kibot(FABRICATION_DOCUMENT_CONFIG, variant, f"{log}_fabrication_document.log", ("pdf_fabrication", "zip_compress_fab"), SKIP_CHECKS),
        ))
        add(Step(
            "Regenerate assembly drawing",
            _kibot(ASSEMBLY_DOCUMENT_CONFIG, variant, f"{log}_assembly_document.log", ("pdf_assembly",), SKIP_CHECKS),
        ))
    add(Step("Create combined review PDF", _python(f"{SCRIPTS}/combine_review_pdf.py", ".")))
    add(Step("Verify silkscreen Gerbers against the board", _python("-m", "boardwright.silkscreen")))
    add(Step("Clean generated outputs", _python(f"{SCRIPTS}/clean_generated_outputs.py", ".")))
    return plan


# --- preflight -------------------------------------------------------------


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)
    if completed.returncode != 0:
        raise BuildError(f"git {' '.join(args)} failed: {completed.stderr.strip()}")
    return completed.stdout


def crlf_checkout_problems(eol_listing: str) -> list[str]:
    """Tracked text files stored with LF but checked out with CRLF.

    ``git ls-files --eol`` output. A Windows checkout with ``core.autocrlf``
    produces these; KiCad and KiBot would then read different bytes than a
    Linux CI checkout. Files whose attributes request CRLF are allowed.
    """

    problems = []
    for line in eol_listing.splitlines():
        info, _, path = line.partition("\t")
        fields = info.split()
        if len(fields) >= 3 and fields[0] == "i/lf" and fields[1] in ("w/crlf", "w/mixed") and "eol=crlf" not in info:
            problems.append(path)
    return problems


def preflight(root: Path) -> dict[str, object]:
    """Checks that must pass before any tool runs. Returns the source state."""

    if (root / LOCAL_OVERRIDES).exists():
        raise BuildError(
            f"{LOCAL_OVERRIDES.as_posix()} exists in the build tree. Local overrides never enter a controlled "
            "build; build from a clean checkout (boardwright container build stages one)."
        )
    if not (root / ".git").exists():
        raise BuildError("The build tree is not a Git checkout; source identity cannot be recorded.")
    crlf = crlf_checkout_problems(_git(root, "ls-files", "--eol"))
    if crlf:
        shown = "\n  ".join(crlf[:10]) + (f"\n  ... {len(crlf) - 10} more" if len(crlf) > 10 else "")
        raise BuildError(
            "Tracked text files are checked out with CRLF line endings (Windows core.autocrlf):\n  "
            f"{shown}\nBuild from a staged LF checkout: python -m boardwright container build."
        )
    modified = [p for p in _git(root, "diff", "--name-only", "HEAD").splitlines() if p]
    preparation = None
    if modified:
        from .release_preparation import verify_preparation

        # Edits made by release --prepare carry a content record; a prepared
        # tree changed afterwards is refused before any tool runs.
        preparation = verify_preparation(root, modified)
        if preparation and preparation["problems"]:
            raise BuildError("Prepared release sources were modified: " + "; ".join(preparation["problems"]))
    return {
        "commit": _git(root, "rev-parse", "HEAD").strip(),
        "modified_before_build": modified,
        "release_preparation": preparation,
    }


def install_fonts(root: Path) -> None:
    """Make the bundled fonts visible to KiCad and the PDF tools (user font dir)."""

    source = root / FONTS
    if not source.is_dir():
        return
    target = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "fonts"
    target.mkdir(parents=True, exist_ok=True)
    for font in sorted(source.glob("*.ttf")):
        shutil.copyfile(font, target / font.name)
    if shutil.which("fc-cache"):
        subprocess.run(["fc-cache", "-f", str(target)], check=False, capture_output=True)


def install_python_requirements(echo=print) -> None:
    """Install CONTAINER_REQUIREMENTS when missing (container only), hash-checked."""

    import importlib.util
    import tempfile

    if importlib.util.find_spec("pypdf") is not None or not os.environ.get("BOARDWRIGHT_CONTAINER"):
        return
    target = Path.home() / ".boardwright-python"
    with tempfile.TemporaryDirectory() as tmp:
        requirements = Path(tmp) / "requirements.txt"
        requirements.write_text(CONTAINER_REQUIREMENTS, encoding="utf-8")
        completed = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "--no-deps",
             "--require-hashes", "--target", str(target), "-r", str(requirements)],
        )
    if completed.returncode != 0:
        raise BuildError("Could not install the pinned container Python requirements.")
    os.environ["PYTHONPATH"] = os.pathsep.join(filter(None, [os.environ.get("PYTHONPATH", ""), str(target)]))
    echo(f"Installed pinned Python requirements into {target}")


def detect_kicad() -> str:
    try:
        output = subprocess.run(["kicad-cli", "--version"], capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise BuildError("kicad-cli is not available; run the build in the Boardwright container.") from exc
    return output.strip()


def tool_versions() -> dict[str, str]:
    versions: dict[str, str] = {}
    for name, argv in (("kicad", ["kicad-cli", "--version"]), ("kibot", ["kibot", "--version"])):
        try:
            versions[name] = subprocess.run(argv, capture_output=True, text=True, timeout=60).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            versions[name] = ""
    versions["python"] = sys.version.split()[0]
    return versions


# --- run -------------------------------------------------------------------


def run_plan(plan: BuildPlan, root: Path, echo=print) -> None:
    for number, step in enumerate(plan.steps, 1):
        echo(f"::group::[{number}/{len(plan.steps)}] {step.name}" if os.environ.get("GITHUB_ACTIONS") else f"==> [{number}/{len(plan.steps)}] {step.name}")
        echo("    " + " ".join(step.argv))
        completed = subprocess.run(list(step.argv), cwd=root)
        if os.environ.get("GITHUB_ACTIONS"):
            echo("::endgroup::")
        if completed.returncode != 0:
            raise BuildError(f"Step failed ({completed.returncode}): {step.name}")


def write_evidence(root: Path, plan: BuildPlan, source: dict[str, object]) -> Path:
    from .output_manifest import build_manifest

    out = root / BUILD_DIR
    out.mkdir(parents=True, exist_ok=True)
    manifest = build_manifest(root)
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    record = {
        "schema": 1,
        "variant": plan.variant,
        "kicad_major": plan.kicad,
        "source_commit": source["commit"],
        "source_modified": source["modified_before_build"],
        # Present only when every modification was made by release --prepare
        # and still has the recorded content (release_preparation.py).
        "release_preparation": source.get("release_preparation"),
        "image": os.environ.get("BOARDWRIGHT_IMAGE", ""),
        # Local ID of the image that ran; the runner verified it carries the
        # pinned digest. Its form depends on the Docker image store, so builds
        # are compared on ``image`` (the pinned reference), not on this ID.
        "image_id": os.environ.get("BOARDWRIGHT_IMAGE_ID", ""),
        "boardwright_source": source["boardwright_source"],
        "tools": tool_versions(),
        "normaliser": manifest["normaliser"],
        "configuration": {
            "variant": plan.variant,
            "kicad_major": plan.kicad,
            "kibot_config_tree": source["kibot_config_tree"],
            "python_requirements": CONTAINER_REQUIREMENTS.splitlines(),
        },
        "steps": [{"name": step.name, "argv": _portable_argv(step.argv)} for step in plan.steps],
        "stage": _stage_marker(root),
        "github_run": {key: os.environ[key] for key in ("GITHUB_REPOSITORY", "GITHUB_RUN_ID") if key in os.environ},
        "container_user": f"{os.getuid()}:{os.getgid()}" if hasattr(os, "getuid") else "",
    }
    (out / "record.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    return out


def _portable_argv(argv: tuple[str, ...] | list[str]) -> list[str]:
    """Step argv with the interpreter path replaced, so records compare across hosts."""

    return ["python" if arg == sys.executable else arg for arg in argv]


def source_provenance(root: Path) -> dict[str, object]:
    """The Boardwright package and KiBot configuration this build runs, as Git trees."""

    return {
        "boardwright_source": boardwright_source(root),
        "kibot_config_tree": _git(root, "rev-parse", "HEAD:boardwright_resources/kibot").strip(),
    }


def boardwright_source(root: Path) -> dict[str, str]:
    """Prove the running Boardwright package is the one in ``root/src``.

    Returns the package path relative to ``root`` and the Git tree ID of
    ``src/boardwright`` at HEAD. Fails if Python imported Boardwright from
    anywhere else (an installed copy or a different checkout).
    """

    package = Path(__file__).resolve().parent
    expected = (root / "src" / "boardwright").resolve()
    if package != expected:
        raise BuildError(f"Boardwright is imported from {package}, not from the checkout ({expected}). Set PYTHONPATH to <root>/src.")
    return {
        "package": "src/boardwright",
        "tree": _git(root, "rev-parse", "HEAD:src/boardwright").strip(),
    }


def _stage_marker(root: Path) -> dict[str, object]:
    """The ``container stage`` marker, if this tree is a local stage."""

    try:
        data = json.loads((root / ".git" / "boardwright-stage").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {"head": data.get("head", ""), "worktree": bool(data.get("worktree"))}


def build(root: Path, variant: str, kicad: str | None, log_prefix: str, keep: tuple[str, ...], echo=print) -> Path:
    from .ci_cleanup import discard_side_effects

    source = preflight(root)
    source.update(source_provenance(root))
    detected = detect_kicad()
    major = detected.split()[-1].split(".")[0] if detected else ""
    if kicad and kicad != major:
        raise BuildError(f"Requested KiCad {kicad} but the environment has {detected!r}.")
    plan = plan_build(variant, kicad or major, log_prefix)
    echo(f"Boardwright build: {variant}, {detected}, source {source['commit'][:12]}")
    install_fonts(root)
    install_python_requirements(echo)
    run_plan(plan, root, echo)
    if not (root / "README.md").is_file() or not (root / "README.md").stat().st_size:
        raise BuildError("README.md was not generated by KiBot.")
    restored, remaining = discard_side_effects(root, GENERATED_TRACKED + keep)
    if remaining:
        raise BuildError("The build left tracked source files modified: " + ", ".join(remaining))
    echo(f"Source side effects discarded ({len(restored)} file(s) restored).")
    return write_evidence(root, plan, source)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m boardwright build", description=__doc__.split("\n\n")[0])
    parser.add_argument("--variant", required=True, choices=VARIANTS)
    parser.add_argument("--kicad", choices=SUPPORTED_KICAD, help="Expected KiCad major version (default: detected).")
    parser.add_argument("--log-prefix", default="build", help="KiBot log names: kibot_<prefix>*.log.")
    parser.add_argument("--keep", nargs="*", default=[], help="Tracked paths the caller commits itself (not restored).")
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--plan", action="store_true", help="Print the steps and exit.")
    args = parser.parse_args(argv)
    # Keep this process's messages (and ::group:: markers) in order with the
    # output of the tools it runs.
    sys.stdout.reconfigure(line_buffering=True)
    try:
        if args.plan:
            for step in plan_build(args.variant, args.kicad or "9", args.log_prefix).steps:
                print(f"{step.name}: {' '.join(step.argv)}")
            return 0
        out = build(args.root, args.variant, args.kicad, args.log_prefix, tuple(args.keep))
    except BuildError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Build evidence: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
