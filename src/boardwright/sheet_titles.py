# SPDX-License-Identifier: LicenseRef-Boardwright-Source-Available
# Copyright (c) Ryan Hicks

from __future__ import annotations

import re
from pathlib import Path

MAX_SHEET_TITLES = 40
SHEET_TITLE_OVERRIDES = Path(".boardwright") / "sheet_titles.env"


def find_primary_schematic(root: Path) -> Path | None:
    root = root.resolve()
    preferred_names = (
        "boardwright.kicad_sch",
        "project.kicad_sch",
    )
    for name in preferred_names:
        path = root / name
        if path.is_file():
            return path
    schematics = sorted(root.glob("*.kicad_sch"))
    return schematics[0] if schematics else None


def collect_actual_sheet_titles(
    root: Path,
    *,
    schematic: Path | None = None,
    max_titles: int = MAX_SHEET_TITLES,
) -> tuple[str, ...]:
    schematic_path = schematic or find_primary_schematic(root)
    titles = ["COVER PAGE"]
    if schematic_path is None:
        titles.extend(["." * 32] * (max_titles - 1))
        return tuple(titles[:max_titles])

    for page_number in range(2, max_titles + 1):
        titles.append(_title_from_schematic(schematic_path, page_number, 32, root))
    return tuple(titles[:max_titles])


def read_sheet_title_overrides(root: Path) -> dict[int, str]:
    path = root / SHEET_TITLE_OVERRIDES
    if not path.is_file():
        return {}

    overrides: dict[int, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, value = line.partition("=")
        if not sep or not name.startswith("SHEET_NAME_"):
            continue
        suffix = name.removeprefix("SHEET_NAME_")
        if not suffix.isdigit():
            continue
        overrides[int(suffix)] = value
    return overrides


def write_sheet_title_overrides(root: Path, titles: tuple[str, ...]) -> Path:
    path = root / SHEET_TITLE_OVERRIDES
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for index, title in enumerate(titles, start=1):
        lines.append(f"SHEET_NAME_{index}={title}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return path


def compare_sheet_title_overrides(
    titles: tuple[str, ...],
    overrides: dict[int, str],
) -> list[str]:
    issues: list[str] = []
    for index, expected in enumerate(titles, start=1):
        actual = overrides.get(index)
        if actual != expected:
            issues.append(
                f"SHEET_NAME_{index}: local={actual!r} schematic={expected!r}"
            )
    return issues


def _title_from_schematic(
    file_path: Path,
    page_number: int,
    dots_number: int,
    project_root: Path,
) -> str:
    titles = _titles_from_schematic(file_path, str(page_number), set(), project_root)
    if not titles:
        return "." * dots_number
    if len(set(titles)) > 1:
        return "Conflicting page numbers"
    return titles[0]


def _titles_from_schematic(
    file_path: Path,
    page_number: str,
    seen: set[Path],
    project_root: Path,
) -> list[str]:
    if not file_path.exists():
        return []

    resolved_path = file_path.resolve()
    if resolved_path in seen:
        return []
    seen.add(resolved_path)

    text = file_path.read_text(encoding="utf-8", errors="replace")
    titles: list[str] = []
    for block in _sheet_blocks(text):
        name = _property_value(block, "Sheetname")
        sheet_file = _property_value(block, "Sheetfile")
        if _block_has_page(block, page_number) and name:
            titles.append(name)
        if sheet_file:
            titles.extend(
                _titles_from_schematic(
                    (file_path.parent / sheet_file)
                    if not Path(sheet_file).is_absolute()
                    else Path(sheet_file),
                    page_number,
                    seen,
                    project_root,
                )
            )
    return titles


def _sheet_blocks(text: str) -> list[str]:
    return text.split("\n\t(sheet\n")[1:]


def _block_has_page(block: str, page_number: str) -> bool:
    return page_number in re.findall(r'\(page "([^"]+)"\)', block)


def _property_value(block: str, property_name: str) -> str:
    marker = f'(property "{property_name}" "'
    start = block.find(marker)
    if start < 0:
        return ""
    start += len(marker)
    end = block.find('"', start)
    if end < 0:
        return ""
    return block[start:end]
