import argparse
import re
import xml.etree.ElementTree as ET
from pathlib import Path

def get_sheet_title(file_path, page_number, dots_number):
    file_path = Path(file_path)
    page_number = str(page_number)
    if file_path.suffix == ".kicad_sch":
        print(_title_from_schematic(file_path, page_number, dots_number))
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
        print(_title_from_schematic(file_path.with_suffix(".kicad_sch"), page_number, dots_number))
    except Exception as e:
        print('.'*dots_number)


def _title_from_schematic(file_path, page_number, dots_number):
    titles = _titles_from_schematic(file_path, page_number, set())
    if not titles:
        return '.' * dots_number
    if len(set(titles)) > 1:
        return "Conflicting page numbers"
    return titles[0]


def _titles_from_schematic(file_path, page_number, seen):
    if not file_path.exists():
        return []

    resolved_path = file_path.resolve()
    if resolved_path in seen:
        return []
    seen.add(resolved_path)

    text = file_path.read_text(encoding="utf-8", errors="replace")
    titles = []
    sheet_blocks = _sheet_blocks(text)
    for block in sheet_blocks:
        name = _property_value(block, "Sheetname")
        sheet_file = _property_value(block, "Sheetfile")
        if _block_has_page(block, page_number) and name:
            titles.append(name)
        if sheet_file:
            titles.extend(_titles_from_schematic(file_path.parent / sheet_file, page_number, seen))
    return titles


def _sheet_blocks(text):
    return text.split("\n\t(sheet\n")[1:]


def _block_has_page(block, page_number):
    return str(page_number) in re.findall(r'\(page "([^"]+)"\)', block)


def _property_value(block, property_name):
    marker = f'(property "{property_name}" "'
    start = block.find(marker)
    if start < 0:
        return ""
    start += len(marker)
    end = block.find('"', start)
    if end < 0:
        return ""
    return block[start:end]


def main():
    parser = argparse.ArgumentParser(description="Get the sheet title based on page number from a KiCad schematic")
    parser.add_argument("-p", "--page-number", type=int, required=True, help="Page number to search")
    parser.add_argument("-f", "--file", type=str, required=True, help="Path to the schematic file")
    parser.add_argument("-d", "--dots-number", type=int, required=True, help="Number of dots for empty lines")

    args = parser.parse_args()
    get_sheet_title(args.file, args.page_number, args.dots_number)


if __name__ == "__main__":
    main()
