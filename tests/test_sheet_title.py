import unittest
from pathlib import Path

from kibot_resources.scripts.get_sheet_title import (
    _title_from_schematic,
    get_sheet_title,
)


class SheetTitleTests(unittest.TestCase):
    def test_falls_back_to_kicad_schematic(self) -> None:
        title = _title_from_schematic(Path("boardwright.kicad_sch"), 6, 8)

        self.assertEqual("Power - Sequencing", title)

    def test_missing_sheet_returns_dots(self) -> None:
        title = _title_from_schematic(Path("boardwright.kicad_sch"), 99, 8)

        self.assertEqual("........", title)

    def test_finds_nested_sheet_a(self) -> None:
        title = _title_from_schematic(Path("boardwright.kicad_sch"), 4, 8)

        self.assertEqual("Section A - Title A", title)

    def test_finds_nested_sheet_b(self) -> None:
        title = _title_from_schematic(Path("boardwright.kicad_sch"), 5, 8)

        self.assertEqual("Section B - Title B", title)

    def test_get_sheet_title_accepts_kicad_sch(self) -> None:
        # Smoke test: should not require an intermediate XML file.
        get_sheet_title("boardwright.kicad_sch", 6, 8)


if __name__ == "__main__":
    unittest.main()
