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
from .config import load_config, update_project_config
from .errors import BoardwrightError
from .git_ops import commit_all, dirty_files, push_branch
from .preview import fetch_latest_preview_artifact
from .preview import build_preview_plan, build_preview_state, dispatch_preview, format_preview_state
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
    from textual.widgets import Button, Footer, Header, Input, Label, ProgressBar, Select, Static

    class ChangelogEntryScreen(ModalScreen[tuple[str, str] | None]):
        CSS = """
        ChangelogEntryScreen,
        ReviewVariantScreen,
        PreviewDispatchScreen,
        ProjectInfoScreen,
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

        #review_status {
            padding: 1;
            border: solid $secondary;
            background: $boost;
        }

        #project_info_scroll {
            height: 28;
            max-height: 65vh;
        }

        #review_message {
            height: auto;
            margin-bottom: 1;
        }

        .muted {
            color: $text-muted;
        }

        Input,
        Select {
            margin-bottom: 1;
        }

        Button {
            width: 100%;
            height: 3;
            min-height: 3;
            margin-top: 0;
            content-align: center middle;
            text-align: center;
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
            preview_status: str,
            preview_message: str,
            run_summary: str,
            runs_text: str,
            can_fetch: bool,
        ) -> None:
            super().__init__()
            self.preview_status = preview_status
            self.preview_message = preview_message
            self.run_summary = run_summary
            self.runs_text = runs_text
            self.can_fetch = can_fetch

        def compose(self) -> ComposeResult:
            with Vertical(id="dialog"):
                yield Label("Review Artifacts", classes="section-title")
                with Vertical(id="review_status"):
                    yield Static(self.preview_status, id="review_state")
                    yield Static(self.preview_message, id="review_message", classes="muted")
                    yield Static(self.run_summary, id="review_run")
                yield ProgressBar(total=100, show_eta=False, id="fetch_progress")
                yield Static(_ci_runs_brief(self.runs_text), id="recent_runs", classes="muted")
                yield Button("Fetch Artifact", id="fetch_artifact")
                yield Button("Close", id="close_review")

        def on_mount(self) -> None:
            self.query_one("#fetch_artifact", Button).disabled = not self.can_fetch
            self.query_one("#fetch_progress", ProgressBar).display = False

        def on_button_pressed(self, event: Button.Pressed) -> None:
            if event.button.id == "fetch_artifact":
                progress = self.query_one("#fetch_progress", ProgressBar)
                progress.display = True
                progress.update(progress=35)
                self.query_one("#fetch_artifact", Button).disabled = True
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

    class PreviewDispatchScreen(ModalScreen[str | None]):
        CSS = ChangelogEntryScreen.CSS

        def compose(self) -> ComposeResult:
            config = load_config()
            with Vertical(id="dialog"):
                yield Label("Generate Preview", classes="section-title")
                yield Static(
                    f"Dispatches {config.preview_workflow} from {config.dev_branch}.",
                    classes="muted",
                )
                yield Select(
                    [(variant, variant) for variant in ("DRAFT", "PRELIMINARY", "CHECKED", "RELEASED")],
                    value=config.preview_variant,
                    id="preview_variant",
                )
                yield Button("Generate Preview", id="dispatch_preview")
                yield Button("Cancel", id="cancel_preview")

        def on_button_pressed(self, event: Button.Pressed) -> None:
            if event.button.id == "cancel_preview":
                self.dismiss(None)
                return
            variant = self.query_one("#preview_variant", Select).value
            self.dismiss(str(variant))

    class ProjectInfoScreen(ModalScreen[dict[str, dict[str, str]] | None]):
        CSS = ChangelogEntryScreen.CSS

        def compose(self) -> ComposeResult:
            config = load_config()
            project = config.project.get("project", {})
            assets = config.assets
            with Vertical(id="dialog"):
                yield Label("Project Info", classes="section-title")
                with VerticalScroll(id="project_info_scroll"):
                    yield Input(value=str(project.get("id", "")), placeholder="Project ID", id="project_id")
                    yield Input(value=str(project.get("name", "")), placeholder="Project name", id="project_name")
                    yield Input(value=config.board_name, placeholder="Board name", id="project_board_name")
                    yield Input(value=str(project.get("company", "")), placeholder="Company", id="project_company")
                    yield Input(value=str(project.get("designer", "")), placeholder="Designer", id="project_designer")
                    yield Input(value=str(project.get("git_url", "")), placeholder="Git URL", id="project_git_url")
                    yield Input(value=str(project.get("github_repo", "")), placeholder="owner/repo", id="project_github_repo")
                    yield Label("Variant Defaults", classes="section-title")
                    for key, label, value in (
                        ("variant_dev", "Dev", config.default_variant),
                        ("variant_preview", "Preview", config.preview_variant),
                        ("variant_main", "Accepted main", config.main_variant),
                        ("variant_release", "Release", config.release_variant),
                    ):
                        yield Static(label, classes="muted")
                        yield Select(
                            [(variant, variant) for variant in ("DRAFT", "PRELIMINARY", "CHECKED", "RELEASED")],
                            value=value,
                            id=key,
                        )
                    yield Label("Assets", classes="section-title")
                    yield Input(value=str(assets.get("logo", "")), placeholder="Logo path", id="asset_logo")
                    yield Input(
                        value=str(assets.get("product_image", "")),
                        placeholder="Product image path",
                        id="asset_product_image",
                    )
                yield Button("Save Project Info", id="save_project_info")
                yield Button("Cancel", id="cancel_project_info")

        def on_button_pressed(self, event: Button.Pressed) -> None:
            if event.button.id == "cancel_project_info":
                self.dismiss(None)
                return
            self.dismiss(
                {
                    "project": {
                        "id": self.query_one("#project_id", Input).value.strip(),
                        "name": self.query_one("#project_name", Input).value.strip(),
                        "board_name": self.query_one("#project_board_name", Input).value.strip(),
                        "company": self.query_one("#project_company", Input).value.strip(),
                        "designer": self.query_one("#project_designer", Input).value.strip(),
                        "git_url": self.query_one("#project_git_url", Input).value.strip(),
                        "github_repo": self.query_one("#project_github_repo", Input).value.strip(),
                    },
                    "variants": {
                        "dev_default": str(self.query_one("#variant_dev", Select).value),
                        "preview_default": str(self.query_one("#variant_preview", Select).value),
                        "main_default": str(self.query_one("#variant_main", Select).value),
                        "release_default": str(self.query_one("#variant_release", Select).value),
                    },
                    "assets": {
                        "logo": self.query_one("#asset_logo", Input).value.strip(),
                        "product_image": self.query_one("#asset_product_image", Input).value.strip(),
                    },
                }
            )

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

        def __init__(self, suggested_message: str) -> None:
            super().__init__()
            self.suggested_message = suggested_message

        def compose(self) -> ComposeResult:
            with Vertical(id="dialog"):
                yield Label("Commit + Push", classes="section-title")
                yield Input(
                    value=self.suggested_message,
                    placeholder="feat: describe the board change",
                    id="commit_message",
                )
                yield Button("Commit + Push", id="confirm_commit_push")
                yield Button("Cancel", id="cancel_commit")

        def on_button_pressed(self, event: Button.Pressed) -> None:
            event.stop()
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
            width: 100%;
            height: 3;
            padding: 1 2 0 2;
            content-align: left middle;
            border-bottom: solid $accent;
            background: $surface;
            overflow-x: auto;
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
            grid-gutter: 1 1;
            height: auto;
        }

        #details {
            width: 1fr;
            padding: 1;
            border: solid $secondary;
        }

        #main_details {
            height: 13;
            min-height: 11;
        }

        #timeline_panel {
            width: 38;
            min-width: 32;
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

        Button.action-button {
            width: 100%;
            height: 4;
            min-height: 4;
            margin-top: 0;
            content-align: center middle;
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
            ("g", "generate_preview", "Generate Preview"),
            ("a", "review_artifacts", "Review Artifacts"),
            ("p", "accept_main", "Accept Main"),
            ("l", "release_ci", "Release"),
            ("i", "project_info", "Project Info"),
        ]

        def __init__(self) -> None:
            super().__init__()
            self.state = collect_dashboard_state()
            self.ci_status = "CI not polled"
            self.review_variant = ""
            self._ci_polling = False

        def compose(self) -> ComposeResult:
            yield Header(show_clock=True)
            yield Static(id="top_status")
            with Horizontal(id="body"):
                with Vertical(id="summary"):
                    yield Label("Actions", classes="panel-title")
                    yield Label("Project", classes="section-title")
                    yield Static(id="project_status")
                    with Vertical(id="actions"):
                        yield Label("Work", classes="section-title")
                        with Grid(classes="action-grid"):
                            yield Button("Record\nChanges", id="record_change", classes="action-button primary-action")
                            yield Button("Commit\n+ Push", id="commit_push", classes="action-button primary-action")
                        yield Label("Preview", classes="section-title")
                        with Grid(classes="action-grid"):
                            yield Button("Generate\nPreview", id="generate_preview", classes="action-button secondary-action")
                            yield Button("Review\nArtifacts", id="review_artifacts", classes="action-button secondary-action")
                        yield Button("Accept\nto Main", id="accept_main", classes="action-button secondary-action")
                        yield Label("Release", classes="section-title")
                        yield Button("Create\nRelease", id="release_ci", classes="action-button danger-action")
                        yield Label("Setup", classes="section-title")
                        with Grid(classes="action-grid"):
                            yield Button("Project\nInfo", id="project_info", classes="action-button secondary-action")
                            yield Button("Refresh", id="refresh", classes="action-button secondary-action")
                with Vertical(id="details"):
                    with Horizontal(id="main_details"):
                        with Vertical(id="timeline_panel"):
                            yield Label("Workflow", classes="panel-title")
                            yield Static(id="workflow_status")
                        with Vertical(id="inspector_panel"):
                            yield Label("Now", classes="panel-title")
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
            self._poll_ci_status()
            self.set_interval(60, self._poll_ci_status)

        def on_button_pressed(self, event: Button.Pressed) -> None:
            if event.button.id == "refresh":
                self.action_refresh()
            elif event.button.id == "record_change":
                self.action_record_change()
            elif event.button.id == "commit_push":
                self.action_commit_push()
            elif event.button.id == "generate_preview":
                self.action_generate_preview()
            elif event.button.id == "review_artifacts":
                self.action_review_artifacts()
            elif event.button.id == "accept_main":
                self.action_accept_main()
            elif event.button.id == "release_ci":
                self.action_release_ci()
            elif event.button.id == "project_info":
                self.action_project_info()

        def action_refresh(self) -> None:
            self.state = collect_dashboard_state()
            self.ci_status = "Polling CI..."
            self._render_state()
            self.notify("Refreshed project state.")
            self._poll_ci_status(notify=True)

        def _poll_ci_status(self, notify: bool = False) -> None:
            if self._ci_polling:
                return
            self._ci_polling = True
            if self.ci_status in {"CI not polled", "Polling CI..."}:
                self.ci_status = "Polling CI..."
                self._render_state()
            Thread(target=self._poll_ci_status_worker, args=(notify,), daemon=True).start()

        def _poll_ci_status_worker(self, notify: bool) -> None:
            preview_state = None
            accepted_state = None
            preview_error = ""
            accepted_error = ""
            try:
                config = load_config()
                try:
                    preview_state = build_preview_state(config, config.preview_variant)
                except BoardwrightError as exc:
                    preview_error = str(exc)
                try:
                    accepted_state = build_accepted_main_state(config)
                except BoardwrightError as exc:
                    accepted_error = str(exc)
                state = collect_dashboard_state(
                    preview_state=preview_state,
                    accepted_state=accepted_state,
                    accepted_error=accepted_error,
                )
                ci_status = _format_polled_ci_status(
                    preview_state,
                    accepted_state,
                    preview_error,
                    accepted_error,
                )
            except BoardwrightError as exc:
                self.call_from_thread(self._finish_ci_poll, None, str(exc), notify)
                return
            self.call_from_thread(self._finish_ci_poll, state, ci_status, notify)

        def _finish_ci_poll(
            self,
            state: DashboardState | None,
            ci_status: str,
            notify: bool,
        ) -> None:
            self._ci_polling = False
            if state is not None:
                self.state = state
            self.ci_status = ci_status
            self._render_state()
            if notify:
                self.notify("Polled CI status.")

        def action_review_artifacts(self) -> None:
            if not self._require_action("Review Artifacts"):
                return
            self.push_screen(ReviewVariantScreen(), self._review_artifact_variant)

        def action_generate_preview(self) -> None:
            if not self._require_action("Generate Preview"):
                return
            self.push_screen(PreviewDispatchScreen(), self._generate_preview)

        def _generate_preview(self, variant: str | None) -> None:
            if variant is None:
                return
            try:
                config = load_config()
                if self.state.status.branch != config.dev_branch:
                    self.notify(
                        f"Cannot generate preview from {self.state.status.branch}; switch to {config.dev_branch}.",
                        severity="error",
                    )
                    return
                if self.state.status.dirty_count:
                    self.notify("Commit and push local changes before generating preview.", severity="error")
                    return
                if self.state.status.ahead:
                    self.notify("Push local commits before generating preview.", severity="error")
                    return
                plan = build_preview_plan(config, variant)
            except BoardwrightError as exc:
                self.notify(str(exc), severity="error")
                return
            self.ci_status = f"Dispatching preview {plan.variant} via {plan.workflow}..."
            self._render_state()
            Thread(target=self._dispatch_preview, args=(plan.variant,), daemon=True).start()

        def _dispatch_preview(self, variant: str) -> None:
            try:
                config = load_config()
                plan = build_preview_plan(config, variant)
                dispatch_preview(plan, config.root)
            except BoardwrightError as exc:
                self.call_from_thread(self._finish_preview_dispatch, variant, str(exc))
                return
            self.call_from_thread(self._finish_preview_dispatch, variant, None)

        def _finish_preview_dispatch(self, variant: str, error: str | None) -> None:
            if error:
                self.ci_status = error
                self._render_state()
                self.notify(error, severity="error")
                return
            self.state = collect_dashboard_state()
            self.ci_status = f"Preview workflow dispatched for {variant}."
            self._render_state()
            self.notify(f"Preview workflow dispatched for {variant}.")

        def action_project_info(self) -> None:
            if not self._require_action("Project Info"):
                return
            self.push_screen(ProjectInfoScreen(), self._save_project_info)

        def _save_project_info(self, result: dict[str, dict[str, str]] | None) -> None:
            if result is None:
                return
            try:
                config = load_config()
                path = update_project_config(
                    config,
                    project_fields=result["project"],
                    variant_fields=result["variants"],
                    asset_fields=result["assets"],
                )
                self.state = collect_dashboard_state()
            except BoardwrightError as exc:
                self.notify(str(exc), severity="error")
                return
            self._render_state()
            self.notify(f"Updated {path.relative_to(load_config().root)}.")

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
                preview_status, preview_message, run_summary = _review_artifact_blocks(preview_state)
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
                ReviewArtifactsScreen(
                    preview_status,
                    preview_message,
                    run_summary,
                    runs_text,
                    can_fetch=preview_state.ready,
                ),
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
            if not self._require_action("Record Changes"):
                return
            self.push_screen(ChangelogEntryScreen(), self._record_change)

        def action_commit_push(self) -> None:
            if not self._require_action("Commit + Push"):
                return
            self.push_screen(CommitScreen(_suggested_commit_message()), self._commit_push)

        def action_accept_main(self) -> None:
            if not self._require_action("Accept to Main"):
                return
            self.push_screen(AcceptMainScreen(), self._accept_main)

        def action_release_ci(self) -> None:
            if not self._require_action("Create Release"):
                return
            self.push_screen(ReleaseScreen(), self._release_ci)

        def _require_action(self, name: str) -> bool:
            action = action_state(self.state.workflow, name)
            if action.enabled:
                return True
            self.notify(action.reason, severity="warning")
            return False

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
            self.ci_status = "Commit + push running..."
            self._render_state()
            self.notify("Committing and pushing dev...")
            Thread(target=self._commit_push_worker, args=(message,), daemon=True).start()

        def _commit_push_worker(self, message: str) -> None:
            try:
                config = load_config()
                status = collect_status(config)
                if status.branch != config.dev_branch:
                    self.call_from_thread(
                        self._finish_commit_push,
                        "",
                        f"Cannot commit + push from {status.branch}; switch to {config.dev_branch}.",
                    )
                    return
                if status.dirty_count and not status.unreleased_changes:
                    self.call_from_thread(
                        self._finish_commit_push,
                        "",
                        "Cannot commit: record a changelog entry first.",
                    )
                    return
                issues = tuple(validate_project(config))
                if any(issue.level == "error" for issue in issues):
                    self.call_from_thread(
                        self._finish_commit_push,
                        "",
                        "Cannot commit: validation failed.",
                    )
                    return
                write_revision_variables(config)
                output = commit_all(config.root, message, dry_run=False)
                if "fatal:" in output.lower() or "error:" in output.lower():
                    self.call_from_thread(self._finish_commit_push, "", output)
                    return
                push_output = push_branch(config.root, config.dev_branch)
            except BoardwrightError as exc:
                self.call_from_thread(self._finish_commit_push, "", str(exc))
                return
            result_message = f"{output or 'Committed changes.'}\n{push_output or 'Pushed dev.'}"
            self.call_from_thread(
                self._finish_commit_push,
                result_message,
                push_output if _command_failed(push_output) else None,
            )

        def _finish_commit_push(self, result: str, error: str | None) -> None:
            self.state = collect_dashboard_state()
            self.ci_status = error or result or "Commit + push complete."
            self._render_state()
            if error:
                self.notify(error, severity="error")
            else:
                self.notify(result or "Commit + push complete.")

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
                action = build_promote_action(
                    config,
                    variant,
                    commit_outputs,
                    source_ref=config.dev_branch,
                    source_sha=preview_state.expected_sha,
                )
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
                        f"Variant: {status.variant}",
                        f"Preview: {load_config().preview_variant}",
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
                ("generate_preview", "Generate Preview"),
                ("review_artifacts", "Review Artifacts"),
                ("accept_main", "Accept to Main"),
                ("release_ci", "Create Release"),
                ("project_info", "Project Info"),
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
        text.append(f"{_workflow_marker(step.state)} ", style=_workflow_state_style(step.state))
        text.append(f"{step.label:<18}", style="bold")
        text.append(" ")
        text.append(step.state, style=_workflow_state_style(step.state))
        text.append("\n")
    return text


def _workflow_marker(state: str) -> str:
    if state in {"done", "passed"}:
        return "[x]"
    if state in {"ready", "needed"}:
        return "[>]"
    if state == "running":
        return "[~]"
    if state in {"blocked", "failed", "locked"}:
        return "[!]"
    return "[ ]"


def _workflow_state_style(state: str) -> str:
    if state in {"done", "ready", "passed"}:
        return "bold green"
    if state in {"needed", "needs action", "waiting", "running", "missing", "stale"}:
        return "bold yellow"
    if state in {"blocked", "locked", "failed"}:
        return "bold red"
    if state == "external":
        return "bold cyan"
    return "bold"


def _suggested_commit_message(seed: str = "") -> str:
    try:
        return suggest_commit_message(load_config().root, seed)
    except BoardwrightError:
        return ""


def _format_inspector(state: DashboardState, ci_status: str = "CI not polled") -> Text:
    text = Text()
    _append_inspector_heading(text, "NOW")
    text.append(state.workflow.next_action, style="bold")
    text.append("\n")
    text.append(state.workflow.reason, style="dim")
    text.append("\n")
    text.append(f"Stage: {state.workflow.stage}", style="dim")
    text.append("\n\n")

    _append_inspector_heading(text, "EVIDENCE")
    text.append("Preview: ", style="bold")
    text.append(_ci_status_short(ci_status), style=_ci_status_style(ci_status))
    text.append("\n")
    text.append("Accepted main: ", style="bold")
    text.append(_accepted_summary_short(state.accepted_summary), style=_accepted_summary_style(state.accepted_summary))
    text.append("\n")
    text.append("Preview CI is dispatched manually from clean dev.", style="dim")
    text.append("\n\n")

    _append_inspector_heading(text, "RELEASE")
    text.append(_release_summary_short(state), style=_release_summary_style(state.release_summary))
    text.append("\n")
    text.append(state.ci_release_summary, style="dim")
    return text


def _append_inspector_heading(text: Text, label: str) -> None:
    text.append(label, style="bold cyan")
    text.append("\n")


def _accepted_summary_short(summary: str) -> str:
    lines = [line.strip() for line in summary.splitlines() if line.strip()]
    state = _line_value(lines, "State")
    run = _line_value(lines, "Run")
    status = _line_value(lines, "Status")
    message = _last_non_metadata_line(lines)

    if state:
        result = state
        if status:
            result += f" ({status})"
        if run:
            result += f" run {run}"
        if message and not message.startswith(("State:", "Status:", "Run:")):
            result += f" - {message}"
        return result
    return lines[0] if lines else "not checked"


def _accepted_summary_style(summary: str) -> str:
    state = _line_value([line.strip() for line in summary.splitlines()], "State")
    if state == "ready":
        return "bold green"
    if state in {"failed", "stale"}:
        return "bold red"
    if state in {"missing", "running"}:
        return "bold yellow"
    return "dim"


def _last_non_metadata_line(lines: list[str]) -> str:
    metadata_prefixes = (
        "Workflow:",
        "State:",
        "Expected source SHA:",
        "Run:",
        "Branch:",
        "Run SHA:",
        "Created:",
        "Status:",
    )
    for line in reversed(lines):
        if not line.startswith(metadata_prefixes):
            return line
    return ""


def _release_summary_short(state: DashboardState) -> str:
    if state.release_summary == "ready for dry-run":
        return "Release inputs look valid."
    return state.release_summary


def _release_summary_style(summary: str) -> str:
    return "bold green" if summary == "ready for dry-run" else "bold yellow"


def _format_review_artifacts(preview_state: "PreviewState", runs_text: str) -> str:
    status, message, run_summary = _review_artifact_blocks(preview_state)
    return "\n".join([status, message, "", run_summary, "", "Recent CI:", runs_text])


def _format_polled_ci_status(
    preview_state: "PreviewState | None",
    accepted_state: "AcceptedMainState | None",
    preview_error: str = "",
    accepted_error: str = "",
) -> str:
    lines: list[str] = []
    if preview_state is not None:
        lines.append(format_preview_state(preview_state))
    elif preview_error:
        lines.append(f"Preview: {preview_error}")
    else:
        lines.append("Preview: not checked")

    lines.append("")
    lines.append("Accepted main:")
    if accepted_state is not None:
        lines.append(format_accepted_state(accepted_state))
    elif accepted_error:
        lines.append(accepted_error)
    else:
        lines.append("Accepted main evidence not checked.")
    return "\n".join(lines)


def _review_artifact_blocks(preview_state: "PreviewState") -> tuple[str, str, str]:
    run = preview_state.run
    status = f"{preview_state.state.upper()} | {preview_state.artifact_name}"
    message = preview_state.message or "No preview message."
    if run is not None:
        run_summary = (
            f"Run {run.database_id or 'unknown'}  "
            f"{run.status}/{run.conclusion or 'unknown'}\n"
            f"{run.branch or 'unknown'} @ {(run.head_sha or '')[:12] or 'unknown'}\n"
            f"Created {run.created_at or 'unknown'}  "
            f"Reviewed {'yes' if preview_state.reviewed else 'no'}"
        )
    else:
        run_summary = "No matching preview run."
    return status, message, run_summary


def _ci_runs_brief(runs_text: str) -> str:
    lines = [line for line in runs_text.splitlines() if line.strip()]
    return "\n".join(lines[:3]) if lines else "No recent CI runs."


def _download_progress_text(variant: str) -> str:
    return (
        f"Downloading boardwright-preview-{variant}...\n"
        "[###.......] fetching artifact with GitHub CLI"
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
        f"Release {checklist.version or '(blank)'} | {checklist.variant} | {checklist.kind}",
        "",
        "Readiness:",
    ]
    blockers: list[str] = []
    for item in checklist.items:
        marker = "[x]" if item.passed else "[ ]"
        lines.append(f"{marker} {item.label}")
        if not item.passed:
            blockers.append(f"- {item.label}: {item.detail}")
    lines.extend(
        [
            "",
            "Accepted main:",
            _accepted_summary_short(checklist.accepted_summary),
        ]
    )
    if blockers:
        lines.extend(["", "Blockers:", *blockers])
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
    lines = ci_status.splitlines()

    if first_line.startswith("Artifact: boardwright-preview-"):
        variant = first_line.removeprefix("Artifact: boardwright-preview-").strip()
        state = _line_value(lines, "State") or "unknown"
        return f"preview {variant} {state}"

    if " | boardwright-preview-" in first_line:
        state, artifact = first_line.split(" | ", 1)
        variant = artifact.removeprefix("boardwright-preview-").strip()
        return f"preview {variant} {state.lower()}"

    if first_line.startswith("Fetched boardwright-preview-"):
        variant = first_line.removeprefix("Fetched boardwright-preview-").split()[0]
        return f"preview {variant} fetched"

    if first_line.startswith("Downloading boardwright-preview-"):
        variant = first_line.removeprefix("Downloading boardwright-preview-").split("...", 1)[0]
        return f"preview {variant} downloading"

    if first_line.startswith("Preview workflow dispatched for "):
        variant = first_line.removeprefix("Preview workflow dispatched for ").rstrip(".")
        return f"preview {variant} dispatched"

    if first_line.startswith("Dispatching preview "):
        words = first_line.split()
        variant = words[2] if len(words) > 2 else ""
        return f"preview {variant} dispatching".strip()

    if len(first_line) > 36:
        return first_line[:33] + "..."
    return first_line


def _line_value(lines: list[str], key: str) -> str:
    prefix = f"{key}:"
    for line in lines:
        if line.startswith(prefix):
            return line.removeprefix(prefix).strip()
    return ""


def _ci_status_style(ci_status: str) -> str:
    lowered = ci_status.lower()
    if "failure" in lowered or "failed" in lowered or "error" in lowered:
        return "bold red"
    if "success" in lowered or "completed" in lowered or "ready" in lowered or "fetched" in lowered:
        return "bold green"
    if (
        "in_progress" in lowered
        or "queued" in lowered
        or "pending" in lowered
        or "running" in lowered
        or "polling" in lowered
        or "dispatching" in lowered
        or "downloading" in lowered
    ):
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
