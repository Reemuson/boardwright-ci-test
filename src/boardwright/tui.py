from __future__ import annotations

from threading import Thread
from dataclasses import dataclass
from importlib.util import find_spec
from typing import TYPE_CHECKING

from rich.text import Text

from .accepted import build_accepted_main_state, format_accepted_state
from .actions import (
    RELEASE_KINDS,
    WorkflowAction,
    build_prepare_release_action,
    build_preview_action,
    build_promote_action,
    dispatch_workflow_action,
    list_recent_workflow_runs,
)
from .changelog import SUPPORTED_SECTIONS, add_unreleased_entry
from .commit_messages import suggest_commit_message
from .config import load_config
from .errors import BoardwrightError
from .git_ops import commit_all, dirty_files, push_branch
from .preview import fetch_latest_preview_artifact
from .preview import build_preview_state, format_preview_state
from .release import build_release_plan, validate_release_plan
from .revision_history import write_revision_variables
from .status import ProjectStatus, collect_status
from .validation import ValidationIssue, validate_project
from .workflow_state import WorkflowState, action_state, build_workflow_state

if TYPE_CHECKING:
    from .accepted import AcceptedMainState
    from .config import BoardwrightConfig
    from .preview import PreviewState


INSTALL_HINT = 'Textual is not installed. Install the TUI with: pip install -e ".[tui]"'


@dataclass(frozen=True)
class DashboardState:
    status: ProjectStatus
    issues: tuple[ValidationIssue, ...]
    preview_summary: str
    promote_summary: str
    ci_release_summary: str
    release_summary: str
    changed_files: tuple[str, ...]
    workflow: WorkflowState
    accepted_summary: str


@dataclass(frozen=True)
class ReleaseChecklistItem:
    label: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class ReleaseChecklist:
    version: str
    variant: str
    kind: str
    accepted_summary: str
    items: tuple[ReleaseChecklistItem, ...]
    action: WorkflowAction | None = None

    @property
    def can_dispatch(self) -> bool:
        return self.action is not None and all(item.passed for item in self.items)


def textual_available() -> bool:
    return find_spec("textual") is not None


def run() -> int:
    if not textual_available():
        _run_console_fallback()
        return 0

    app = _build_textual_app()
    app().run()
    return 0


def collect_dashboard_state(
    release_version: str = "0.1.0",
    preview_state: "PreviewState | None" = None,
    accepted_state: "AcceptedMainState | None" = None,
    accepted_error: str = "",
) -> DashboardState:
    config = load_config()
    status = collect_status(config)
    issues = tuple(validate_project(config))
    preview_action = build_preview_action(config)
    promote_action = build_promote_action(config, "CHECKED")
    ci_release_action = build_prepare_release_action(
        config,
        release_version,
        "RELEASED",
        "release",
    )
    release_plan = build_release_plan(config, release_version, check_remote=False)
    release_problems = validate_release_plan(release_plan, allow_dirty=True)

    preview_summary = (
        f"{preview_action.workflow} | "
        f"{_field_value(preview_action.fields, 'variant')} | "
        f"{preview_action.ref} -> {config.preview_branch}"
    )
    promote_summary = (
        f"{promote_action.workflow} | "
        f"{_field_value(promote_action.fields, 'variant')} | ref {promote_action.ref}"
    )
    ci_release_summary = (
        f"{ci_release_action.workflow} | "
        f"{_field_value(ci_release_action.fields, 'variant')} | "
        f"{_field_value(ci_release_action.fields, 'release_kind')}"
    )
    release_summary = (
        "ready for dry-run"
        if not release_problems
        else "; ".join(release_problems)
    )
    workflow = build_workflow_state(
        config,
        status,
        issues,
        release_summary,
        preview_state,
        accepted_state,
    )
    accepted_summary = (
        format_accepted_state(accepted_state)
        if accepted_state is not None
        else (accepted_error or "Accepted main evidence not checked.")
    )
    return DashboardState(
        status,
        issues,
        preview_summary,
        promote_summary,
        ci_release_summary,
        release_summary,
        tuple(dirty_files(config.root)),
        workflow,
        accepted_summary,
    )


def _run_console_fallback() -> None:
    state = collect_dashboard_state()
    status = state.status
    print("Boardwright")
    print()
    print(f"Project: {status.project_id} - {status.project_name}")
    print(f"Branch: {status.branch}")
    print(f"Variant: {status.variant}")
    print(f"Working tree: {'dirty' if status.dirty_count else 'clean'}")
    print(f"Unreleased changes: {'yes' if status.unreleased_changes else 'no'}")
    print(f"Preview: {state.preview_summary}")
    print(f"Accept to main: {state.promote_summary}")
    print(f"CI release: {state.ci_release_summary}")
    print(f"Release dry-run: {state.release_summary}")
    print(f"Changed files: {len(state.changed_files)}")
    print()
    if state.issues:
        print("Validation:")
        for issue in state.issues:
            print(f"- {issue.level}: {issue.message}")
        print()
    print(INSTALL_HINT)


def _build_textual_app():
    from textual.app import App, ComposeResult
    from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
    from textual.screen import ModalScreen
    from textual.widgets import Button, Footer, Header, Input, Label, Select, Static

    class ChangelogEntryScreen(ModalScreen[tuple[str, str] | None]):
        CSS = """
        ChangelogEntryScreen,
        ReviewVariantScreen,
        ReviewArtifactsScreen,
        AcceptMainScreen,
        CommitScreen,
        ReleaseScreen,
        ReleaseChecklistScreen {
            align: center middle;
        }

        #dialog {
            width: 80;
            max-width: 86%;
            height: auto;
            max-height: 80%;
            padding: 1 2;
            margin: 0;
            border: solid $accent;
            background: $surface;
        }
        """

        def compose(self) -> ComposeResult:
            with Vertical(id="dialog"):
                yield Label("Record Changelog Entry", classes="section-title")
                yield Select(
                    [(section, section) for section in SUPPORTED_SECTIONS],
                    value="Changed",
                    id="change_section",
                )
                yield Input(placeholder="What changed?", id="change_message")
                yield Button("Save", id="save_change")
                yield Button("Cancel", id="cancel_change")

        def on_button_pressed(self, event: Button.Pressed) -> None:
            if event.button.id == "cancel_change":
                self.dismiss(None)
                return
            section = self.query_one("#change_section", Select).value
            message = self.query_one("#change_message", Input).value
            self.dismiss((str(section), message))

    class ReviewArtifactsScreen(ModalScreen[str | None]):
        CSS = ChangelogEntryScreen.CSS

        def __init__(
            self,
            preview_text: str,
            runs_text: str,
            can_fetch: bool,
        ) -> None:
            super().__init__()
            self.preview_text = preview_text
            self.runs_text = runs_text
            self.can_fetch = can_fetch

        def compose(self) -> ComposeResult:
            with Vertical(id="dialog"):
                yield Label("Review Artifacts", classes="section-title")
                yield Static(self.preview_text, id="preview_evidence")
                yield Label("Recent CI", classes="section-title")
                yield Static(self.runs_text, id="recent_runs")
                yield Button("Fetch Artifact", id="fetch_artifact")
                yield Button("Close", id="close_review")

        def on_mount(self) -> None:
            self.query_one("#fetch_artifact", Button).disabled = not self.can_fetch

        def on_button_pressed(self, event: Button.Pressed) -> None:
            if event.button.id == "fetch_artifact":
                self.dismiss("fetch")
                return
            self.dismiss(None)

    class ReviewVariantScreen(ModalScreen[str | None]):
        CSS = ChangelogEntryScreen.CSS

        def compose(self) -> ComposeResult:
            config = load_config()
            with Vertical(id="dialog"):
                yield Label("Review Variant", classes="section-title")
                yield Select(
                    [(variant, variant) for variant in ("DRAFT", "PRELIMINARY", "CHECKED", "RELEASED")],
                    value=config.preview_variant,
                    id="review_variant",
                )
                yield Button("Review", id="review_variant_confirm")
                yield Button("Cancel", id="review_variant_cancel")

        def on_button_pressed(self, event: Button.Pressed) -> None:
            if event.button.id == "review_variant_cancel":
                self.dismiss(None)
                return
            variant = self.query_one("#review_variant", Select).value
            self.dismiss(str(variant))

    class AcceptMainScreen(ModalScreen[tuple[str, bool] | None]):
        CSS = ChangelogEntryScreen.CSS

        def compose(self) -> ComposeResult:
            with Vertical(id="dialog"):
                yield Label("Accept To Main", classes="section-title")
                yield Select(
                    [(variant, variant) for variant in ("DRAFT", "PRELIMINARY", "CHECKED", "RELEASED")],
                    value="CHECKED",
                    id="accept_variant",
                )
                yield Select(
                    [("Update main README/snapshot", "yes"), ("Upload only", "no")],
                    value="yes",
                    id="accept_commit",
                )
                yield Button("Accept", id="dispatch_accept")
                yield Button("Cancel", id="cancel_accept")

        def on_button_pressed(self, event: Button.Pressed) -> None:
            if event.button.id == "cancel_accept":
                self.dismiss(None)
                return
            variant = self.query_one("#accept_variant", Select).value
            commit = self.query_one("#accept_commit", Select).value
            self.dismiss((str(variant), str(commit) == "yes"))

    class CommitScreen(ModalScreen[str | None]):
        CSS = ChangelogEntryScreen.CSS

        def compose(self) -> ComposeResult:
            with Vertical(id="dialog"):
                yield Label("Commit + Push", classes="section-title")
                yield Input(placeholder="feat: describe the board change", id="commit_message")
                yield Button("Commit + Push", id="commit_push")
                yield Button("Cancel", id="cancel_commit")

        def on_button_pressed(self, event: Button.Pressed) -> None:
            if event.button.id == "cancel_commit":
                self.dismiss(None)
                return
            message = self.query_one("#commit_message", Input).value
            self.dismiss(message)

    class ReleaseScreen(ModalScreen[tuple[str, str, str] | None]):
        CSS = ChangelogEntryScreen.CSS

        def compose(self) -> ComposeResult:
            with Vertical(id="dialog"):
                yield Label("Create Release", classes="section-title")
                yield Input(placeholder="0.1.2", id="release_version")
                yield Select(
                    [(variant, variant) for variant in ("DRAFT", "PRELIMINARY", "CHECKED", "RELEASED")],
                    value="RELEASED",
                    id="release_variant",
                )
                yield Select(
                    [(kind, kind) for kind in RELEASE_KINDS],
                    value="release",
                    id="release_kind",
                )
                yield Button("Dispatch", id="dispatch_release")
                yield Button("Cancel", id="cancel_release")

        def on_button_pressed(self, event: Button.Pressed) -> None:
            if event.button.id == "cancel_release":
                self.dismiss(None)
                return
            version = self.query_one("#release_version", Input).value
            variant = self.query_one("#release_variant", Select).value
            kind = self.query_one("#release_kind", Select).value
            self.dismiss((version, str(variant), str(kind)))

    class ReleaseChecklistScreen(ModalScreen[ReleaseChecklist | None]):
        CSS = ChangelogEntryScreen.CSS

        def __init__(self, checklist: ReleaseChecklist) -> None:
            super().__init__()
            self.checklist = checklist

        def compose(self) -> ComposeResult:
            with Vertical(id="dialog"):
                yield Label("Release Readiness", classes="section-title")
                yield Static(_format_release_checklist(self.checklist), id="release_checklist")
                yield Button("Dispatch", id="confirm_release")
                yield Button("Cancel", id="cancel_release")

        def on_mount(self) -> None:
            self.query_one("#confirm_release", Button).disabled = not self.checklist.can_dispatch

        def on_button_pressed(self, event: Button.Pressed) -> None:
            if event.button.id == "confirm_release":
                self.dismiss(self.checklist)
                return
            self.dismiss(None)

    class BoardwrightTui(App):
        TITLE = "Boardwright"
        SUB_TITLE = "KiCad/KiBot workflow cockpit"

        CSS = """
        Screen {
            layout: vertical;
        }

        #top_status {
            height: 3;
            padding: 1 2 0 2;
            content-align: left middle;
            border-bottom: solid $accent;
            background: $surface;
        }

        #body {
            height: 1fr;
            padding: 0 1 1 1;
        }

        #summary {
            width: 34;
            padding: 1 1;
            border: solid $primary;
        }

        #actions {
            height: 1fr;
            padding-right: 1;
        }

        .action-grid {
            grid-size: 2;
            grid-columns: 1fr 1fr;
            grid-gutter: 0 1;
            height: auto;
        }

        #details {
            width: 1fr;
            padding: 1;
            border: solid $secondary;
        }

        #main_details {
            height: 2fr;
        }

        #timeline_panel {
            width: 2fr;
            padding-right: 1;
        }

        #inspector_panel {
            width: 1fr;
            padding-left: 1;
            border-left: solid $secondary;
        }

        #workflow_status,
        #inspector_status {
            height: 1fr;
            padding: 0 1 1 1;
            background: $boost;
            overflow-y: auto;
        }

        #lower_details {
            height: 1fr;
            margin-top: 1;
        }

        #validation_panel {
            width: 1fr;
            padding-right: 1;
        }

        #git_panel {
            width: 1fr;
            padding-left: 1;
            border-left: solid $secondary;
        }

        #validation_status,
        #git_scroll {
            height: 1fr;
            overflow-y: auto;
        }

        #git_status {
            height: auto;
        }

        .panel-title {
            width: 100%;
            text-style: bold;
            color: $accent;
            margin-bottom: 1;
            content-align: center middle;
            text-align: center;
        }

        .section-title {
            width: 100%;
            text-style: bold;
            margin-bottom: 0;
            margin-top: 1;
            color: $text-muted;
            content-align: center middle;
            text-align: center;
        }

        Button {
            width: 100%;
            height: 5;
            margin-top: 0;
            text-align: center;
        }

        Button.primary-action {
            border: tall $success;
        }

        Button.secondary-action {
            border: tall $primary;
        }

        Button.danger-action {
            border: tall $error;
        }
        """

        BINDINGS = [
            ("q", "quit", "Quit"),
            ("r", "refresh", "Refresh"),
            ("c", "record_change", "Record Change"),
            ("m", "commit_push", "Commit + Push"),
            ("a", "review_artifacts", "Review Artifacts"),
            ("p", "accept_main", "Accept Main"),
            ("l", "release_ci", "Release"),
        ]

        def __init__(self) -> None:
            super().__init__()
            self.state = collect_dashboard_state()
            self.ci_status = "CI not polled"
            self.review_variant = ""

        def compose(self) -> ComposeResult:
            yield Header(show_clock=True)
            yield Static(id="top_status")
            with Horizontal(id="body"):
                with Vertical(id="summary"):
                    yield Label("Actions", classes="panel-title")
                    yield Label("Project", classes="section-title")
                    yield Static(id="project_status")
                    with Vertical(id="actions"):
                        yield Label("Workflow", classes="section-title")
                        with Grid(classes="action-grid"):
                            yield Button("Record\nChanges", id="record_change", classes="primary-action")
                            yield Button("Commit\n+ Push", id="commit_push", classes="primary-action")
                            yield Button("Review\nArtifacts", id="review_artifacts", classes="secondary-action")
                            yield Button("Accept\nto Main", id="accept_main", classes="secondary-action")
                            yield Button("Create\nRelease", id="release_ci", classes="danger-action")
                            yield Button("Refresh", id="refresh", classes="secondary-action")
                with Vertical(id="details"):
                    with Horizontal(id="main_details"):
                        with Vertical(id="timeline_panel"):
                            yield Label("Workflow Timeline", classes="panel-title")
                            yield Static(id="workflow_status")
                        with Vertical(id="inspector_panel"):
                            yield Label("Next Action", classes="panel-title")
                            yield Static(id="inspector_status")
                    with Horizontal(id="lower_details"):
                        with Vertical(id="validation_panel"):
                            yield Label("Validation", classes="panel-title")
                            yield Static(id="validation_status")
                        with Vertical(id="git_panel"):
                            yield Label("Changed Files", classes="panel-title")
                            with VerticalScroll(id="git_scroll"):
                                yield Static(id="git_status")
            yield Footer()

        def on_mount(self) -> None:
            self._render_state()

        def on_button_pressed(self, event: Button.Pressed) -> None:
            if event.button.id == "refresh":
                self.action_refresh()
            elif event.button.id == "record_change":
                self.action_record_change()
            elif event.button.id == "commit_push":
                self.action_commit_push()
            elif event.button.id == "review_artifacts":
                self.action_review_artifacts()
            elif event.button.id == "accept_main":
                self.action_accept_main()
            elif event.button.id == "release_ci":
                self.action_release_ci()

        def action_refresh(self) -> None:
            accepted_state = None
            accepted_error = ""
            try:
                accepted_state = build_accepted_main_state(load_config())
            except BoardwrightError as exc:
                accepted_error = str(exc)
            self.state = collect_dashboard_state(
                accepted_state=accepted_state,
                accepted_error=accepted_error,
            )
            self._render_state()
            self.notify("Refreshed project state.")

        def action_review_artifacts(self) -> None:
            self.push_screen(ReviewVariantScreen(), self._review_artifact_variant)

        def _review_artifact_variant(self, variant: str | None) -> None:
            if variant is None:
                return
            try:
                config = load_config()
                runs = list_recent_workflow_runs(config)
            except BoardwrightError as exc:
                self.ci_status = str(exc)
                self._render_state()
                self.notify(str(exc), severity="error")
                return
            runs_text = _format_ci_runs(runs)
            try:
                preview_state = build_preview_state(config, variant)
                preview_text = format_preview_state(preview_state)
                self.ci_status = _format_review_artifacts(preview_state, runs_text)
                self.state = collect_dashboard_state(preview_state=preview_state)
                self.review_variant = variant
            except BoardwrightError as exc:
                self.ci_status = str(exc)
                self._render_state()
                self.notify(str(exc), severity="error")
                return
            self._render_state()
            self.push_screen(
                ReviewArtifactsScreen(preview_text, runs_text, can_fetch=preview_state.ready),
                self._review_artifacts,
            )

        def _review_artifacts(self, result: str | None) -> None:
            if result != "fetch":
                return
            variant = self.review_variant or load_config().preview_variant
            self.ci_status = _download_progress_text(variant)
            self._render_state()
            self.notify("Downloading preview artifact...")
            Thread(target=self._fetch_review_artifact, args=(variant,), daemon=True).start()

        def _fetch_review_artifact(self, variant: str) -> None:
            try:
                result = fetch_latest_preview_artifact(load_config(), variant)
            except BoardwrightError as exc:
                self.call_from_thread(self._finish_review_fetch, None, str(exc))
                return
            self.call_from_thread(self._finish_review_fetch, result, None)

        def _finish_review_fetch(self, result: str | None, error: str | None) -> None:
            if error:
                self.ci_status = error
                self._render_state()
                self.notify(error, severity="error")
                return
            self.state = collect_dashboard_state()
            self.ci_status = result
            self._render_state()
            self.notify(result or "Preview artifact fetched.")

        def action_record_change(self) -> None:
            self.push_screen(ChangelogEntryScreen(), self._record_change)

        def action_commit_push(self) -> None:
            self.push_screen(CommitScreen(), self._commit_push)

        def action_accept_main(self) -> None:
            self.push_screen(AcceptMainScreen(), self._accept_main)

        def action_release_ci(self) -> None:
            self.push_screen(ReleaseScreen(), self._release_ci)

        def _record_change(self, result: tuple[str, str] | None) -> None:
            if result is None:
                return
            section, message = result
            try:
                config = load_config()
                add_unreleased_entry(config.root, section, message)
                write_revision_variables(config)
                issues = tuple(validate_project(config))
                suggestion = suggest_commit_message(config.root, message)
            except BoardwrightError as exc:
                self.notify(str(exc), severity="error")
                return
            self.state = collect_dashboard_state()
            self._render_state()
            if any(issue.level == "error" for issue in issues):
                self.notify(
                    "Recorded change; validation has blocking issues.",
                    severity="error",
                )
            elif issues:
                self.notify(
                    f"Recorded change; validation has warnings. Suggested commit: {suggestion}",
                    severity="warning",
                )
            else:
                self.notify(f"Recorded change. Suggested commit: {suggestion}")

        def _commit_push(self, result: str | None) -> None:
            if result is None:
                return
            message = result.strip()
            if not message:
                self.notify("Commit message cannot be empty.", severity="error")
                return
            try:
                config = load_config()
                if self.state.status.branch != config.dev_branch:
                    self.notify(
                        f"Cannot commit + push from {self.state.status.branch}; switch to {config.dev_branch}.",
                        severity="error",
                    )
                    return
                if self.state.status.dirty_count and not self.state.status.unreleased_changes:
                    self.notify("Cannot commit: record a changelog entry first.", severity="error")
                    return
                issues = tuple(validate_project(config))
                if any(issue.level == "error" for issue in issues):
                    self.state = collect_dashboard_state()
                    self._render_state()
                    self.notify("Cannot commit: validation failed.", severity="error")
                    return
                write_revision_variables(config)
                output = commit_all(config.root, message, dry_run=False)
                if "fatal:" in output.lower() or "error:" in output.lower():
                    self.state = collect_dashboard_state()
                    self._render_state()
                    self.notify(output, severity="error")
                    return
                push_output = push_branch(config.root, config.dev_branch)
            except BoardwrightError as exc:
                self.notify(str(exc), severity="error")
                return
            self.state = collect_dashboard_state()
            self._render_state()
            if _command_failed(push_output):
                self.notify(push_output, severity="error")
            else:
                message = output or "Committed changes."
                self.notify(f"{message}\n{push_output or 'Pushed dev.'}")

        def _accept_main(self, result: tuple[str, bool] | None) -> None:
            if result is None:
                return
            variant, commit_outputs = result
            try:
                config = load_config()
                preview_state = build_preview_state(config, variant)
                if not preview_state.ready:
                    self.ci_status = format_preview_state(preview_state)
                    self._render_state()
                    self.notify(
                        f"Cannot accept to main: preview is {preview_state.state}.",
                        severity="error",
                    )
                    return
                if not preview_state.reviewed:
                    self.ci_status = format_preview_state(preview_state)
                    self._render_state()
                    self.notify(
                        "Cannot accept to main: review the fresh preview artifact first.",
                        severity="error",
                    )
                    return
                action = build_promote_action(config, variant, commit_outputs)
                dispatch_workflow_action(config, action)
            except BoardwrightError as exc:
                self.notify(str(exc), severity="error")
                return
            self.state = collect_dashboard_state()
            self._render_state()
            self.notify(f"Dispatched {action.workflow} to accept {variant} on main.")

        def _release_ci(self, result: tuple[str, str, str] | None) -> None:
            if result is None:
                return
            version, variant, kind = result
            try:
                config = load_config()
                accepted_state = build_accepted_main_state(config)
                checklist = build_release_checklist(config, version, variant, kind, accepted_state)
            except BoardwrightError as exc:
                self.notify(str(exc), severity="error")
                return
            self.ci_status = _format_release_checklist(checklist)
            self.state = collect_dashboard_state(
                checklist.version or "0.1.0",
                accepted_state=accepted_state,
            )
            self._render_state()
            self.push_screen(ReleaseChecklistScreen(checklist), self._dispatch_release_ci)

        def _dispatch_release_ci(self, checklist: ReleaseChecklist | None) -> None:
            if checklist is None:
                return
            if not checklist.can_dispatch or checklist.action is None:
                self.notify("Cannot create release: checklist has blocking items.", severity="error")
                return
            try:
                config = load_config()
                dispatch_workflow_action(config, checklist.action)
            except BoardwrightError as exc:
                self.notify(str(exc), severity="error")
                return
            self.state = collect_dashboard_state(checklist.version or "0.1.0")
            self._render_state()
            self.notify(f"Dispatched {checklist.action.workflow}.")

        def _render_state(self) -> None:
            status = self.state.status
            dirty_summary = f"{status.dirty_count} changed" if status.dirty_count else "clean"
            self.query_one("#top_status", Static).update(
                _format_top_status(status, self.state.issues, self.ci_status)
            )
            self.query_one("#project_status", Static).update(
                "\n".join(
                    [
                        f"Name: {status.project_name}",
                        f"Unreleased: {'yes' if status.unreleased_changes else 'no'}",
                        f"Git: {dirty_summary}",
                        f"Remote: ahead {status.ahead}, behind {status.behind}",
                    ]
                )
            )
            self.query_one("#workflow_status", Static).update(
                _format_timeline(self.state.workflow.steps)
            )
            self.query_one("#inspector_status", Static).update(
                _format_inspector(self.state, self.ci_status)
            )
            self.query_one("#validation_status", Static).update(
                _format_issues(self.state.issues)
            )
            self.query_one("#git_status", Static).update(
                _format_changed_files(self.state.changed_files)
            )
            for button_id, action_name in (
                ("record_change", "Record Changes"),
                ("commit_push", "Commit + Push"),
                ("review_artifacts", "Review Artifacts"),
                ("accept_main", "Accept to Main"),
                ("release_ci", "Create Release"),
                ("refresh", "Refresh"),
            ):
                self.query_one(f"#{button_id}", Button).disabled = not action_state(
                    self.state.workflow,
                    action_name,
                ).enabled

    return BoardwrightTui


def _format_issues(issues: tuple[ValidationIssue, ...]) -> str:
    if not issues:
        return "Validation passed."
    return "\n".join(f"{issue.level}: {issue.message}" for issue in issues)


def _format_top_status(
    status: ProjectStatus,
    issues: tuple[ValidationIssue, ...],
    ci_status: str,
) -> Text:
    text = Text()
    text.append(status.project_id, style="bold")
    text.append(" | branch ")
    text.append(status.branch, style="cyan")
    text.append(" | git ")
    if status.dirty_count:
        text.append(f"{status.dirty_count} changed", style="bold yellow")
    else:
        text.append("clean", style="bold green")
    if status.ahead or status.behind:
        text.append(" | remote ")
        text.append(f"+{status.ahead}/-{status.behind}", style="bold yellow")
    text.append(" | variant ")
    text.append(status.variant, style="magenta")
    text.append(" | tag ")
    text.append(status.latest_tag or "none", style="cyan" if status.latest_tag else "dim")
    text.append(" | ")
    text.append(_ci_status_short(ci_status), style=_ci_status_style(ci_status))
    text.append(" | ")
    text.append(_issue_summary(issues), style=_issue_summary_style(issues))
    return text


def _issue_summary(issues: tuple[ValidationIssue, ...]) -> str:
    errors = sum(1 for issue in issues if issue.level == "error")
    warnings = sum(1 for issue in issues if issue.level == "warning")
    if errors:
        return f"validation {errors} error(s), {warnings} warning(s)"
    if warnings:
        return f"validation {warnings} warning(s)"
    return "validation ok"


def _issue_summary_style(issues: tuple[ValidationIssue, ...]) -> str:
    if any(issue.level == "error" for issue in issues):
        return "bold red"
    if any(issue.level == "warning" for issue in issues):
        return "bold yellow"
    return "bold green"


def _workflow_steps(state: DashboardState) -> tuple[WorkflowStep, ...]:
    return state.workflow.steps


def _format_timeline(steps: tuple[WorkflowStep, ...]) -> Text:
    text = Text()
    for step in steps:
        text.append(f"{step.label:<20}", style="bold")
        text.append(f" {step.state}\n", style=_workflow_state_style(step.state))
        text.append(f"  {step.detail}\n\n", style="dim")
    return text


def _workflow_state_style(state: str) -> str:
    if state in {"done", "ready", "passed"}:
        return "bold green"
    if state in {"needed", "needs action", "waiting", "running"}:
        return "bold yellow"
    if state in {"blocked", "locked", "failed"}:
        return "bold red"
    if state == "external":
        return "bold cyan"
    return "bold"


def _format_inspector(state: DashboardState, ci_status: str = "CI not polled") -> str:
    return "\n".join(
        [
            f"{state.workflow.next_action}: {state.workflow.reason}",
            f"Stage: {state.workflow.stage}",
            "",
            "Preview runs from dev pushes.",
            "Accepted main:",
            state.accepted_summary,
            "",
            f"Release: {state.ci_release_summary}",
            "",
            "Latest CI:",
            ci_status,
        ]
    )


def _format_review_artifacts(preview_state: "PreviewState", runs_text: str) -> str:
    run = preview_state.run
    lines = [
        f"{preview_state.state.upper()}: {preview_state.artifact_name}",
        preview_state.message,
    ]
    if run is not None:
        lines.extend(
            [
                "",
                f"Run {run.database_id or 'unknown'} | {run.status}/{run.conclusion or 'unknown'}",
                f"{run.branch or 'unknown'} @ {(run.head_sha or '')[:12] or 'unknown'}",
                f"Created {run.created_at or 'unknown'}",
                f"Reviewed: {'yes' if preview_state.reviewed else 'no'}",
            ]
        )
    lines.extend(["", "Recent CI:", runs_text])
    return "\n".join(lines)


def _download_progress_text(variant: str) -> str:
    return (
        f"Downloading boardwright-preview-{variant}...\n"
        "[##########] fetching artifact with GitHub CLI"
    )


def build_release_checklist(
    config: "BoardwrightConfig",
    version: str,
    variant: str,
    kind: str,
    accepted_state: "AcceptedMainState",
) -> ReleaseChecklist:
    selected_version = version.strip()
    selected_variant = variant.strip()
    selected_kind = kind.strip()
    accepted_summary = format_accepted_state(accepted_state)
    action: WorkflowAction | None = None
    action_error = ""
    release_plan = None
    release_error = ""

    try:
        action = build_prepare_release_action(
            config,
            selected_version,
            selected_variant,
            selected_kind,
        )
    except BoardwrightError as exc:
        action_error = str(exc)

    try:
        release_plan = build_release_plan(config, selected_version, check_remote=False)
    except BoardwrightError as exc:
        release_error = str(exc)

    items = [
        ReleaseChecklistItem(
            "Accepted main outputs",
            accepted_state.ready,
            accepted_state.message or f"State: {accepted_state.state}",
        ),
        ReleaseChecklistItem(
            "Release inputs",
            action is not None,
            (
                f"{selected_version} | {selected_variant} | {selected_kind}"
                if action is not None
                else action_error
            ),
        ),
        ReleaseChecklistItem(
            "Unreleased changelog",
            bool(release_plan and release_plan.has_unreleased_changes),
            (
                "CHANGELOG.md has content ready to promote."
                if release_plan and release_plan.has_unreleased_changes
                else "CHANGELOG.md has no unreleased changes."
            ),
        ),
        ReleaseChecklistItem(
            "Release tag available",
            bool(release_plan and not release_plan.local_tag_exists),
            (
                f"No local tag named {selected_version}."
                if release_plan and not release_plan.local_tag_exists
                else release_error or f"Local tag already exists: {selected_version}."
            ),
        ),
        ReleaseChecklistItem(
            "Dispatch target",
            action is not None,
            (
                f"{action.workflow} on {action.ref}"
                if action is not None
                else "Dispatch action could not be built."
            ),
        ),
    ]
    return ReleaseChecklist(
        selected_version,
        selected_variant,
        selected_kind,
        accepted_summary,
        tuple(items),
        action,
    )


def _format_release_checklist(checklist: ReleaseChecklist) -> str:
    lines = [
        f"Version: {checklist.version or '(blank)'}",
        f"Variant: {checklist.variant}",
        f"Kind: {checklist.kind}",
        "",
        "Accepted main:",
        checklist.accepted_summary,
        "",
        "Checklist:",
    ]
    for item in checklist.items:
        marker = "[x]" if item.passed else "[ ]"
        lines.append(f"{marker} {item.label}: {item.detail}")
    lines.extend(
        [
            "",
            (
                "Ready to dispatch prepare-release."
                if checklist.can_dispatch
                else "Resolve blocking items before dispatch."
            ),
        ]
    )
    return "\n".join(lines)


def _next_action(state: DashboardState) -> str:
    return f"{state.workflow.next_action}: {state.workflow.reason}"


def _format_ci_runs(runs: tuple[object, ...]) -> str:
    if not runs:
        return "No recent workflow runs found."

    lines: list[str] = []
    for run in runs[:5]:
        workflow = getattr(run, "workflow", "unknown")
        status = getattr(run, "status", "unknown")
        conclusion = getattr(run, "conclusion", "") or "pending"
        branch = getattr(run, "branch", "")
        run_id = getattr(run, "database_id", "")
        lines.append(f"{workflow}: {status}/{conclusion} on {branch} #{run_id}")
    return "\n".join(lines)


def _ci_status_short(ci_status: str) -> str:
    first_line = ci_status.splitlines()[0] if ci_status else "CI not polled"
    if len(first_line) > 36:
        return first_line[:33] + "..."
    return first_line


def _ci_status_style(ci_status: str) -> str:
    lowered = ci_status.lower()
    if "failure" in lowered or "failed" in lowered or "error" in lowered:
        return "bold red"
    if "success" in lowered or "completed" in lowered:
        return "bold green"
    if "in_progress" in lowered or "queued" in lowered or "pending" in lowered:
        return "bold yellow"
    return "dim"


def _field_value(fields: tuple[tuple[str, str], ...], key: str) -> str:
    return next((value for field_key, value in fields if field_key == key), "")


def _notification_severity(issues: tuple[ValidationIssue, ...]) -> str:
    if any(issue.level == "error" for issue in issues):
        return "error"
    return "warning"


def _command_failed(output: str) -> bool:
    lowered = output.lower()
    return any(marker in lowered for marker in ("fatal:", "error:", "rejected", "failed"))


def _format_changed_files(changed_files: tuple[str, ...]) -> str:
    if not changed_files:
        return "Working tree clean."
    return "\n".join(changed_files[:12])
