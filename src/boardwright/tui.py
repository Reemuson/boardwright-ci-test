# SPDX-License-Identifier: LicenseRef-Boardwright-Source-Available
# Copyright (c) Ryan Hicks

from __future__ import annotations

import csv
import io
from threading import Thread
from dataclasses import dataclass
import os
import subprocess
import shutil
from importlib.util import find_spec
from pathlib import Path
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
from .preview import latest_preview_variant, list_preview_runs
from .release import build_release_plan, validate_release_plan
from .revision_history import document_revision_rows, write_document_revision_rows, write_revision_variables
from .schematic_generation import add_hierarchical_sheet, slugify
from .sheet_titles import find_primary_schematic
from .status import ProjectStatus, collect_status
from .validation import ValidationIssue, validate_project
from .worksheet_logo import embed_logo_in_worksheets
from .workflow_state import WorkflowState, action_state, build_workflow_state

if TYPE_CHECKING:
    from .accepted import AcceptedMainState
    from .config import BoardwrightConfig
    from .preview import PreviewState


INSTALL_HINT = 'Textual is not installed. Install the TUI with: pip install -e ".[tui]"'

BRAND_TEAL = "#0d7c88"
SURFACE_DARK = "#14252a"
SURFACE_RAISED = "#1b2f34"
SURFACE_TOP = "#102227"
BORDER_SUBTLE = "#29515a"
STATUS_SUCCESS = "#4db889"
STATUS_LOADING = "#4f9dc4"
STATUS_WARNING = "#d7a642"
STATUS_ERROR = "#d46a66"
STATUS_UNSET = "#80919a"
STATUS_INFO = "#6fa5c8"


def _legal_profile_options(
    profiles: dict[str, object],
    selected: str,
) -> list[tuple[str, str]]:
    names = {str(name) for name in profiles if str(name).strip()}
    if selected.strip():
        names.add(selected.strip())
    if not names:
        names.add("public-hardware")
    return [(name, name) for name in sorted(names)]


def _refresh_project_text_variables() -> Path:
    return write_revision_variables(load_config(), "schematic")


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
    drawing_revision: str
    revision_description: str
    accepted_summary: str
    items: tuple[ReleaseChecklistItem, ...]
    action: WorkflowAction | None = None
    impact_lines: tuple[str, ...] = ()

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


def _responsive_layout_mode(width: int, height: int) -> str:
    if height < 34:
        return "tiny"
    if width < 150:
        return "compact"
    if height < 44:
        return "short"
    return "wide"


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
        config.document_settings("schematic").get("revision", "A"),
        "Release readiness",
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
        else (accepted_error or "Main evidence not checked.")
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
    print(f"Dev default variant: {status.variant}")
    print(f"Working tree: {'dirty' if status.dirty_count else 'clean'}")
    print(f"Unreleased changes: {'yes' if status.unreleased_changes else 'no'}")
    print(f"Preview: {state.preview_summary}")
    print(f"Accept: {state.promote_summary}")
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


def _embed_template_sheet_image(config: "BoardwrightConfig") -> None:
    image = Path(config.template_sheet_image)
    image = image if image.is_absolute() else config.root / image
    worksheets = sorted((config.root / "Templates").glob("*.kicad_wks"))
    if not worksheets:
        return
    embed_logo_in_worksheets(worksheets, image)


def _semantic_style(kind: str, *, bold: bool = True) -> str:
    palette = {
        "success": STATUS_SUCCESS,
        "loaded": STATUS_SUCCESS,
        "loading": STATUS_LOADING,
        "warning": STATUS_WARNING,
        "error": STATUS_ERROR,
        "unset": STATUS_UNSET,
        "info": STATUS_INFO,
    }
    color = palette.get(kind, STATUS_UNSET)
    return f"{'bold ' if bold else ''}{color}"


def _action_button_labels(mode: str) -> dict[str, str]:
    if mode in {"short", "tiny"}:
        return {
            "record_change": "Record",
            "commit_push": "Commit",
            "generate_preview": "Preview",
            "review_artifacts": "Review",
            "open_preview_folder": "Folder",
            "open_preview_readme": "README",
            "generate_sheet": "New Sheet",
            "accept_main": "Accept",
            "release_ci": "Release",
            "project_info": "Info",
            "refresh": "Refresh",
        }
    return {
        "record_change": "Record",
        "commit_push": "Commit",
        "generate_preview": "Preview",
        "review_artifacts": "Review",
        "open_preview_folder": "Folder",
        "open_preview_readme": "README",
        "generate_sheet": "New Sheet",
        "accept_main": "Accept",
        "release_ci": "Release",
        "project_info": "Info",
        "refresh": "Refresh",
    }


def _action_display_name(name: str) -> str:
    return {
        "Record": "Record",
        "Commit": "Commit",
        "Preview": "Preview",
        "Review": "Review",
        "Folder": "Folder",
        "README": "README",
        "Sheet": "Sheet",
        "Accept": "Accept",
        "Release": "Release",
        "Info": "Info",
        "Refresh": "Refresh",
    }.get(name, name)


def _build_textual_app():
    from textual.app import App, ComposeResult
    from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
    from textual.screen import ModalScreen
    from textual.widgets import (
        Button,
        Header,
        Input,
        Label,
        ProgressBar,
        Select,
        Static,
        TextArea,
    )

    class ChangelogEntryScreen(ModalScreen[tuple[str, str] | None]):
        CSS = (
            """
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
            border: solid {BRAND_TEAL};
            background: {SURFACE_DARK};
        }

        #project_info_dialog {
            width: 116;
            max-width: 96%;
            height: auto;
            max-height: 90%;
            padding: 1 2;
            margin: 0;
            border: solid {BRAND_TEAL};
            background: {SURFACE_DARK};
        }

        #review_status {
            padding: 1;
            border: solid {BORDER_SUBTLE};
            background: {SURFACE_RAISED};
        }

        #project_info_body {
            height: 1fr;
            max-height: 72vh;
        }

        #project_info_nav {
            width: 22;
            height: 1fr;
            padding: 1 1 1 0;
            border-right: solid {BORDER_SUBTLE};
        }

        #project_info_content {
            width: 1fr;
            height: 1fr;
        }

        .info-nav-button {
            width: 100%;
            height: 3;
            min-height: 3;
            margin-bottom: 0;
        }

        .info-nav-active {
            border: tall {BRAND_TEAL};
            background: {SURFACE_RAISED};
        }

        .project-info-pane {
            height: 1fr;
            padding: 1 2;
            overflow-y: auto;
        }

        .field-block {
            width: 1fr;
            height: auto;
            margin-bottom: 0;
        }

        .section-block {
            width: 1fr;
            height: auto;
            margin-top: 1;
        }

        #change_message {
            height: 9;
            margin-bottom: 1;
        }

        #sheet_legal_notice {
            height: 8;
            margin-bottom: 1;
        }

        #legal_safety_notice,
        #manufacturing_fabrication_notes,
        #manufacturing_assembly_notes,
        #manufacturing_testpoint_policy,
        #manufacturing_impedance_notes {
            height: 6;
            margin-bottom: 1;
        }

        #manufacturing_impedance_table {
            height: 10;
            margin-bottom: 1;
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
            .replace("{BRAND_TEAL}", BRAND_TEAL)
            .replace("{SURFACE_DARK}", SURFACE_DARK)
            .replace("{SURFACE_RAISED}", SURFACE_RAISED)
            .replace("{BORDER_SUBTLE}", BORDER_SUBTLE)
            .replace("{STATUS_INFO}", STATUS_INFO)
            .replace("{STATUS_ERROR}", STATUS_ERROR)
        )

        def compose(self) -> ComposeResult:
            with Vertical(id="dialog"):
                yield Label("Changelog", classes="section-title")
                yield Select(
                    [(section, section) for section in SUPPORTED_SECTIONS],
                    value="Changed",
                    id="change_section",
                )
                yield TextArea(
                    "",
                    id="change_message",
                )
                yield Button("Save", id="save_change")
                yield Button("Cancel", id="cancel_change")

        def on_button_pressed(self, event: Button.Pressed) -> None:
            if event.button.id == "cancel_change":
                self.dismiss(None)
                return
            section = self.query_one("#change_section", Select).value
            message = self.query_one("#change_message", TextArea).text
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
                yield Label("Artifacts", classes="section-title")
                with Vertical(id="review_status"):
                    yield Static(self.preview_status, id="review_state")
                    yield Static(self.preview_message, id="review_message", classes="muted")
                    yield Static(self.run_summary, id="review_run")
                yield ProgressBar(total=100, show_eta=False, id="fetch_progress")
                yield Static(_ci_runs_brief(self.runs_text), id="recent_runs", classes="muted")
                yield Button("Fetch", id="fetch_artifact")
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
                yield Label("Variant", classes="section-title")
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
                yield Label("Preview", classes="section-title")
                yield Static(
                    f"Runs {config.preview_workflow} from {config.dev_branch}.",
                    classes="muted",
                )
                yield Select(
                    [(variant, variant) for variant in ("DRAFT", "PRELIMINARY", "CHECKED", "RELEASED")],
                    value=config.preview_variant,
                    id="preview_variant",
                )
                yield Button("Run", id="dispatch_preview")
                yield Button("Cancel", id="cancel_preview")

        def on_button_pressed(self, event: Button.Pressed) -> None:
            if event.button.id == "cancel_preview":
                self.dismiss(None)
                return
            variant = self.query_one("#preview_variant", Select).value
            self.dismiss(str(variant))

    class ProjectInfoScreen(ModalScreen[dict[str, object] | None]):
        CSS = ChangelogEntryScreen.CSS

        def compose(self) -> ComposeResult:
            config = load_config()
            project = config.project.get("project", {})
            branches = config.branch_settings
            assets = config.assets
            template = config.template
            sheet = config.sheet
            legal_project = config.legal_project_defaults
            legal_profiles = config.legal_profiles
            document = config.document_settings("schematic")
            manufacturing = config.manufacturing
            impedance_table_text = _format_impedance_table_input(config.impedance_entries)
            outputs = config.output_settings
            revision_table_text = _format_document_revision_rows_input(document_revision_rows(config, "schematic"))

            bool_options = [("true", "true"), ("false", "false")]
            optional_bool_options = [("Unspecified", ""), ("true", "true"), ("false", "false")]
            revision_scheme_options = [(scheme, scheme) for scheme in ("lettered", "numeric", "semantic")]
            variant_options = [(variant, variant) for variant in ("DRAFT", "PRELIMINARY", "CHECKED", "RELEASED")]

            def field_input(
                label: str,
                value: str,
                *,
                placeholder: str,
                widget_id: str,
                hint: str = "",
            ):
                with Vertical(classes="field-block"):
                    yield Label(label, classes="field-label")
                    if hint:
                        yield Static(hint, classes="field-hint")
                    yield Input(value=value, placeholder=placeholder, id=widget_id)

            def field_select(
                label: str,
                value: str,
                *,
                widget_id: str,
                hint: str = "",
                options: list[tuple[str, str]],
            ):
                with Vertical(classes="field-block"):
                    yield Label(label, classes="field-label")
                    if hint:
                        yield Static(hint, classes="field-hint")
                    yield Select(options, value=value, id=widget_id)

            def field_textarea(
                label: str,
                value: str,
                *,
                widget_id: str,
                hint: str = "",
            ):
                with Vertical(classes="field-block"):
                    yield Label(label, classes="field-label")
                    if hint:
                        yield Static(hint, classes="field-hint")
                    yield TextArea(value, id=widget_id)

            def section_intro(title: str, hint: str):
                with Vertical(classes="section-block"):
                    yield Label(title, classes="section-title")
                    yield Static(hint, classes="field-hint")

            with Vertical(id="project_info_dialog"):
                with Horizontal(id="project_info_body"):
                    with Vertical(id="project_info_nav"):
                        yield Static("Sections", classes="section-title")
                        yield Button("Project", id="info_nav_project", classes="info-nav-button")
                        yield Button("Drawing", id="info_nav_drawing", classes="info-nav-button")
                        yield Button("Revisions", id="info_nav_revisions", classes="info-nav-button")
                        yield Button("Workflow", id="info_nav_workflow", classes="info-nav-button")
                        yield Button("Outputs", id="info_nav_outputs", classes="info-nav-button")
                        yield Button("Branding", id="info_nav_branding", classes="info-nav-button")
                        yield Button("Legal", id="info_nav_legal", classes="info-nav-button")
                        yield Button("Fabrication", id="info_nav_fabrication", classes="info-nav-button")
                        yield Button("Notes", id="info_nav_notes", classes="info-nav-button")
                    with Vertical(id="project_info_content"):
                        with VerticalScroll(id="info_section_project", classes="project-info-pane info-section"):
                            yield from section_intro(
                                "Project",
                                "Core identifiers and ownership used across Boardwright outputs.",
                            )
                            yield from field_input(
                                "Project name (${PROJECT_NUMBER})",
                                config.project_name,
                                placeholder="Customer Project",
                                widget_id="project_name",
                                hint="KiCad `${PROJECT_NUMBER}`; also used as the Boardwright project identifier.",
                            )
                            yield from field_input(
                                "Company",
                                str(project.get("company", "")),
                                placeholder="Acme Labs",
                                widget_id="project_company",
                                hint="Organisation or studio name.",
                            )
                            yield from field_input(
                                "Designer",
                                str(project.get("designer", "")),
                                placeholder="Jane Doe",
                                widget_id="project_designer",
                                hint="Primary engineer or maintainer.",
                            )
                            yield from field_input(
                                "Git URL",
                                str(project.get("git_url", "")),
                                placeholder="https://github.com/owner/repo.git",
                                widget_id="project_git_url",
                                hint="Canonical repository URL. GitHub repo metadata is derived from this.",
                            )
                        with VerticalScroll(id="info_section_drawing", classes="project-info-pane info-section"):
                            yield from section_intro(
                                "Board identity",
                                "Hardware and controlled drawing metadata kept separate from release versioning.",
                            )
                            yield from field_input(
                                "PCBA name (${PCBA_NAME})",
                                config.pcba_name,
                                placeholder="Main Controller Assembly",
                                widget_id="project_pcba_name",
                                hint="KiCad `${PCBA_NAME}`; schematic and assembly `${DRAWING_NUMBER}`.",
                            )
                            yield from field_input(
                                "PCB name (${PCB_NAME})",
                                config.pcb_name,
                                placeholder="Main Controller PCB",
                                widget_id="project_pcb_name",
                                hint="KiCad `${PCB_NAME}`; fabrication `${DRAWING_NUMBER}`.",
                            )
                            yield from field_input(
                                "Board revision",
                                config.board_revision,
                                placeholder="A",
                                widget_id="project_board_revision",
                                hint="Current hardware revision string.",
                            )
                            yield from section_intro(
                                "Document control",
                                "Formal drawing metadata for the schematic document.",
                            )
                            yield from field_input(
                                "Drawing revision",
                                str(document.get("revision") or ""),
                                placeholder="A",
                                widget_id="project_document_revision",
                                hint="Controlled drawing revision printed on formal sheets.",
                            )
                            yield from field_select(
                                "Revision scheme",
                                str(document.get("revision_scheme") or "lettered"),
                                widget_id="project_document_revision_scheme",
                                hint="Controls validation and future revision helpers.",
                                options=revision_scheme_options,
                            )
                            yield from field_input(
                                "Drawn by",
                                str(document.get("drawn_by") or ""),
                                placeholder="Initials or name",
                                widget_id="project_drawn_by",
                                hint="Person or role that drew the document.",
                            )
                            yield from field_input(
                                "Drawn date",
                                str(document.get("drawn_date") or ""),
                                placeholder="YYYY-MM-DD",
                                widget_id="project_drawn_date",
                                hint="Use YYYY-MM-DD.",
                            )
                        with VerticalScroll(id="info_section_revisions", classes="project-info-pane info-section"):
                            yield from section_intro(
                                "Revision history",
                                "Newest first. CSV columns: REV, DATE, DESCRIPTION, DRAWN.",
                            )
                            yield from field_textarea(
                                "Rows",
                                revision_table_text,
                                widget_id="document_revision_rows",
                                hint="One row per line. Example: B, 2026-08-01, Updated connector callouts, RH",
                            )
                        with VerticalScroll(id="info_section_workflow", classes="project-info-pane info-section"):
                            yield from section_intro(
                                "Variant defaults",
                                "Choose which lifecycle state each workflow stage should target by default.",
                            )
                            yield from field_select("Dev default", str(config.default_variant), widget_id="variant_dev", hint="Used for local work and normal commits.", options=variant_options)
                            yield from field_select("Preview default", str(config.preview_variant), widget_id="variant_preview", hint="Used when dispatching preview artifacts.", options=variant_options)
                            yield from field_select("Main default", str(config.main_variant), widget_id="variant_main", hint="Used when promoting accepted output to main.", options=variant_options)
                            yield from field_select("Release default", str(config.release_variant), widget_id="variant_release", hint="Used when preparing formal releases.", options=variant_options)
                            yield from section_intro(
                                "Branch names",
                                "Where Boardwright records, previews, and releases work.",
                            )
                            yield from field_input("Development branch", str(branches.get("development", config.dev_branch)), placeholder="dev", widget_id="branch_development", hint="Branch required for local recording and commit actions.")
                            yield from field_input("Preview branch", str(branches.get("preview", config.preview_branch)), placeholder="preview", widget_id="branch_preview", hint="Branch used for preview artifact review when enabled.")
                            yield from field_input("Release branch", str(branches.get("release", config.release_branch)), placeholder="release", widget_id="branch_release", hint="Branch used for release preparation and tags.")
                        with VerticalScroll(id="info_section_outputs", classes="project-info-pane info-section"):
                            yield from section_intro(
                                "Generated outputs",
                                "Small set of output policy switches for normal workflow use.",
                            )
                            yield from field_select(
                                "Commit generated outputs to main",
                                str(outputs.get("commit_generated_outputs_to_main", "false")).lower(),
                                widget_id="outputs_commit_generated_outputs_to_main",
                                hint="If true, release outputs are committed on `main`.",
                                options=bool_options,
                            )
                            yield from field_select(
                                "Use preview branch",
                                str(outputs.get("use_preview_branch", "true")).lower(),
                                widget_id="outputs_use_preview_branch",
                                hint="If true, preview review artifacts are tracked on the preview branch.",
                                options=bool_options,
                            )
                            yield from field_select(
                                "Source archive",
                                str(outputs.get("release_include_source_archive", "false")).lower(),
                                widget_id="outputs_release_include_source_archive",
                                hint="Include a source archive alongside release outputs.",
                                options=bool_options,
                            )
                            yield from field_select(
                                "Combined review PDF",
                                str(outputs.get("combined_review_pdf", "false")).lower(),
                                widget_id="outputs_combined_review_pdf",
                                hint="Generate the standard combined review PDF with ToC, bookmarks, and sheet numbering.",
                                options=bool_options,
                            )
                        with VerticalScroll(id="info_section_branding", classes="project-info-pane info-section"):
                            yield from section_intro(
                                "Worksheet assets",
                                "Title block worksheet and embedded sheet image.",
                            )
                            yield Static(
                                f"Worksheet: {config.worksheet}",
                                id="template_worksheet_display",
                                classes="project-summary",
                            )
                            yield from field_input("Sheet image", str(template.get("sheet_image") or config.template_sheet_image), placeholder="Sheet image path", widget_id="template_sheet_image", hint="Image embedded into generated worksheet templates.")
                            yield from section_intro("Project imagery", "Paths used by README and product-facing outputs.")
                            yield from field_input("Logo", str(assets.get("logo", "")), placeholder="Logo path", widget_id="asset_logo", hint="Primary logo or wordmark asset.")
                            yield from field_input("Render image", str(assets.get("product_image", "")), placeholder="Render image path", widget_id="asset_product_image", hint="Hero render or product image used in docs.")
                        with VerticalScroll(id="info_section_legal", classes="project-info-pane info-section"):
                            yield from section_intro("Legal profile", "Defaults for notices, worksheets, and release packaging.")
                            yield from field_select("Profile", config.legal_profile, widget_id="legal_profile", hint="Select the baseline legal profile for this project.", options=_legal_profile_options(legal_profiles, config.legal_profile))
                            yield from field_input("Hardware design license", str(legal_project.get("hardware_design_license", "")), placeholder="CERN-OHL-S-2.0", widget_id="legal_hardware_design_license", hint="Primary hardware design license identifier.")
                            yield from field_input("Copyright holder", str(legal_project.get("copyright_holder", "")), placeholder="Acme Labs", widget_id="legal_copyright_holder", hint="Entity or person named in copyright notices.")
                            yield from field_input("Notice file", str(legal_project.get("notice_file", "NOTICE.md")), placeholder="NOTICE.md", widget_id="legal_notice_file", hint="Repository file that contains attribution and legal notice text.")
                            yield from field_textarea("Safety notice", str(legal_project.get("safety_notice", "")), widget_id="legal_safety_notice", hint="Important warning text included in generated legal outputs.")
                            yield from field_textarea("Sheet notice", str(sheet.get("legal_notice") or config.sheet_legal_notice), widget_id="sheet_legal_notice", hint="Expanded into `${SHEET_LEGAL_NOTICE}` in the worksheet.")
                        with VerticalScroll(id="info_section_fabrication", classes="project-info-pane info-section"):
                            yield from section_intro("Fabrication defaults", "Manufacturing assumptions used by reports and fab notes.")
                            for label, key, default, hint in (
                                ("RoHS / Pb-free", "rohs_pb_free", "true", "Marks the design as RoHS / Pb-free by default."),
                                ("Conformal coating", "conformal_coating", "false", "Set true if coating is a normal build requirement."),
                                ("Silkscreen legend", "silkscreen_enabled", "true", "Set false when no silkscreen legend should be manufactured."),
                                ("Tented vias", "tented_vias", "true", "Controls the default via tenting expectation."),
                                ("Controlled impedance", "impedance_enabled", "false", "Enable when the board requires impedance notes and a table."),
                                ("Halogen-free material", "halogen_free", "true", "Marks equivalent laminate requirements as halogen-free."),
                            ):
                                value = str(manufacturing.get(key, default)).strip().lower()
                                options = bool_options if key == "impedance_enabled" else optional_bool_options
                                yield from field_select(label, value, widget_id=f"manufacturing_{key}", hint=hint, options=options)
                            yield from field_input("Manufacturing standard", str(manufacturing.get("manufacturing_standard", "IPC-6012 Class 2")), placeholder="IPC-6012 Class 2", widget_id="manufacturing_manufacturing_standard", hint="Fabrication workmanship standard named in fab notes.")
                            yield from field_input("Core material", str(manufacturing.get("core_material", "FR-4")), placeholder="FR-4", widget_id="manufacturing_core_material", hint="Laminate or core material requirement.")
                            yield from field_input("Flammability", str(manufacturing.get("flammability_rating", "UL94V-0")), placeholder="UL94V-0", widget_id="manufacturing_flammability_rating", hint="Minimum flammability rating for laminate material.")
                            yield from field_input("Tg rating", str(manufacturing.get("tg_rating", "170 C")), placeholder="170 C", widget_id="manufacturing_tg_rating", hint="Minimum glass-transition temperature or equivalent requirement.")
                        with VerticalScroll(id="info_section_notes", classes="project-info-pane info-section"):
                            yield from section_intro("Manufacturing notes", "Freeform notes exported into fabrication and assembly outputs.")
                            yield from field_textarea("Fab notes", str(manufacturing.get("fabrication_notes", "")), widget_id="manufacturing_fabrication_notes", hint="Special fabrication instructions for the board house.")
                            yield from field_textarea("Assembly notes", str(manufacturing.get("assembly_notes", "")), widget_id="manufacturing_assembly_notes", hint="Special assembly instructions for population and handling.")
                            yield from field_textarea("Testpoint policy", str(manufacturing.get("testpoint_policy", "")), widget_id="manufacturing_testpoint_policy", hint="Define expectations for test access and coverage.")
                            yield from field_textarea("Impedance notes", str(manufacturing.get("impedance_notes", "")), widget_id="manufacturing_impedance_notes", hint="Impedance assumptions.")
                            yield from field_textarea(
                                "Impedance table",
                                impedance_table_text,
                                widget_id="manufacturing_impedance_table",
                                hint="CSV: Transmission Line, Impedance, Tolerance, Layer, Width, Gap, Ref. Layers.",
                            )
                yield Button("Save", id="save_project_info")
                yield Button("Cancel", id="cancel_project_info")

        def on_mount(self) -> None:
            self._show_info_section("project")

        def _show_info_section(self, key: str) -> None:
            for section in ("project", "drawing", "revisions", "workflow", "outputs", "branding", "legal", "fabrication", "notes"):
                pane = self.query_one(f"#info_section_{section}")
                pane.display = section == key
                button = self.query_one(f"#info_nav_{section}", Button)
                if section == key:
                    button.add_class("info-nav-active")
                else:
                    button.remove_class("info-nav-active")

        def on_button_pressed(self, event: Button.Pressed) -> None:
            if event.button.id and event.button.id.startswith("info_nav_"):
                self._show_info_section(event.button.id.removeprefix("info_nav_"))
                event.stop()
                return
            if event.button.id == "cancel_project_info":
                self.dismiss(None)
                return
            self.dismiss(
                {
                    "project": {
                        "name": self.query_one("#project_name", Input).value.strip(),
                        "pcba_name": self.query_one("#project_pcba_name", Input).value.strip(),
                        "pcb_name": self.query_one("#project_pcb_name", Input).value.strip(),
                        "board_revision": self.query_one("#project_board_revision", Input).value.strip(),
                        "company": self.query_one("#project_company", Input).value.strip(),
                        "designer": self.query_one("#project_designer", Input).value.strip(),
                        "git_url": self.query_one("#project_git_url", Input).value.strip(),
                    },
                    "documents": {
                        "schematic": {
                            "title": "Schematic",
                            "type": "SCHEMATIC",
                            "number_source": "pcba_name",
                            "revision": self.query_one("#project_document_revision", Input).value.strip(),
                            "revision_scheme": str(self.query_one("#project_document_revision_scheme", Select).value),
                            "drawn_by": self.query_one("#project_drawn_by", Input).value.strip(),
                            "drawn_date": self.query_one("#project_drawn_date", Input).value.strip(),
                            "include_full_revision_history": True,
                        }
                    },
                    "document_revisions": {
                        "schematic": _parse_document_revision_rows_input(
                            self.query_one("#document_revision_rows", TextArea).text
                        ),
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
                    "template": {
                        "sheet_image": self.query_one("#template_sheet_image", Input).value.strip(),
                    },
                    "sheet": {
                        "legal_notice": self.query_one("#sheet_legal_notice", TextArea).text.strip(),
                    },
                    "branches": {
                        "development": self.query_one("#branch_development", Input).value.strip(),
                        "preview": self.query_one("#branch_preview", Input).value.strip(),
                        "release": self.query_one("#branch_release", Input).value.strip(),
                    },
                    "legal": {
                        "profile": str(self.query_one("#legal_profile", Select).value),
                        "hardware_design_license": self.query_one("#legal_hardware_design_license", Input).value.strip(),
                        "copyright_holder": self.query_one("#legal_copyright_holder", Input).value.strip(),
                        "notice_file": self.query_one("#legal_notice_file", Input).value.strip(),
                        "safety_notice": self.query_one("#legal_safety_notice", TextArea).text.strip(),
                    },
                    "manufacturing": {
                        "rohs_pb_free": str(self.query_one("#manufacturing_rohs_pb_free", Select).value),
                        "conformal_coating": str(self.query_one("#manufacturing_conformal_coating", Select).value),
                        "silkscreen_enabled": str(self.query_one("#manufacturing_silkscreen_enabled", Select).value),
                        "tented_vias": str(self.query_one("#manufacturing_tented_vias", Select).value),
                        "impedance_enabled": str(self.query_one("#manufacturing_impedance_enabled", Select).value),
                        "halogen_free": str(self.query_one("#manufacturing_halogen_free", Select).value),
                        "manufacturing_standard": self.query_one("#manufacturing_manufacturing_standard", Input).value.strip(),
                        "core_material": self.query_one("#manufacturing_core_material", Input).value.strip(),
                        "flammability_rating": self.query_one("#manufacturing_flammability_rating", Input).value.strip(),
                        "tg_rating": self.query_one("#manufacturing_tg_rating", Input).value.strip(),
                        "fabrication_notes": self.query_one("#manufacturing_fabrication_notes", TextArea).text.strip(),
                        "assembly_notes": self.query_one("#manufacturing_assembly_notes", TextArea).text.strip(),
                        "testpoint_policy": self.query_one("#manufacturing_testpoint_policy", TextArea).text.strip(),
                        "impedance_notes": self.query_one("#manufacturing_impedance_notes", TextArea).text.strip(),
                        "impedance_table": _parse_impedance_table_input(
                            self.query_one("#manufacturing_impedance_table", TextArea).text
                        ),
                    },
                    "outputs": {
                        "commit_generated_outputs_to_main": str(self.query_one("#outputs_commit_generated_outputs_to_main", Select).value),
                        "use_preview_branch": str(self.query_one("#outputs_use_preview_branch", Select).value),
                        "release_include_source_archive": str(self.query_one("#outputs_release_include_source_archive", Select).value),
                        "combined_review_pdf": str(self.query_one("#outputs_combined_review_pdf", Select).value),
                    },
                }
            )

    class AcceptMainScreen(ModalScreen[tuple[str, bool] | None]):
        CSS = ChangelogEntryScreen.CSS

        def compose(self) -> ComposeResult:
            with Vertical(id="dialog"):
                yield Label("Accept", classes="section-title")
                yield Static("Pick how outputs land on main.", classes="muted")
                yield Select(
                    [(variant, variant) for variant in ("DRAFT", "PRELIMINARY", "CHECKED", "RELEASED")],
                    value="CHECKED",
                    id="accept_variant",
                )
                yield Select(
                    [("README", "yes"), ("Upload", "no")],
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
                yield Label("Commit", classes="section-title")
                yield Input(
                    value=self.suggested_message,
                    placeholder="feat: describe the board change",
                    id="commit_message",
                )
                yield Button("Commit", id="confirm_commit_push")
                yield Button("Cancel", id="cancel_commit")

        def on_button_pressed(self, event: Button.Pressed) -> None:
            event.stop()
            if event.button.id == "cancel_commit":
                self.dismiss(None)
                return
            message = self.query_one("#commit_message", Input).value
            self.dismiss(message)

    class ReleaseScreen(ModalScreen[tuple[str, ...] | None]):
        CSS = ChangelogEntryScreen.CSS

        def compose(self) -> ComposeResult:
            with Vertical(id="dialog"):
                yield Label("Release", classes="section-title")
                yield Static("Review package release and controlled drawing revision.", classes="muted")
                yield Input(placeholder="0.1.2", id="release_version")
                yield Input(placeholder="Drawing revision, e.g. A", id="release_drawing_revision")
                yield Input(placeholder="Revision table note, 60 chars max", id="release_revision_description")
                yield Input(
                    placeholder="Change classes: documentation, electrical, fabrication, assembly, tooling",
                    id="release_change_classes",
                )
                yield Input(placeholder="Justification (recorded in release ledger)", id="release_justification")
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
                yield Button("Run", id="dispatch_release")
                yield Button("Cancel", id="cancel_release")

        def on_button_pressed(self, event: Button.Pressed) -> None:
            if event.button.id == "cancel_release":
                self.dismiss(None)
                return
            version = self.query_one("#release_version", Input).value
            drawing_revision = self.query_one("#release_drawing_revision", Input).value
            revision_description = self.query_one("#release_revision_description", Input).value
            variant = self.query_one("#release_variant", Select).value
            kind = self.query_one("#release_kind", Select).value
            change_classes = self.query_one("#release_change_classes", Input).value
            justification = self.query_one("#release_justification", Input).value
            self.dismiss(
                (version, str(variant), str(kind), drawing_revision, revision_description, change_classes, justification)
            )

    class ReleaseChecklistScreen(ModalScreen[ReleaseChecklist | None]):
        CSS = ChangelogEntryScreen.CSS

        def __init__(self, checklist: ReleaseChecklist) -> None:
            super().__init__()
            self.checklist = checklist

        def compose(self) -> ComposeResult:
            with Vertical(id="dialog"):
                yield Label("Release", classes="section-title")
                yield Static("Confirm the checklist before dispatch.", classes="muted")
                yield Static(_format_release_checklist(self.checklist), id="release_checklist")
                yield Button("Run", id="confirm_release")
                yield Button("Cancel", id="cancel_release")

        def on_mount(self) -> None:
            self.query_one("#confirm_release", Button).disabled = not self.checklist.can_dispatch

        def on_button_pressed(self, event: Button.Pressed) -> None:
            if event.button.id == "confirm_release":
                self.dismiss(self.checklist)
                return
            self.dismiss(None)

    class GenerateSheetScreen(ModalScreen[dict[str, str] | None]):
        CSS = ChangelogEntryScreen.CSS

        def compose(self) -> ComposeResult:
            config = load_config()
            parent = find_primary_schematic(config.root)
            default_parent = str(parent) if parent is not None else "boardwright.kicad_sch"
            with Vertical(id="dialog"):
                yield Label("New Sheet", classes="section-title")
                yield Static(
                    "Add a hierarchical sheet and create the child schematic.",
                    classes="muted",
                )
                yield Input(value=default_parent, placeholder="Parent schematic", id="sheet_parent")
                yield Input(value="Section A", placeholder="Sheet name", id="sheet_name")
                yield Input(
                    value="section-a.kicad_sch",
                    placeholder="Child schematic filename",
                    id="sheet_child_file",
                )
                yield Input(
                    value="Section A",
                    placeholder="Child schematic title",
                    id="sheet_child_title",
                )
                yield Button("Create", id="generate_sheet_confirm")
                yield Button("Cancel", id="cancel_generate_sheet")

        def on_button_pressed(self, event: Button.Pressed) -> None:
            if event.button.id == "cancel_generate_sheet":
                self.dismiss(None)
                return
            self.dismiss(
                {
                    "parent": self.query_one("#sheet_parent", Input).value.strip(),
                    "sheet_name": self.query_one("#sheet_name", Input).value.strip(),
                    "child_file": self.query_one("#sheet_child_file", Input).value.strip(),
                    "child_title": self.query_one("#sheet_child_title", Input).value.strip(),
                }
            )

    class ShortcutHelpScreen(ModalScreen[None]):
        CSS = ChangelogEntryScreen.CSS

        def compose(self) -> ComposeResult:
            with Vertical(id="dialog"):
                yield Label("Shortcuts", classes="section-title")
                yield Static(_format_shortcut_help(), id="shortcut_help")
                yield Button("Close", id="close_shortcuts")

        def on_button_pressed(self, event: Button.Pressed) -> None:
            self.dismiss(None)

    class ValidationReportScreen(ModalScreen[None]):
        CSS = ChangelogEntryScreen.CSS

        def __init__(self, issues: tuple[ValidationIssue, ...]) -> None:
            super().__init__()
            self.issues = issues

        def compose(self) -> ComposeResult:
            with Vertical(id="dialog"):
                yield Label("Validation", classes="section-title")
                yield Static(
                    "Use `boardwright doctor` for the full check set.",
                    classes="muted",
                )
                yield Static(_format_validation_details(self.issues), id="validation_help")
                yield Button("Close", id="close_validation")

        def on_button_pressed(self, event: Button.Pressed) -> None:
            self.dismiss(None)

    class BoardwrightTui(App):
        TITLE = "Boardwright"
        SUB_TITLE = "Design. Ship."

        CSS = (
            """
        Screen {
            layout: vertical;
        }

        #top_status {
            width: 100%;
            height: 3;
            padding: 1 2 0 2;
            content-align: left middle;
            border-bottom: solid {BRAND_TEAL};
            background: {SURFACE_TOP};
            overflow-x: auto;
        }

        #body {
            layout: horizontal;
            height: 1fr;
            padding: 0 1 1 1;
        }

        #body.compact {
            padding: 0 0 1 0;
        }

        #body.short {
            padding: 0 0 1 0;
        }

        #body.tiny {
            padding: 0 0 1 0;
        }

        #summary {
            width: 26;
            min-width: 18;
            height: 1fr;
            overflow-y: auto;
            padding: 1 1;
            border: solid {BRAND_TEAL};
            background: {SURFACE_DARK};
        }

        #body.compact #summary,
        #body.short #summary,
        #body.tiny #summary {
            width: 20;
            min-width: 16;
            padding: 0 1;
        }

        #body.tiny .panel-title,
        #body.tiny .section-title {
            margin-top: 0;
            margin-bottom: 0;
        }

        #body.tiny .section-title {
            height: 1;
        }

        #body.compact #project_heading,
        #body.short #project_heading,
        #body.tiny #project_heading,
        #body.compact #project_status,
        #body.short #project_status,
        #body.tiny #project_status {
            display: none;
        }

        #body.compact #actions {
            padding-right: 0;
        }

        #actions {
            height: 1fr;
            min-width: 0;
            padding-right: 1;
            overflow-y: auto;
        }

        .action-grid {
            grid-size: 1;
            grid-columns: 1fr;
            grid-gutter: 0;
            height: auto;
        }

        #body.compact Button.action-button {
            height: 3;
            min-height: 3;
        }

        #body.short Button.action-button,
        #body.tiny Button.action-button {
            height: 3;
            min-height: 3;
            padding: 0;
        }

        #details {
            width: 1fr;
            height: 1fr;
            overflow-y: auto;
            padding: 1;
            border: solid {BORDER_SUBTLE};
            background: {SURFACE_DARK};
        }

        #body.compact #details {
            width: 1fr;
            min-width: 0;
            height: 1fr;
            padding: 0 0 0 1;
        }

        #body.short #details {
            width: 1fr;
            min-width: 0;
            height: 1fr;
            padding: 0 0 0 1;
        }

        #body.tiny #details {
            width: 1fr;
            min-width: 0;
            height: 1fr;
            padding: 0 0 0 1;
        }

        #main_details {
            height: 12;
            min-height: 10;
            min-width: 0;
        }

        #body.compact #main_details {
            layout: vertical;
            height: 1fr;
            min-height: 0;
        }

        #body.compact #lower_details {
            height: auto;
        }

        #body.short #main_details,
        #body.short #lower_details {
            height: auto;
        }

        #body.tiny #main_details {
            layout: vertical;
            height: 9;
            min-height: 9;
            margin-top: 0;
        }

        #body.tiny #lower_details {
            display: none;
        }

        #timeline_panel {
            width: 38;
            min-width: 0;
            padding-right: 1;
        }

        #body.compact #timeline_panel,
        #body.compact #inspector_panel,
        #body.compact #validation_panel,
        #body.compact #git_panel {
            width: 1fr;
            min-width: 0;
            padding-left: 0;
            padding-right: 0;
            border-left: none;
        }

        #body.compact #timeline_panel,
        #body.compact #inspector_panel {
            padding-right: 0;
        }

        #body.compact #inspector_panel,
        #body.compact #git_panel {
            border-top: solid {BORDER_SUBTLE};
            margin-top: 1;
            padding-top: 1;
        }

        #body.compact #inspector_panel {
            margin-top: 0;
        }

        #body.short #timeline_panel,
        #body.short #inspector_panel,
        #body.short #validation_panel,
        #body.short #git_panel {
            width: 1fr;
            min-width: 0;
            padding-left: 0;
            padding-right: 0;
            border-left: none;
        }

        #body.short #inspector_panel,
        #body.short #git_panel {
            border-top: solid {BORDER_SUBTLE};
            margin-top: 1;
            padding-top: 1;
        }

        #body.tiny #timeline_panel,
        #body.tiny #inspector_panel {
            width: 1fr;
            min-width: 0;
            padding-left: 0;
            padding-right: 0;
            border-left: none;
        }

        #body.tiny #inspector_panel {
            border-top: solid {BORDER_SUBTLE};
            margin-top: 1;
            padding-top: 1;
        }

        #inspector_panel {
            width: 1fr;
            min-width: 0;
            padding-left: 1;
            border-left: solid {BORDER_SUBTLE};
        }

        #workflow_status,
        #inspector_status {
            height: 1fr;
            min-width: 0;
            padding: 0 1 1 1;
            background: {SURFACE_RAISED};
            overflow-y: auto;
            content-align: left top;
            text-align: left;
        }

        #lower_details {
            height: 1fr;
            margin-top: 1;
        }

        #body.compact #lower_details {
            layout: vertical;
        }

        #body.short #lower_details {
            layout: vertical;
            margin-top: 0;
        }

        #body.short #main_details {
            height: 8;
            min-height: 8;
        }

        #body.short #workflow_status,
        #body.short #inspector_status {
            height: 4;
        }

        #body.tiny #workflow_status,
        #body.tiny #inspector_status {
            height: 3;
        }

        #body.tiny #top_status {
            height: 2;
        }

        #body.tiny .section-title,
        #body.tiny .panel-title {
            display: none;
        }

        #body.short #validation_status,
        #body.short #git_scroll {
            height: 3;
        }

        #body.tiny #validation_status,
        #body.tiny #git_scroll {
            height: 2;
        }

        #validation_panel {
            width: 0.75fr;
            padding-right: 0;
        }

        #git_panel {
            width: 1.25fr;
            padding-left: 0;
            border-left: solid {BORDER_SUBTLE};
        }

        #validation_status,
        #git_scroll {
            height: 1fr;
            overflow-y: auto;
        }

        #git_scroll {
            padding-left: 0;
            padding-right: 0;
            content-align: left top;
        }

        #git_status {
            width: 100%;
            height: auto;
            content-align: left top;
            text-align: left;
        }

        #validation_status {
            padding: 0 1 0 1;
        }

        #shortcut_hint {
            width: 100%;
            height: 1;
            padding: 0 2;
            content-align: left middle;
            color: $text-muted;
            border-top: solid {BORDER_SUBTLE};
            background: {SURFACE_TOP};
            overflow-x: auto;
        }

        #shortcut_help {
            height: auto;
            margin-bottom: 1;
        }

        #validation_help {
            height: 1fr;
            overflow-y: auto;
            margin-bottom: 1;
        }

        .panel-title {
            width: 100%;
            text-style: bold;
            color: {BRAND_TEAL};
            margin-bottom: 1;
            content-align: left middle;
            text-align: left;
        }

        .section-title {
            width: 100%;
            text-style: bold;
            margin-bottom: 0;
            margin-top: 1;
            color: {STATUS_INFO};
            content-align: left middle;
            text-align: left;
        }

        .field-label {
            width: 100%;
            text-style: bold;
            margin-top: 1;
            margin-bottom: 0;
            color: {STATUS_INFO};
        }

        .field-hint {
            width: 100%;
            color: $text-muted;
            margin-bottom: 0;
        }

        Button.action-button {
            width: 100%;
            height: 3;
            min-height: 3;
            margin-top: 0;
            padding: 0 1;
            content-align: left middle;
            text-align: left;
        }

        Button.primary-action {
            border: tall {BRAND_TEAL};
        }

        Button.secondary-action {
            border: tall {STATUS_INFO};
        }

        Button.danger-action {
            border: tall {STATUS_ERROR};
        }
        """
            .replace("{BRAND_TEAL}", BRAND_TEAL)
            .replace("{SURFACE_DARK}", SURFACE_DARK)
            .replace("{SURFACE_TOP}", SURFACE_TOP)
            .replace("{SURFACE_RAISED}", SURFACE_RAISED)
            .replace("{BORDER_SUBTLE}", BORDER_SUBTLE)
            .replace("{STATUS_INFO}", STATUS_INFO)
            .replace("{STATUS_ERROR}", STATUS_ERROR)
        )

        BINDINGS = [
            ("q", "quit", "Quit"),
            ("r", "refresh", "Refresh"),
            ("c", "record_change", "Record"),
            ("m", "commit_push", "Commit"),
            ("g", "generate_preview", "Preview"),
            ("a", "review_artifacts", "Review"),
            ("v", "view_validation", "Validation"),
            ("o", "open_preview_folder", "Folder"),
            ("O", "open_preview_readme", "README"),
            ("n", "generate_sheet", "Sheet"),
            ("p", "accept_main", "Accept"),
            ("l", "release_ci", "Release"),
            ("i", "project_info", "Info"),
            ("f1", "show_shortcuts", "Help"),
        ]

        def __init__(self) -> None:
            super().__init__()
            self.state = collect_dashboard_state()
            self.ci_status = "CI not polled"
            self.review_variant = ""
            self.review_destination: Path | None = None
            self._ci_polling = False
            self._layout_mode = "wide"

        def _apply_layout_mode(self, width: int, height: int) -> None:
            layout_mode = _responsive_layout_mode(width, height)
            if layout_mode == self._layout_mode:
                return
            self._layout_mode = layout_mode
            body = self.query_one("#body")
            body.set_class(layout_mode == "tiny", "tiny")
            body.set_class(layout_mode == "compact", "compact")
            body.set_class(layout_mode == "short", "short")
            self._sync_action_labels()

        def _screen_title(self) -> str:
            return "Boardwright"

        def _screen_subtitle(self) -> str:
            return "Design. Ship."

        def _sync_action_labels(self) -> None:
            labels = _action_button_labels(self._layout_mode)
            for button_id, label in labels.items():
                button = self.query_one(f"#{button_id}", Button)
                if str(button.label) != label:
                    button.label = label

        def watch_size(self, old_size, new_size) -> None:  # type: ignore[override]
            self._apply_layout_mode(new_size.width, new_size.height)

        def on_resize(self, event) -> None:  # pragma: no cover - Textual event hook
            self._apply_layout_mode(self.size.width, self.size.height)

        def compose(self) -> ComposeResult:
            yield Header(show_clock=False)
            yield Static(id="top_status")
            with Horizontal(id="body"):
                with Vertical(id="summary"):
                    yield Label("Project", classes="section-title", id="project_heading")
                    yield Static(id="project_status")
                    with Vertical(id="actions"):
                        yield Label("Work", classes="section-title")
                        with Grid(classes="action-grid"):
                            yield Button("Record", id="record_change", classes="action-button primary-action")
                            yield Button("Commit", id="commit_push", classes="action-button primary-action")
                        yield Label("Artifacts", classes="section-title")
                        with Grid(classes="action-grid"):
                            yield Button("Preview", id="generate_preview", classes="action-button secondary-action")
                            yield Button("Review", id="review_artifacts", classes="action-button secondary-action")
                            yield Button("Folder", id="open_preview_folder", classes="action-button secondary-action")
                            yield Button("README", id="open_preview_readme", classes="action-button secondary-action")
                        yield Label("Schematics", classes="section-title")
                        yield Button("New Sheet", id="generate_sheet", classes="action-button secondary-action")
                        yield Button("Accept", id="accept_main", classes="action-button secondary-action")
                        yield Label("Release", classes="section-title")
                        yield Button("Release", id="release_ci", classes="action-button danger-action")
                        yield Label("Setup", classes="section-title")
                        with Grid(classes="action-grid"):
                            yield Button("Info", id="project_info", classes="action-button secondary-action")
                            yield Button("Refresh", id="refresh", classes="action-button secondary-action")
                with Vertical(id="details"):
                    with Horizontal(id="main_details"):
                        with Vertical(id="timeline_panel"):
                            yield Label("Workflow", classes="panel-title")
                            yield Static(id="workflow_status")
                        with Vertical(id="inspector_panel"):
                            yield Label("Inspector", classes="panel-title")
                            yield Static(id="inspector_status")
                    with Horizontal(id="lower_details"):
                        with Vertical(id="validation_panel"):
                            yield Label("Validation", classes="panel-title")
                            yield Static(id="validation_status")
                        with Vertical(id="git_panel"):
                            yield Label("Changed Files", classes="panel-title")
                            with VerticalScroll(id="git_scroll"):
                                yield Static(id="git_status")
            yield Static(id="shortcut_hint")

        def on_mount(self) -> None:
            self._apply_layout_mode(self.size.width, self.size.height)
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
            elif event.button.id == "open_preview_folder":
                self.action_open_preview_folder()
            elif event.button.id == "open_preview_readme":
                self.action_open_preview_readme()
            elif event.button.id == "generate_sheet":
                self.action_generate_sheet()
            elif event.button.id == "accept_main":
                self.action_accept_main()
            elif event.button.id == "release_ci":
                self.action_release_ci()
            elif event.button.id == "project_info":
                self.action_project_info()
            elif event.button.id == "view_validation":
                self.action_view_validation()

        def action_refresh(self) -> None:
            variable_sync_error = ""
            try:
                _refresh_project_text_variables()
            except (BoardwrightError, OSError) as exc:
                variable_sync_error = str(exc)
            self.state = collect_dashboard_state()
            self.ci_status = "Polling CI..."
            self._render_state()
            if variable_sync_error:
                self.notify(
                    f"Dashboard refreshed, but project text variables could not be synchronized: "
                    f"{variable_sync_error}",
                    severity="error",
                )
            else:
                self.notify("Refreshed project metadata and dashboard.")
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
            release_status = "Release: not checked"
            try:
                config = load_config()
                try:
                    preview_runs = list_preview_runs(config)
                    preview_variant = latest_preview_variant(preview_runs, config.preview_variant)
                    preview_state = build_preview_state(
                        config,
                        preview_variant,
                        runs=preview_runs,
                    )
                except BoardwrightError as exc:
                    preview_error = str(exc)
                try:
                    accepted_state = build_accepted_main_state(config)
                except BoardwrightError as exc:
                    accepted_error = str(exc)
                try:
                    release_status = _release_ci_status_from_runs(
                        list_recent_workflow_runs(config, limit=12),
                    )
                except BoardwrightError as exc:
                    release_status = f"Release: {exc}"
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
                    release_status,
                )
            except BoardwrightError as exc:
                self._call_from_worker(self._finish_ci_poll, None, str(exc), notify)
                return
            self._call_from_worker(self._finish_ci_poll, state, ci_status, notify)

        def _call_from_worker(self, callback, *args) -> None:
            """Deliver a worker result unless the app has already shut down.

            Background polls can outlive the event loop (quit during a poll);
            dropping the result then is correct, crashing the thread is not.
            """
            try:
                self.call_from_thread(callback, *args)
            except RuntimeError as exc:
                if "closed" not in str(exc) and "not running" not in str(exc):
                    raise

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
                self.notify("CI refreshed.")

        def action_review_artifacts(self) -> None:
            if not self._require_action("Review"):
                return
            self.push_screen(ReviewVariantScreen(), self._review_artifact_variant)

        def action_generate_preview(self) -> None:
            if not self._require_action("Preview"):
                return
            self.push_screen(PreviewDispatchScreen(), self._generate_preview)

        def _generate_preview(self, variant: str | None) -> None:
            if variant is None:
                return
            try:
                config = load_config()
                if self.state.status.branch != config.dev_branch:
                    self.notify(
                        f"Switch to {config.dev_branch} to preview.",
                        severity="error",
                    )
                    return
                if self.state.status.dirty_count:
                    self.notify("Commit local changes before preview.", severity="error")
                    return
                if self.state.status.ahead:
                    self.notify("Push commits before preview.", severity="error")
                    return
                plan = build_preview_plan(config, variant)
            except BoardwrightError as exc:
                self.notify(str(exc), severity="error")
                return
            self.ci_status = f"Preview {plan.variant} dispatching..."
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
            self.ci_status = f"Preview {variant} dispatched."
            self._render_state()
            self.notify(f"Preview {variant} dispatched.")

        def action_project_info(self) -> None:
            self.push_screen(ProjectInfoScreen(), self._save_project_info)

        def action_view_validation(self) -> None:
            self.push_screen(ValidationReportScreen(self.state.issues))

        def action_generate_sheet(self) -> None:
            self.push_screen(GenerateSheetScreen(), self._generate_sheet)

        def _save_project_info(self, result: dict[str, object] | None) -> None:
            if result is None:
                return
            try:
                config = load_config()
                path = update_project_config(
                    config,
                    project_fields=result["project"],
                    variant_fields=result["variants"],
                    asset_fields=result["assets"],
                    template_fields=result["template"],
                    sheet_fields=result["sheet"],
                    branch_fields=result["branches"],
                    legal_fields=result["legal"],
                    manufacturing_fields=result["manufacturing"],
                    output_fields=result["outputs"],
                    document_fields=result["documents"],
                )
                updated_config = load_config()
                write_document_revision_rows(
                    updated_config,
                    result.get("document_revisions", {}).get("schematic", []),
                    "schematic",
                )
                updated_config = load_config()
                write_revision_variables(updated_config, "schematic")
                _embed_template_sheet_image(updated_config)
                self.state = collect_dashboard_state()
            except BoardwrightError as exc:
                self.notify(str(exc), severity="error")
                return
            self._render_state()
            self.notify("Updated config and worksheet image.")

        def _generate_sheet(self, result: dict[str, str] | None) -> None:
            if result is None:
                return
            try:
                config = load_config()
                parent = Path(result["parent"]).expanduser()
                if not parent.is_absolute():
                    parent = config.root / parent
                if not parent.exists():
                    fallback_parent = find_primary_schematic(config.root)
                    if fallback_parent is None:
                        raise BoardwrightError("Could not find a parent schematic to update.")
                    parent = fallback_parent
                child_file = Path(result["child_file"]).expanduser()
                if not child_file.is_absolute():
                    child_file = parent.parent / child_file
                if not child_file.name:
                    child_file = parent.with_name(f"{slugify(result['sheet_name'])}.kicad_sch")
                add_hierarchical_sheet(
                    parent,
                    child_file,
                    sheet_name=result["sheet_name"],
                    child_title=result["child_title"] or result["sheet_name"],
                )
                self.state = collect_dashboard_state()
            except BoardwrightError as exc:
                self.notify(str(exc), severity="error")
                return
            self._render_state()
            self.notify(f"Generated {child_file.name}; updated {parent.name}.")

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
                config = load_config()
                result = fetch_latest_preview_artifact(config, variant)
                preview_state = build_preview_state(config, variant)
                try:
                    accepted_state = build_accepted_main_state(config)
                    accepted_error = ""
                except BoardwrightError as exc:
                    accepted_state = None
                    accepted_error = str(exc)
            except BoardwrightError as exc:
                self.call_from_thread(self._finish_review_fetch, variant, str(exc), None, None, str(exc))
                return
            self.call_from_thread(
                self._finish_review_fetch,
                variant,
                result,
                preview_state,
                accepted_state,
                accepted_error,
            )

        def _finish_review_fetch(
            self,
            variant: str,
            result: str | None,
            preview_state: "PreviewState | None",
            accepted_state: "AcceptedMainState | None",
            accepted_error: str,
        ) -> None:
            if preview_state is None:
                self.ci_status = result or "Preview fetch failed."
                self._render_state()
                self.notify(self.ci_status, severity="error")
                return
            self.review_destination = load_config().root / "boardwright-preview"
            self.state = collect_dashboard_state(
                preview_state=preview_state,
                accepted_state=accepted_state,
                accepted_error=accepted_error,
            )
            self.ci_status = format_preview_state(preview_state)
            self._render_state()
            if preview_state.reviewed:
                self.notify("Preview reviewed; Accept unlocked.")
            else:
                self.notify(result or "Preview fetched.")

        def action_open_preview_folder(self) -> None:
            if self.review_destination is None or not self.review_destination.exists():
                self.notify("Fetch a preview artifact first.", severity="warning")
                return
            try:
                message = _open_path(self.review_destination)
            except BoardwrightError as exc:
                self.notify(str(exc), severity="error")
                return
            self.notify(message)

        def action_open_preview_readme(self) -> None:
            if self.review_destination is None or not self.review_destination.exists():
                self.notify("Fetch a preview artifact first.", severity="warning")
                return
            readme = self.review_destination / "README.md"
            target = readme if readme.exists() else self.review_destination
            try:
                message = _open_path(target)
            except BoardwrightError as exc:
                self.notify(str(exc), severity="error")
                return
            self.notify(message)

        def action_record_change(self) -> None:
            if not self._require_action("Record"):
                return
            self.push_screen(ChangelogEntryScreen(), self._record_change)

        def action_commit_push(self) -> None:
            if not self._require_action("Commit"):
                return
            self.push_screen(CommitScreen(_suggested_commit_message()), self._commit_push)

        def action_accept_main(self) -> None:
            if not self._require_action("Accept"):
                return
            self.push_screen(AcceptMainScreen(), self._accept_main)

        def action_release_ci(self) -> None:
            if not self._require_action("Release"):
                return
            self.push_screen(ReleaseScreen(), self._release_ci)

        def action_show_shortcuts(self) -> None:
            self.push_screen(ShortcutHelpScreen())

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
                    "Recorded change; validation blocks commit.",
                    severity="error",
                )
            elif issues:
                self.notify(
                    f"Recorded change; warnings. Suggested commit: {suggestion}",
                    severity="warning",
                )
            else:
                self.notify(f"Recorded change. Suggested commit: {suggestion}")

        def _commit_push(self, result: str | None) -> None:
            if result is None:
                return
            message = result.strip()
            if not message:
                self.notify("Commit message is empty.", severity="error")
                return
            self.ci_status = f"{_action_display_name('Commit')} running..."
            self._render_state()
            self.notify("Committing...")
            Thread(target=self._commit_push_worker, args=(message,), daemon=True).start()

        def _commit_push_worker(self, message: str) -> None:
            try:
                config = load_config()
                status = collect_status(config)
                if status.branch != config.dev_branch:
                    self.call_from_thread(
                        self._finish_commit_push,
                        "",
                        f"Switch to {config.dev_branch} to commit.",
                    )
                    return
                if status.dirty_count and not status.unreleased_changes:
                    self.call_from_thread(
                        self._finish_commit_push,
                        "",
                        "Record a changelog entry first.",
                    )
                    return
                issues = tuple(validate_project(config))
                if any(issue.level == "error" for issue in issues):
                    self.call_from_thread(
                        self._finish_commit_push,
                        "",
                        "Validation failed.",
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
            result_message = f"{output or 'Committed changes.'}\n{push_output or 'Pushed.'}"
            self.call_from_thread(
                self._finish_commit_push,
                result_message,
                push_output if _command_failed(push_output) else None,
            )

        def _finish_commit_push(self, result: str, error: str | None) -> None:
            self.state = collect_dashboard_state()
            self.ci_status = error or result or "Commit complete."
            self._render_state()
            if error:
                self.notify(error, severity="error")
            else:
                self.notify(result or "Commit complete.")

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
                        f"Cannot accept: preview is {preview_state.state}.",
                        severity="error",
                    )
                    return
                if not preview_state.reviewed:
                    self.ci_status = format_preview_state(preview_state)
                    self._render_state()
                    self.notify(
                        "Review the fresh preview first.",
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
            self.ci_status = f"Accept {variant} dispatched."
            self._render_state()
            self.notify(f"Accept {variant} dispatched.")
            self._poll_ci_status()

        def _release_ci(self, result: tuple[str, ...] | None) -> None:
            if result is None:
                return
            version, variant, kind, drawing_revision, revision_description, *extra = result
            change_classes = extra[0] if extra else ""
            justification = extra[1] if len(extra) > 1 else ""
            try:
                config = load_config()
                accepted_state = build_accepted_main_state(config)
                checklist = build_release_checklist(
                    config,
                    version,
                    variant,
                    kind,
                    drawing_revision,
                    revision_description,
                    accepted_state,
                    change_classes=change_classes,
                    justification=justification,
                )
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
                self.notify("Release checklist has blocking items.", severity="error")
                return
            try:
                config = load_config()
                dispatch_workflow_action(config, checklist.action)
            except BoardwrightError as exc:
                self.notify(str(exc), severity="error")
                return
            self.state = collect_dashboard_state(checklist.version or "0.1.0")
            self.ci_status = f"Release {checklist.version} dispatched."
            self._render_state()
            self.notify(f"Release {checklist.version} dispatched.")
            self._poll_ci_status()

        def _render_state(self) -> None:
            status = self.state.status
            dirty_summary = f"{status.dirty_count} changed" if status.dirty_count else "clean"
            brief = self._layout_mode == "tiny"
            self.title = self._screen_title()
            self.sub_title = self._screen_subtitle()
            self._sync_action_labels()
            self.query_one("#top_status", Static).update(
                _format_top_status(status, self.state.issues, self.ci_status, brief=brief)
            )
            self.query_one("#project_status", Static).update(
                _format_project_summary(status, brief=brief)
            )
            self.query_one("#workflow_status", Static).update(
                _format_timeline(self.state.workflow.steps)
            )
            self.query_one("#inspector_status", Static).update(
                _format_inspector(self.state, self.ci_status, brief=brief)
            )
            self.query_one("#validation_status", Static).update(
                _format_issues(self.state.issues, brief=brief)
            )
            self.query_one("#git_status", Static).update(
                _format_changed_files(self.state.changed_files, brief=brief)
            )
            self.query_one("#shortcut_hint", Static).update(_format_footer_hint(self.state))
            for button_id, action_name in (
                ("record_change", "Record"),
                ("commit_push", "Commit"),
                ("generate_preview", "Preview"),
                ("review_artifacts", "Review"),
                ("accept_main", "Accept"),
                ("release_ci", "Release"),
            ):
                self.query_one(f"#{button_id}", Button).disabled = not action_state(
                    self.state.workflow,
                    action_name,
                ).enabled
            self.query_one("#project_info", Button).disabled = False
            self.query_one("#refresh", Button).disabled = False

    return BoardwrightTui


def _format_issues(issues: tuple[ValidationIssue, ...], *, brief: bool = False) -> Text:
    text = Text()
    if not issues:
        text.append("Validation ok.", style=_semantic_style("loaded"))
        return text

    errors = [issue for issue in issues if issue.level == "error"]
    warnings = [issue for issue in issues if issue.level == "warning"]
    text.append(
        f"Validation: {len(errors)} error(s), {len(warnings)} warning(s)\n",
        style=_semantic_style("error" if errors else "warning"),
    )

    shown = 0
    limit = 1 if brief else 3
    for issue in [*errors, *warnings]:
        if shown >= limit:
            break
        style = _semantic_style("error" if issue.level == "error" else "warning")
        prefix = "!" if issue.level == "error" else "~"
        text.append(f"{prefix} {issue.message}\n", style=style)
        shown += 1

    remaining = len(issues) - shown
    if remaining > 0:
        text.append(
            f"...and {remaining} more. Press `v` for details.",
            style=_semantic_style("info", bold=False),
        )
    return text


def _format_validation_details(issues: tuple[ValidationIssue, ...]) -> Text:
    text = Text()
    if not issues:
        text.append("Validation ok.\n", style=_semantic_style("loaded"))
        text.append("No blocking issues.", style=_semantic_style("loaded", bold=False))
        return text

    errors = [issue for issue in issues if issue.level == "error"]
    warnings = [issue for issue in issues if issue.level == "warning"]
    text.append(
        f"{len(errors)} error(s), {len(warnings)} warning(s)\n\n",
        style=_semantic_style("warning" if warnings and not errors else "error" if errors else "loaded"),
    )
    if errors:
        text.append("Errors\n", style=f"bold {STATUS_ERROR}")
        for issue in errors:
            text.append(f"! {issue.message}\n", style=_semantic_style("error"))
        text.append("\n")
    if warnings:
        text.append("Warnings\n", style=f"bold {STATUS_WARNING}")
        for issue in warnings:
            text.append(f"~ {issue.message}\n", style=_semantic_style("warning"))
        text.append("\n")
    text.append(
        "Use `boardwright doctor` for the full check set.",
        style=_semantic_style("info", bold=False),
    )
    return text


def _format_top_status(
    status: ProjectStatus,
    issues: tuple[ValidationIssue, ...],
    ci_status: str,
    *,
    brief: bool = False,
) -> Text:
    if brief:
        return _format_top_status_brief(status, issues, ci_status)

    text = Text()
    text.append(status.project_id, style=f"bold {BRAND_TEAL}")
    text.append(" | branch ")
    text.append(status.branch, style=_semantic_style("info"))
    text.append(" | git ")
    if status.dirty_count:
        text.append(f"{status.dirty_count} changed", style=_semantic_style("warning"))
    else:
        text.append("clean", style=_semantic_style("loaded"))
    if status.ahead or status.behind:
        text.append(" | remote ")
        text.append(f"+{status.ahead}/-{status.behind}", style=_semantic_style("warning"))
    text.append(" | dev ")
    text.append(status.variant, style=_semantic_style("info"))
    text.append(" | tag ")
    text.append(status.latest_tag or "none", style=_semantic_style("loaded" if status.latest_tag else "unset"))
    text.append(" | ")
    text.append(_ci_status_short(ci_status), style=_ci_status_style(ci_status))
    text.append(" | ")
    text.append(_issue_summary(issues), style=_issue_summary_style(issues))
    return text


def _format_top_status_brief(
    status: ProjectStatus,
    issues: tuple[ValidationIssue, ...],
    ci_status: str,
) -> Text:
    text = Text()
    text.append(status.project_id, style=f"bold {BRAND_TEAL}")
    text.append(" | ")
    text.append(f"git {'changed' if status.dirty_count else 'clean'}", style=_semantic_style("warning" if status.dirty_count else "loaded"))
    text.append(" | ")
    text.append(f"dev {status.variant}", style=_semantic_style("info"))
    text.append(" | tag ")
    text.append(status.latest_tag or "none", style=_semantic_style("loaded" if status.latest_tag else "unset"))
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
        return _semantic_style("error")
    if any(issue.level == "warning" for issue in issues):
        return _semantic_style("warning")
    return _semantic_style("loaded")


def _format_project_summary(status: ProjectStatus, *, brief: bool = False) -> str:
    lines = [
        f"PCBA: {status.project_name}",
        f"Dev default: {status.variant}",
        f"Unreleased: {'yes' if status.unreleased_changes else 'no'}",
        f"Git: {status.dirty_count} changed" if status.dirty_count else "Git: clean",
    ]
    if not brief:
        lines.insert(2, f"Preview default: {load_config().preview_variant}")
        lines.append(f"Remote: ahead {status.ahead}, behind {status.behind}")
    return "\n".join(lines)


def _workflow_steps(state: DashboardState) -> tuple[WorkflowStep, ...]:
    return state.workflow.steps


def _format_timeline(steps: tuple[WorkflowStep, ...]) -> Text:
    text = Text()
    for step in steps:
        text.append(f"{_workflow_marker(step.state)} ", style=_workflow_state_style(step.state))
        text.append(_timeline_step_label(step.label), style="bold")
        text.append(" | ", style="dim")
        text.append(step.state, style=_workflow_state_style(step.state))
        text.append("\n")
    return text


def _timeline_step_label(label: str) -> str:
    stripped = label.split(" ", 1)[1] if label[:1].isdigit() and " " in label else label
    return {
        "Edit in KiCad": "Edit",
    }.get(stripped, stripped)


def _workflow_marker(state: str) -> str:
    if state in {"done", "passed"}:
        return "[x]"
    if state in {"ready", "needed"}:
        return "[>]"
    if state == "running":
        return "[~]"
    if state in {"blocked", "failed"}:
        return "[!]"
    return "[ ]"


def _workflow_state_style(state: str) -> str:
    if state in {"done", "ready", "passed"}:
        return _semantic_style("loaded")
    if state in {"running", "queued", "polling", "dispatching", "downloading"}:
        return _semantic_style("loading")
    if state in {"needed", "needs action", "waiting", "stale", "locked"}:
        return _semantic_style("warning")
    if state in {"missing", "not checked"}:
        return _semantic_style("unset")
    if state in {"blocked", "failed"}:
        return _semantic_style("error")
    if state == "external":
        return _semantic_style("info")
    return "bold"


def _suggested_commit_message(seed: str = "") -> str:
    try:
        return suggest_commit_message(load_config().root, seed)
    except BoardwrightError:
        return ""


def _format_inspector(state: DashboardState, ci_status: str = "CI not polled", *, brief: bool = False) -> Text:
    text = Text()
    _append_inspector_heading(text, "NOW")
    if brief:
        text.append(
            f"Next: {_action_display_name(state.workflow.next_action)} | {state.workflow.stage}\n",
            style="bold",
        )
    else:
        text.append(f"Next: {_action_display_name(state.workflow.next_action)}\n", style="bold")
        text.append(f"Reason: {state.workflow.reason}\n", style="dim")
        text.append(f"Stage: {state.workflow.stage}", style="dim")
    text.append("\n\n")

    _append_inspector_heading(text, "EVIDENCE")
    preview_evidence = _preview_evidence_from_ci(ci_status)
    accept_evidence = _accept_evidence_from_ci(ci_status) or _accepted_summary_compact(state.accepted_summary)
    release_evidence = _release_evidence_from_ci(ci_status)
    text.append(f"Preview: {preview_evidence}\n", style=_evidence_style(preview_evidence))
    text.append(f"Accept: {accept_evidence}\n", style=_evidence_style(accept_evidence))
    text.append(f"Release: {release_evidence}\n", style=_evidence_style(release_evidence))
    review_hint = _review_hint(state)
    if review_hint and not brief:
        text.append(f"Review: {review_hint}", style=_semantic_style("warning"))
        text.append("\n")
    lock_lines = _locked_action_lines(state.workflow)
    if lock_lines and not brief:
        text.append("\n\n")
        _append_inspector_heading(text, "LOCKED")
        locked = "; ".join(f"{_action_display_name(name)} - {reason}" for name, reason in lock_lines)
        text.append(locked, style="dim")
        text.append("\n")
    text.append("\n")

    _append_inspector_heading(text, "RELEASE")
    text.append(_release_summary_short(state), style=_release_summary_style(state.release_summary))
    text.append("\n")
    if state.release_summary == "ready for dry-run":
        text.append("Ready for dry run.", style="dim")
    else:
        text.append(state.ci_release_summary, style="dim")
    return text


def _append_inspector_heading(text: Text, label: str) -> None:
    text.append(label, style=f"bold {BRAND_TEAL}")
    text.append("\n")


def _format_footer_hint(state: DashboardState) -> Text:
    text = Text()
    text.append("Next: ", style=f"bold {BRAND_TEAL}")
    text.append(_action_display_name(state.workflow.next_action), style="bold")
    text.append(
        " | Main: F1 help, v details, q quit, r refresh, c record, m commit, g preview, a review, o folder, O README, n sheet, p accept, l release, i info"
    )
    return text


def _format_shortcut_help() -> Text:
    text = Text()
    text.append("Main screen\n", style=f"bold {BRAND_TEAL}")
    text.append("F1", style=_semantic_style("info"))
    text.append(
        " help\nv details\nq quit\nr refresh\nc record\nm commit\ng preview\na review\nn new sheet\np accept\nl release\ni info\no folder\nO README\n"
    )
    text.append("\nCues\n", style=f"bold {BRAND_TEAL}")
    text.append(
        "Footer tracks the current screen; the subtitle shows stage and next action."
    )
    return text


def _review_hint(state: DashboardState) -> str:
    if state.workflow.next_action == "Preview":
        return "run preview after push."
    if state.workflow.next_action == "Fetch":
        return f"download preview to unlock {_action_display_name('Accept')}."
    if state.workflow.next_action == "Accept":
        return f"preview reviewed; {_action_display_name('Accept')} is ready."
    return ""


def _locked_action_lines(workflow: WorkflowState) -> list[tuple[str, str]]:
    names = {"Preview", "Review", "Accept", "Release"}
    return [
        (action.name, action.reason)
        for action in workflow.actions
        if action.name in names and not action.enabled
    ][:3]


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


def _accepted_summary_compact(summary: str) -> str:
    lines = [line.strip() for line in summary.splitlines() if line.strip()]
    state = _line_value(lines, "State")
    if state:
        return state
    return lines[0] if lines else "not checked"


def _accepted_summary_style(summary: str) -> str:
    state = _line_value([line.strip() for line in summary.splitlines()], "State")
    if state == "ready":
        return _semantic_style("loaded")
    if state == "failed":
        return _semantic_style("error")
    if state == "running":
        return _semantic_style("loading")
    if state in {"missing", "stale", "not checked"}:
        return _semantic_style("unset")
    if state:
        return _semantic_style("warning")
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
        return "Release inputs look good."
    return state.release_summary


def _release_summary_style(summary: str) -> str:
    if summary == "ready for dry-run":
        return _semantic_style("loaded")
    if "running" in summary or "dispatching" in summary:
        return _semantic_style("loading")
    if "error" in summary.lower() or "failed" in summary.lower():
        return _semantic_style("error")
    if "not checked" in summary.lower() or "no release runs" in summary.lower():
        return _semantic_style("unset")
    return _semantic_style("warning")


def _format_review_artifacts(preview_state: "PreviewState", runs_text: str) -> str:
    status, message, run_summary = _review_artifact_blocks(preview_state)
    return "\n".join([status, message, "", run_summary, "", "Recent CI:", runs_text])


def _format_polled_ci_status(
    preview_state: "PreviewState | None",
    accepted_state: "AcceptedMainState | None",
    preview_error: str = "",
    accepted_error: str = "",
    release_status: str = "Release: not checked",
) -> str:
    lines: list[str] = []
    preview_summary = "Preview:not checked"
    if preview_state is not None:
        preview_summary = _preview_summary_compact(preview_state)
    elif preview_error:
        preview_summary = "Preview:error"

    accept_summary = "Accept:not checked"
    if accepted_state is not None:
        accept_summary = f"Accept:{accepted_state.state}"
    elif accepted_error:
        accept_summary = "Accept:error"

    release_summary = _release_status_compact(release_status, include_publish=False)
    preview_summary = preview_summary.replace("Preview:", "Preview: ")
    accept_summary = accept_summary.replace("Accept:", "Accept: ")
    release_summary = release_summary.removeprefix("Release:").strip()
    lines.append(f"CI: {preview_summary} | {accept_summary} | Release: {release_summary}")
    lines.append("")
    lines.append("Preview:")
    if preview_state is not None:
        lines.append(format_preview_state(preview_state))
    elif preview_error:
        lines.append(f"Preview: {preview_error}")
    else:
        lines.append("Preview: not checked")

    lines.append("")
    lines.append("Main:")
    if accepted_state is not None:
        lines.append(format_accepted_state(accepted_state))
    elif accepted_error:
        lines.append(accepted_error)
    else:
        lines.append("Main evidence not checked.")
    lines.append("")
    lines.append(release_status)
    return "\n".join(lines)


def _preview_summary_short(preview_state: "PreviewState") -> str:
    variant = preview_state.artifact_name.removeprefix("boardwright-preview-")
    if preview_state.state == "ready" and not preview_state.reviewed:
        return f"preview {variant} review needed"
    return f"preview {variant} {preview_state.state}"


def _preview_summary_compact(preview_state: "PreviewState") -> str:
    variant = preview_state.artifact_name.removeprefix("boardwright-preview-")
    state = "review" if preview_state.state == "ready" and not preview_state.reviewed else preview_state.state
    return f"Preview:{variant} {state}"


def _release_ci_status_from_runs(runs: tuple[object, ...]) -> str:
    prepare = _latest_run_by_title_or_workflow(
        runs,
        title_prefixes=("Prepare ",),
        workflow_markers=("prepare release",),
    )
    publish = _latest_run_by_title_or_workflow(
        runs,
        title_prefixes=("Publish ",),
        workflow_markers=("publish release",),
    )
    parts: list[str] = []
    if prepare is not None:
        parts.append("prepare " + _run_status_short(prepare))
    if publish is not None:
        parts.append("publish " + _run_status_short(publish))
    return "Release: " + (" | ".join(parts) if parts else "no release runs")


def _latest_run_by_title_or_workflow(
    runs: tuple[object, ...],
    *,
    title_prefixes: tuple[str, ...],
    workflow_markers: tuple[str, ...],
) -> object | None:
    lowered_prefixes = tuple(prefix.lower() for prefix in title_prefixes)
    lowered_markers = tuple(marker.lower() for marker in workflow_markers)
    for run in runs:
        title = str(getattr(run, "title", "") or "").lower()
        workflow = str(getattr(run, "workflow", "") or "").lower()
        if any(title.startswith(prefix) for prefix in lowered_prefixes):
            return run
        if any(marker in workflow for marker in lowered_markers):
            return run
    return None


def _run_status_short(run: object) -> str:
    status = str(getattr(run, "status", "") or "unknown")
    conclusion = str(getattr(run, "conclusion", "") or "")
    run_id = str(getattr(run, "database_id", "") or "")
    title = str(getattr(run, "title", "") or "")
    state = conclusion if status == "completed" and conclusion else status
    bits = [state]
    if run_id:
        bits.append(f"run {run_id}")
    if title:
        bits.append(title)
    return " ".join(bits)


def _release_status_compact(release_status: str, *, include_publish: bool = True) -> str:
    value = release_status.removeprefix("Release:").strip()
    if not value:
        return "not checked"
    if value in {"not checked", "no release runs"}:
        return value

    parts: list[str] = []
    publish_part = ""
    for raw_part in value.split("|"):
        words = raw_part.strip().split()
        if not words:
            continue
        if len(words) == 1:
            parts.append(words[0])
            continue
        step = words[0]
        state = words[1]
        compact = f"{step} {_ci_state_word(state)}"
        if step == "publish":
            publish_part = compact
            if include_publish:
                parts.append(compact)
            continue
        parts.append(compact)
    if not parts and publish_part:
        parts.append(publish_part)
    return " | ".join(parts) if parts else value


def _ci_state_word(state: str) -> str:
    if state == "in_progress":
        return "running"
    return state


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
    drawing_revision: str,
    revision_description: str,
    accepted_state: "AcceptedMainState",
    *,
    change_classes: str = "",
    justification: str = "",
) -> ReleaseChecklist:
    from .impact import assess_release_impact, format_impact

    selected_version = version.strip()
    selected_variant = variant.strip()
    selected_kind = kind.strip()
    selected_drawing_revision = drawing_revision.strip()
    selected_revision_description = " ".join(revision_description.split())
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
            selected_drawing_revision,
            selected_revision_description,
            change_classes=change_classes,
            justification=justification,
            source_sha=accepted_state.expected_sha if accepted_state.ready else "",
        )
    except BoardwrightError as exc:
        action_error = str(exc)

    impact = None
    impact_error = ""
    try:
        impact = assess_release_impact(
            config,
            selected_version,
            change_classes=change_classes,
            justification=justification or selected_revision_description,
            schematic_revision=selected_drawing_revision,
        )
    except BoardwrightError as exc:
        impact_error = str(exc)

    try:
        release_plan = build_release_plan(config, selected_version, check_remote=False)
    except BoardwrightError as exc:
        release_error = str(exc)
    items = [
        ReleaseChecklistItem(
            "Main",
            accepted_state.ready,
            accepted_state.message or f"State: {accepted_state.state}",
        ),
        ReleaseChecklistItem(
            "Inputs",
            action is not None,
            (
                f"{selected_version} | {selected_variant} | {selected_kind} | drawing rev {selected_drawing_revision}"
                if action is not None
                else action_error
            ),
        ),
        ReleaseChecklistItem(
            "Revision row",
            bool(selected_drawing_revision and selected_revision_description and len(selected_revision_description) <= 60),
            (
                f"{selected_drawing_revision}: {selected_revision_description}"
                if selected_drawing_revision and selected_revision_description and len(selected_revision_description) <= 60
                else "Drawing revision and a 60-character description are required."
            ),
        ),
        ReleaseChecklistItem(
            "Changelog",
            release_plan is not None,
            (
                "Changelog has entries."
                if release_plan and release_plan.has_unreleased_changes
                else "No entries; notes will be generated."
            ),
        ),
        ReleaseChecklistItem(
            "Tag",
            bool(release_plan and not release_plan.local_tag_exists),
            (
                f"No local tag named {selected_version}."
                if release_plan and not release_plan.local_tag_exists
                else release_error or f"Local tag already exists: {selected_version}."
            ),
        ),
        ReleaseChecklistItem(
            "Impact",
            impact is not None and impact.ok,
            (
                impact_error
                if impact is None
                else ("Revision identities consistent with detected changes." if impact.ok else "; ".join(impact.problems))
            ),
        ),
        ReleaseChecklistItem(
            "Dispatch",
            action is not None,
            (
                f"{action.workflow} on {action.ref}"
                if action is not None
                else "Release action could not be built."
            ),
        ),
    ]
    return ReleaseChecklist(
        selected_version,
        selected_variant,
        selected_kind,
        selected_drawing_revision,
        selected_revision_description,
        accepted_summary,
        tuple(items),
        action,
        tuple(format_impact(impact)) if impact is not None else (),
    )


def _format_release_checklist(checklist: ReleaseChecklist) -> str:
    lines = [
        f"Release {checklist.version or '(blank)'} | {checklist.variant} | {checklist.kind}",
        f"Drawing rev {checklist.drawing_revision or '(blank)'} | {checklist.revision_description or '(blank)'}",
    ]
    impact_rows = [line for line in checklist.impact_lines if not line.startswith("Problem: ")]
    if impact_rows:
        lines.extend(["", "Identities and impact:", *impact_rows])
    lines.extend(["", "Readiness:"])
    blockers: list[str] = []
    for item in checklist.items:
        marker = "[x]" if item.passed else "[ ]"
        lines.append(f"{marker} {item.label}")
        if not item.passed:
            blockers.append(f"- {item.label}: {item.detail}")
    lines.extend(
        [
            "",
            "Main:",
            _accepted_summary_short(checklist.accepted_summary),
        ]
    )
    if blockers:
        lines.extend(["", "Blockers:", *blockers])
    lines.extend(
        [
            "",
            (
                "Ready to dispatch."
                if checklist.can_dispatch
                else "Resolve blockers first."
            ),
        ]
    )
    return "\n".join(lines)


def _next_action(state: DashboardState) -> str:
    return f"{_action_display_name(state.workflow.next_action)}: {state.workflow.reason}"


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

    if first_line.startswith("CI: "):
        return first_line.removeprefix("CI: ").strip()

    if first_line.startswith("Artifact: boardwright-preview-"):
        variant = first_line.removeprefix("Artifact: boardwright-preview-").strip()
        state = _line_value(lines, "State") or "unknown"
        reviewed = _line_value(lines, "Reviewed")
        if state == "ready" and reviewed == "no":
            return f"preview {variant} review needed"
        return f"preview {variant} {state}"

    if " | boardwright-preview-" in first_line:
        state, artifact = first_line.split(" | ", 1)
        variant = artifact.removeprefix("boardwright-preview-").strip()
        if state.upper() == "READY" and "Reviewed no" in ci_status:
            return f"preview {variant} review needed"
        return f"preview {variant} {state.lower()}"

    if first_line.startswith("Fetched boardwright-preview-"):
        variant = first_line.removeprefix("Fetched boardwright-preview-").split()[0]
        return f"preview {variant} fetched"

    if first_line.startswith("Downloading boardwright-preview-"):
        variant = first_line.removeprefix("Downloading boardwright-preview-").split("...", 1)[0]
        return f"preview {variant} downloading"

    if first_line.startswith("Preview ") and first_line.endswith(" dispatching..."):
        variant = first_line.removeprefix("Preview ").removesuffix(" dispatching...")
        return f"preview {variant} dispatching"

    if first_line.startswith("Preview ") and first_line.endswith(" dispatched."):
        variant = first_line.removeprefix("Preview ").removesuffix(" dispatched.")
        return f"preview {variant} dispatched"

    if len(first_line) > 36:
        return first_line[:33] + "..."
    return first_line


def _preview_evidence_from_ci(ci_status: str) -> str:
    preview = _compact_ci_value(ci_status, "Preview")
    if preview:
        return preview.replace(" review", " review needed")
    return _ci_status_short(ci_status)


def _accept_evidence_from_ci(ci_status: str) -> str:
    return _compact_ci_value(ci_status, "Accept")


def _release_evidence_from_ci(ci_status: str) -> str:
    release = _compact_ci_value(ci_status, "Release")
    if release:
        return release
    for line in ci_status.splitlines():
        if line.startswith("Release:"):
            return _release_status_compact(line, include_publish=False)
    return "not checked"


def _compact_ci_value(ci_status: str, key: str) -> str:
    first_line = ci_status.splitlines()[0] if ci_status else ""
    if not first_line.startswith("CI: "):
        return ""
    prefix = f"{key}:"
    for part in first_line.removeprefix("CI: ").split(" | "):
        part = part.strip()
        if part.startswith(prefix):
            return part.removeprefix(prefix).strip()
    return ""


def _line_value(lines: list[str], key: str) -> str:
    prefix = f"{key}:"
    for line in lines:
        if line.startswith(prefix):
            return line.removeprefix(prefix).strip()
    return ""


def _ci_status_style(ci_status: str) -> str:
    lowered = ci_status.lower()
    if "failure" in lowered or "failed" in lowered or "error" in lowered:
        return _semantic_style("error")
    if (
        "in_progress" in lowered
        or "queued" in lowered
        or "pending" in lowered
        or "running" in lowered
        or "polling" in lowered
        or "dispatching" in lowered
        or "downloading" in lowered
        or "review needed" in lowered
        or " review" in lowered
        or "reviewed no" in lowered
    ):
        return _semantic_style("loading")
    if "stale" in lowered or "needed" in lowered:
        return _semantic_style("warning")
    if "missing" in lowered or "not checked" in lowered or "none" in lowered:
        return _semantic_style("unset")
    if "success" in lowered or "completed" in lowered or "ready" in lowered or "fetched" in lowered:
        return _semantic_style("loaded")
    return _semantic_style("info", bold=False)


def _evidence_style(value: str) -> str:
    return _ci_status_style(value)


def _field_value(fields: tuple[tuple[str, str], ...], key: str) -> str:
    return next((value for field_key, value in fields if field_key == key), "")


def _notification_severity(issues: tuple[ValidationIssue, ...]) -> str:
    if any(issue.level == "error" for issue in issues):
        return "error"
    return "warning"


def _command_failed(output: str) -> bool:
    lowered = output.lower()
    return any(marker in lowered for marker in ("fatal:", "error:", "rejected", "failed"))


def _format_changed_files(changed_files: tuple[str, ...], *, brief: bool = False) -> Text:
    text = Text()
    if not changed_files:
        text.append("Working tree clean.", style=_semantic_style("loaded"))
        return text
    if not brief:
        text.append("Legend: ", style=f"bold {BRAND_TEAL}")
        legend_items = (
            ("MOD", "modified", "warning"),
            ("NEW", "new", "loaded"),
            ("DEL", "deleted", "error"),
            ("REN", "renamed", "info"),
            ("IGN", "ignored", "unset"),
            ("CON", "conflict", "error"),
        )
        for index, (badge, label, kind) in enumerate(legend_items):
            if index:
                text.append(" | ", style=_semantic_style("info", bold=False))
            text.append(f"[{badge}] ", style=_semantic_style(kind))
            text.append(label, style="bold")
        text.append("\n\n")
    limit = 6 if brief else 12
    for line in changed_files[:limit]:
        badge, style = _git_status_badge(line[:2] if len(line) >= 2 else line)
        path = line[3:].strip() if len(line) > 3 else line.strip()
        text.append(f"[{badge}] ", style=style)
        text.append(path or line.strip(), style="bold")
        text.append("\n")
    remaining = len(changed_files) - limit
    if remaining > 0:
        text.append(f"...and {remaining} more file(s)", style=_semantic_style("info", bold=False))
    return text


def _git_status_badge(code: str) -> tuple[str, str]:
    normalized = code.strip()
    if normalized == "??":
        return "NEW", _semantic_style("info")
    if normalized == "!!":
        return "IGN", _semantic_style("unset")
    primary = next((char for char in code if char != " "), "")
    if primary == "A":
        return "NEW", _semantic_style("loaded")
    if primary == "D":
        return "DEL", _semantic_style("error")
    if primary == "R":
        return "REN", _semantic_style("info")
    if primary == "C":
        return "CPY", _semantic_style("info")
    if primary == "U":
        return "CON", _semantic_style("error")
    if primary == "M":
        return "MOD", _semantic_style("warning")
    if primary:
        return primary, _semantic_style("info")
    return "UNK", _semantic_style("unset")



def _format_document_revision_rows_input(rows: list[dict[str, object]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["REV", "DATE", "DESCRIPTION", "DRAWN"])
    for row in rows:
        writer.writerow(
            [
                str(row.get("revision", "")),
                str(row.get("date", "")),
                str(row.get("description", "")),
                str(row.get("drawn_by", "")),
            ]
        )
    return buffer.getvalue()


def _parse_document_revision_rows_input(text: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    reader = csv.reader(text.splitlines())
    for raw_row in reader:
        row = [cell.strip() for cell in raw_row]
        if not any(row):
            continue
        first = row[0].lower() if row else ""
        if first in {"rev", "revision"}:
            continue
        while len(row) < 4:
            row.append("")
        rows.append(
            {
                "revision": row[0],
                "date": row[1],
                "description": row[2],
                "drawn_by": row[3],
            }
        )
    return rows

def _format_impedance_table_input(entries: list[dict[str, object]]) -> str:
    if not entries:
        return "Transmission Line,Impedance [ohms],Tolerance [ohms],Layer,Trace Width [mm],Gap [mm],Ref. Layers\n"

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "Transmission Line",
            "Impedance [ohms]",
            "Tolerance [ohms]",
            "Layer",
            "Trace Width [mm]",
            "Gap [mm]",
            "Ref. Layers",
        ]
    )
    for entry in entries:
        writer.writerow(
            [
                str(entry.get("transmission_line", "")),
                str(entry.get("impedance_ohms", "")),
                str(entry.get("tolerance_ohms", "")),
                str(entry.get("layer", "")),
                str(entry.get("trace_width_mm", "")),
                str(entry.get("gap_mm", "")),
                str(entry.get("ref_layers", "")),
            ]
        )
    return buffer.getvalue()


def _parse_impedance_table_input(text: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    reader = csv.reader(text.splitlines())
    for raw_row in reader:
        row = [cell.strip() for cell in raw_row]
        if not any(row):
            continue
        if row[0].lower().startswith("transmission line"):
            continue
        while len(row) < 7:
            row.append("")
        rows.append(
            {
                "transmission_line": row[0],
                "impedance_ohms": row[1],
                "tolerance_ohms": row[2],
                "layer": row[3],
                "trace_width_mm": row[4],
                "gap_mm": row[5],
                "ref_layers": row[6],
            }
        )
    return rows


def _open_path(path: Path) -> str:
    resolved = path.resolve()
    if not resolved.exists():
        raise BoardwrightError(f"Path does not exist: {resolved}")

    try:
        if hasattr(os, "startfile"):
            os.startfile(str(resolved))  # type: ignore[attr-defined]
        elif os.name == "posix":
            opener = "open" if shutil.which("open") else "xdg-open"
            if shutil.which(opener) is None:
                raise BoardwrightError("No system file opener is available.")
            completed = subprocess.run(
                [opener, str(resolved)],
                text=True,
                capture_output=True,
                check=False,
            )
            if completed.returncode != 0:
                message = completed.stderr.strip() or completed.stdout.strip()
                raise BoardwrightError(message or f"Could not open {resolved}.")
        else:
            raise BoardwrightError("No supported file opener is available on this platform.")
    except OSError as exc:
        raise BoardwrightError(f"Could not open {resolved}: {exc}") from exc

    return f"Opened {resolved}."
