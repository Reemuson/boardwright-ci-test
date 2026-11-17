# SPDX-License-Identifier: LicenseRef-Boardwright-Source-Available
# Copyright (c) Ryan Hicks

"""Detect and migrate deprecated KiCad text-variable references.

Boardwright's project sync removes these variables from ``*.kicad_pro`` (see
``kicad_variables.DEPRECATED_KICAD_TEXT_VARIABLES``). Any schematic, PCB or
active worksheet that still references one renders the literal ``${NAME}``.
In a schematic this is an ERC ``unresolved_variable`` error; worksheet text is
not checked by ERC at all.

Mappings are explicit. Only replacements that are unambiguous in the file
context are rewritten automatically; everything else is reported for manual
review, with the recommended choices.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .kicad_variables import DEPRECATED_KICAD_TEXT_VARIABLES

# kind -> {variable: replacement}. ``None`` means "ambiguous: manual review".
# Kinds: "schematic" (*.kicad_sch), "pcb" (*.kicad_pcb), "worksheet" (*.kicad_wks).
_COMMON = {
    "DOCUMENT_REVISION": "DRAWING_REVISION",
    "PCB_NUMBER": "PCB_NAME",
    "PCBA_NUMBER": "PCBA_NAME",
    "BOARD_NAME": None,
    "PROJECT_NAME": None,
    "PROJECT_ID": None,
    "DOCUMENT_REVISION_SCHEME": None,
}
REPLACEMENTS: dict[str, dict[str, str | None]] = {
    "schematic": {**_COMMON, "REVISION": "DRAWING_REVISION"},
    "pcb": {**_COMMON, "REVISION": "PCB_REVISION"},
    # In a worksheet ${REVISION} is KiCad's built-in title-block revision field,
    # which Boardwright title blocks set explicitly; it is not deprecated there.
    "worksheet": dict(_COMMON),
}
MANUAL_GUIDANCE = {
    "BOARD_NAME": "use ${PCBA_NAME} (assembled board identifier) or ${PCB_NAME} (bare PCB identifier) "
    "according to what the text describes",
    "PROJECT_NAME": "use ${PROJECT_NUMBER} (project.name) or KiCad's built-in ${PROJECTNAME}",
    "PROJECT_ID": "no Boardwright equivalent; replace with explicit text or a project field",
    "DOCUMENT_REVISION_SCHEME": "no Boardwright equivalent; remove",
}
assert set(REPLACEMENTS["schematic"]) == set(DEPRECATED_KICAD_TEXT_VARIABLES)

_REFERENCE = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")


@dataclass(frozen=True)
class DeprecatedReference:
    path: str
    line: int
    variable: str
    kind: str
    replacement: str | None

    @property
    def recommendation(self) -> str:
        if self.replacement:
            return f"replace with ${{{self.replacement}}}"
        return MANUAL_GUIDANCE.get(self.variable, "manual review")

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: ${{{self.variable}}} ({self.kind}) - {self.recommendation}"


def _kind(path: Path) -> str | None:
    return {".kicad_sch": "schematic", ".kicad_pcb": "pcb", ".kicad_wks": "worksheet"}.get(path.suffix)


def active_worksheets(root: Path) -> list[Path]:
    """Worksheets referenced by the project's ``*.kicad_pro`` files."""
    found: set[Path] = set()
    for project in root.glob("*.kicad_pro"):
        try:
            data = json.loads(project.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for section in ("schematic", "pcbnew"):
            settings = data.get(section) if isinstance(data, dict) else None
            value = str((settings or {}).get("page_layout_descr_file") or "").strip()
            if not value:
                continue
            value = value.replace("${KIPRJMOD}/", "").replace("\\", "/")
            if value.startswith("kicad-embed://"):
                continue
            candidate = (root / value) if not Path(value).is_absolute() else Path(value)
            if candidate.is_file():
                found.add(candidate.resolve())
    return sorted(found)


def project_files(root: Path) -> list[Path]:
    files = sorted(root.glob("*.kicad_sch")) + sorted(root.glob("*.kicad_pcb"))
    return files + active_worksheets(root)


def find_deprecated_references(root: Path, files: list[Path] | None = None) -> list[DeprecatedReference]:
    references: list[DeprecatedReference] = []
    for path in files if files is not None else project_files(root):
        kind = _kind(path)
        if kind is None:
            continue
        table = REPLACEMENTS[kind]
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        try:
            label = path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            label = path.as_posix()
        for number, line in enumerate(text.splitlines(), start=1):
            for match in _REFERENCE.finditer(line):
                name = match.group(1)
                if name in table:
                    references.append(DeprecatedReference(label, number, name, kind, table[name]))
    return references


def rewrite_unambiguous(root: Path, files: list[Path] | None = None) -> tuple[list[Path], list[DeprecatedReference]]:
    """Rewrite unambiguous references in place, byte-for-byte otherwise.

    Returns (written files, remaining references needing manual review).
    """
    written: list[Path] = []
    for path in files if files is not None else project_files(root):
        kind = _kind(path)
        if kind is None:
            continue
        data = path.read_bytes()
        updated = data
        for name, replacement in REPLACEMENTS[kind].items():
            if replacement:
                updated = updated.replace(f"${{{name}}}".encode(), f"${{{replacement}}}".encode())
        if updated != data:
            path.write_bytes(updated)
            written.append(path)
    remaining = [ref for ref in find_deprecated_references(root, files) if ref.replacement is None]
    return written, remaining


__all__ = [
    "DeprecatedReference",
    "MANUAL_GUIDANCE",
    "REPLACEMENTS",
    "active_worksheets",
    "find_deprecated_references",
    "project_files",
    "rewrite_unambiguous",
]
