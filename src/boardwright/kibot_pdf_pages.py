from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path


FABRICATION_PDF_YAML = Path("boardwright_resources/kibot/yaml/kibot_out_pdf_fabrication.yaml")


@dataclass(frozen=True)
class PdfPagePruneResult:
    config_path: Path
    removed_pages: tuple[str, ...]


def prune_empty_testpoint_pdf_pages(root: Path | str = ".") -> PdfPagePruneResult:
    """Remove testpoint PDF pages whose generated side-specific CSV has no rows."""
    resolved_root = Path(root).resolve()
    config_path = resolved_root / FABRICATION_PDF_YAML
    text = config_path.read_text(encoding="utf-8")

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

    if removed:
        config_path.write_text(text, encoding="utf-8")

    return PdfPagePruneResult(config_path=config_path, removed_pages=tuple(removed))


def format_pdf_page_prune_summary(result: PdfPagePruneResult) -> str:
    if not result.removed_pages:
        return "PDF page pruning: no empty testpoint pages removed."
    pages = ", ".join(result.removed_pages)
    return f"PDF page pruning: removed empty page(s): {pages}."


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
