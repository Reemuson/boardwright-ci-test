from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .errors import BoardwrightError


SUPPORTED_SECTIONS = ("Status", "Added", "Changed", "Fixed", "Removed", "Notes")
HEADING_RE = re.compile(
    r"^## \[(?P<name>[^\]]+)\](?: - (?P<date>\d{4}-\d{2}-\d{2}))?\s*$",
    re.MULTILINE,
)
SECTION_RE = re.compile(r"^### (?P<name>.+?)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class ReleaseSection:
    name: str
    date: str | None
    body: str


def changelog_path(root: Path) -> Path:
    return root / "CHANGELOG.md"


def read_changelog(root: Path) -> str:
    path = changelog_path(root)
    if not path.exists():
        raise BoardwrightError("Missing CHANGELOG.md")
    return path.read_text(encoding="utf-8")


def parse_releases(text: str) -> list[ReleaseSection]:
    matches = list(HEADING_RE.finditer(text, re.MULTILINE))
    releases: list[ReleaseSection] = []
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        releases.append(
            ReleaseSection(
                name=match.group("name"),
                date=match.group("date"),
                body=text[start:end].strip(),
            )
        )
    return releases


def unreleased_has_content(text: str) -> bool:
    unreleased = next((r for r in parse_releases(text) if r.name == "Unreleased"), None)
    if unreleased is None:
        return False
    body_without_headings = SECTION_RE.sub("", unreleased.body)
    return any(line.strip().startswith("-") for line in body_without_headings.splitlines())


def has_release(text: str, version: str) -> bool:
    return any(release.name == version for release in parse_releases(text))


def promote_unreleased(text: str, version: str, release_date: date | None = None) -> str:
    if has_release(text, version):
        raise BoardwrightError(f"CHANGELOG.md already contains release {version}.")
    if not unreleased_has_content(text):
        raise BoardwrightError("CHANGELOG.md has no unreleased entries to release.")

    release_date = release_date or date.today()
    unreleased_match = re.search(r"^## \[Unreleased\]\s*$", text, re.MULTILINE)
    if unreleased_match is None:
        raise BoardwrightError("CHANGELOG.md must contain a '## [Unreleased]' section.")

    insert_at = unreleased_match.end()
    heading = f"\n\n## [{version}] - {release_date.isoformat()}"
    promoted = text[:insert_at].rstrip() + heading + text[insert_at:]
    return re.sub(r"(?<!\n)\n(?=## \[)", "\n\n", promoted)


def promote_unreleased_file(root: Path, version: str, release_date: date | None = None) -> None:
    path = changelog_path(root)
    path.write_text(
        promote_unreleased(read_changelog(root), version, release_date),
        encoding="utf-8",
        newline="\n",
    )


def add_unreleased_entry(root: Path, section: str, message: str) -> None:
    normalized = _normalize_section(section)
    cleaned_message = message.strip()
    if not cleaned_message:
        raise BoardwrightError("Change message cannot be empty.")

    path = changelog_path(root)
    text = read_changelog(root)
    if "## [Unreleased]" not in text:
        raise BoardwrightError("CHANGELOG.md must contain a '## [Unreleased]' section.")

    updated = _insert_unreleased_entry(text, normalized, cleaned_message)
    path.write_text(updated, encoding="utf-8", newline="\n")


def _normalize_section(section: str) -> str:
    for candidate in SUPPORTED_SECTIONS:
        if candidate.lower() == section.strip().lower():
            return candidate
    allowed = ", ".join(SUPPORTED_SECTIONS)
    raise BoardwrightError(f"Unsupported changelog section '{section}'. Use one of: {allowed}.")


def _insert_unreleased_entry(text: str, section: str, message: str) -> str:
    unreleased_match = re.search(r"^## \[Unreleased\]\s*$", text, re.MULTILINE)
    if unreleased_match is None:
        raise BoardwrightError("CHANGELOG.md must contain a '## [Unreleased]' section.")

    next_release_match = re.search(r"^## \[", text[unreleased_match.end() :], re.MULTILINE)
    unreleased_end = (
        unreleased_match.end() + next_release_match.start()
        if next_release_match
        else len(text)
    )

    before = text[: unreleased_match.end()]
    body = text[unreleased_match.end() : unreleased_end]
    after = text[unreleased_end:]

    section_match = re.search(
        rf"^### {re.escape(section)}\s*$",
        body,
        re.MULTILINE,
    )
    entry = f"- {message}"

    if section_match:
        insert_at = section_match.end()
        following_section = re.search(r"^### ", body[insert_at:], re.MULTILINE)
        section_end = (
            insert_at + following_section.start()
            if following_section
            else len(body)
        )
        section_body = body[insert_at:section_end].rstrip()
        replacement = f"\n\n{entry}\n"
        if section_body.strip():
            replacement = f"{section_body}\n{entry}\n\n"
        body = body[:insert_at] + replacement + body[section_end:]
    else:
        prefix = body.rstrip()
        body = f"{prefix}\n\n### {section}\n\n{entry}\n"

    return before.rstrip() + "\n" + body.rstrip() + "\n" + after
