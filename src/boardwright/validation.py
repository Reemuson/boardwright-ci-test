# SPDX-License-Identifier: LicenseRef-Boardwright-Source-Available
# Copyright (c) Ryan Hicks

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from .changelog import parse_releases, read_changelog
from .config import BoardwrightConfig, CONFIG_DIR
from .git_ops import remote_url
from .variants import VARIANTS


@dataclass(frozen=True)
class ValidationIssue:
    level: str
    message: str


def validate_project(config: BoardwrightConfig) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    root = config.root

    _require_file(root / CONFIG_DIR / "project.yaml", issues)
    _require_file(root / CONFIG_DIR / "branches.yaml", issues)
    _require_file(root / CONFIG_DIR / "legal.yaml", issues)
    _require_file(root / CONFIG_DIR / "revision_history.yaml", issues)
    _require_file(root / "CHANGELOG.md", issues)
    _require_file(root / "LICENSE", issues)
    _require_file(root / "README.md", issues)

    _validate_project_config(config, issues)
    _validate_changelog(config, issues)
    _validate_revision_history_config(config, issues)
    _validate_kicad_and_kibot(root, issues)
    _validate_assets(config, issues)
    _validate_repository_metadata(config, issues)
    _validate_readme_template(root, issues)
    _validate_known_local_branding(root, issues)
    _validate_deprecated_schematic_variables(root, issues, config.project)
    _validate_fabrication_requirements(config, issues)
    _validate_worksheet_logo(config, issues)

    return issues


def _validate_fabrication_requirements(config: BoardwrightConfig, issues: list[ValidationIssue]) -> None:
    """Errors for anything that would stop fabrication notes rendering truthfully."""
    from .fabrication_requirements import (
        FabricationRequirementError,
        derive_project_requirements,
        undeclared_manufacturing_keys,
        undeclared_message,
    )

    from .fabrication_requirements import unstated_requirements
    from .kicad_tables import fabrication_notes_template

    missing = undeclared_manufacturing_keys(config.project.get("manufacturing"))
    if missing:
        issues.append(ValidationIssue("error", undeclared_message(missing)))
    try:
        template, source = fabrication_notes_template(config.root)
    except OSError as error:
        issues.append(ValidationIssue("error", f"Fabrication notes template is unreadable: {error}"))
    else:
        unstated = unstated_requirements(template, config.project.get("manufacturing"))
        if unstated:
            issues.append(
                ValidationIssue(
                    "error",
                    f"{source} does not state declared requirement(s): "
                    + ", ".join(f"manufacturing.{key}" for key in unstated),
                )
            )
    pcbs = sorted(config.root.glob("*.kicad_pcb"))
    if len(pcbs) != 1:
        return  # _validate_kicad_and_kibot reports missing or ambiguous boards.
    try:
        derive_project_requirements(config.root, pcbs[0])
    except FabricationRequirementError as error:
        issues.append(ValidationIssue("error", str(error)))
    _validate_silkscreen_plotting(config, pcbs[0], issues)


def _validate_worksheet_logo(config: BoardwrightConfig, issues: list[ValidationIssue]) -> None:
    """Active drawing sheets must resolve, and render the configured logo.

    Shared with `python -m boardwright.drawing_sheets check` (run in CI). A
    configured logo that is missing, unreadable or not what an active sheet
    renders is an error: Boardwright never falls back to its own logo.
    """
    from .drawing_sheets import branding_issues, sheet_issues

    found = sheet_issues(config.root, str(config.template.get("worksheet") or "")) + branding_issues(config)
    issues.extend(ValidationIssue(issue.level, issue.message) for issue in found)


def _validate_silkscreen_plotting(
    config: BoardwrightConfig, pcb: Path, issues: list[ValidationIssue]
) -> None:
    """Visible silkscreen text must reach the Gerbers (F20)."""
    from .silkscreen import validate_plot_options

    try:
        pcb_text = pcb.read_text(encoding="utf-8")
    except OSError as error:
        issues.append(ValidationIssue("error", f"{pcb.name} is unreadable: {error}"))
        return
    for problem in validate_plot_options(config.root, pcb_text):
        issues.append(ValidationIssue("error", problem))


def _require_file(path: Path, issues: list[ValidationIssue]) -> None:
    if not path.exists():
        issues.append(ValidationIssue("error", f"Missing required file: {path.name}"))


def _validate_project_config(
    config: BoardwrightConfig, issues: list[ValidationIssue]
) -> None:
    project = config.project.get("project", {})
    for key in ("name", "company", "designer"):
        if not str(project.get(key, "")).strip():
            issues.append(ValidationIssue("error", f"Missing project.{key} in project.yaml"))

    documents = config.documents
    for document_key, document in documents.items():
        if not isinstance(document, dict):
            issues.append(ValidationIssue("error", f"documents.{document_key} must be a mapping"))
            continue
        scheme = str(document.get("revision_scheme") or "lettered").strip()
        if scheme not in {"lettered", "numeric", "semantic"}:
            issues.append(
                ValidationIssue(
                    "error",
                    f"Unsupported documents.{document_key}.revision_scheme '{scheme}' in project.yaml",
                )
            )
        number_source = str(document.get("number_source") or "pcba_name").strip()
        if number_source not in {"name", "pcba_name", "pcb_name"}:
            issues.append(
                ValidationIssue(
                    "error",
                    f"Unsupported documents.{document_key}.number_source '{number_source}' in project.yaml",
                )
            )
        for key in ("drawn_date",):
            value = str(document.get(key, "")).strip()
            if value and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                issues.append(
                    ValidationIssue(
                        "warning",
                        f"documents.{document_key}.{key} should use YYYY-MM-DD format",
                    )
                )

    variants = config.project.get("variants", {})
    for key in ("dev_default", "preview_default", "main_default", "release_default"):
        variant = str(variants.get(key, "")).strip()
        if not variant:
            issues.append(ValidationIssue("error", f"Missing variants.{key} in project.yaml"))
        elif variant not in VARIANTS:
            issues.append(
                ValidationIssue(
                    "error",
                    f"Unsupported variants.{key} '{variant}' in project.yaml",
                )
            )

    outputs = config.project.get("outputs", {})
    preview_engine = str(outputs.get("preview_engine", "github-actions"))
    if preview_engine != "github-actions":
        issues.append(
            ValidationIssue(
                "error",
                f"Unsupported outputs.preview_engine '{preview_engine}' in project.yaml",
            )
        )

    _validate_workflow_exists(
        config,
        str(outputs.get("preview_workflow", "dev-preview.yaml")),
        "Preview",
        issues,
    )
    _validate_workflow_exists(
        config,
        str(outputs.get("main_workflow", "main-outputs.yaml")),
        "Main output",
        issues,
    )
    _validate_workflow_exists(
        config,
        str(outputs.get("prepare_release_workflow", "prepare-release.yaml")),
        "Prepare release",
        issues,
    )
    _validate_workflow_exists(
        config,
        str(outputs.get("release_workflow", "release.yaml")),
        "Release",
        issues,
    )


def _validate_changelog(config: BoardwrightConfig, issues: list[ValidationIssue]) -> None:
    try:
        text = read_changelog(config.root)
    except Exception as exc:
        issues.append(ValidationIssue("error", str(exc)))
        return

    releases = parse_releases(text)
    if not any(release.name == "Unreleased" for release in releases):
        issues.append(ValidationIssue("error", "CHANGELOG.md is missing ## [Unreleased]"))

    seen: set[str] = set()
    for release in releases:
        if release.name in seen and release.name != "Unreleased":
            issues.append(
                ValidationIssue("error", f"Duplicate changelog release: {release.name}")
            )
        seen.add(release.name)


def _validate_revision_history_config(
    config: BoardwrightConfig, issues: list[ValidationIssue]
) -> None:
    settings = config.revision_history.get("revision_history", {})
    slots = int(settings.get("slots", 4))
    preflight_slots = int(settings.get("preflight_slots", 12))
    if slots < 1:
        issues.append(ValidationIssue("error", "revision_history.slots must be at least 1"))
    if slots > preflight_slots:
        issues.append(
            ValidationIssue(
                "warning",
                "revision_history.slots is greater than revision_history.preflight_slots; "
                "KiBot may not define every REVTABLE_* variable.",
            )
        )


def _validate_readme_template(root: Path, issues: list[ValidationIssue]) -> None:
    template = root / "boardwright_resources/kibot/resources" / "templates" / "readme.txt"
    if not template.exists():
        issues.append(
            ValidationIssue(
                "warning",
                "Missing README template: boardwright_resources/kibot/resources/templates/readme.txt",
            )
        )
        return

    text = template.read_text(encoding="utf-8", errors="replace")
    required = ("LICENSE", "NOTICE.md", "THIRD_PARTY_NOTICES.md")
    missing = [name for name in required if name not in text]
    if missing:
        issues.append(
            ValidationIssue(
                "warning",
                "README template should mention legal files: "
                + ", ".join(missing),
            )
        )


def _validate_kicad_and_kibot(root: Path, issues: list[ValidationIssue]) -> None:
    pcb_files = list(root.glob("*.kicad_pcb"))
    if not list(root.glob("*.kicad_pro")):
        issues.append(ValidationIssue("error", "No KiCad project (*.kicad_pro) found"))
    if not list(root.glob("*.kicad_sch")):
        issues.append(ValidationIssue("error", "No KiCad schematic (*.kicad_sch) found"))
    if not pcb_files:
        issues.append(ValidationIssue("error", "No KiCad PCB (*.kicad_pcb) found"))
    elif not any(_has_edge_cuts_geometry(path) for path in pcb_files):
        issues.append(
            ValidationIssue(
                "warning",
                "No Edge.Cuts board outline geometry found; CHECKED/RELEASED CI builds may fail DRC.",
            )
        )
    if not (root / "boardwright_resources/kibot/yaml" / "kibot_main.yaml").exists():
        issues.append(
            ValidationIssue("error", "Missing KiBot config: boardwright_resources/kibot/yaml/kibot_main.yaml")
        )


def _has_edge_cuts_geometry(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    for marker in ('(layer "Edge.Cuts")', "(layer Edge.Cuts)"):
        if marker in text:
            return True
    return False


def _validate_assets(config: BoardwrightConfig, issues: list[ValidationIssue]) -> None:
    assets = config.assets
    logo = str(assets.get("logo", "")).strip()
    product_image = str(assets.get("product_image", "")).strip()

    if not logo:
        issues.append(ValidationIssue("warning", "Missing assets.logo in project.yaml"))
    elif not (config.root / logo).exists():
        issues.append(ValidationIssue("warning", f"Logo path does not exist: {logo}"))

    if product_image and not (config.root / product_image).exists():
        issues.append(
            ValidationIssue("warning", f"Product image path does not exist: {product_image}")
        )


def _validate_repository_metadata(config: BoardwrightConfig, issues: list[ValidationIssue]) -> None:
    if config.github_repo or str(config.project.get("project", {}).get("git_url", "")).strip():
        return
    if remote_url(config.root):
        return
    issues.append(
        ValidationIssue(
            "warning",
            "Repository URL metadata is blank; generated README release links will be limited until git_url is configured.",
        )
    )


def _validate_known_local_branding(root: Path, issues: list[ValidationIssue]) -> None:
    hits = _find_known_local_branding(root)
    for path, marker in hits:
        issues.append(
            ValidationIssue(
                "warning",
                f"Known local branding marker '{marker}' found in {path}",
            )
        )


def _validate_deprecated_schematic_variables(
    root: Path,
    issues: list[ValidationIssue],
    project: dict | None = None,
) -> None:
    """Report deprecated text-variable references with location and replacement.

    Schematics and active worksheets: error (Boardwright's sync removes these
    variables, so they render as literal ``${NAME}``; in schematics ERC fails
    with ``unresolved_variable``). PCB files: warning, because a board may still
    define the name through a board-level ``(property ...)``.
    """
    from .deprecated_variables import find_deprecated_references

    for reference in find_deprecated_references(root):
        level = "warning" if reference.kind == "pcb" else "error"
        issues.append(ValidationIssue(level, f"Deprecated text variable {reference}."))
    legacy = (project or {}).get("project", {}) if isinstance(project, dict) else {}
    if isinstance(legacy, dict) and legacy.get("board_name") not in (None, ""):
        issues.append(
            ValidationIssue(
                "warning",
                f"project.board_name ({legacy['board_name']!r}) is deprecated and ignored. Set "
                "project.pcba_name (assembled board identifier) and/or project.pcb_name (bare PCB "
                "identifier) explicitly; until then PCBA_NAME falls back to project.name.",
            )
        )


def _find_known_local_branding(root: Path) -> list[tuple[str, str]]:
    markers = (
        "Ryan Dynamics",
        "assets/logos/rd-logo.png",
        "KDT_Hierarchical_KiBot",
    )
    patterns = (
        ".boardwright/*.yaml",
        "Templates/*.kicad_wks",
        "*.kicad_pro",
        "*.kicad_sch",
        "*.kicad_pcb",
        "*.kicad_dru",
        "meta/*",
        "boardwright_resources/kibot/yaml/*.yaml",
    )
    hits: list[tuple[str, str]] = []
    for pattern in patterns:
        for path in root.glob(pattern):
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for marker in markers:
                if marker in text:
                    try:
                        label = str(path.relative_to(root))
                    except ValueError:
                        label = str(path)
                    hits.append((label, marker))
    return hits


def _validate_workflow_exists(
    config: BoardwrightConfig,
    workflow: str,
    label: str,
    issues: list[ValidationIssue],
) -> None:
    if not (config.root / ".github" / "workflows" / workflow).exists():
        issues.append(
            ValidationIssue(
                "warning",
                f"{label} workflow does not exist yet: .github/workflows/{workflow}",
            )
        )
