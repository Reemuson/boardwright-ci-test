# SPDX-License-Identifier: LicenseRef-Boardwright-Source-Available
# Copyright (c) Ryan Hicks

"""Embed PNG logos into KiCad worksheet bitmap blocks."""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from pathlib import Path

from .errors import BoardwrightError


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
DEFAULT_CHUNK_SIZE = 76
DEFAULT_DATA_INDENT = "\t\t"
MAX_DATA_INDENT_CHARS = 32


@dataclass(frozen=True)
class WorksheetLogoResult:
    worksheet: Path
    logo: Path
    bitmap_index: int
    png_width: int
    png_height: int
    encoded_bytes: int


def embed_logo_in_worksheet(
    worksheet: Path,
    logo: Path,
    *,
    bitmap_index: int = 0,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> WorksheetLogoResult:
    """Replace a KiCad worksheet bitmap's PNG payload with `logo`.

    KiCad worksheets store bitmap images as normal PNG bytes encoded as base64
    chunks inside a `(bitmap ... (data "..."))` block. This function only
    replaces the `(data ...)` payload, leaving worksheet position, scale, and
    other drawing attributes untouched.
    """

    worksheet = worksheet.resolve()
    logo = logo.resolve()
    if bitmap_index < 0:
        raise BoardwrightError("Bitmap index must be zero or greater.")
    if not worksheet.is_file():
        raise BoardwrightError(f"Missing worksheet: {worksheet}")
    if not logo.is_file():
        raise BoardwrightError(f"Missing logo PNG: {logo}")

    png = logo.read_bytes()
    width, height = png_dimensions(png)
    text = worksheet.read_text(encoding="utf-8")
    start, end = _nth_bitmap_block(text, bitmap_index)
    block = text[start : end + 1]
    data_start, data_end = _data_block_bounds(block)
    data_line_start, indent = _data_line_start_and_indent(block, data_start)
    updated_data = format_bitmap_data(png, indent=indent, chunk_size=chunk_size)
    updated_block = block[:data_line_start] + updated_data + block[data_end + 1 :]

    worksheet.write_text(text[:start] + updated_block + text[end + 1 :], encoding="utf-8", newline="\n")
    return WorksheetLogoResult(
        worksheet=worksheet,
        logo=logo,
        bitmap_index=bitmap_index,
        png_width=width,
        png_height=height,
        encoded_bytes=len(base64.b64encode(png)),
    )


def embed_logo_in_worksheets(
    worksheets: list[Path],
    logo: Path,
    *,
    bitmap_index: int = 0,
) -> tuple[WorksheetLogoResult, ...]:
    return tuple(
        embed_logo_in_worksheet(path, logo, bitmap_index=bitmap_index)
        for path in worksheets
    )


def format_bitmap_data(
    png: bytes,
    *,
    indent: str = "\t\t",
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> str:
    if chunk_size <= 0:
        raise BoardwrightError("Bitmap data chunk size must be positive.")
    encoded = base64.b64encode(png).decode("ascii")
    chunks = [
        encoded[index : index + chunk_size]
        for index in range(0, len(encoded), chunk_size)
    ]
    if not chunks:
        raise BoardwrightError("Logo PNG is empty.")

    continuation_indent = indent + "\t"
    lines = [f'{indent}(data "{chunks[0]}"']
    lines.extend(f'{continuation_indent}"{chunk}"' for chunk in chunks[1:])
    lines.append(f"{indent})")
    return "\n".join(lines)


# SHA-256 of the Boardwright placeholder logo embedded in the shipped
# Templates/Boardwright_*.kicad_wks (assets/logos/logo-block-black.png).
# tests/test_worksheet_logo.py keeps this in step with the templates.
BOARDWRIGHT_PLACEHOLDER_LOGO_SHA256 = "59a323ca19ba853839487d9fd13e0fa9a8b9fa1a6311fbaa63d738372840c822"


def embedded_png(text: str, bitmap_index: int = 0) -> bytes | None:
    """The PNG embedded in a worksheet bitmap block, or None if absent."""
    import base64

    try:
        start, end = _nth_bitmap_block(text, bitmap_index)
        block = text[start : end + 1]
        data_start, data_end = _data_block_bounds(block)
    except BoardwrightError:
        return None
    chunks = re.findall(r'"([A-Za-z0-9+/=]*)"', block[data_start:data_end])
    try:
        return base64.b64decode("".join(chunks))
    except ValueError:
        return None


def png_dimensions(png: bytes) -> tuple[int, int]:
    if not png.startswith(PNG_SIGNATURE):
        raise BoardwrightError("Logo must be a PNG file.")
    if len(png) < 24 or png[12:16] != b"IHDR":
        raise BoardwrightError("PNG is missing an IHDR header.")
    width = int.from_bytes(png[16:20], "big")
    height = int.from_bytes(png[20:24], "big")
    if width <= 0 or height <= 0:
        raise BoardwrightError("PNG dimensions are invalid.")
    return width, height


def _nth_bitmap_block(text: str, bitmap_index: int) -> tuple[int, int]:
    index = 0
    seen = 0
    while True:
        start = text.find("(bitmap", index)
        if start == -1:
            raise BoardwrightError(f"Worksheet bitmap index {bitmap_index} not found.")
        end = _find_matching_paren(text, start)
        if seen == bitmap_index:
            return start, end
        seen += 1
        index = end + 1


def _data_block_bounds(block: str) -> tuple[int, int]:
    start = block.find("(data ")
    if start == -1:
        raise BoardwrightError("Worksheet bitmap has no data block.")
    return start, _find_matching_paren(block, start)


def _data_line_start_and_indent(text: str, index: int) -> tuple[int, str]:
    line_start = text.rfind("\n", 0, index)
    line_start = 0 if line_start == -1 else line_start + 1
    raw_indent = text[line_start:index]
    if raw_indent.strip():
        return index, DEFAULT_DATA_INDENT
    if 0 < len(raw_indent) <= MAX_DATA_INDENT_CHARS:
        return line_start, raw_indent
    return line_start, DEFAULT_DATA_INDENT


def _find_matching_paren(text: str, start: int) -> int:
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
    raise BoardwrightError("Worksheet contains an unterminated s-expression.")


def git_tracked(root: Path, paths: list[Path]) -> list[Path]:
    """The subset of ``paths`` tracked by Git in ``root`` (none outside a repository)."""
    import subprocess

    tracked: list[Path] = []
    for path in paths:
        try:
            relative = path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            continue
        completed = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", relative],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode == 0:
            tracked.append(path)
    return tracked


def select_logo_targets(
    config,
    logo: Path,
    *,
    explicit: list[Path] | None = None,
    all_templates: bool = False,
    output: Path | None = None,
) -> list[Path]:
    """Worksheets a logo is written into, shared by the CLI and the TUI.

    By default these are the external worksheets the project's drawings
    actually render with. An embedded (``kicad-embed://``) active sheet is an
    error: writing Templates/ would not change the drawings. In the Boardwright
    A logo taken from the untracked ``.boardwright/local.yaml`` is never
    written into Git-tracked worksheets; use ``output`` for personal copies.
    """
    import shutil

    from .drawing_sheets import active_sheet_files, active_sheet_refs

    root = config.root
    template_worksheet = str(config.template.get("worksheet") or "")
    if explicit:
        targets = list(explicit)
    elif all_templates:
        targets = sorted((root / "Templates").glob("*.kicad_wks"))
    else:
        embedded = [
            f"{document} ({ref.setting})"
            for document, ref in active_sheet_refs(root, template_worksheet).items()
            if ref.kind == "embedded"
        ]
        if embedded:
            raise BoardwrightError(
                "These drawings render a worksheet embedded in the design file, so a logo written to "
                "Templates/ would not reach them: " + ", ".join(sorted(set(embedded)))
                + ". Run `boardwright worksheet adopt` first, or pass --worksheet explicitly."
            )
        targets = active_sheet_files(root, template_worksheet)
    if not targets:
        raise BoardwrightError("No worksheet files selected.")
    missing = [path for path in targets if not path.is_file()]
    if missing:
        raise BoardwrightError("Missing worksheet: " + ", ".join(str(path) for path in missing))
    if not logo.is_file():
        raise BoardwrightError(f"Missing logo PNG: {logo}")

    if output is not None:
        output.mkdir(parents=True, exist_ok=True)
        copies = []
        for path in targets:
            copy = output / path.name
            if copy.resolve() != path.resolve():
                shutil.copyfile(path, copy)
            copies.append(copy)
        return copies

    if "template.sheet_image" in tuple(getattr(config, "local_overrides", ()) or ()) and logo.resolve() == _configured_path(config):
        tracked = git_tracked(root, targets)
        if tracked:
            raise BoardwrightError(
                "Refusing to write the logo from .boardwright/local.yaml (a personal, untracked override) into "
                "tracked worksheets: " + ", ".join(path.name for path in tracked)
                + ". Use --output <untracked dir> for branded copies, or declare the logo in project.yaml."
            )
    return targets


def bitmap_payloads(text: str) -> list[tuple[int, int]]:
    """(start, end) of every bitmap ``(data ...)`` block, in order."""
    spans: list[tuple[int, int]] = []
    index = 0
    while True:
        start = text.find("(bitmap", index)
        if start == -1:
            return spans
        end = _find_matching_paren(text, start)
        block = text[start : end + 1]
        try:
            data_start, data_end = _data_block_bounds(block)
        except BoardwrightError:
            index = end + 1
            continue
        spans.append((start + data_start, start + data_end + 1))
        index = end + 1


def transplant_bitmaps(donor: str, recipient: str) -> str | None:
    """``recipient`` with each bitmap payload replaced by ``donor``'s.

    Used by `boardwright update`: the worksheet logo is project-owned, so the
    project's images are carried into the upstream texts before a three-way
    merge, and only the layout is merged. None when the bitmap counts differ.
    """
    donor_spans = bitmap_payloads(donor)
    recipient_spans = bitmap_payloads(recipient)
    if len(donor_spans) != len(recipient_spans):
        return None
    result = recipient
    for (d_start, d_end), (r_start, r_end) in reversed(list(zip(donor_spans, recipient_spans))):
        result = result[:r_start] + donor[d_start:d_end] + result[r_end:]
    return result


def _configured_path(config) -> Path:
    value = Path(str(getattr(config, "template_sheet_image", "") or ""))
    return (value if value.is_absolute() else Path(config.root) / value).resolve()
