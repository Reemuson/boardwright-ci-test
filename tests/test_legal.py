import unittest
from pathlib import Path

from boardwright.config import BoardwrightConfig
from boardwright.legal import render_notice


class LegalTests(unittest.TestCase):
    def test_notice_contains_license_and_safety(self) -> None:
        config = BoardwrightConfig(
            root=Path("."),
            project={"project": {"name": "Test Board"}},
            branches={"branches": {}},
            legal={
                "legal": {
                    "hardware_license": "CERN-OHL-W-2.0",
                    "branding_reserved": True,
                    "compatibility": {"enabled": False},
                    "safety_notice": "Verify safety before use.",
                }
            },
            revision_history={"revision_history": {}},
        )

        notice = render_notice(config)

        self.assertIn("CERN-OHL-W-2.0", notice)
        self.assertIn("Verify safety before use.", notice)
        self.assertIn("not legal advice", notice)


if __name__ == "__main__":
    unittest.main()
