import unittest
from pathlib import Path


class KiBotRevisionHistoryTests(unittest.TestCase):
    def test_preflight_defines_revision_history_ceiling(self) -> None:
        text = Path("kibot_yaml/kibot_pre_set_text_variables.yaml").read_text(
            encoding="utf-8"
        )

        self.assertIn("REVHIST_1_TITLE", text)
        self.assertIn("REVHIST_12_BODY", text)
        self.assertNotIn("RELEASE_BODY_VAR", text)

    def test_revision_sheet_uses_title_body_slots(self) -> None:
        text = Path("revision-history.kicad_sch").read_text(encoding="utf-8")

        self.assertIn("${REVHIST_1_TITLE}", text)
        self.assertIn("${REVHIST_4_BODY}", text)
        self.assertNotIn("RELEASE_BODY_", text)


if __name__ == "__main__":
    unittest.main()
