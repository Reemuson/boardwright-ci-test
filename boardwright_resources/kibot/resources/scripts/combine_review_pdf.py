# SPDX-License-Identifier: LicenseRef-Boardwright-Source-Available
# Copyright (c) Ryan Hicks

"""Create an optional combined review PDF after KiBot output generation."""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    src = root / "src"
    if src.is_dir():
        sys.path.insert(0, str(src.resolve()))

    from boardwright.combined_pdf import format_combined_pdf_summary, write_combined_pdf
    from boardwright.config import load_config

    plan = write_combined_pdf(load_config(root), root)
    print(format_combined_pdf_summary(plan))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
