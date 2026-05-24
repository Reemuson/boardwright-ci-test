from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path


_PDF_PAGE_RE = re.compile(r"^(?P<base>.+)-(?P<page>[1-9][0-9]*)\.pdf$", re.IGNORECASE)
_EMPTY_TABLE_NAME_MARKERS = (
    "comp_count",
    "component_count",
    "components_count",
    "impedance",
    "testpoint",
    "testpoints",
)


@dataclass(frozen=True)
class GeneratedOutputCleanup:
    root: Path
    removed_pdf_pages: tuple[Path, ...]
    removed_empty_tables: tuple[Path, ...]

    @property
    def removed_paths(self) -> tuple[Path, ...]:
        return self.removed_pdf_pages + self.removed_empty_tables


def clean_generated_outputs(root: Path | str = ".") -> GeneratedOutputCleanup:
    """Remove CI packaging noise from KiBot output directories."""
    resolved_root = Path(root).resolve()
    removed_pdf_pages = _remove_numbered_pdf_pages(resolved_root)
    removed_empty_tables = _remove_empty_tables(resolved_root)
    return GeneratedOutputCleanup(
        root=resolved_root,
        removed_pdf_pages=tuple(removed_pdf_pages),
        removed_empty_tables=tuple(removed_empty_tables),
    )


def format_cleanup_summary(cleanup: GeneratedOutputCleanup) -> str:
    lines = [
        f"Generated output cleanup: removed {len(cleanup.removed_paths)} file(s).",
    ]
    for path in cleanup.removed_paths:
        lines.append(f"- {path.relative_to(cleanup.root)}")
    return "\n".join(lines)


def _remove_numbered_pdf_pages(root: Path) -> list[Path]:
    removed: list[Path] = []
    for path in sorted(root.rglob("*.pdf")):
        match = _PDF_PAGE_RE.match(path.name)
        if not match:
            continue
        combined = path.with_name(f"{match.group('base')}.pdf")
        if not combined.is_file():
            continue
        path.unlink()
        removed.append(path)
    return removed


def _remove_empty_tables(root: Path) -> list[Path]:
    removed: list[Path] = []
    for path in sorted(root.rglob("*.csv")):
        if not _is_cleanup_table_name(path):
            continue
        if not _csv_has_data_row(path):
            path.unlink()
            removed.append(path)
    return removed


def _is_cleanup_table_name(path: Path) -> bool:
    name = path.name.lower()
    return any(marker in name for marker in _EMPTY_TABLE_NAME_MARKERS)


def _csv_has_data_row(path: Path) -> bool:
    text = path.read_text(encoding="utf-8", errors="ignore")
    if not text.strip():
        return False

    rows = list(csv.reader(text.splitlines()))
    non_empty_rows = [
        row
        for row in rows
        if any(cell.strip() for cell in row)
    ]
    return len(non_empty_rows) > 1
