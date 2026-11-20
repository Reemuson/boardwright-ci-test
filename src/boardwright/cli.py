# SPDX-License-Identifier: LicenseRef-Boardwright-Source-Available
# Copyright (c) Ryan Hicks

from __future__ import annotations

import argparse
import os
import sys
import shutil
import subprocess
from pathlib import Path

from .accepted import build_accepted_main_state, format_accepted_state
from .actions import (
    RELEASE_KINDS,
    build_prepare_release_action,
    build_promote_action,
    dispatch_workflow_action,
)
from .changelog import SUPPORTED_SECTIONS, add_unreleased_entry
from .commit_messages import subject_warnings, suggest_commit_message
from .config import find_project_root, init_config, load_config
from .doctor import doctor_exit_code, format_doctor_report, run_doctor
from .errors import BoardwrightError
from .generated_outputs import clean_generated_outputs, format_cleanup_summary
from .git_ops import commit_all, dirty_files
from .legal import generate_legal_files
from .managed_project import (
    apply_update_plan,
    build_update_plan,
    update_status,
    write_plan,
)
from .migration import (
    apply_migration_plan,
    build_migration_plan,
    looks_like_nguyen_project,
    migration_summary,
)
from .preview import build_preview_plan, dispatch_preview, preview_manual_fallback
from .preview import build_preview_state, fetch_latest_preview_artifact, format_preview_state
from .project_setup import setup_project_repository
from .impact import format_impact
from .release_date import parse_release_date, release_env_lines
from .release_preparation import record_preparation
from .release import assess_plan_impact, build_release_plan, prepare_release, validate_release_plan
from .revision_history import write_revision_variables
from .schematic_generation import (
    add_hierarchical_sheet,
    generate_hierarchical_project,
    generate_one_sheet_schematic,
    generate_revision_history_sheet,
    slugify,
)
from .sheet_titles import (
    collect_actual_sheet_titles,
    compare_sheet_title_overrides,
    find_primary_schematic,
    read_sheet_title_overrides,
    write_sheet_title_overrides,
)
from .source_package import build_source_package
from .status import collect_status
from .testbench import build_testbench_plan, format_testbench_plan, init_testbench
from .validation import validate_project
from .worksheet_logo import embed_logo_in_worksheets
from .workflow_state import build_workflow_state
from .git_ops import remote_url


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    # build and container own their argument parsing (shared with CI).
    if arguments[:1] == ["build"]:
        from .build import main as build_main

        return build_main(arguments[1:])
    if arguments[:1] == ["container"]:
        from .container import main as container_main

        return container_main(arguments[1:])
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "init":
            return _init(args)
        if args.command == "status":
            return _status()
        if args.command == "change":
            return _change(args)
        if args.command == "legal":
            return _legal(args)
        if args.command == "preview":
            return _preview(args)
        if args.command == "promote":
            return _promote(args)
        if args.command == "release":
            return _release(args)
        if args.command == "revision-history":
            return _revision_history()
        if args.command == "git-status":
            return _git_status()
        if args.command == "commit":
            return _commit(args)
        if args.command == "validate":
            return _validate()
        if args.command == "tui":
            return _tui()
        if args.command == "suggest-commit":
            return _suggest_commit(args)
        if args.command == "accepted":
            return _accepted()
        if args.command == "doctor":
            return _doctor()
        if args.command == "review":
            return _review(args)
        if args.command == "testbench":
            return _testbench(args)
        if args.command == "adopt":
            return _adopt(args)
        if args.command == "migrate":
            return _migrate(args)
        if args.command == "update":
            return _update(args)
        if args.command == "generate":
            return _generate(args)
        if args.command == "outputs":
            return _outputs(args)
        if args.command == "sheet-title":
            return _sheet_title(args)
        if args.command == "worksheet-logo":
            return _worksheet_logo(args)
        if args.command == "worksheet":
            return _worksheet(args)
        if args.command == "source-package":
            return _source_package(args)
        if args.command == "docker-kibot":
            return _docker_kibot(args)
        return _tui()
    except BoardwrightError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="boardwright")
    subparsers = parser.add_subparsers(dest="command")

    init = subparsers.add_parser("init", help="Create default Boardwright project files.")
    init.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing Boardwright config and changelog files.",
    )
    init.add_argument(
        "--no-workflows",
        action="store_true",
        help="Do not install Boardwright GitHub workflow files.",
    )

    init.add_argument(
        "--remote-url",
        help="GitHub remote URL or OWNER/repo slug to configure as origin.",
    )
    init.add_argument(
        "--no-git",
        action="store_true",
        help="Create Boardwright files without initializing a git repository.",
    )
    subparsers.add_parser("status", help="Show project workflow status.")

    change = subparsers.add_parser("change", help="Add an unreleased changelog entry.")
    change.add_argument("message", nargs="?", help="Change text to record.")
    change.add_argument(
        "--section",
        "-s",
        default="Changed",
        choices=SUPPORTED_SECTIONS,
        help="Changelog section to update.",
    )
    change.add_argument(
        "--suggest-commit",
        action="store_true",
        help="Print a suggested commit message after updating the changelog.",
    )

    suggest = subparsers.add_parser(
        "suggest-commit",
        help="Suggest a conventional commit message from the working tree.",
    )
    suggest.add_argument("message", nargs="?", help="Optional summary seed.")

    legal = subparsers.add_parser("legal", help="Manage legal and notice files.")
    legal_subparsers = legal.add_subparsers(dest="legal_command")
    legal_init = legal_subparsers.add_parser("init", help="Generate notice files.")
    legal_init.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing notice files.",
    )

    subparsers.add_parser("validate", help="Validate core Boardwright project files.")
    subparsers.add_parser("tui", help="Launch the Boardwright TUI.")

    preview = subparsers.add_parser("preview", help="Plan or dispatch preview outputs.")
    preview.add_argument(
        "--variant",
        "-v",
        help="Preview variant. Defaults to variants.preview_default from project.yaml.",
    )
    preview.add_argument(
        "--dispatch",
        action="store_true",
        help="Dispatch the configured GitHub Actions preview workflow.",
    )

    promote = subparsers.add_parser(
        "promote",
        help="Plan or dispatch accepted output generation on main.",
    )
    promote.add_argument(
        "--variant",
        "-v",
        default="CHECKED",
        help="Variant to promote to main.",
    )
    promote.add_argument(
        "--no-commit-outputs",
        action="store_true",
        help="Generate/upload outputs without committing them to main.",
    )
    promote.add_argument(
        "--dispatch",
        action="store_true",
        help="Dispatch the configured main output workflow.",
    )

    release = subparsers.add_parser("release", help="Prepare a release locally.")
    release.add_argument("version", help="Semantic version, such as 0.1.0.")
    release.add_argument(
        "--prepare",
        action="store_true",
        help="Update CHANGELOG.md and revision-history variables.",
    )
    release.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Allow release preparation with a dirty working tree.",
    )
    release.add_argument(
        "--variant",
        "-v",
        default="RELEASED",
        help="Variant for CI-owned release preparation when using --dispatch.",
    )
    release.add_argument(
        "--kind",
        choices=RELEASE_KINDS,
        default="release",
        help="GitHub Release state for CI-owned release preparation.",
    )
    release.add_argument(
        "--drawing-revision",
        required=False,
        help="Controlled drawing revision to add to document_revisions.yaml.",
    )
    release.add_argument(
        "--revision-description",
        required=False,
        help="Short revision-table description, 60 characters or fewer.",
    )
    release.add_argument(
        "--dispatch",
        action="store_true",
        help="Dispatch the CI-owned prepare-release workflow.",
    )
    release.add_argument(
        "--change-class",
        action="append",
        default=[],
        metavar="CLASS",
        help="Change class (repeatable or comma-separated): documentation, electrical, fabrication, assembly, tooling.",
    )
    release.add_argument(
        "--justification",
        default="",
        help="Why this release is needed; recorded in .boardwright/releases.yaml.",
    )
    release.add_argument(
        "--release-env",
        action="store_true",
        help="With --prepare: also write .boardwright/release.env (version, variant, kind, release date) for the publish workflow.",
    )
    release.add_argument(
        "--release-date",
        default="",
        metavar="YYYY-MM-DD",
        help="With --prepare: the release date (default: today's UTC date). An approved run passes the reviewed candidate's date.",
    )
    release.add_argument(
        "--source-sha",
        default="",
        help="Reviewed commit the prepare-release workflow must build (default: pushed release branch head).",
    )

    subparsers.add_parser(
        "revision-history",
        help="Write fixed REVTABLE_* variables from document_revisions.yaml.",
    )

    subparsers.add_parser("git-status", help="List changed files.")

    commit = subparsers.add_parser("commit", help="Commit all changed files locally.")
    commit.add_argument("--message", "-m", required=True, help="Commit message.")
    commit.add_argument(
        "--apply",
        action="store_true",
        help="Actually create the commit. Without this, the command is a dry run.",
    )

    subparsers.add_parser("accepted", help="Show accepted main-output workflow state.")
    subparsers.add_parser("doctor", help="Check local Boardwright workflow readiness.")

    review = subparsers.add_parser("review", help="Show or fetch preview artifact review state.")
    review.add_argument(
        "--variant",
        "-v",
        help="Preview variant. Defaults to variants.preview_default from project.yaml.",
    )
    review.add_argument(
        "--fetch",
        action="store_true",
        help="Download the fresh preview artifact and mark it reviewed.",
    )
    review.add_argument(
        "--output-dir",
        type=Path,
        help="Directory for downloaded preview artifacts. Defaults to boardwright-preview.",
    )

    testbench = subparsers.add_parser("testbench", help="Plan or create a live-test repo.")
    testbench_subparsers = testbench.add_subparsers(dest="testbench_command")
    testbench_plan = testbench_subparsers.add_parser(
        "plan",
        help="Print the live-testbench command sequence.",
    )
    testbench_plan.add_argument("--target", type=Path, help="Target directory for the testbench.")
    testbench_plan.add_argument("--github-repo", help="GitHub repo slug, such as OWNER/repo.")
    testbench_init = testbench_subparsers.add_parser(
        "init",
        help="Copy this template into a separate local testbench repo.",
    )
    testbench_init.add_argument("--target", type=Path, help="Target directory for the testbench.")
    testbench_init.add_argument("--github-repo", help="GitHub repo slug, such as OWNER/repo.")
    testbench_init.add_argument(
        "--force",
        action="store_true",
        help="Overwrite the target directory if it already exists.",
    )
    testbench_init.add_argument(
        "--no-git-init",
        action="store_true",
        help="Copy files without initializing git branches.",
    )

    adopt = subparsers.add_parser(
        "adopt",
        help="Adopt an existing KiCad project into Boardwright configuration.",
    )
    adopt.add_argument(
        "--github-repo",
        help="GitHub repo slug, such as OWNER/repo. Defaults to origin when available.",
    )
    adopt.add_argument(
        "--force",
        action="store_true",
        help="Overwrite the default Boardwright files if they already exist.",
    )
    adopt.add_argument(
        "--no-workflows",
        action="store_true",
        help="Do not install Boardwright GitHub workflow files.",
    )

    migrate = subparsers.add_parser(
        "migrate",
        help="Migrate a Nguyen hierarchical template project to Boardwright.",
    )
    migrate_subparsers = migrate.add_subparsers(dest="migrate_command")
    migrate_plan = migrate_subparsers.add_parser(
        "plan",
        help="Inspect a legacy project and write a reviewable migration plan.",
    )
    migrate_plan.add_argument(
        "--project",
        type=Path,
        help="Primary root KiCad project when more than one .kicad_pro exists.",
    )
    migrate_plan.add_argument(
        "--output",
        type=Path,
        required=True,
        help="YAML migration plan to write.",
    )
    migrate_apply = migrate_subparsers.add_parser(
        "apply",
        help="Apply a reviewed migration plan on a clean migration branch.",
    )
    migrate_apply.add_argument("plan", type=Path, help="Reviewed migration plan YAML.")

    update = subparsers.add_parser(
        "update",
        help="Inspect or update Boardwright-managed project infrastructure.",
    )
    update_subparsers = update.add_subparsers(dest="update_command")
    update_subparsers.add_parser("status", help="Show installed payload state and local modifications.")
    update_plan = update_subparsers.add_parser(
        "plan",
        help="Write a reviewable Boardwright update plan.",
    )
    update_plan.add_argument("--to", default="", help="Target payload version.")
    update_plan.add_argument("--output", type=Path, required=True, help="YAML update plan to write.")
    update_apply = update_subparsers.add_parser(
        "apply",
        help="Apply a conflict-free Boardwright update plan.",
    )
    update_apply.add_argument("plan", type=Path, help="Reviewed update plan YAML.")

    generate = subparsers.add_parser(
        "generate",
        help="Generate schematic skeletons and extra sheet files.",
    )
    generate_subparsers = generate.add_subparsers(dest="generate_command")
    generate_revision = generate_subparsers.add_parser(
        "revision-history",
        help="Copy the revision-history sheet template to a new file.",
    )
    generate_revision.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Target schematic file to write.",
    )
    generate_revision.add_argument(
        "--title",
        default="REVISION HISTORY",
        help="Revision-history title to place in the title block.",
    )
    generate_revision.add_argument(
        "--force",
        action="store_true",
        help="Overwrite the target file if it already exists.",
    )
    generate_one_sheet = generate_subparsers.add_parser(
        "one-sheet",
        help="Create a single-sheet schematic skeleton.",
    )
    generate_one_sheet.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Target schematic file to write.",
    )
    generate_one_sheet.add_argument(
        "--title",
        help="Schematic title. Defaults to the configured PCBA name.",
    )
    generate_one_sheet.add_argument(
        "--company",
        help="Company line. Defaults to the configured company.",
    )
    generate_one_sheet.add_argument(
        "--revision",
        help="Revision text. Defaults to the configured board revision.",
    )
    generate_one_sheet.add_argument(
        "--force",
        action="store_true",
        help="Overwrite the target file if it already exists.",
    )
    generate_hierarchy = generate_subparsers.add_parser(
        "hierarchy",
        help="Create a hierarchical project skeleton.",
    )
    generate_hierarchy.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="Directory to receive the generated project files.",
    )
    generate_hierarchy.add_argument(
        "--project-name",
        help="Project title. Defaults to the configured PCBA name.",
    )
    generate_hierarchy.add_argument(
        "--company",
        help="Company line. Defaults to the configured company.",
    )
    generate_hierarchy.add_argument(
        "--revision",
        help="Revision text. Defaults to the configured board revision.",
    )
    generate_hierarchy.add_argument(
        "--sheet-name",
        default="Section A",
        help="Hierarchical sheet name.",
    )
    generate_hierarchy.add_argument(
        "--child-file",
        help="Child schematic filename. Defaults to a slugified sheet name.",
    )
    generate_hierarchy.add_argument(
        "--force",
        action="store_true",
        help="Overwrite generated files if they already exist.",
    )
    generate_sheet = generate_subparsers.add_parser(
        "sheet",
        help="Add a hierarchical sheet to an existing schematic.",
    )
    generate_sheet.add_argument(
        "--parent",
        type=Path,
        help="Parent schematic to update. Defaults to the primary schematic.",
    )
    generate_sheet.add_argument(
        "--sheet-name",
        required=True,
        help="Hierarchical sheet name.",
    )
    generate_sheet.add_argument(
        "--child-file",
        type=Path,
        help="Child schematic file to create. Defaults to a slugified sheet name.",
    )
    generate_sheet.add_argument(
        "--child-title",
        help="Child schematic title. Defaults to the sheet name.",
    )
    generate_sheet.add_argument(
        "--force",
        action="store_true",
        help="Overwrite generated files if they already exist.",
    )

    outputs = subparsers.add_parser("outputs", help="Manage generated KiBot output packages.")
    outputs_subparsers = outputs.add_subparsers(dest="outputs_command")
    outputs_clean = outputs_subparsers.add_parser(
        "clean",
        help="Remove packaging noise from generated KiBot outputs.",
    )
    outputs_clean.add_argument(
        "--root",
        type=Path,
        default=Path("."),
        help="Generated output root. Defaults to the current directory.",
    )

    sheet_title = subparsers.add_parser(
        "sheet-title",
        help="Check or sync local sheet-title placeholder overrides.",
    )
    sheet_title.add_argument(
        "--sync",
        action="store_true",
        help="Write .boardwright/sheet_titles.env from the current schematic titles.",
    )
    sheet_title.add_argument(
        "--schematic",
        type=Path,
        help="Top-level schematic to inspect. Defaults to the primary project schematic.",
    )
    sheet_title.add_argument(
        "--limit",
        type=int,
        default=40,
        help="Maximum sheet title entries to track.",
    )

    worksheet_logo = subparsers.add_parser(
        "worksheet-logo",
        help="Embed a PNG logo into KiCad worksheet bitmap data.",
    )
    worksheet_logo.add_argument(
        "--logo",
        type=Path,
        help="PNG image to embed. Defaults to template.sheet_image from project.yaml.",
    )
    worksheet_logo.add_argument(
        "--worksheet",
        type=Path,
        action="append",
        help="Worksheet to update. Can be repeated. Defaults to the worksheet from the KiCad project.",
    )
    worksheet_logo.add_argument(
        "--all-templates",
        action="store_true",
        help="Update every .kicad_wks file under Templates/.",
    )
    worksheet_logo.add_argument(
        "--bitmap-index",
        type=int,
        default=0,
        help="Zero-based bitmap block to replace. Defaults to the first bitmap.",
    )
    worksheet_logo.add_argument(
        "--output",
        type=Path,
        help="Write branded copies into this directory instead of editing the worksheets in place.",
    )

    worksheet = subparsers.add_parser(
        "worksheet",
        help="Inspect or adopt the project's active KiCad drawing sheets.",
    )
    worksheet.add_argument(
        "action",
        choices=("status", "adopt", "extract"),
        help=(
            "status: show the sheet each drawing renders and any problems; adopt: switch embedded "
            "sheets to their identical Templates/ file; extract: write embedded sheets for review."
        ),
    )
    worksheet.add_argument("--apply", action="store_true", help="adopt: write the .kicad_pro change.")
    worksheet.add_argument(
        "--out",
        type=Path,
        default=Path(".boardwright-checks/embedded-worksheets"),
        help="extract: output directory (default .boardwright-checks/embedded-worksheets).",
    )

    source_package = subparsers.add_parser(
        "source-package",
        help="Create a curated source archive from the current repository checkout.",
    )
    source_package.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Archive path to write.",
    )
    source_package.add_argument(
        "--prefix",
        help="Top-level folder name in the archive. Defaults to the repository folder name.",
    )
    source_package.add_argument(
        "--force",
        action="store_true",
        help="Overwrite the archive if it already exists.",
    )

    docker_kibot = subparsers.add_parser(
        "docker-kibot",
        help="Alias for 'container shell': interactive shell in the pinned KiBot image.",
    )
    docker_kibot.add_argument(
        "--version",
        "-v",
        choices=("9", "10"),
        default="9",
        help="KiCad major version. Defaults to 9.",
    )
    subparsers.add_parser(
        "build",
        add_help=False,
        help="Run the output build in this checkout (inside the container); see 'build --help'.",
    )
    subparsers.add_parser(
        "container",
        add_help=False,
        help="Run builds and checks in the pinned KiBot container; see 'container --help'.",
    )

    return parser


def _init(args: argparse.Namespace) -> int:
    project_root = Path.cwd().resolve()
    written = init_config(root=project_root, force=args.force, workflows=not args.no_workflows)
    config = load_config(project_root)
    remote = (args.remote_url or "").strip()
    if not remote and not args.no_git and sys.stdin.isatty():
        remote = input("GitHub remote URL or OWNER/repo (blank to skip): ").strip()

    setup = setup_project_repository(config, remote, git=not args.no_git)
    if not written:
        print("Boardwright config already exists. Use --force to overwrite defaults.")
    for path in written:
        print(f"wrote {path}")
    for message in setup.messages:
        print(message)
    if setup.remote_url:
        print(f"Remote: {setup.remote_url}")
    return 0
def _status() -> int:
    config = load_config()
    status = collect_status(config)
    issues = tuple(validate_project(config))
    release_plan = build_release_plan(config, "0.1.0", check_remote=False)
    release_problems = validate_release_plan(release_plan, allow_dirty=True)
    release_summary = (
        "ready for dry-run"
        if not release_problems
        else "; ".join(release_problems)
    )
    accepted_state = None
    try:
        accepted_state = build_accepted_main_state(config)
    except BoardwrightError:
        accepted_state = None
    workflow = build_workflow_state(
        config,
        status,
        issues,
        release_summary,
        accepted_state=accepted_state,
    )
    print(f"Project: {status.project_id} - {status.project_name}")
    print(f"Branch: {status.branch}")
    print(f"Dev default variant: {status.variant}")
    print(f"Working tree: {'dirty' if status.dirty_count else 'clean'}")
    print(f"Changed files: {status.dirty_count}")
    print(f"Latest tag: {status.latest_tag or 'none'}")
    print(f"Unreleased changes: {'yes' if status.unreleased_changes else 'no'}")
    print(f"Workflow stage: {workflow.stage}")
    print(f"Next action: {workflow.next_action} - {workflow.reason}")
    return 0


def _accepted() -> int:
    state = build_accepted_main_state(load_config())
    print(format_accepted_state(state))
    return 0


def _doctor() -> int:
    checks = run_doctor(load_config())
    print(format_doctor_report(checks))
    return doctor_exit_code(checks)


def _review(args: argparse.Namespace) -> int:
    config = load_config()
    if args.fetch:
        print(fetch_latest_preview_artifact(config, args.variant, args.output_dir))
        return 0

    state = build_preview_state(config, args.variant, output_dir=args.output_dir)
    print(format_preview_state(state))
    if not state.ready:
        return 1
    return 0


def _testbench(args: argparse.Namespace) -> int:
    config = load_config()
    if args.testbench_command in {None, "plan"}:
        plan = build_testbench_plan(
            config,
            getattr(args, "target", None),
            getattr(args, "github_repo", None) or "",
        )
        print(format_testbench_plan(plan))
        return 0
    if args.testbench_command == "init":
        messages = init_testbench(
            config,
            args.target,
            args.github_repo or "",
            force=args.force,
            git_init=not args.no_git_init,
        )
        for message in messages:
            print(message)
        return 0
    print("usage: boardwright testbench {plan,init}")
    return 0


def _outputs(args: argparse.Namespace) -> int:
    if args.outputs_command in {None, "clean"}:
        print(format_cleanup_summary(clean_generated_outputs(args.root)))
        return 0
    print("usage: boardwright outputs clean [--root PATH]")
    return 0


def _adopt(args: argparse.Namespace) -> int:
    project_root = find_project_root(Path.cwd())
    if not (project_root / ".boardwright").is_dir() and looks_like_nguyen_project(project_root):
        raise BoardwrightError(
            "Detected a Nguyen hierarchical template project. "
            "Use `boardwright migrate plan --output migration.yaml`."
        )
    written = init_config(root=project_root, force=args.force, workflows=not args.no_workflows)
    config = load_config(project_root)
    repo = (args.github_repo or _github_repo_from_remote(config.root)).strip()
    updates = {}
    git_url = remote_url(config.root)
    if not git_url and repo:
        git_url = f"https://github.com/{repo}.git"
    if git_url:
        current_git_url = str(config.project.get("project", {}).get("git_url", "")).strip()
        if git_url != current_git_url:
            updates["git_url"] = git_url
    if updates:
        from .config import update_project_config

        update_project_config(config, project_fields=updates)
        written.append(config.root / ".boardwright" / "project.yaml")

    for path in written:
        print(f"wrote {path}")
    print("Adopted existing project into Boardwright.")
    return 0


def _migrate(args: argparse.Namespace) -> int:
    if args.migrate_command == "plan":
        root = find_project_root(Path.cwd())
        plan = build_migration_plan(root, args.project)
        target = write_plan(args.output, plan)
        print(migration_summary(plan))
        print(f"Wrote migration plan: {target}")
        return 1 if plan.get("blockers") else 0
    if args.migrate_command == "apply":
        written = apply_migration_plan(args.plan)
        for path in written:
            print(f"wrote {path}")
        print("Migration applied. Review and commit the changes; no commit was created.")
        return 0
    print("usage: boardwright migrate {plan,apply}")
    return 0


def _update(args: argparse.Namespace) -> int:
    root = find_project_root(Path.cwd())
    if args.update_command in {None, "status"}:
        status = update_status(root)
        print(f"Schema version: {status['schema_version']}")
        print(f"Installed template: {status['installed_version']}")
        print(f"Available template: {status['available_version']}")
        print(f"Modified managed files: {len(status['modified'])}")
        print(f"Missing managed files: {len(status['missing'])}")
        print(f"Update blockers: {len(status['blockers'])}")
        return 0
    if args.update_command == "plan":
        plan = build_update_plan(root, to_version=args.to)
        target = write_plan(args.output, plan)
        print(f"Wrote update plan: {target}")
        print(f"Blockers: {len(plan.get('blockers', []))}")
        for notice in plan.get("notices", []):
            print(f"note: {notice}")
        return 1 if plan.get("blockers") else 0
    if args.update_command == "apply":
        written = apply_update_plan(args.plan)
        for path in written:
            print(f"updated {path}")
        print("Boardwright update applied; no commit was created.")
        return 0
    print("usage: boardwright update {status,plan,apply}")
    return 0


def _generate(args: argparse.Namespace) -> int:
    config = load_config()
    if args.generate_command == "revision-history":
        result = generate_revision_history_sheet(
            config.root,
            args.output if args.output.is_absolute() else config.root / args.output,
            title=args.title,
            force=args.force,
        )
        print(f"wrote {result.path}")
        return 0
    if args.generate_command == "one-sheet":
        output = args.output if args.output.is_absolute() else config.root / args.output
        result = generate_one_sheet_schematic(
            output,
            title=args.title or config.pcba_name,
            company=args.company or str(config.project.get("project", {}).get("company", "")),
            revision=args.revision or config.board_revision,
            force=args.force,
        )
        print(f"wrote {result.path}")
        return 0
    if args.generate_command == "hierarchy":
        result = generate_hierarchical_project(
            args.output_dir if args.output_dir.is_absolute() else config.root / args.output_dir,
            project_name=args.project_name or config.pcba_name,
            company=args.company or str(config.project.get("project", {}).get("company", "")),
            revision=args.revision or config.board_revision,
            sheet_name=args.sheet_name,
            child_filename=args.child_file,
            force=args.force,
        )
        for item in result:
            print(f"wrote {item.path}")
        return 0
    if args.generate_command == "sheet":
        parent = args.parent or find_primary_schematic(config.root)
        if parent is None:
            raise BoardwrightError("Could not find a top-level KiCad schematic.")
        parent = parent if parent.is_absolute() else config.root / parent
        child_file = args.child_file or parent.with_name(f"{slugify(args.sheet_name)}.kicad_sch")
        child_file = child_file if child_file.is_absolute() else config.root / child_file
        result = add_hierarchical_sheet(
            parent,
            child_file,
            sheet_name=args.sheet_name,
            child_title=args.child_title,
            force=args.force,
        )
        for item in result:
            print(f"wrote {item.path}")
        return 0
    print("usage: boardwright generate {revision-history,one-sheet,hierarchy,sheet}")
    return 0


def _sheet_title(args: argparse.Namespace) -> int:
    config = load_config()
    schematic = args.schematic or find_primary_schematic(config.root)
    if schematic is None:
        raise BoardwrightError("Could not find a top-level KiCad schematic.")

    titles = collect_actual_sheet_titles(
        config.root,
        schematic=schematic,
        max_titles=args.limit,
    )

    if args.sync:
        path = write_sheet_title_overrides(config.root, titles)
        print(
            f"Updated {_display_path(path, config.root)} with {len(titles)} sheet title placeholder(s)."
        )
        return 0

    overrides = read_sheet_title_overrides(config.root)
    if not overrides:
        print(
            "No local sheet-title overrides found. Run `boardwright sheet-title --sync` "
            "to create .boardwright/sheet_titles.env."
        )
        return 1

    issues = compare_sheet_title_overrides(titles, overrides)
    if issues:
        print(f"Sheet title overrides drift from {_display_path(schematic, config.root)}:")
        for issue in issues[:8]:
            print(f"- {issue}")
        remaining = len(issues) - 8
        if remaining > 0:
            print(f"- ... and {remaining} more")
        print("Run `boardwright sheet-title --sync` to refresh .boardwright/sheet_titles.env.")
        return 1

    print(
        f"Sheet title overrides are in sync with {_display_path(schematic, config.root)}."
    )
    return 0


def _worksheet_logo(args: argparse.Namespace) -> int:
    from .worksheet_logo import select_logo_targets

    config = load_config()
    logo = args.logo or Path(config.template_sheet_image)
    if not str(logo):
        raise BoardwrightError("No logo given and template.sheet_image is not configured.")
    logo = logo if logo.is_absolute() else config.root / logo
    worksheets = select_logo_targets(
        config,
        logo,
        explicit=[path if path.is_absolute() else config.root / path for path in args.worksheet or []],
        all_templates=args.all_templates,
        output=(args.output if args.output is None or args.output.is_absolute() else config.root / args.output),
    )
    results = embed_logo_in_worksheets(
        worksheets,
        logo,
        bitmap_index=args.bitmap_index,
    )
    for result in results:
        worksheet_label = _display_path(result.worksheet, config.root)
        logo_label = _display_path(result.logo, config.root)
        print(
            f"updated {worksheet_label} with {logo_label} "
            f"({result.png_width}x{result.png_height}, bitmap {result.bitmap_index})"
        )
    if config.local_overrides:
        print("note: using .boardwright/local.yaml overrides: " + ", ".join(config.local_overrides))
    return 0


def _worksheet(args: argparse.Namespace) -> int:
    from . import drawing_sheets

    config = load_config()
    template_worksheet = str(config.template.get("worksheet") or "")
    if args.action == "status":
        for document, ref in drawing_sheets.active_sheet_refs(config.root, template_worksheet).items():
            where = ref.path if ref.kind in ("file", "relative", "absolute") else ref.setting or "(KiCad default)"
            print(f"{document:12} {ref.kind:9} {where}")
        logo = config.template_sheet_image or "(none)"
        suffix = " (local override)" if "template.sheet_image" in config.local_overrides else ""
        print(f"{'logo':12} {'':9} {logo}{suffix}")
        issues = drawing_sheets.sheet_issues(config.root, template_worksheet) + drawing_sheets.branding_issues(config)
        for issue in issues:
            print(f"- {issue.level}: {issue.message}")
        return 1 if any(issue.level == "error" for issue in issues) else 0
    if args.action == "adopt":
        results = drawing_sheets.plan_adoption(config.root)
        for result in results:
            print(f"{result.section:10} {result.action:13} {result.detail}")
        if not args.apply:
            if any(result.action == "adopted" for result in results):
                print("Dry run: re-run with --apply to update the .kicad_pro.")
            return 0
        written = drawing_sheets.apply_adoption(config.root, results)
        for path in written:
            print(f"updated {_display_path(path, config.root)}")
        blocked = [result for result in results if result.action in ("diverged", "missing", "unverifiable")]
        return 1 if blocked else 0
    out = args.out if args.out.is_absolute() else config.root / args.out
    written = drawing_sheets.extract_embedded(config.root, out)
    for path in written:
        print(f"wrote {_display_path(path, config.root)}")
    if not written:
        print("No embedded worksheets.")
    return 0


def _source_package(args: argparse.Namespace) -> int:
    config = load_config()
    result = build_source_package(
        config.root,
        args.output if args.output.is_absolute() else config.root / args.output,
        prefix=args.prefix,
        force=args.force,
    )
    print(f"wrote {result.path} ({result.file_count} file(s))")
    return 0


def _docker_kibot(args: argparse.Namespace) -> int:
    # Compatibility alias: the old helper scripts mounted /etc/shadow and used a
    # moving KiCad 8 image (audit M7). The pinned container replaces them.
    from .container import main as container_main

    print("note: docker-kibot is now 'boardwright container shell'.", file=sys.stderr)
    return container_main(["shell", "--kicad", str(args.version)])


def _display_path(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _github_repo_from_remote(root: Path) -> str:
    origin = remote_url(root)
    if not origin:
        return ""
    patterns = (
        r"^https://github\.com/(?P<repo>[^/]+/[^/]+?)(?:\.git)?/?$",
        r"^git@github\.com:(?P<repo>[^/]+/[^/]+?)(?:\.git)?$",
        r"^ssh://git@github\.com/(?P<repo>[^/]+/[^/]+?)(?:\.git)?/?$",
    )
    import re

    for pattern in patterns:
        match = re.match(pattern, origin)
        if match:
            return match.group("repo")
    return ""


def _change(args: argparse.Namespace) -> int:
    config = load_config()
    message = args.message or input("What changed? ").strip()
    add_unreleased_entry(config.root, args.section, message)
    print(f"Recorded {args.section} change.")
    if args.suggest_commit:
        print(suggest_commit_message(config.root, message))
    return 0


def _suggest_commit(args: argparse.Namespace) -> int:
    config = load_config()
    message = suggest_commit_message(config.root, args.message)
    print(message)
    # Advisory only (docs/COMMITS.md); a suggestion is never refused.
    for note in subject_warnings(message):
        print(f"note: {note}", file=sys.stderr)
    return 0


def _legal(args: argparse.Namespace) -> int:
    if args.legal_command != "init":
        print("usage: boardwright legal init [--force]")
        return 0

    written = generate_legal_files(load_config(), force=args.force)
    if not written:
        print("Legal files already exist. Use --force to overwrite generated files.")
        return 0
    for path in written:
        print(f"wrote {path}")
    return 0


def _validate() -> int:
    issues = validate_project(load_config())
    if not issues:
        print("Validation passed.")
        return 0

    has_error = False
    for issue in issues:
        print(f"{issue.level}: {issue.message}")
        has_error = has_error or issue.level == "error"
    return 1 if has_error else 0


def _tui() -> int:
    from .tui import run

    return run()


def _preview(args: argparse.Namespace) -> int:
    config = load_config()
    plan = build_preview_plan(config, args.variant)
    print(f"Engine: {plan.engine}")
    print(f"Workflow: {plan.workflow}")
    print(f"Branch: {plan.branch}")
    print(f"Preview branch: {plan.preview_branch}")
    print(f"Variant: {plan.variant}")
    print(f"GitHub CLI: {'available' if plan.gh_available else 'not found'}")
    if not plan.gh_available:
        print()
        print(preview_manual_fallback(plan))
    print("Expected output paths:")
    for path in plan.output_paths:
        exists = "exists" if path.exists() else "missing"
        print(f"- {path} ({exists})")

    if args.dispatch:
        dispatch_preview(plan, config.root)
        print("Preview workflow dispatched.")
    else:
        print("Preview dispatch skipped. Use --dispatch to run the workflow.")
    return 0


def _promote(args: argparse.Namespace) -> int:
    config = load_config()
    action = build_promote_action(
        config,
        args.variant,
        commit_outputs=not args.no_commit_outputs,
    )
    print(f"Workflow: {action.workflow}")
    print(f"Ref: {action.ref}")
    print(f"GitHub CLI: {'available' if action.gh_available else 'not found'}")
    print("Inputs:")
    for key, value in action.fields:
        print(f"- {key}: {value}")
    print("Command:")
    print(" ".join(action.command))
    if not action.gh_available:
        print()
        print(action.manual_fallback)

    if args.dispatch:
        dispatch_workflow_action(config, action)
        print("Promote workflow dispatched.")
    else:
        print("Promote dispatch skipped. Use --dispatch to run the workflow.")
    return 0


def _write_release_env(config, version: str, variant: str, kind: str, release_date: str) -> None:
    # Read by release.yaml and by KiBot's text variables (RELEASE_DATE*); the
    # include flag comes from tracked configuration only.
    tracked = load_config(config.root, local=False)
    lines = [
        f"RELEASE_VERSION={version}",
        f"RELEASE_VARIANT={variant}",
        f"RELEASE_KIND={kind}",
        f"RELEASE_INCLUDE_SOURCE_ARCHIVE={str(tracked.release_include_source_archive).lower()}",
        *release_env_lines(parse_release_date(release_date)),
    ]
    target = config.root / ".boardwright" / "release.env"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def _release(args: argparse.Namespace) -> int:
    config = load_config()
    if args.dispatch:
        action = build_prepare_release_action(
            config,
            args.version,
            args.variant,
            args.kind,
            args.drawing_revision or "",
            args.revision_description or "",
            change_classes=args.change_class,
            justification=args.justification,
            source_sha=args.source_sha or _pushed_release_sha(config),
        )
        impact = assess_plan_impact(
            config,
            args.version,
            change_classes=args.change_class,
            justification=args.justification or args.revision_description or "",
            drawing_revision=args.drawing_revision or "",
        )
        for line in format_impact(impact):
            print(line)
        if impact.problems:
            print("Release impact has blocking problems; not dispatching.")
            return 1
        print(f"Workflow: {action.workflow}")
        print(f"Ref: {action.ref}")
        print(f"GitHub CLI: {'available' if action.gh_available else 'not found'}")
        print("Inputs:")
        for key, value in action.fields:
            print(f"- {key}: {value}")
        print("Command:")
        print(" ".join(action.command))
        if not action.gh_available:
            print()
            print(action.manual_fallback)
        dispatch_workflow_action(config, action)
        print("Prepare-release workflow dispatched.")
        return 0

    if args.prepare:
        plan = prepare_release(
            config,
            args.version,
            allow_dirty=args.allow_dirty,
            dry_run=False,
            drawing_revision=args.drawing_revision or "",
            revision_description=args.revision_description or "",
            change_classes=args.change_class,
            justification=args.justification,
            variant=args.variant,
            release_date=args.release_date,
        )
        print(f"Prepared release {plan.version} dated {plan.release_date} (UTC release date).")
        if args.release_env:
            _write_release_env(config, args.version, args.variant, args.kind, plan.release_date)
            print("Wrote .boardwright/release.env.")
        record_preparation(config.root, version=plan.version, release_date=plan.release_date)
        print("Recorded release identities in .boardwright/releases.yaml.")
        print("Updated CHANGELOG.md, .boardwright/document_revisions.yaml, and revision-history variables.")
        print(f"Suggested commit: release: prepare {plan.version}")
        print("Next steps: review changes and prefer boardwright release --dispatch for CI-owned tagging.")
        return 0

    plan = build_release_plan(config, args.version)
    problems = validate_release_plan(plan, allow_dirty=args.allow_dirty)
    print(f"Release: {plan.version}")
    print(f"Branch: {plan.branch}")
    print(f"Release branch: {plan.release_branch}")
    print(f"Working tree changes: {plan.dirty_count}")
    print(f"Local tag exists: {'yes' if plan.local_tag_exists else 'no'}")
    print(f"Remote tag: {plan.remote_tag_state}" + (f" ({plan.remote_error})" if plan.remote_error else ""))
    print(f"Unreleased changes: {'yes' if plan.has_unreleased_changes else 'no'}")
    impact = assess_plan_impact(
        config,
        args.version,
        change_classes=args.change_class,
        justification=args.justification or args.revision_description or "",
        drawing_revision=args.drawing_revision or "",
    )
    print()
    for line in format_impact(impact):
        print(line)
    problems = [*problems, *impact.problems]
    if problems:
        print("Problems:")
        for problem in problems:
            print(f"- {problem}")
        return 1
    print("Dry run passed. Use --prepare with --drawing-revision and --revision-description to update release files.")
    print(f"Suggested commit after prepare: release: prepare {plan.version}")
    return 0


def _pushed_release_sha(config) -> str:
    try:
        from .accepted import latest_pushed_main_sha

        return latest_pushed_main_sha(config)
    except BoardwrightError:
        return ""


def _revision_history() -> int:
    path = write_revision_variables(load_config())
    print(f"wrote {path}")
    return 0


def _git_status() -> int:
    files = dirty_files(load_config().root)
    if not files:
        print("Working tree clean.")
        return 0
    for line in files:
        print(line)
    return 0


def _commit(args: argparse.Namespace) -> int:
    config = load_config()
    result = commit_all(config.root, args.message, dry_run=not args.apply)
    print(result)
    if not args.apply:
        print("Dry run only. Add --apply to create the commit.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
