# SPDX-License-Identifier: MIT
# Original template material copyright Nguyen Vincent.
# Modified for Boardwright by Ryan Hicks.

import argparse
import xml.etree.ElementTree as ET
from pathlib import Path

from boardwright.combined_pdf import read_combined_sheet_title, read_combined_sheet_total
from boardwright.sheet_titles import (
    collect_actual_sheet_titles,
    read_sheet_title_overrides,
    _title_from_schematic as _resolve_title,
)

def get_sheet_title(file_path, page_number, dots_number):
    file_path = Path(file_path)
    page_number = str(page_number)
    override = _override_sheet_title(file_path, page_number)
    if override is not None:
        print(override)
        return
    if file_path.suffix == ".kicad_sch":
        print(_resolve_title(file_path, int(page_number), dots_number, file_path.parent))
        return

    try:
        tree = ET.parse(file_path)
        root = tree.getroot()
        
        titles = []

        for sheet in root.findall(".//sheet"):
            number = sheet.get("number")
            if number == page_number:
                # Get the last part of the 'name' attribute after '/'
                name = sheet.get("name")
                title_block = sheet.find("title_block")
                title = title_block.find("title").text if title_block is not None else None
                if name:
                    titles.append(name.split("/")[-2 if name.endswith("/") else -1])
        
        if not titles:
            print('.'*dots_number)

        elif len(set(titles)) > 1:
            print("Conflicting page numbers")
        else:
            print(titles[0])
    except ET.ParseError:
        print('.'*dots_number)
    except FileNotFoundError:
        print(_resolve_title(file_path.with_suffix(".kicad_sch"), int(page_number), dots_number, file_path.parent))
    except Exception:
        print('.'*dots_number)


def _title_from_schematic(file_path, page_number, dots_number):
    override = _override_sheet_title(Path(file_path), page_number)
    if override is not None:
        return override
    return _resolve_title(Path(file_path), int(page_number), dots_number, Path(file_path).parent)


def _override_sheet_title(file_path: Path, page_number: str | int) -> str | None:
    try:
        combined = read_combined_sheet_title(file_path.parent, int(page_number))
    except (TypeError, ValueError):
        combined = None
    if combined is not None:
        return combined
    overrides = read_sheet_title_overrides(file_path.parent)
    try:
        return overrides.get(int(page_number))
    except (TypeError, ValueError):
        return None



def _trimmed_title_count(titles):
    trimmed = list(titles)
    while trimmed and set(trimmed[-1]) == {"."}:
        trimmed.pop()
    return max(1, len(trimmed))

def main():
    parser = argparse.ArgumentParser(description="Get the sheet title based on page number from a KiCad schematic")
    parser.add_argument("-p", "--page-number", type=int, required=True, help="Page number to search")
    parser.add_argument("-f", "--file", type=str, required=True, help="Path to the schematic file")
    parser.add_argument("-d", "--dots-number", type=int, required=True, help="Number of dots for empty lines")
    parser.add_argument("--total", action="store_true", help="Print the combined native sheet total")

    args = parser.parse_args()
    if args.total:
        file_path = Path(args.file)
        total = read_combined_sheet_total(file_path.parent)
        if total is None:
            titles = collect_actual_sheet_titles(file_path.parent, schematic=file_path, max_titles=40)
            total = _trimmed_title_count(titles)
        print(total)
        return
    get_sheet_title(args.file, args.page_number, args.dots_number)


if __name__ == "__main__":
    main()
