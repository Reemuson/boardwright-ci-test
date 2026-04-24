import argparse
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
    if not file_path.exists():
        return '.' * dots_number

    text = file_path.read_text(encoding="utf-8", errors="replace")
    sheet_blocks = text.split("\n\t(sheet\n")[1:]
    for block in sheet_blocks:
        if f'(page "{page_number}")' not in block:
            continue
        name = _property_value(block, "Sheetname")
        if name:
            return name
    return '.' * dots_number


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
