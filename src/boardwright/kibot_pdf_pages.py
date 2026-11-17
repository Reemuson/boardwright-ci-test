# SPDX-License-Identifier: LicenseRef-Boardwright-Source-Available
# Copyright (c) Ryan Hicks

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


FABRICATION_PDF_YAML = Path("boardwright_resources/kibot/yaml/kibot_out_pdf_fabrication.yaml")
COPPER_LAYER_RE = re.compile(r'^\s*\((\d+)\s+"((?:F|B|In\d+)\.Cu)"\s+', re.MULTILINE)


@dataclass(frozen=True)
class PdfPagePruneResult:
    config_path: Path
    removed_pages: tuple[str, ...]
    expanded_pages: tuple[str, ...] = ()


def prune_empty_testpoint_pdf_pages(root: Path | str = ".") -> PdfPagePruneResult:
    """Prepare conditional KiBot PDF pages after table CSV generation."""
    resolved_root = Path(root).resolve()
    config_path = resolved_root / FABRICATION_PDF_YAML
    text = config_path.read_text(encoding="utf-8")
    text, expanded = _expand_repeated_layer_pages(text, resolved_root)

    removed: list[str] = []
    for side, title in (
        ("top", "TOP TEST POINTS"),
        ("bottom", "BOTTOM TEST POINTS"),
    ):
        if _testpoint_side_has_rows(resolved_root, side):
            continue
        updated = _remove_page_by_sheet_title(text, title)
        if updated != text:
            text = updated
            removed.append(title)

    if removed or expanded:
        config_path.write_text(text, encoding="utf-8")

    return PdfPagePruneResult(
        config_path=config_path,
        removed_pages=tuple(removed),
        expanded_pages=tuple(expanded),
    )


def format_pdf_page_prune_summary(result: PdfPagePruneResult) -> str:
    parts: list[str] = []
    if result.expanded_pages:
        pages = ", ".join(result.expanded_pages)
        parts.append(f"expanded repeated PCB page(s): {pages}")
    if result.removed_pages:
        pages = ", ".join(result.removed_pages)
        parts.append(f"removed empty page(s): {pages}")
    if not parts:
        return "PDF page pruning: no PDF page changes needed."
    return "PDF page pruning: " + "; ".join(parts) + "."


def _expand_repeated_layer_pages(text: str, root: Path) -> tuple[str, list[str]]:
    copper_layers = _copper_layers(root)
    if not copper_layers:
        return text, []

    labels = {layer: f"L{index}" for index, layer in enumerate(copper_layers, start=1)}
    expanded: list[str] = []

    text, drill_expanded = _replace_repeated_page(
        text,
        "drill_pairs",
        lambda block: [_concrete_drill_page(block, _drill_title(tuple(labels.values())))],
    )
    expanded.extend(drill_expanded)

    text, copper_expanded = _replace_repeated_page(
        text,
        "copper",
        lambda block: [_concrete_copper_page(block, layer, labels[layer]) for layer in copper_layers],
    )
    expanded.extend(copper_expanded)
    return text, expanded


def _copper_layers(root: Path) -> tuple[str, ...]:
    pcb_path = _project_pcb_path(root)
    if pcb_path is None:
        return ()
    text = pcb_path.read_text(encoding="utf-8", errors="ignore")
    matches = [(int(match.group(1)), match.group(2)) for match in COPPER_LAYER_RE.finditer(text)]
    return tuple(layer for _, layer in sorted(matches))


def _project_pcb_path(root: Path) -> Path | None:
    for project in sorted(root.glob("*.kicad_pro")):
        candidate = project.with_suffix(".kicad_pcb")
        if candidate.exists():
            return candidate
    boards = sorted(path for path in root.glob("*.kicad_pcb") if path.name != "example.kicad_pcb")
    if boards:
        return boards[0]
    examples = sorted(root.glob("*.kicad_pcb"))
    return examples[0] if examples else None


def _replace_repeated_page(
    text: str,
    repeat_type: str,
    expand: Callable[[str], list[str]],
) -> tuple[str, list[str]]:
    needle = f"repeat_layers: '{repeat_type}'"
    marker_index = text.find(needle)
    if marker_index == -1:
        return text, []

    page_start = text.rfind("\n      - scaling:", 0, marker_index)
    if page_start == -1:
        return text, []

    next_page = text.find("\n      - scaling:", page_start + 1)
    if next_page == -1:
        next_page = text.find("\n...", page_start + 1)
    if next_page == -1:
        next_page = len(text)

    block = text[page_start:next_page]
    pages = expand(block)
    names = [_page_title(page) for page in pages if _page_title(page)]
    return text[:page_start] + "".join(pages) + text[next_page:], names


def _concrete_drill_page(block: str, title: str) -> str:
    page = block
    page = _replace_yaml_string(page, "title", f"{title} DRILL MAP")
    page = _replace_yaml_string(page, "sheet", f"DRILL DRAWING ({title})")
    page = _replace_yaml_string(page, "layer_var", f"DRILL DRAWING {title} (SCALE @SCALING@:1)")
    return page


def _concrete_copper_page(block: str, layer: str, label: str) -> str:
    page = _without_repeat_lines(block)
    page = _replace_yaml_string(page, "title", f"{label} COPPER")
    page = _replace_yaml_string(page, "sheet", f"{label} COPPER (SCALE @SCALING@:1)")
    page = _replace_yaml_string(page, "layer_var", f"{label} COPPER (SCALE @SCALING@:1)")
    return re.sub(r"(\n\s*-\s*layer:\s*)'[^']+'", rf"\1'{layer}'", page, count=1)


def _without_repeat_lines(block: str) -> str:
    return re.sub(r"\n\s*repeat_(?:for_layer|layers):[^\n]*", "", block)


def _replace_yaml_string(block: str, key: str, value: str) -> str:
    return re.sub(rf"(\n\s*{re.escape(key)}:\s*)'[^']*'", rf"\1'{value}'", block, count=1)


def _drill_title(labels: tuple[str, ...]) -> str:
    if not labels:
        return "DRILL"
    if len(labels) == 1:
        return labels[0]
    return f"{labels[0]}-{labels[-1]}"


def _page_title(block: str) -> str:
    match = re.search(r"\n\s*title:\s*'([^']*)'", block)
    return match.group(1) if match else ""


def _testpoint_side_has_rows(root: Path, side: str) -> bool:
    patterns = (
        f"Testing/Testpoints/*-testpoints-{side}.csv",
        f"Testing/Testpoints/*-testpoints-{side}*.csv",
    )
    for pattern in patterns:
        for path in sorted(root.glob(pattern)):
            if _csv_has_data_row(path):
                return True
    return False


def _csv_has_data_row(path: Path) -> bool:
    text = path.read_text(encoding="utf-8", errors="ignore")
    rows = [
        row
        for row in csv.reader(text.splitlines())
        if any(cell.strip() for cell in row)
    ]
    return len(rows) > 1


def _remove_page_by_sheet_title(text: str, title: str) -> str:
    marker = f"sheet: '{title}"
    marker_index = text.find(marker)
    if marker_index == -1:
        return text

    page_start = text.rfind("\n      - scaling:", 0, marker_index)
    if page_start == -1:
        return text

    next_page = text.find("\n      - scaling:", page_start + 1)
    if next_page == -1:
        next_page = text.find("\n...", page_start + 1)
    if next_page == -1:
        next_page = len(text)

    return text[:page_start] + text[next_page:]
