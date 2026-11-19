# SPDX-License-Identifier: LicenseRef-Boardwright-Source-Available
# Copyright (c) Ryan Hicks

"""Plan drawing revision-history rows for a release.

A drawing's revision table records drawing revisions, not project releases.
Release versions and tooling-only releases belong in the release ledger and
CHANGELOG. A release therefore:

- leaves a document's rows untouched when its revision does not advance;
- adds exactly one new row when its revision advances;
- never changes a previously issued row (revision, date, description or
  author).

Issued rows come from the previous release tag's
``.boardwright/document_revisions.yaml`` when that tag exists, so a working-tree
edit cannot silently rewrite issued history. Without a tag (first release, or a
repository without tags) the working tree is the only source.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import subprocess
from pathlib import Path
from typing import Any

import yaml

from .config import BoardwrightConfig, CONFIG_DIR, _read_yaml_optional, _write_yaml
from .errors import BoardwrightError

DRAWING_DOCUMENTS = ("schematic", "fabrication", "assembly")
ROW_FIELDS = ("revision", "date", "description", "drawn_by")
REVISIONS_FILE = f"{CONFIG_DIR}/document_revisions.yaml"
DESCRIPTION_LIMIT = 60


@dataclass(frozen=True)
class DocumentRevisionPlan:
    document: str
    previous_revision: str
    proposed_revision: str
    rows: tuple[dict[str, str], ...]
    added: dict[str, str] | None = None

    @property
    def changed(self) -> bool:
        return self.added is not None


@dataclass(frozen=True)
class RevisionHistoryPlan:
    documents: tuple[DocumentRevisionPlan, ...]
    source: str
    problems: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def changed(self) -> tuple[DocumentRevisionPlan, ...]:
        return tuple(item for item in self.documents if item.changed)


def normalise_row(row: dict[str, Any]) -> dict[str, str]:
    """Return a row as plain strings (YAML may load an unquoted date as a date)."""

    clean = {name: " ".join(str(row.get(name) or "").split()) for name in ROW_FIELDS}
    return {name: value for name, value in clean.items() if value or name != "drawn_by"}


def rows_from_data(data: Any, document: str) -> list[dict[str, str]]:
    revisions = data.get("document_revisions", {}) if isinstance(data, dict) else {}
    rows: Any = []
    if isinstance(revisions, dict):
        entry = revisions.get(document, {})
        if isinstance(entry, dict):
            rows = entry.get("rows", [])
        elif isinstance(entry, list):
            rows = entry
    if not isinstance(rows, list):
        return []
    return [
        normalise_row(row)
        for row in rows
        if isinstance(row, dict) and any(str(value).strip() for value in row.values())
    ]


def issued_rows_at_tag(root: Path, version: str) -> dict[str, list[dict[str, str]]] | None:
    """Rows recorded in the release tag, or None when the tag has no table."""

    if not version:
        return None
    try:
        completed = subprocess.run(
            ["git", "show", f"refs/tags/{version}:{REVISIONS_FILE}"],
            cwd=root,
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    try:
        data = yaml.safe_load(completed.stdout.decode("utf-8")) or {}
    except yaml.YAMLError as exc:
        raise BoardwrightError(f"Release {version} has an unreadable {REVISIONS_FILE}: {exc}") from exc
    return {document: rows_from_data(data, document) for document in DRAWING_DOCUMENTS}


def plan_revision_history(
    config: BoardwrightConfig,
    *,
    previous_version: str,
    previous: dict[str, str],
    proposed: dict[str, str],
    description: str,
    date: str,
    issued: dict[str, list[dict[str, str]]] | None = None,
) -> RevisionHistoryPlan:
    """Plan each drawing's rows from the previous and proposed revisions.

    ``previous`` and ``proposed`` map document name to revision. An empty
    previous revision means the document has not been issued yet.
    """

    if issued is None and previous_version:
        issued = issued_rows_at_tag(config.root, previous_version)
    source = f"tag {previous_version}" if issued is not None else "working tree"
    current_data = _read_yaml_optional(config.root / REVISIONS_FILE)
    clean_description = " ".join(str(description or "").split())
    problems: list[str] = []
    notes: list[str] = []
    plans: list[DocumentRevisionPlan] = []

    for document in DRAWING_DOCUMENTS:
        current = rows_from_data(current_data, document)
        history = issued[document] if issued is not None else current
        old = str(previous.get(document) or "").strip()
        new = str(proposed.get(document) or "").strip()
        issued_revisions = [row.get("revision", "") for row in history]
        pending = [row for row in current if row not in history]

        if issued is not None:
            for row in history:
                if row not in current:
                    problems.append(
                        f"The issued {document} revision {row.get('revision') or '(blank)'} row differs from "
                        f"release {previous_version}; issued revision history must not change."
                    )

        if not new or (new == old) or (not old and new in issued_revisions):
            # The revision does not advance: the table stays exactly as issued.
            if pending:
                problems.append(
                    f"The {document} revision table has rows that were not issued in "
                    f"{previous_version or 'any release'} ("
                    + ", ".join(row.get("revision") or "(blank)" for row in pending)
                    + f") but the {document} revision does not advance."
                )
            plans.append(DocumentRevisionPlan(document, old, new, tuple(history)))
            continue

        if new in issued_revisions:
            problems.append(f"The {document} revision {new} was already issued; choose a new revision.")
            plans.append(DocumentRevisionPlan(document, old, new, tuple(history)))
            continue
        drafts = [row for row in pending if row.get("revision") == new]
        others = [row for row in pending if row.get("revision") != new]
        if others:
            problems.append(
                f"The {document} revision table has unissued rows other than {new} ("
                + ", ".join(row.get("revision") or "(blank)" for row in others)
                + ")."
            )
        draft = drafts[0] if drafts else {}
        row_description = clean_description or draft.get("description", "")
        if not row_description:
            problems.append(f"The {document} revision advances to {new}; a revision table description is required.")
        elif len(row_description) > DESCRIPTION_LIMIT:
            problems.append(f"Revision table description must be {DESCRIPTION_LIMIT} characters or fewer.")
        drawn_by = (
            draft.get("drawn_by")
            or str(config.document_settings(document).get("drawn_by") or "").strip()
            or next((row.get("drawn_by", "") for row in history if row.get("drawn_by")), "")
        )
        added = normalise_row({"revision": new, "date": date, "description": row_description, "drawn_by": drawn_by})
        plans.append(DocumentRevisionPlan(document, old, new, (added, *history), added))

    if not any(plan.changed for plan in plans) and clean_description:
        notes.append(
            "No drawing revision advances, so the revision table description is not recorded; "
            "the release is recorded in the ledger and CHANGELOG."
        )
    return RevisionHistoryPlan(tuple(plans), source, tuple(problems), tuple(notes))


def apply_revision_history(config: BoardwrightConfig, plan: RevisionHistoryPlan) -> Path | None:
    """Write the documents whose revision advances; touch nothing otherwise."""

    if plan.problems:
        raise BoardwrightError("; ".join(plan.problems))
    changed = plan.changed
    path = config.root / REVISIONS_FILE
    if not changed:
        return None
    data = _read_yaml_optional(path)
    revisions = data.setdefault("document_revisions", {})
    if not isinstance(revisions, dict):
        raise BoardwrightError("document_revisions.yaml field `document_revisions` must be a mapping.")
    for item in changed:
        entry = revisions.get(item.document)
        if not isinstance(entry, dict):
            entry = {}
            revisions[item.document] = entry
        entry["rows"] = [dict(row) for row in item.rows]
    _write_yaml(path, data)
    return path
