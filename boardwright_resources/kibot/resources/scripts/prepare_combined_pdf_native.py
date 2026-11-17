# SPDX-License-Identifier: LicenseRef-Boardwright-Source-Available
# Copyright (c) Ryan Hicks

from __future__ import annotations

import sys
from pathlib import Path


def main() -> None:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
    root = root.resolve()
    sys.path.insert(0, str(root / "src"))

    from boardwright.combined_pdf import (  # noqa: WPS433
        format_combined_sheet_manifest_summary,
        format_native_combined_output_summary,
        prepare_native_combined_outputs,
    )
    from boardwright.config import load_config  # noqa: WPS433

    result = prepare_native_combined_outputs(load_config(root), root)
    print(format_combined_sheet_manifest_summary(result.manifest))
    print(format_native_combined_output_summary(result))


if __name__ == "__main__":
    main()
