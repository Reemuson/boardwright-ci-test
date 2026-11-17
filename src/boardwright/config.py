# SPDX-License-Identifier: LicenseRef-Boardwright-Source-Available
# Copyright (c) Ryan Hicks

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from .errors import BoardwrightError
from .variants import normalize_variant


CONFIG_DIR = ".boardwright"
DOCUMENT_DEFAULTS: dict[str, dict[str, Any]] = {
    "schematic": {
        "title": "Schematic",
        "type": "SCHEMATIC",
        "number_source": "pcba_name",
        "revision": "A",
        "revision_scheme": "lettered",
        "drawn_by": "",
        "drawn_date": "",
        "include_full_revision_history": True,
    },
    "assembly": {
        "title": "Assembly Drawing",
        "type": "ASSEMBLY",
        "number_source": "pcba_name",
        "revision": "A",
        "revision_scheme": "lettered",
    },
    "fabrication": {
        "title": "PCB Fabrication Drawing",
        "type": "FABRICATION",
        "number_source": "pcb_name",
        "revision": "A",
        "revision_scheme": "lettered",
    },
    "package": {
        "title": "Package",
        "type": "PACKAGE",
        "number_source": "pcba_name",
        "revision": "A",
        "revision_scheme": "lettered",
    },
    "review": {
        "title": "Review Package",
        "type": "REVIEW",
        "number_source": "pcba_name",
        "revision": "A",
        "revision_scheme": "lettered",
    },
}

DEFAULT_CONFIG_FILES = {
    "project.yaml": """project:
  name: Boardwright KiCad/KiBot Template
  pcba_name: Boardwright KiCad/KiBot Template
  pcb_name: Boardwright KiCad/KiBot Template PCB
  board_revision: A
  company: RYAN HICKS
  designer: R. HICKS
  git_url: ""
  product_family: BOARDWRIGHT
  product_generation: TEMPLATE

release:
  version: ""
  date: ""

documents:
  schematic:
    title: Schematic
    type: SCHEMATIC
    number_source: pcba_name
    revision: A
    revision_scheme: lettered
    drawn_by: ""
    drawn_date: ""
    include_full_revision_history: true
  assembly:
    title: Assembly Drawing
    type: ASSEMBLY
    number_source: pcba_name
    revision: A
    revision_scheme: lettered
  fabrication:
    title: PCB Fabrication Drawing
    type: FABRICATION
    number_source: pcb_name
    revision: A
    revision_scheme: lettered

variants:
  dev_default: DRAFT
  preview_default: PRELIMINARY
  main_default: CHECKED
  release_default: RELEASED

outputs:
  commit_generated_outputs_to_main: false
  use_preview_branch: true
  preview_engine: github-actions
  preview_workflow: dev-preview.yaml
  main_workflow: main-outputs.yaml
  prepare_release_workflow: prepare-release.yaml
  release_workflow: release.yaml
  release_include_source_archive: false
  combined_review_pdf: false

assets:
  logo: assets/logos/logo-wordmark-colour.png
  product_image: ""

template:
  sheet_image: assets/logos/logo-block-black.png

sheet:
  legal_notice: "THIS DESIGN AND/OR DRAWING IS THE PROPERTY OF ${COMPANY}\\nAND SHALL NOT BE REPRODUCED WITHOUT AUTHORISATION."

manufacturing:
  rohs_pb_free: true
  conformal_coating: false
  silkscreen_enabled: true
  tented_vias: true
  impedance_enabled: false
  manufacturing_standard: IPC-6012 Class 2
  core_material: FR-4
  flammability_rating: UL94V-0
  tg_rating: 170 C
  halogen_free: true
  fabrication_notes: ""
  assembly_notes: ""
  testpoint_policy: ""
  impedance_notes: ""
  impedance_table: []
""",
    "branches.yaml": """branches:
  development: dev
  preview: preview
  release: main
""",
    "legal.yaml": """boardwright:
  tooling_license: LicenseRef-Boardwright-Source-Available
  inherited_notice_file: LICENSES/Nguyen-MIT.txt
  third_party_notice_file: THIRD_PARTY_NOTICES.md
  default_profile: public-hardware

legal_profiles:
  public-hardware:
    hardware_design_license: CERN-OHL-W-2.0
    copyright_holder: ""
    notice_file: NOTICE.md
    third_party_notice_file: THIRD_PARTY_NOTICES.md
    allow_project_owner_override: true
    branding_reserved: true
    compatibility:
      enabled: false
      wording: ""
      trademark_owner: ""
    safety_notice: >
      This is a hardware/electronics project. Anyone building, modifying,
      testing, selling, or using the design is responsible for verifying
      isolation, creepage and clearance, electrical safety, regulatory
      compliance, manufacturability, and fitness for purpose.

  compatibility-friendly:
    hardware_design_license: CERN-OHL-S-2.0
    copyright_holder: ""
    notice_file: NOTICE.md
    third_party_notice_file: THIRD_PARTY_NOTICES.md
    allow_project_owner_override: true
    branding_reserved: true
    compatibility:
      enabled: true
      wording: compatible with selected instruments
      trademark_owner: the original manufacturer
    safety_notice: >
      This is a hardware/electronics project. Anyone building, modifying,
      testing, selling, or using the design is responsible for verifying
      isolation, creepage and clearance, electrical safety, regulatory
      compliance, manufacturability, and fitness for purpose.

  internal-prototype:
    hardware_design_license: "See LICENSE"
    copyright_holder: ""
    notice_file: NOTICE.md
    third_party_notice_file: THIRD_PARTY_NOTICES.md
    allow_project_owner_override: false
    branding_reserved: false
    compatibility:
      enabled: false
      wording: ""
      trademark_owner: ""
    safety_notice: >
      This is a hardware/electronics project. Anyone building, modifying,
      testing, selling, or using the design is responsible for verifying
      isolation, creepage and clearance, electrical safety, regulatory
      compliance, manufacturability, and fitness for purpose.

downstream_project_defaults:
  profile: public-hardware
  hardware_design_license: CERN-OHL-W-2.0
  copyright_holder: ""
  notice_file: NOTICE.md
  third_party_notice_file: THIRD_PARTY_NOTICES.md
  allow_project_owner_override: true
  branding_reserved: true
  compatibility:
    enabled: false
    wording: ""
    trademark_owner: ""
  safety_notice: >
    This is a hardware/electronics project. Anyone building, modifying,
    testing, selling, or using the design is responsible for verifying
    isolation, creepage and clearance, electrical safety, regulatory
    compliance, manufacturability, and fitness for purpose.

generated_outputs:
  output_exception_enabled: true
  copied_template_material_notice_required: true
""",
    "revision_history.yaml": """revision_history:
  slots: 4
  preflight_slots: 12
  source: CHANGELOG.md
  blank_unused_slots: true
  include_unreleased_in_preview: true
""",
    "document_revisions.yaml": """document_revisions:
  schematic:
    rows: []
  assembly:
    rows: []
  fabrication:
    rows: []
""",
}


@dataclass(frozen=True)
class BoardwrightConfig:
    root: Path
    project: dict[str, Any]
    branches: dict[str, Any]
    legal: dict[str, Any]
    revision_history: dict[str, Any]
    document_revisions: dict[str, Any] | None = None

    @property
    def project_id(self) -> str:
        return self.project_name

    @property
    def project_name(self) -> str:
        project = self.project.get("project", {})
        return str(project.get("name") or "unknown")

    @property
    def pcba_name(self) -> str:
        project = self.project.get("project", {})
        return str(project.get("pcba_name") or self.project_name)

    @property
    def pcb_name(self) -> str:
        project = self.project.get("project", {})
        return str(project.get("pcb_name") or project.get("pcba_name") or self.project_name)

    @property
    def board_revision(self) -> str:
        return str(self.project.get("project", {}).get("board_revision", "A"))

    @property
    def release_settings(self) -> dict[str, Any]:
        settings = self.project.get("release", {})
        return settings if isinstance(settings, dict) else {}

    @property
    def release_version(self) -> str:
        return str(self.release_settings.get("version") or "")

    @property
    def release_date(self) -> str:
        return str(self.release_settings.get("date") or "")

    @property
    def documents(self) -> dict[str, Any]:
        documents = self.project.get("documents", {})
        return documents if isinstance(documents, dict) else {}

    def document_settings(self, document_key: str = "schematic") -> dict[str, Any]:
        key = str(document_key or "schematic").strip().lower()
        defaults = dict(DOCUMENT_DEFAULTS.get(key, DOCUMENT_DEFAULTS["schematic"]))
        configured = self.documents.get(key, {})
        if isinstance(configured, dict):
            defaults.update(configured)
        return defaults

    def drawing_variables(self, document_key: str = "schematic") -> dict[str, str]:
        project = self.project.get("project", {})
        document = self.document_settings(document_key)
        schematic = self.document_settings("schematic")
        number_source = str(document.get("number_source") or "pcba_name").strip()
        number = str(project.get(number_source) or "")
        if not number:
            number = self.pcb_name if document_key == "fabrication" else self.pcba_name
        return {
            "COMPANY": str(project.get("company") or ""),
            "DESIGNER": str(project.get("designer") or ""),
            "PROJECT_NUMBER": self.project_name,
            "PCBA_NAME": self.pcba_name,
            "PCB_NAME": self.pcb_name,
            "BOARD_REVISION": self.board_revision,
            "PCB_REVISION": self.board_revision,
            "SCHEMATIC_REVISION": str(schematic.get("revision") or "").strip(),
            "DOCUMENT_TYPE": str(document.get("type") or "").strip(),
            "DRAWING_TITLE": str(document.get("title") or "").strip().upper(),
            "DRAWING_NUMBER": number,
            "DRAWING_REVISION": str(document.get("revision") or "").strip(),
            "RELEASE_VERSION": self.release_version,
            "RELEASE_DATE": self.release_date,
            "DRAWN_BY": str(document.get("drawn_by") or schematic.get("drawn_by") or "").strip(),
            "DRAWN_DATE": str(document.get("drawn_date") or schematic.get("drawn_date") or "").strip(),
        }



    @property
    def github_repo(self) -> str:
        project = self.project.get("project", {})
        git_url = str(project.get("git_url", "") or "").strip()
        if git_url:
            return _normalize_github_repo_slug(git_url)
        return _normalize_github_repo_slug(str(project.get("github_repo", "") or ""))

    @property
    def dev_branch(self) -> str:
        return str(self.branches.get("branches", {}).get("development", "dev"))

    @property
    def preview_branch(self) -> str:
        return str(self.branches.get("branches", {}).get("preview", "preview"))

    @property
    def release_branch(self) -> str:
        return str(self.branches.get("branches", {}).get("release", "main"))

    @property
    def branch_settings(self) -> dict[str, Any]:
        branches = self.branches.get("branches", {})
        return branches if isinstance(branches, dict) else {}

    @property
    def default_variant(self) -> str:
        variants = self.project.get("variants", {})
        return str(variants.get("dev_default", "CHECKED"))

    @property
    def preview_variant(self) -> str:
        variants = self.project.get("variants", {})
        return str(variants.get("preview_default", self.default_variant))

    @property
    def main_variant(self) -> str:
        variants = self.project.get("variants", {})
        return str(variants.get("main_default", "CHECKED"))

    @property
    def release_variant(self) -> str:
        variants = self.project.get("variants", {})
        return str(variants.get("release_default", "RELEASED"))

    @property
    def preview_workflow(self) -> str:
        outputs = self.project.get("outputs", {})
        return str(outputs.get("preview_workflow", "dev-preview.yaml"))

    @property
    def preview_engine(self) -> str:
        outputs = self.project.get("outputs", {})
        return str(outputs.get("preview_engine", "github-actions"))

    @property
    def main_workflow(self) -> str:
        outputs = self.project.get("outputs", {})
        return str(outputs.get("main_workflow", "main-outputs.yaml"))

    @property
    def release_workflow(self) -> str:
        outputs = self.project.get("outputs", {})
        return str(outputs.get("release_workflow", "release.yaml"))

    @property
    def prepare_release_workflow(self) -> str:
        outputs = self.project.get("outputs", {})
        return str(outputs.get("prepare_release_workflow", "prepare-release.yaml"))

    @property
    def commit_generated_outputs_to_main(self) -> bool:
        outputs = self.project.get("outputs", {})
        return bool(outputs.get("commit_generated_outputs_to_main", False))

    @property
    def use_preview_branch(self) -> bool:
        outputs = self.project.get("outputs", {})
        return bool(outputs.get("use_preview_branch", True))

    @property
    def release_include_source_archive(self) -> bool:
        outputs = self.project.get("outputs", {})
        return bool(outputs.get("release_include_source_archive", False))

    @property
    def output_settings(self) -> dict[str, Any]:
        outputs = self.project.get("outputs", {})
        return outputs if isinstance(outputs, dict) else {}

    @property
    def assets(self) -> dict[str, Any]:
        assets = self.project.get("assets", {})
        return assets if isinstance(assets, dict) else {}

    @property
    def template(self) -> dict[str, Any]:
        template = self.project.get("template", {})
        return template if isinstance(template, dict) else {}

    @property
    def sheet(self) -> dict[str, Any]:
        sheet = self.project.get("sheet", {})
        return sheet if isinstance(sheet, dict) else {}

    @property
    def manufacturing(self) -> dict[str, Any]:
        manufacturing = self.project.get("manufacturing", {})
        return manufacturing if isinstance(manufacturing, dict) else {}

    @property
    def impedance_entries(self) -> list[dict[str, Any]]:
        entries = self.manufacturing.get("impedance_table", [])
        if not isinstance(entries, list):
            return []
        return [entry for entry in entries if isinstance(entry, dict)]

    @property
    def worksheet(self) -> str:
        return (
            str(self.template.get("worksheet") or "").strip()
            or _worksheet_from_kicad_project(self.root, preferred_sections=("schematic", "pcbnew"))
            or "Templates/Boardwright_Template_PCB_GIT_A4.kicad_wks"
        )

    @property
    def template_logo(self) -> str:
        return self.template_sheet_image

    @property
    def template_sheet_image(self) -> str:
        return str(
            self.template.get("sheet_image")
            or self.template.get("logo")
            or self.assets.get("logo")
            or ""
        )

    @property
    def sheet_legal_notice(self) -> str:
        notice = str(
            self.sheet.get("legal_notice")
            or "THIS DESIGN AND/OR DRAWING IS THE PROPERTY OF ${COMPANY}\n"
            "AND SHALL NOT BE REPRODUCED WITHOUT AUTHORISATION."
        )
        return notice.replace("\\n", "\n")

    @property
    def legal_boardwright(self) -> dict[str, Any]:
        boardwright = self.legal.get("boardwright", {})
        return boardwright if isinstance(boardwright, dict) else {}

    @property
    def legal_project_defaults(self) -> dict[str, Any]:
        defaults = self.legal.get("downstream_project_defaults", {})
        return defaults if isinstance(defaults, dict) else {}

    @property
    def legal_profiles(self) -> dict[str, Any]:
        profiles = self.legal.get("legal_profiles", {})
        return profiles if isinstance(profiles, dict) else {}

    @property
    def legal_profile(self) -> str:
        default_profile = self.legal.get("boardwright", {}).get("default_profile", "public-hardware")
        return str(self.legal_project_defaults.get("profile", default_profile))

    @property
    def hardware_design_license(self) -> str:
        return str(self.legal_project_defaults.get("hardware_design_license", ""))

    @property
    def copyright_holder(self) -> str:
        return str(self.legal_project_defaults.get("copyright_holder", ""))

    @property
    def notice_file(self) -> str:
        return str(self.legal_project_defaults.get("notice_file", "NOTICE.md"))

    @property
    def third_party_notice_file(self) -> str:
        return str(self.legal_project_defaults.get("third_party_notice_file", "THIRD_PARTY_NOTICES.md"))

    @property
    def safety_notice(self) -> str:
        return str(self.legal_project_defaults.get("safety_notice", ""))


def find_project_root(start: Path | None = None) -> Path:
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / CONFIG_DIR).is_dir() or (candidate / ".git").exists():
            return candidate
    raise BoardwrightError("Could not find a Boardwright project root.")


def load_config(root: Path | None = None) -> BoardwrightConfig:
    project_root = find_project_root(root)
    config_root = project_root / CONFIG_DIR
    if not config_root.is_dir():
        raise BoardwrightError(f"Missing config directory: {config_root}")

    return BoardwrightConfig(
        root=project_root,
        project=_read_yaml(config_root / "project.yaml"),
        branches=_read_yaml(config_root / "branches.yaml"),
        legal=_read_yaml(config_root / "legal.yaml"),
        revision_history=_read_yaml(config_root / "revision_history.yaml"),
        document_revisions=_read_yaml_optional(config_root / "document_revisions.yaml"),
    )

def init_config(
    root: Path | None = None,
    force: bool = False,
    workflows: bool = True,
) -> list[Path]:
    if root is not None:
        project_root = Path(root).resolve()
    else:
        try:
            project_root = find_project_root()
        except BoardwrightError:
            project_root = Path.cwd().resolve()
    config_root = project_root / CONFIG_DIR
    config_root.mkdir(exist_ok=True)

    written: list[Path] = []
    for filename, content in DEFAULT_CONFIG_FILES.items():
        path = config_root / filename
        if path.exists() and not force:
            continue
        path.write_text(content, encoding="utf-8", newline="\n")
        written.append(path)

    changelog = project_root / "CHANGELOG.md"
    if not changelog.exists() or force:
        changelog.write_text("# Changelog\n\n## [Unreleased]\n", encoding="utf-8", newline="\n")
        written.append(changelog)

    from .managed_project import install_initial_payload

    written.extend(
        install_initial_payload(
            project_root,
            workflows=workflows,
            force=force,
            origin="boardwright",
        )
    )

    return written


def update_project_config(
    config: BoardwrightConfig,
    *,
    project_fields: dict[str, Any] | None = None,
    variant_fields: dict[str, Any] | None = None,
    asset_fields: dict[str, Any] | None = None,
    template_fields: dict[str, Any] | None = None,
    sheet_fields: dict[str, Any] | None = None,
    branch_fields: dict[str, Any] | None = None,
    output_fields: dict[str, Any] | None = None,
    legal_fields: dict[str, Any] | None = None,
    manufacturing_fields: dict[str, Any] | None = None,
    release_fields: dict[str, Any] | None = None,
    document_fields: dict[str, dict[str, Any]] | None = None,
) -> Path:
    """Update editable project metadata in `.boardwright/project.yaml`."""

    path = config.root / CONFIG_DIR / "project.yaml"
    data = _read_yaml(path)

    project = data.setdefault("project", {})
    if not isinstance(project, dict):
        raise BoardwrightError("project.yaml field `project` must be a mapping.")
    for key, value in (project_fields or {}).items():
        project[key] = value

    variants = data.setdefault("variants", {})
    if not isinstance(variants, dict):
        raise BoardwrightError("project.yaml field `variants` must be a mapping.")
    for key, value in (variant_fields or {}).items():
        variants[key] = normalize_variant(value)

    assets = data.setdefault("assets", {})
    if not isinstance(assets, dict):
        raise BoardwrightError("project.yaml field `assets` must be a mapping.")
    for key, value in (asset_fields or {}).items():
        assets[key] = value

    template = data.setdefault("template", {})
    if not isinstance(template, dict):
        raise BoardwrightError("project.yaml field `template` must be a mapping.")
    for key, value in (template_fields or {}).items():
        template[key] = value

    sheet = data.setdefault("sheet", {})
    if not isinstance(sheet, dict):
        raise BoardwrightError("project.yaml field `sheet` must be a mapping.")
    for key, value in (sheet_fields or {}).items():
        sheet[key] = value

    manufacturing = data.setdefault("manufacturing", {})
    if not isinstance(manufacturing, dict):
        raise BoardwrightError("project.yaml field `manufacturing` must be a mapping.")
    for key, value in (manufacturing_fields or {}).items():
        if key == "impedance_table":
            manufacturing[key] = _normalize_impedance_table(value)
        else:
            manufacturing[key] = _coerce_project_value(value)

    branches_path = config.root / CONFIG_DIR / "branches.yaml"
    if branch_fields:
        branches_data = _read_yaml(branches_path)
        branches = branches_data.setdefault("branches", {})
        if not isinstance(branches, dict):
            raise BoardwrightError("branches.yaml field `branches` must be a mapping.")
        branch_aliases = {
            "development": "development",
            "preview": "preview",
            "release": "release",
        }
        for key, value in branch_fields.items():
            branch_key = branch_aliases.get(key, key)
            branches[branch_key] = value
        _write_yaml(branches_path, branches_data)


    if release_fields:
        release = data.setdefault("release", {})
        if not isinstance(release, dict):
            raise BoardwrightError("project.yaml field `release` must be a mapping.")
        for key, value in release_fields.items():
            release[key] = _coerce_project_value(value)

    if document_fields:
        documents = data.setdefault("documents", {})
        if not isinstance(documents, dict):
            raise BoardwrightError("project.yaml field `documents` must be a mapping.")
        for document_key, fields in document_fields.items():
            document = documents.setdefault(document_key, {})
            if not isinstance(document, dict):
                raise BoardwrightError(f"project.yaml field `documents.{document_key}` must be a mapping.")
            for key, value in fields.items():
                document[key] = _coerce_project_value(value)

    if output_fields:
        outputs = data.setdefault("outputs", {})
        if not isinstance(outputs, dict):
            raise BoardwrightError("project.yaml field `outputs` must be a mapping.")
        for key, value in output_fields.items():
            outputs[key] = _coerce_project_value(value)

    if legal_fields:
        legal_path = config.root / CONFIG_DIR / "legal.yaml"
        legal_data = _read_yaml(legal_path)
        boardwright = legal_data.setdefault("boardwright", {})
        if not isinstance(boardwright, dict):
            raise BoardwrightError("legal.yaml field `boardwright` must be a mapping.")
        profiles = legal_data.get("legal_profiles", {})
        if not isinstance(profiles, dict):
            profiles = {}
        defaults = legal_data.setdefault("downstream_project_defaults", {})
        if not isinstance(defaults, dict):
            raise BoardwrightError("legal.yaml field `downstream_project_defaults` must be a mapping.")
        profile_name = str(legal_fields.get("profile", defaults.get("profile", boardwright.get("default_profile", "public-hardware")))).strip()
        if profile_name:
            defaults["profile"] = profile_name
            profile_defaults = profiles.get(profile_name, {})
            if isinstance(profile_defaults, dict):
                for key, value in profile_defaults.items():
                    if key not in legal_fields:
                        defaults[key] = value
        boardwright_keys = {
            "tooling_license",
            "inherited_notice_file",
            "third_party_notice_file",
        }
        for key, value in legal_fields.items():
            if key in boardwright_keys:
                boardwright[key] = value
            else:
                defaults[key] = _coerce_project_value(value)
        _write_yaml(legal_path, legal_data)

    _write_yaml(path, data)
    return path


def _normalize_github_repo_slug(value: str) -> str:
    text = value.strip()
    if text.startswith("https://github.com/"):
        text = text.removeprefix("https://github.com/")
    elif text.startswith("http://github.com/"):
        text = text.removeprefix("http://github.com/")
    elif text.startswith("git@github.com:"):
        text = text.removeprefix("git@github.com:")
    elif text.startswith("ssh://git@github.com/"):
        text = text.removeprefix("ssh://git@github.com/")
    text = text.strip().strip("/")
    if text.endswith(".git"):
        text = text[:-4]
    return text

def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise BoardwrightError(f"Missing config file: {path}")

    text = path.read_text(encoding="utf-8")
    try:
        import yaml
    except ImportError:
        return _read_simple_yaml(text)

    loaded = yaml.safe_load(text)
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise BoardwrightError(f"Expected a mapping in {path}")
    return loaded


def _read_yaml_optional(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return _read_yaml(path)


def _write_yaml(path: Path, data: dict[str, Any]) -> None:
    try:
        import yaml
    except ImportError:
        path.write_text(_dump_simple_project_yaml(data), encoding="utf-8", newline="\n")
        return

    path.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
        newline="\n",
    )


def _dump_simple_project_yaml(data: dict[str, Any]) -> str:
    lines: list[str] = []
    for section, values in data.items():
        lines.append(f"{section}:")
        if isinstance(values, dict):
            for key, value in values.items():
                lines.append(f"  {key}: {_quote_yaml_scalar(value)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _quote_yaml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value)
    if text == "":
        return '""'
    if "\n" in text:
        return '"' + text.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"') + '"'
    if any(char in text for char in ":#{}[]&,*?|-<>=!%@`\"'") or text.strip() != text:
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return text


def _worksheet_from_kicad_project(
    root: Path,
    preferred_sections: tuple[str, ...] = ("schematic", "pcbnew"),
) -> str:
    for path in sorted(root.glob("*.kicad_pro")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        for section_name in preferred_sections:
            section = data.get(section_name)
            if not isinstance(section, dict):
                continue
            value = str(section.get("page_layout_descr_file") or "").strip()
            if value:
                return _normalize_kicad_project_worksheet(root, value)
    return ""


def _normalize_kicad_project_worksheet(root: Path, value: str) -> str:
    normalized = value.replace("\\", "/").strip()
    embed_prefix = "kicad-embed://"
    if normalized.startswith(embed_prefix):
        name = normalized.removeprefix(embed_prefix).lstrip("/")
        template_path = Path("Templates") / name
        if (root / template_path).is_file():
            return template_path.as_posix()
        return name
    if normalized.startswith("${KIPRJMOD}/"):
        return normalized.removeprefix("${KIPRJMOD}/")
    return normalized


def _read_simple_yaml(text: str) -> dict[str, Any]:
    """Tiny fallback parser for Boardwright's own simple config files."""
    root: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(-1, root)]
    lines = text.splitlines()
    index = 0

    while index < len(lines):
        raw_line = lines[index]
        line = raw_line.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            index += 1
            continue

        indent = len(raw_line) - len(raw_line.lstrip(" "))
        if ":" not in line:
            index += 1
            continue
        key, value = line.strip().split(":", 1)
        value = value.strip()

        while stack and indent <= stack[-1][0]:
            stack.pop()

        parent = stack[-1][1]
        if value == "":
            child: dict[str, Any] = {}
            parent[key] = child
            stack.append((indent, child))
        elif value == ">":
            folded: list[str] = []
            index += 1
            while index < len(lines):
                folded_line = lines[index]
                folded_indent = len(folded_line) - len(folded_line.lstrip(" "))
                if folded_line.strip() and folded_indent <= indent:
                    index -= 1
                    break
                if folded_line.strip():
                    folded.append(folded_line.strip())
                index += 1
            parent[key] = " ".join(folded)
        else:
            parent[key] = _coerce_scalar(value)
        index += 1

    return root


def _coerce_scalar(value: str) -> Any:
    if value in {"true", "false"}:
        return value == "true"
    if value in {'""', "''"}:
        return ""
    if value.startswith('"') and value.endswith('"'):
        return value[1:-1].replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\")
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1]
    try:
        return int(value)
    except ValueError:
        return value


def _coerce_project_value(value: Any) -> Any:
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "false"}:
            return lowered == "true"
    return value


def _normalize_impedance_table(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    normalized: list[dict[str, Any]] = []
    for row in value:
        if isinstance(row, dict):
            normalized.append(
                {
                    "transmission_line": str(row.get("transmission_line", "")).strip(),
                    "impedance_ohms": str(row.get("impedance_ohms", "")).strip(),
                    "tolerance_ohms": str(row.get("tolerance_ohms", "")).strip(),
                    "layer": str(row.get("layer", "")).strip(),
                    "trace_width_mm": str(row.get("trace_width_mm", "")).strip(),
                    "gap_mm": str(row.get("gap_mm", "")).strip(),
                    "ref_layers": str(row.get("ref_layers", "")).strip(),
                }
            )
    return normalized
