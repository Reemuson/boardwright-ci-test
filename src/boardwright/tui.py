from __future__ import annotations

from dataclasses import dataclass
from importlib.util import find_spec
from typing import TYPE_CHECKING

from .changelog import SUPPORTED_SECTIONS, add_unreleased_entry
from .commit_messages import suggest_commit_message
from .config import load_config
from .errors import BoardwrightError
from .git_ops import dirty_files
from .preview import build_preview_plan
from .release import build_release_plan, validate_release_plan
from .revision_history import write_revision_variables
from .status import ProjectStatus, collect_status
from .validation import ValidationIssue, validate_project

if TYPE_CHECKING:
    from .config import BoardwrightConfig


INSTALL_HINT = 'Textual is not installed. Install the TUI with: pip install -e ".[tui]"'


@dataclass(frozen=True)
class DashboardState:
    status: ProjectStatus
    issues: tuple[ValidationIssue, ...]
    preview_summary: str
    release_summary: str
    changed_files: tuple[str, ...]


def textual_available() -> bool:
    return find_spec("textual") is not None


def run() -> int:
    if not textual_available():
        _run_console_fallback()
        return 0

    app = _build_textual_app()
    app().run()
    return 0


def collect_dashboard_state(release_version: str = "0.1.0") -> DashboardState:
    config = load_config()
    status = collect_status(config)
    issues = tuple(validate_project(config))
    preview_plan = build_preview_plan(config)
    release_plan = build_release_plan(config, release_version, check_remote=False)
    release_problems = validate_release_plan(release_plan, allow_dirty=True)

    preview_summary = (
        f"{preview_plan.workflow} | {preview_plan.variant} | "
        f"{preview_plan.branch} -> {preview_plan.preview_branch}"
    )
    release_summary = (
        "ready for dry-run"
        if not release_problems
        else "; ".join(release_problems)
    )
    return DashboardState(
        status,
        issues,
        preview_summary,
        release_summary,
        tuple(dirty_files(config.root)),
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
    from textual.containers import Horizontal, Vertical
    from textual.screen import ModalScreen
    from textual.widgets import Button, Footer, Header, Input, Label, Select, Static

    class ChangelogEntryScreen(ModalScreen[tuple[str, str] | None]):
        CSS = """
        ChangelogEntryScreen {
            align: center middle;
        }

        #dialog {
            width: 72;
            padding: 1 2;
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

    class BoardwrightTui(App):
        TITLE = "Boardwright"
        SUB_TITLE = "KiCad/KiBot workflow cockpit"

        CSS = """
        Screen {
            layout: vertical;
        }

        #splash {
            height: 5;
            padding: 1 2;
            border: solid $accent;
        }

        #body {
            height: 1fr;
            padding: 1;
        }

        #summary {
            width: 36;
            padding: 1 2;
            border: solid $primary;
        }

        #details {
            width: 1fr;
            padding: 1 2;
            border: solid $secondary;
        }

        .section-title {
            text-style: bold;
            margin-bottom: 1;
        }

        Button {
            width: 100%;
            margin-top: 1;
        }
        """

        BINDINGS = [
            ("q", "quit", "Quit"),
            ("r", "refresh", "Refresh"),
            ("v", "validate", "Validate"),
            ("h", "revision_history", "Revision History"),
            ("c", "record_change", "Record Change"),
        ]

        def __init__(self) -> None:
            super().__init__()
            self.state = collect_dashboard_state()

        def compose(self) -> ComposeResult:
            yield Header(show_clock=True)
            yield Static(
                "Boardwright\nGuided PCB/PCBA project workflow for KiCad and KiBot",
                id="splash",
            )
            with Horizontal(id="body"):
                with Vertical(id="summary"):
                    yield Label("Boardwright", classes="section-title")
                    yield Static(id="project_status")
                    yield Button("Refresh", id="refresh")
                    yield Button("Record Change", id="record_change")
                    yield Button("Validate", id="validate")
                    yield Button("Write Revision History", id="revision_history")
                with Vertical(id="details"):
                    yield Label("Workflow", classes="section-title")
                    yield Static(id="workflow_status")
                    yield Label("Validation", classes="section-title")
                    yield Static(id="validation_status")
                    yield Label("Git", classes="section-title")
                    yield Static(id="git_status")
            yield Footer()

        def on_mount(self) -> None:
            self._render_state()

        def on_button_pressed(self, event: Button.Pressed) -> None:
            if event.button.id == "refresh":
                self.action_refresh()
            elif event.button.id == "validate":
                self.action_validate()
            elif event.button.id == "revision_history":
                self.action_revision_history()
            elif event.button.id == "record_change":
                self.action_record_change()

        def action_refresh(self) -> None:
            self.state = collect_dashboard_state()
            self._render_state()
            self.notify("Refreshed project state.")

        def action_validate(self) -> None:
            self.state = collect_dashboard_state()
            self._render_state()
            if self.state.issues:
                self.notify(
                    "Validation completed with warnings or errors.",
                    severity=_notification_severity(self.state.issues),
                )
            else:
                self.notify("Validation passed.")

        def action_revision_history(self) -> None:
            try:
                path = write_revision_variables(load_config())
            except BoardwrightError as exc:
                self.notify(str(exc), severity="error")
                return
            self.state = collect_dashboard_state()
            self._render_state()
            self.notify(f"Wrote {path.name}.")

        def action_record_change(self) -> None:
            self.push_screen(ChangelogEntryScreen(), self._record_change)

        def _record_change(self, result: tuple[str, str] | None) -> None:
            if result is None:
                return
            section, message = result
            try:
                config = load_config()
                add_unreleased_entry(config.root, section, message)
                suggestion = suggest_commit_message(config.root, message)
            except BoardwrightError as exc:
                self.notify(str(exc), severity="error")
                return
            self.state = collect_dashboard_state()
            self._render_state()
            self.notify(f"Recorded change. Suggested commit: {suggestion}")

        def _render_state(self) -> None:
            status = self.state.status
            self.query_one("#project_status", Static).update(
                "\n".join(
                    [
                        f"Project: {status.project_id}",
                        f"Name: {status.project_name}",
                        f"Branch: {status.branch}",
                        f"Variant: {status.variant}",
                        f"Working tree: {'dirty' if status.dirty_count else 'clean'}",
                        f"Changed files: {status.dirty_count}",
                        f"Latest tag: {status.latest_tag or 'none'}",
                        f"Unreleased: {'yes' if status.unreleased_changes else 'no'}",
                    ]
                )
            )
            self.query_one("#workflow_status", Static).update(
                "\n".join(
                    [
                        f"Preview: {self.state.preview_summary}",
                        f"Release dry-run: {self.state.release_summary}",
                    ]
                )
            )
            self.query_one("#validation_status", Static).update(
                _format_issues(self.state.issues)
            )
            self.query_one("#git_status", Static).update(
                _format_changed_files(self.state.changed_files)
            )

    return BoardwrightTui


def _format_issues(issues: tuple[ValidationIssue, ...]) -> str:
    if not issues:
        return "Validation passed."
    return "\n".join(f"{issue.level}: {issue.message}" for issue in issues)


def _notification_severity(issues: tuple[ValidationIssue, ...]) -> str:
    if any(issue.level == "error" for issue in issues):
        return "error"
    return "warning"


def _format_changed_files(changed_files: tuple[str, ...]) -> str:
    if not changed_files:
        return "Working tree clean."
    return "\n".join(changed_files[:12])
