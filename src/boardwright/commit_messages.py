from __future__ import annotations

from pathlib import Path

from . import git_ops


def suggest_commit_message(root: Path, change_text: str | None = None) -> str:
    paths = git_ops.changed_paths(root)
    scope = _suggest_scope(paths)
    commit_type = _suggest_type(paths)
    summary = _summarize(change_text, paths)
    return f"{commit_type}({scope}): {summary}"


def _suggest_scope(paths: list[str]) -> str:
    lowered = " ".join(path.lower() for path in paths)
    if _has_boardwright_files(lowered):
        return "template"
    if "readme" in lowered or lowered.endswith(".md"):
        return "docs"
    if ".github/" in lowered or "kibot_yaml" in lowered:
        return "ci"
    if "license" in lowered or "notice" in lowered:
        return "legal"
    if ".kicad_pcb" in lowered:
        return "pcb"
    if ".kicad_sch" in lowered:
        return "schematic"
    return "hardware"


def _suggest_type(paths: list[str]) -> str:
    lowered = " ".join(path.lower() for path in paths)
    if _has_boardwright_files(lowered):
        return "chore"
    if "readme" in lowered or lowered.endswith(".md"):
        return "docs"
    if ".github/" in lowered or "kibot_yaml" in lowered:
        return "ci"
    return "chore"


def _summarize(change_text: str | None, paths: list[str]) -> str:
    if change_text:
        summary = change_text.strip().rstrip(".")
        return summary[:1].lower() + summary[1:] if summary else "update project"
    if paths:
        return "update project files"
    return "record project state"


def _has_boardwright_files(lowered_paths: str) -> bool:
    return any(
        marker in lowered_paths
        for marker in (
            ".boardwright/",
            "src/boardwright",
            "src\\boardwright",
            "pyproject.toml",
            "todo.md",
            "spec.md",
            "roadmap.md",
        )
    )
