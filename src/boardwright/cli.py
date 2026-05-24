from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .accepted import build_accepted_main_state, format_accepted_state
from .actions import (
    RELEASE_KINDS,
    build_prepare_release_action,
    build_promote_action,
    dispatch_workflow_action,
)
from .changelog import SUPPORTED_SECTIONS, add_unreleased_entry
from .commit_messages import suggest_commit_message
from .config import init_config, load_config
from .doctor import doctor_exit_code, format_doctor_report, run_doctor
from .errors import BoardwrightError
from .generated_outputs import clean_generated_outputs, format_cleanup_summary
from .git_ops import commit_all, dirty_files
from .legal import generate_legal_files
from .preview import build_preview_plan, dispatch_preview, preview_manual_fallback
from .preview import build_preview_state, fetch_latest_preview_artifact, format_preview_state
from .release import build_release_plan, prepare_release, validate_release_plan
from .revision_history import write_revision_variables
from .status import collect_status
from .testbench import build_testbench_plan, format_testbench_plan, init_testbench
from .validation import validate_project
from .workflow_state import build_workflow_state


def main(argv: list[str] | None = None) -> int:
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
        if args.command == "outputs":
            return _outputs(args)
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
    subparsers.add_parser("tui", help="Open the Boardwright TUI.")

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
        "--dispatch",
        action="store_true",
        help="Dispatch the CI-owned prepare-release workflow.",
    )

    subparsers.add_parser(
        "revision-history",
        help="Write fixed REVHIST_* variables from CHANGELOG.md.",
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

    return parser


def _init(args: argparse.Namespace) -> int:
    written = init_config(force=args.force, workflows=not args.no_workflows)
    if not written:
        print("Boardwright config already exists. Use --force to overwrite defaults.")
        return 0
    for path in written:
        print(f"wrote {path}")
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
    print(f"Variant: {status.variant}")
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
    print(suggest_commit_message(config.root, args.message))
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


def _release(args: argparse.Namespace) -> int:
    config = load_config()
    if args.dispatch:
        action = build_prepare_release_action(
            config,
            args.version,
            args.variant,
            args.kind,
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
        dispatch_workflow_action(config, action)
        print("Prepare-release workflow dispatched.")
        return 0

    if args.prepare:
        plan = prepare_release(
            config,
            args.version,
            allow_dirty=args.allow_dirty,
            dry_run=False,
        )
        print(f"Prepared release {plan.version}.")
        print("Updated CHANGELOG.md and .boardwright/revision_history_variables.env.")
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
    print(f"Remote tag exists: {'yes' if plan.remote_tag_exists else 'no'}")
    print(f"Unreleased changes: {'yes' if plan.has_unreleased_changes else 'no'}")
    if problems:
        print("Problems:")
        for problem in problems:
            print(f"- {problem}")
        return 1
    print("Dry run passed. Use --prepare to update release files.")
    print(f"Suggested commit after prepare: release: prepare {plan.version}")
    return 0


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
