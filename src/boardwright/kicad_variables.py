# SPDX-License-Identifier: LicenseRef-Boardwright-Source-Available
# Copyright (c) Ryan Hicks

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import BoardwrightConfig
from .git_ops import remote_url


DEPRECATED_KICAD_TEXT_VARIABLES = {
    "BOARD_NAME",
    "DOCUMENT_REVISION",
    "DOCUMENT_REVISION_SCHEME",
    "PCB_NUMBER",
    "PCBA_NUMBER",
    "PROJECT_ID",
    "PROJECT_NAME",
    "REVISION",
}


def sync_kicad_project_text_variables(
    config: BoardwrightConfig,
    document_key: str = "schematic",
    extra_variables: dict[str, str] | None = None,
) -> list[Path]:
    """Write Boardwright text variables into KiCad project files."""

    variables = build_kicad_text_variables(config, document_key)
    if extra_variables:
        variables.update({key: str(value) for key, value in extra_variables.items()})

    written: list[Path] = []
    for path in sorted(config.root.glob("*.kicad_pro")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        text_variables = data.setdefault("text_variables", {})
        if not isinstance(text_variables, dict):
            text_variables = {}
            data["text_variables"] = text_variables
        changed = False
        for key in DEPRECATED_KICAD_TEXT_VARIABLES:
            if key in text_variables:
                del text_variables[key]
                changed = True
        for key, value in variables.items():
            if text_variables.get(key) != value:
                text_variables[key] = value
                changed = True
        if changed:
            path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
            written.append(path)
    return written


def build_kicad_text_variables(
    config: BoardwrightConfig,
    document_key: str = "schematic",
) -> dict[str, str]:
    project = config.project.get("project", {})
    variables = dict(config.drawing_variables(document_key))
    variables.update(_read_revision_variables(config.root / ".boardwright" / "revision_history_variables.env"))
    variables.update(
        {
            "LOGO": str(config.assets.get("logo") or ""),
            "TEMPLATE_LOGO": config.template_logo,
            "SHEET_LEGAL_NOTICE": _render_project_text(config.sheet_legal_notice, project),
            "GIT_URL": _git_url(config),
        }
    )
    return {key: str(value) for key, value in variables.items()}


def _read_revision_variables(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = _unquote(value.strip())
    return values


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] == '"':
        value = value[1:-1]
    return value.replace("\\n", "\n").replace('\\"', '"').replace("\\\\", "\\")


def _git_url(config: BoardwrightConfig) -> str:
    project = config.project.get("project", {})
    configured = str(project.get("git_url") or "").strip()
    if configured:
        return configured
    if config.github_repo:
        return f"https://github.com/{config.github_repo}"
    return remote_url(config.root)


def _render_project_text(value: str, project: dict[str, Any]) -> str:
    rendered = value
    replacements = {
        "COMPANY": str(project.get("company") or ""),
        "DESIGNER": str(project.get("designer") or ""),
        "PROJECT_NUMBER": str(project.get("name") or ""),
        "PCBA_NAME": str(project.get("pcba_name") or project.get("name") or ""),
        "PCB_NAME": str(project.get("pcb_name") or project.get("pcba_name") or project.get("name") or ""),
        "BOARD_REVISION": str(project.get("board_revision") or ""),
    }
    for key, replacement in replacements.items():
        rendered = rendered.replace("${" + key + "}", replacement)
        rendered = rendered.replace("{" + key + "}", replacement)
    return rendered
