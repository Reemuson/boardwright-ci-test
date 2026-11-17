# SPDX-License-Identifier: LicenseRef-Boardwright-Source-Available
# Copyright (c) Ryan Hicks

"""Discard source side effects of a CI output build, fail-closed.

Building outputs rewrites tracked sources (the ``*.kicad_pro`` text-variable
sync, PCB table placeholders, KiBot PDF page pruning, ``kibot_main.yaml``
definitions). Workflows must not commit those, and leftover modifications stop
``git switch`` to the target branch.

The previous shell form ``git checkout HEAD -- '*.kicad_pcb' … '*.kicad_prl' || true``
restored nothing when any one pathspec matched no tracked file, and the failure
was hidden. This command restores every tracked modification outside the
``--keep`` paths, removes generated KiBot configs, then verifies the tree and
exits non-zero if anything unexpected is still modified.

Usage::

    python -m boardwright.ci_cleanup [--keep PATH ...]
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

GENERATED_UNTRACKED = ("boardwright_resources/kibot/yaml/kibot_document_*.generated.yaml",)
OUTPUT_DIRS = ("HTML", "KiRI", "Manufacturing", "Reports", "Schematic", "Testing")


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout


def _kept(path: str, keep: tuple[str, ...]) -> bool:
    return any(path == item or path.startswith(item.rstrip("/") + "/") for item in keep)


def modified_tracked(root: Path) -> list[str]:
    output = _git(root, "diff", "--name-only", "-z", "HEAD")
    return sorted(item for item in output.split("\0") if item)


def describe(root: Path, paths: list[str]) -> str:
    """Evidence for a set of modified paths: what changed and how."""
    if not paths:
        return ""
    parts = [
        _git(root, "diff", "--stat", "--summary", "HEAD", "--", *paths),
        _git(root, "ls-files", "--eol", "--", *paths),
    ]
    for path in paths:
        target = root / path
        if target.exists():
            info = target.stat()
            parts.append(f"{path}: mode {oct(info.st_mode & 0o7777)} uid {getattr(info, 'st_uid', '?')} size {info.st_size}\n")
    return "".join(parts)


def discard_side_effects(root: Path, keep: tuple[str, ...]) -> tuple[list[str], list[str]]:
    """Return (restored paths, paths still modified outside keep)."""
    keep = tuple(keep) + OUTPUT_DIRS
    restore = [path for path in modified_tracked(root) if not _kept(path, keep)]
    if restore:
        print("Build side effects before restore:")
        print(describe(root, restore), end="")
        # Attribute files first: they control how the remaining files are
        # converted when written back.
        attributes = [path for path in restore if Path(path).name == ".gitattributes"]
        others = [path for path in restore if path not in attributes]
        for batch in (attributes, others):
            if batch:
                subprocess.run(["git", "checkout", "HEAD", "--", *batch], cwd=root, check=True)
    for pattern in GENERATED_UNTRACKED:
        for path in root.glob(pattern):
            if not _git(root, "ls-files", "--", path.relative_to(root).as_posix()).strip():
                path.unlink()
    remaining = [path for path in modified_tracked(root) if not _kept(path, keep)]
    return restore, remaining


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m boardwright.ci_cleanup")
    parser.add_argument("--root", default=".")
    parser.add_argument("--keep", nargs="*", default=[], help="Tracked paths the workflow commits itself.")
    args = parser.parse_args(argv)
    restored, remaining = discard_side_effects(Path(args.root), tuple(args.keep))
    for path in restored:
        print(f"restored {path}")
    if remaining:
        for path in remaining:
            print(f"ERROR still modified: {path}", file=sys.stderr)
        print("State of files still modified after restore:", file=sys.stderr)
        print(describe(Path(args.root), remaining), end="", file=sys.stderr)
        return 1
    print(f"Source side effects discarded ({len(restored)} file(s) restored).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
