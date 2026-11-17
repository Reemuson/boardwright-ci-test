# SPDX-License-Identifier: LicenseRef-Boardwright-Source-Available
# Copyright (c) Ryan Hicks

"""Embed PNG logos into KiCad worksheet bitmap blocks."""

from __future__ import annotations

import base64
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
