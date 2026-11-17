# SPDX-License-Identifier: LicenseRef-Boardwright-Source-Available
# Copyright (c) Ryan Hicks

from __future__ import annotations

import zipfile
from dataclasses import dataclass
from pathlib import Path

from .errors import BoardwrightError


DEFAULT_EXCLUDED_DIRS = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    "HTML",
    "KiRI",
    "Manufacturing",
    "Reports",
    "Schematic",
    "Testing",
}
DEFAULT_EXCLUDED_SUFFIXES = {".log", ".zip"}
DEFAULT_EXCLUDED_PATHS = {
    ".boardwright/sheet_titles.env",
}


@dataclass(frozen=True)
class SourcePackage:
    path: Path
    file_count: int


def build_source_package(
    root: Path,
    output: Path,
    *,
    prefix: str | None = None,
    force: bool = False,
) -> SourcePackage:
    if output.exists() and not force:
        raise BoardwrightError(f"Refusing to overwrite existing file: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    prefix = _package_prefix(root, prefix)

    file_count = 0
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            if _should_exclude(path, root):
                continue
            archive.write(path, f"{prefix}/{path.relative_to(root).as_posix()}")
            file_count += 1
    return SourcePackage(output, file_count)


def _package_prefix(root: Path, prefix: str | None) -> str:
    candidate = (prefix or root.name or "boardwright-source").strip().strip("/")
    return candidate or "boardwright-source"


def _should_exclude(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    relative_text = relative.as_posix()
    if relative_text in DEFAULT_EXCLUDED_PATHS:
        return True
    parts = relative.parts
    if any(part in DEFAULT_EXCLUDED_DIRS for part in parts):
        return True
    if path.suffix.lower() in DEFAULT_EXCLUDED_SUFFIXES:
        return True
    if relative_text.startswith("assets/renders/"):
        return True
    if relative_text.startswith("assets/3d/"):
        return True
    return False
