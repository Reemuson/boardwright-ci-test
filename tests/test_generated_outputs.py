from __future__ import annotations

import unittest
import shutil
import uuid
from pathlib import Path

from boardwright.generated_outputs import clean_generated_outputs, format_cleanup_summary


class GeneratedOutputCleanupTests(unittest.TestCase):
    def make_root(self) -> Path:
        root = Path("tests") / ".tmp_generated_outputs" / uuid.uuid4().hex
        root.mkdir(parents=True)
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        self.addCleanup(lambda: shutil.rmtree(root.parent, ignore_errors=True))
        return root

    def test_removes_numbered_pdf_pages_when_combined_pdf_exists(self) -> None:
        root = self.make_root()
        output_dir = root / "Schematic"
        output_dir.mkdir()
        combined = output_dir / "boardwright-schematic.pdf"
        page = output_dir / "boardwright-schematic-1.pdf"
        unrelated = output_dir / "other-1.pdf"
        combined.write_bytes(b"%PDF combined")
        page.write_bytes(b"%PDF page")
        unrelated.write_bytes(b"%PDF unrelated")

        cleanup = clean_generated_outputs(root)

        self.assertTrue(combined.exists())
        self.assertFalse(page.exists())
        self.assertTrue(unrelated.exists())
        self.assertEqual((page.resolve(),), cleanup.removed_pdf_pages)

    def test_removes_empty_generated_csv_tables(self) -> None:
        root = self.make_root()
        testing = root / "Testing"
        testing.mkdir()
        empty_testpoints = testing / "board-testpoints.csv"
        empty_component_count = testing / "board-components_count.csv"
        populated_testpoints = testing / "board-testpoints-top.csv"
        bom = testing / "board-bom.csv"
        empty_testpoints.write_text("Ref,Side\n", encoding="utf-8")
        empty_component_count.write_text("\n", encoding="utf-8")
        populated_testpoints.write_text("Ref,Side\nTP1,F\n", encoding="utf-8")
        bom.write_text("Ref,Value\n", encoding="utf-8")

        cleanup = clean_generated_outputs(root)

        self.assertFalse(empty_testpoints.exists())
        self.assertFalse(empty_component_count.exists())
        self.assertTrue(populated_testpoints.exists())
        self.assertTrue(bom.exists())
        self.assertEqual(2, len(cleanup.removed_empty_tables))

    def test_format_cleanup_summary_lists_removed_files(self) -> None:
        root = self.make_root()
        schematic = root / "Schematic"
        schematic.mkdir()
        (schematic / "board.pdf").write_bytes(b"%PDF")
        (schematic / "board-1.pdf").write_bytes(b"%PDF")

        summary = format_cleanup_summary(clean_generated_outputs(root))

        self.assertIn("removed 1 file(s)", summary)
        self.assertIn("Schematic", summary)


if __name__ == "__main__":
    unittest.main()
