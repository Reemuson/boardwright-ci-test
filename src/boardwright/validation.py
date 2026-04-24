from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .changelog import parse_releases, read_changelog
from .config import BoardwrightConfig, CONFIG_DIR
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
    _validate_readme_template(root, issues)

    return issues


def _require_file(path: Path, issues: list[ValidationIssue]) -> None:
    if not path.exists():
        issues.append(ValidationIssue("error", f"Missing required file: {path.name}"))


def _validate_project_config(
    config: BoardwrightConfig, issues: list[ValidationIssue]
) -> None:
    project = config.project.get("project", {})
    for key in ("id", "name", "company", "designer"):
        if not str(project.get(key, "")).strip():
            issues.append(ValidationIssue("error", f"Missing project.{key} in project.yaml"))

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
                "KiBot may not define every REVHIST_* variable.",
            )
        )


def _validate_readme_template(root: Path, issues: list[ValidationIssue]) -> None:
    template = root / "kibot_resources" / "templates" / "readme.txt"
    if not template.exists():
        issues.append(
            ValidationIssue(
                "warning",
                "Missing README template: kibot_resources/templates/readme.txt",
            )
        )
        return

    text = template.read_text(encoding="utf-8", errors="replace")
    if "LICENSE" not in text or "NOTICE" not in text:
        issues.append(
            ValidationIssue(
                "warning",
                "README template should mention LICENSE and NOTICE legal files",
            )
        )


def _validate_kicad_and_kibot(root: Path, issues: list[ValidationIssue]) -> None:
    if not list(root.glob("*.kicad_pro")):
        issues.append(ValidationIssue("error", "No KiCad project (*.kicad_pro) found"))
    if not list(root.glob("*.kicad_sch")):
        issues.append(ValidationIssue("error", "No KiCad schematic (*.kicad_sch) found"))
    if not list(root.glob("*.kicad_pcb")):
        issues.append(ValidationIssue("error", "No KiCad PCB (*.kicad_pcb) found"))
    if not (root / "kibot_yaml" / "kibot_main.yaml").exists():
        issues.append(
            ValidationIssue("error", "Missing KiBot config: kibot_yaml/kibot_main.yaml")
        )


def _validate_assets(config: BoardwrightConfig, issues: list[ValidationIssue]) -> None:
    assets = config.assets
    logo = str(assets.get("logo", "")).strip()
    product_image = str(assets.get("product_image", "")).strip()

    if not logo:
        issues.append(ValidationIssue("warning", "Missing assets.logo in project.yaml"))
    elif not (config.root / logo).exists():
        issues.append(ValidationIssue("warning", f"Logo path does not exist: {logo}"))

    if not product_image:
        issues.append(
            ValidationIssue("warning", "Missing assets.product_image in project.yaml")
        )
    elif not (config.root / product_image).exists():
        issues.append(
            ValidationIssue("warning", f"Product image path does not exist: {product_image}")
        )


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
