# SPDX-License-Identifier: LicenseRef-Boardwright-Source-Available
# Copyright (c) Ryan Hicks

from __future__ import annotations

from dataclasses import dataclass
import csv
from pathlib import Path
from typing import Any

from .changelog import ReleaseSection, parse_releases
from .config import BoardwrightConfig, CONFIG_DIR, _read_yaml_optional, _write_yaml, update_project_config
from .errors import BoardwrightError
from .kicad_variables import sync_kicad_project_text_variables

TITLE_BLOCK_REVISION_ROWS = 6




def write_document_revision_rows(
    config: BoardwrightConfig,
    rows: list[dict[str, Any]],
    document_key: str = "schematic",
) -> Path:
    key = str(document_key or "schematic").strip().lower()
    normalized_rows: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        normalized = {
            "revision": str(row.get("revision") or "").strip(),
            "date": str(row.get("date") or "").strip(),
            "description": " ".join(str(row.get("description") or "").split()),
            "drawn_by": str(row.get("drawn_by") or "").strip(),
        }
        if any(normalized.values()):
            normalized_rows.append(normalized)

    path = config.root / CONFIG_DIR / "document_revisions.yaml"
    data = _read_yaml_optional(path)
    document_revisions = data.setdefault("document_revisions", {})
    if not isinstance(document_revisions, dict):
        raise BoardwrightError("document_revisions.yaml field `document_revisions` must be a mapping.")
    document = document_revisions.setdefault(key, {})
    if not isinstance(document, dict):
        raise BoardwrightError(f"document_revisions.yaml field `document_revisions.{key}` must be a mapping.")
    document["rows"] = normalized_rows
    _write_yaml(path, data)
    return path

def record_document_revision_release(
    config: BoardwrightConfig,
    *,
    revision: str,
    description: str,
    date: str,
    document_key: str = "schematic",
) -> Path:
    clean_revision = str(revision or "").strip()
    clean_description = " ".join(str(description or "").split())
    key = str(document_key or "schematic").strip().lower()
    if not clean_revision:
        raise BoardwrightError("Drawing revision is required for release preparation.")
    if not clean_description:
        raise BoardwrightError("Revision table description is required for release preparation.")
    if len(clean_description) > 60:
        raise BoardwrightError("Revision table description must be 60 characters or fewer.")

    document = config.document_settings(key)
    row = {
        "revision": clean_revision,
        "date": str(date or "").strip(),
        "description": clean_description,
    }
    drawn_by = str(document.get("drawn_by") or "").strip()
    if drawn_by:
        row["drawn_by"] = drawn_by

    path = config.root / CONFIG_DIR / "document_revisions.yaml"
    data = _read_yaml_optional(path)
    document_revisions = data.setdefault("document_revisions", {})
    if not isinstance(document_revisions, dict):
        raise BoardwrightError("document_revisions.yaml field `document_revisions` must be a mapping.")
    document_data = document_revisions.setdefault(key, {})
    if not isinstance(document_data, dict):
        raise BoardwrightError(f"document_revisions.yaml field `document_revisions.{key}` must be a mapping.")
    rows = document_data.setdefault("rows", [])
    if not isinstance(rows, list):
        raise BoardwrightError(f"document_revisions.yaml field `document_revisions.{key}.rows` must be a list.")

    remaining_rows = [
        existing
        for existing in rows
        if not (isinstance(existing, dict) and str(existing.get("revision") or "").strip() == clean_revision)
    ]
    document_data["rows"] = [row, *remaining_rows]
    _write_yaml(path, data)

    updated_document = dict(document)
    updated_document["revision"] = clean_revision
    update_project_config(config, document_fields={key: updated_document})
    return path


@dataclass(frozen=True)
class RevisionSlot:
    index: int
    version: str
    date: str
    title: str
    body: str
    revision: str = ""
    description: str = ""
    drawn_by: str = ""
    drawn_date: str = ""


def build_revision_slots(
    config: BoardwrightConfig,
    document_key: str = "schematic",
    slot_count: int = TITLE_BLOCK_REVISION_ROWS,
) -> list[RevisionSlot]:
    return _build_document_revision_slots(
        document_revision_rows(config, document_key),
        slot_count=slot_count,
    )


def document_revision_rows(
    config: BoardwrightConfig,
    document_key: str = "schematic",
) -> list[dict[str, Any]]:
    data = getattr(config, "document_revisions", None) or {}
    document_revisions = data.get("document_revisions", {})
    rows: Any = []
    key = str(document_key or "schematic").strip().lower()
    if isinstance(document_revisions, dict):
        document = document_revisions.get(key, {})
        if isinstance(document, dict):
            rows = document.get("rows", [])
        elif isinstance(document, list):
            rows = document
        elif "rows" in document_revisions:
            rows = document_revisions.get("rows", [])
    elif isinstance(document_revisions, list):
        rows = document_revisions
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict) and any(str(value).strip() for value in row.values())]


def write_revision_variables(
    config: BoardwrightConfig,
    document_key: str = "schematic",
) -> Path:
    path = config.root / ".boardwright" / "revision_history_variables.env"
    slots = build_revision_slots(config, document_key, TITLE_BLOCK_REVISION_ROWS)
    total_rows = len(document_revision_rows(config, document_key))
    values: dict[str, str] = {}
    for slot in slots:
        prefix = f"REVTABLE_{slot.index}"
        values.update(
            {
                f"{prefix}_REV": slot.revision or slot.version,
                f"{prefix}_DATE": slot.date,
                f"{prefix}_DESC": slot.description or slot.body,
                f"{prefix}_DRAWN": slot.drawn_by,
            }
        )
    values["REVTABLE_NOTE"] = (
        "Latest 6 revisions shown. See full revision history."
        if total_rows > TITLE_BLOCK_REVISION_ROWS
        else ""
    )
    lines = [f"{key}={_quote(value)}" for key, value in values.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    write_full_revision_history(config, document_key)
    sync_kicad_project_text_variables(config, document_key, values)
    return path


def write_full_revision_history(
    config: BoardwrightConfig,
    document_key: str = "schematic",
) -> Path:
    key = str(document_key or "schematic").strip().lower()
    path = config.root / ".boardwright" / f"revision_history_{key}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(("REV", "DATE", "DESCRIPTION", "DRAWN"))
        for row in document_revision_rows(config, key):
            writer.writerow(
                (
                    _row_value(row, "revision"),
                    _row_value(row, "date"),
                    _row_value(row, "description"),
                    _row_value(row, "drawn_by"),
                )
            )
    return path


def build_revision_slots_from_text(
    changelog_text: str,
    slot_count: int = 4,
    include_unreleased: bool = True,
) -> list[RevisionSlot]:
    releases = parse_releases(changelog_text)

    selected: list[ReleaseSection] = []
    for release in releases:
        if release.name == "Unreleased" and not include_unreleased:
            continue
        if not release.body.strip():
            continue
        selected.append(release)
        if len(selected) == slot_count:
            break

    slots: list[RevisionSlot] = []
    for index in range(1, slot_count + 1):
        release = selected[index - 1] if index <= len(selected) else None
        if release is None:
            slots.append(_blank_slot(index))
            continue
        body = _release_body(release)
        slots.append(
            RevisionSlot(
                index=index,
                version=release.name,
                date=release.date or "",
                title=_release_title(release),
                body=body,
                revision=release.name,
                description=body,
            )
        )
    return slots


def _build_document_revision_slots(rows: list[dict[str, Any]], slot_count: int) -> list[RevisionSlot]:
    visible_rows = list(reversed(rows[:slot_count]))
    slots: list[RevisionSlot] = []
    for index in range(1, slot_count + 1):
        row = visible_rows[index - 1] if index <= len(visible_rows) else None
        if row is None:
            slots.append(_blank_slot(index))
            continue
        revision = _row_value(row, "revision")
        description = _row_value(row, "description")
        date = _row_value(row, "date")
        slots.append(
            RevisionSlot(
                index=index,
                version=revision,
                date=date,
                title=_document_revision_title(revision, date),
                body=description,
                revision=revision,
                description=description,
                drawn_by=_row_value(row, "drawn_by"),
                drawn_date=_row_value(row, "drawn_date"),
            )
        )
    return slots


def _blank_slot(index: int) -> RevisionSlot:
    return RevisionSlot(index, "", "", "", "")


def _row_value(row: dict[str, Any], key: str) -> str:
    return str(row.get(key) or "").strip()


def _document_revision_title(revision: str, date: str) -> str:
    if revision and date:
        return f"{revision} - {date}"
    return revision or date


def _release_title(release: ReleaseSection) -> str:
    if release.name == "Unreleased":
        return "Unreleased"
    if release.date:
        return f"{release.name} - {release.date}"
    return release.name


def _release_body(release: ReleaseSection) -> str:
    sections: list[tuple[str, list[str]]] = []
    current_section = ""
    current_items: list[str] = []

    for line in release.body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("###"):
            if current_items:
                sections.append((current_section, current_items))
            current_section = stripped.lstrip("#").strip()
            current_items = []
            continue
        if stripped.startswith("-"):
            current_items.append(_normalize_bullet(stripped))

    if current_items:
        sections.append((current_section, current_items))

    lines: list[str] = []
    for section, items in sections:
        if section:
            lines.append(f"{section}:")
        lines.extend(f"  {item}" for item in items)
    return "\n".join(lines)


def _normalize_bullet(value: str) -> str:
    if not value.startswith("-"):
        return value
    return "- " + value.lstrip("-").strip()


def _quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')
    return f'"{escaped}"'
