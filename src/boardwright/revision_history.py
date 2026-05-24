from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .changelog import ReleaseSection, parse_releases, read_changelog
from .config import BoardwrightConfig


@dataclass(frozen=True)
class RevisionSlot:
    index: int
    version: str
    date: str
    title: str
    body: str


def build_revision_slots(config: BoardwrightConfig) -> list[RevisionSlot]:
    settings = config.revision_history.get("revision_history", {})
    slot_count = int(settings.get("slots", 4))
    include_unreleased = bool(settings.get("include_unreleased_in_preview", True))
    return build_revision_slots_from_text(
        read_changelog(config.root),
        slot_count=slot_count,
        include_unreleased=include_unreleased,
    )


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
            slots.append(RevisionSlot(index, "", "", "", ""))
            continue
        slots.append(
            RevisionSlot(
                index=index,
                version=release.name,
                date=release.date or "",
                title=_release_title(release),
                body=_release_body(release),
            )
        )
    return slots


def write_revision_variables(config: BoardwrightConfig) -> Path:
    path = config.root / ".boardwright" / "revision_history_variables.env"
    lines: list[str] = []
    for slot in build_revision_slots(config):
        prefix = f"REVHIST_{slot.index}"
        lines.extend(
            [
                f"{prefix}_TITLE={_quote(slot.title)}",
                f"{prefix}_BODY={_quote(slot.body)}",
            ]
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return path


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
