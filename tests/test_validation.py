import unittest
from pathlib import Path

from boardwright.config import BoardwrightConfig
from boardwright.validation import _has_edge_cuts_geometry, validate_project


class ValidationTests(unittest.TestCase):
    def test_validates_variant_values(self) -> None:
        config = BoardwrightConfig(
            root=Path("."),
            project={
                "project": {
                    "id": "TEST",
                    "name": "Test",
                    "company": "Company",
                    "designer": "Designer",
                },
                "variants": {
                    "dev_default": "FAST",
                    "preview_default": "CHECKED",
                    "main_default": "CHECKED",
                    "release_default": "RELEASED",
                },
                "outputs": {"preview_engine": "github-actions"},
            },
            branches={"branches": {}},
            legal={"legal": {}},
            revision_history={"revision_history": {}},
        )

        issues = validate_project(config)

        self.assertTrue(
            any("Unsupported variants.dev_default" in issue.message for issue in issues)
        )

    def test_warns_when_pcb_has_no_edge_cuts_geometry(self) -> None:
        self.assertFalse(_has_edge_cuts_geometry(Path("boardwright.kicad_pcb")))

    def test_accepts_edge_cuts_geometry(self) -> None:
        path = Path("tests/fixtures_edge_cuts_geometry.kicad_pcb")
        try:
            path.write_text(
                '(kicad_pcb (gr_rect (start 0 0) (end 1 1) (layer "Edge.Cuts")))',
                encoding="utf-8",
            )
            self.assertTrue(_has_edge_cuts_geometry(path))
        finally:
            if path.exists():
                path.unlink()


if __name__ == "__main__":
    unittest.main()
