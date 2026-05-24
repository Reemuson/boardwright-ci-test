from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import BoardwrightError
from .workflows import install_workflows


CONFIG_DIR = ".boardwright"
DEFAULT_CONFIG_FILES = {
    "project.yaml": """project:
  id: BOARDWRIGHT-TEMPLATE
  name: Boardwright KiCad/KiBot Template
  company: RYAN DYNAMICS
  designer: R. HICKS
  git_url: ""
  github_repo: ""
  product_family: BOARDWRIGHT
  product_generation: TEMPLATE

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

assets:
  logo: assets/logos/rd-logo.png
  product_image: ""
""",
    "branches.yaml": """branches:
  development: dev
  preview: preview
  release: main
""",
    "legal.yaml": """legal:
  hardware_license: CERN-OHL-W-2.0
  branding_reserved: true
  compatibility:
    enabled: false
    wording: ""
    trademark_owner: ""
  safety_notice: >
    This is a hardware/electronics project. Anyone building, modifying,
    testing, selling, or using the design is responsible for verifying
    isolation, creepage and clearance, electrical safety, regulatory
    compliance, and fitness for purpose.
""",
    "revision_history.yaml": """revision_history:
  slots: 4
  preflight_slots: 12
  source: CHANGELOG.md
  blank_unused_slots: true
  include_unreleased_in_preview: true
""",
}


@dataclass(frozen=True)
class BoardwrightConfig:
    root: Path
    project: dict[str, Any]
    branches: dict[str, Any]
    legal: dict[str, Any]
    revision_history: dict[str, Any]

    @property
    def project_id(self) -> str:
        return str(self.project.get("project", {}).get("id", "unknown"))

    @property
    def project_name(self) -> str:
        return str(self.project.get("project", {}).get("name", "unknown"))

    @property
    def github_repo(self) -> str:
        return str(self.project.get("project", {}).get("github_repo", "")).strip()

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
    def assets(self) -> dict[str, Any]:
        assets = self.project.get("assets", {})
        return assets if isinstance(assets, dict) else {}


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
    )


def init_config(
    root: Path | None = None,
    force: bool = False,
    workflows: bool = True,
) -> list[Path]:
    project_root = find_project_root(root)
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

    if workflows:
        written.extend(install_workflows(project_root, force=force))

    return written


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
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        return value[1:-1]
    try:
        return int(value)
    except ValueError:
        return value
