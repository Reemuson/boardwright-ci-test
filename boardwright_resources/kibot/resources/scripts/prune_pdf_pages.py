"""Prune empty conditional KiBot PDF pages after table CSV generation."""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    src = root / "src"
    if src.is_dir():
        sys.path.insert(0, str(src.resolve()))

    from boardwright.kibot_pdf_pages import (
        format_pdf_page_prune_summary,
        prune_empty_testpoint_pdf_pages,
    )

    print(format_pdf_page_prune_summary(prune_empty_testpoint_pdf_pages(root)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
