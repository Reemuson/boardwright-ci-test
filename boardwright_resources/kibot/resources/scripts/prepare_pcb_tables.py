"""Prepare Boardwright-owned KiCad table placeholders for CI rendering."""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    src = root / "src"
    if src.is_dir():
        sys.path.insert(0, str(src.resolve()))

    from boardwright.kicad_tables import prepare_pcb_tables

    result = prepare_pcb_tables(root)
    print(
        f"Prepared component count table: {result.total} components -> "
        f"{result.csv_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
