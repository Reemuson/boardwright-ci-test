# SPDX-License-Identifier: LicenseRef-Boardwright-Source-Available
# Copyright (c) Ryan Hicks

"""Suggest commit messages in the project convention (``docs/COMMITS.md``).

Subjects take the form ``type(scope): imperative summary``. The scope is
omitted when the changed files span several areas. Suggestions are a starting
point for the person committing; nothing here rejects a message.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

from . import git_ops

TYPES = ("feat", "fix", "docs", "test", "refactor", "ci", "build", "chore", "revert", "release")
SUBJECT_LIMIT = 72
_SUBJECT_RE = re.compile(r"^(?P<type>[a-z]+)(?:\((?P<scope>[a-z0-9][a-z0-9._/-]*)\))?!?: (?P<summary>\S.*)$")

# area -> (commit type, scope, default summary). The scope never repeats the
# type; an empty scope is omitted. Hardware edits default to ``chore``: the
# tool cannot tell a feature from a fix, so the person committing decides.
_AREAS = {
    "ci": ("ci", "workflows", "update GitHub workflows"),
    "payload": ("chore", "payload", "update the Boardwright payload"),
    "tests": ("test", "", "update tests"),
    "fab-notes": ("docs", "fab-notes", "update fabrication notes"),
    "drawings": ("chore", "drawings", "update drawing sheets"),
    "pcb": ("chore", "pcb", "update the PCB layout"),
    "schematic": ("chore", "schematic", "update the schematic"),
    "project": ("chore", "project", "update project configuration"),
    "changelog": ("docs", "changelog", "update the changelog"),
    "legal": ("docs", "legal", "update legal notices"),
    "docs": ("docs", "", "update documentation"),
}


def _area(path: str) -> str:
    p = PurePosixPath(path.replace("\\", "/"))
    name, parts, suffix = p.name.lower(), [part.lower() for part in p.parts], p.suffix.lower()
    if parts[:2] == [".github", "workflows"]:
        return "ci"
    if parts[:2] == ["src", "boardwright"] or parts[:1] == ["boardwright_resources"] or name == "pyproject.toml":
        return "payload"
    if parts[:1] == ["tests"]:
        return "tests"
    if name == "fabrication_notes.txt" or name == "assembly_notes.txt":
        return "fab-notes"
    if suffix == ".kicad_wks" or parts[:1] == ["templates"]:
        return "drawings"
    if suffix in (".kicad_pcb", ".kicad_dru"):
        return "pcb"
    if suffix == ".kicad_sch":
        return "schematic"
    if parts[:1] == [".boardwright"] or suffix == ".kicad_pro":
        return "project"
    if name == "changelog.md":
        return "changelog"
    if name.startswith(("license", "notice")) or name == "third_party_notices.md":
        return "legal"
    if suffix == ".md" or parts[:1] == ["docs"]:
        return "docs"
    return ""


def suggest_commit_message(root: Path, change_text: str | None = None) -> str:
    return suggest_from_paths(git_ops.changed_paths(root), change_text)


def suggest_from_paths(paths: list[str], change_text: str | None = None) -> str:
    areas = {_area(path) for path in paths}
    area = areas.pop() if len(areas) == 1 else ""
    commit_type, scope, default_summary = _AREAS.get(
        area, ("chore", "", "update project files" if paths else "record project state")
    )
    summary = _summary(change_text) or default_summary
    return f"{commit_type}({scope}): {summary}" if scope else f"{commit_type}: {summary}"


def _summary(change_text: str | None) -> str:
    text = " ".join(str(change_text or "").split()).rstrip(".")
    if not text:
        return ""
    # Lower-case a leading capital word ("Add" -> "add") but keep acronyms ("PCB").
    if len(text) > 1 and text[0].isupper() and not text[1].isupper():
        text = text[0].lower() + text[1:]
    return text


def subject_warnings(message: str) -> list[str]:
    """Advisory notes on a commit subject; never a reason to refuse a commit."""

    subject = message.splitlines()[0] if message else ""
    notes: list[str] = []
    match = _SUBJECT_RE.match(subject)
    if subject.startswith(("Merge ", "Revert \"")):
        return notes
    if not match:
        notes.append("subject is not in the form type(scope): summary")
    else:
        if match["type"] not in TYPES:
            notes.append(f"type {match['type']!r} is not one of: {', '.join(TYPES)}")
        if match["scope"] and match["scope"] == match["type"]:
            notes.append("scope repeats the type")
        if subject.rstrip().endswith("."):
            notes.append("subject ends with a full stop")
    if len(subject) > SUBJECT_LIMIT:
        notes.append(f"subject is {len(subject)} characters; prefer {SUBJECT_LIMIT} or fewer")
    return notes
