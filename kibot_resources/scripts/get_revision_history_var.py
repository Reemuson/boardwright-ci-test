#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("variable")
    parser.add_argument(
        "-f",
        "--file",
        default=".boardwright/revision_history_variables.env",
        help="Revision history variable file.",
    )
    args = parser.parse_args()

    values = read_env(Path(args.file))
    print(values.get(args.variable, ""))
    return 0


def read_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}

    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = unquote(value.strip())
    return values


def unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] == '"':
        value = value[1:-1]
    return value.replace("\\n", "\n").replace('\\"', '"').replace("\\\\", "\\")


if __name__ == "__main__":
    raise SystemExit(main())
