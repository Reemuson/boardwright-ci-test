import unittest
from pathlib import Path


class KiBotCiTests(unittest.TestCase):
    def test_pdf_outputs_do_not_require_custom_kicad_theme(self) -> None:
        for path in (
            Path("boardwright_resources/kibot/yaml/kibot_out_pdf_schematic.yaml"),
            Path("boardwright_resources/kibot/yaml/kibot_out_pdf_fabrication.yaml"),
            Path("boardwright_resources/kibot/yaml/kibot_out_pdf_assembly.yaml"),
            Path("boardwright_resources/kibot/yaml/kibot_main.yaml"),
        ):
            text = path.read_text(encoding="utf-8")

            self.assertNotIn("color_theme:", text, str(path))
            self.assertNotIn("KiCad_Theme", text, str(path))

    def test_pcb_template_does_not_require_arial(self) -> None:
        pcb = Path("boardwright.kicad_pcb").read_text(encoding="utf-8")

        self.assertNotIn('face "Arial"', pcb)

    def test_pdf_outputs_do_not_embed_generated_tables(self) -> None:
        for path in (
            Path("boardwright_resources/kibot/yaml/kibot_out_pdf_fabrication.yaml"),
            Path("boardwright_resources/kibot/yaml/kibot_out_pdf_assembly.yaml"),
        ):
            text = path.read_text(encoding="utf-8")

            self.assertNotIn("include_table:", text, str(path))
            self.assertNotIn("NAME_IMPEDANCE_TABLE", text, str(path))
            self.assertNotIn("NAME_COMP_COUNT", text, str(path))

    def test_fabrication_pdf_does_not_embed_drill_pair_page(self) -> None:
        fabrication = Path(
            "boardwright_resources/kibot/yaml/kibot_out_pdf_fabrication.yaml"
        ).read_text(encoding="utf-8")
        main = Path("boardwright_resources/kibot/yaml/kibot_main.yaml").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("repeat_layers: 'drill_pairs'", fabrication)
        self.assertNotIn("repeat_layers: 'drill_pairs'", main)

    def test_impedance_table_is_not_generated_by_default(self) -> None:
        main = Path("boardwright_resources/kibot/yaml/kibot_main.yaml").read_text(
            encoding="utf-8"
        )
        include_table = Path(
            "boardwright_resources/kibot/yaml/kibot_pre_include_table.yaml"
        ).read_text(encoding="utf-8")

        self.assertNotIn("CSV_IMPEDANCE_TABLE_OUTPUT", main)
        self.assertNotIn("impedance_table", main)
        self.assertNotIn("NAME_IMPEDANCE_TABLE", include_table)
        self.assertNotIn("csv_impedance_table", include_table)

    def test_fabrication_pdf_has_non_crashing_drill_and_notes_pages(self) -> None:
        fabrication = Path(
            "boardwright_resources/kibot/yaml/kibot_out_pdf_fabrication.yaml"
        ).read_text(encoding="utf-8")

        self.assertIn("sheet: 'DRILL TABLE'", fabrication)
        self.assertIn("layer: '@LAYER_DRILL_MAP@'", fabrication)
        self.assertIn("sheet: 'FABRICATION NOTES'", fabrication)
        self.assertIn("layer: '@LAYER_FAB_NOTES@'", fabrication)
        self.assertNotIn("draw_drill_map", fabrication)


if __name__ == "__main__":
    unittest.main()
