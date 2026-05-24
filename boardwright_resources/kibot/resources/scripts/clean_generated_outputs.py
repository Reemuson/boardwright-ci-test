from __future__ import annotations

import sys
from pathlib import Path


def _src_path() -> Path:
    return Path(__file__).resolve().parents[4] / "src"


sys.path.insert(0, str(_src_path()))

from boardwright.generated_outputs import clean_generated_outputs, format_cleanup_summary  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    root = Path((argv or sys.argv[1:] or ["."])[0])
    cleanup = clean_generated_outputs(root)
    print(format_cleanup_summary(cleanup))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
