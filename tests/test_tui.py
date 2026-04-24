import unittest

from boardwright import tui
from boardwright.validation import ValidationIssue


class TuiTests(unittest.TestCase):
    def test_textual_is_optional(self) -> None:
        self.assertIsInstance(tui.textual_available(), bool)
        self.assertIn("pip install", tui.INSTALL_HINT)

    def test_dashboard_state_collects(self) -> None:
        state = tui.collect_dashboard_state()

        self.assertTrue(state.status.project_id)
        self.assertIn("->", state.preview_summary)
        self.assertIsInstance(state.changed_files, tuple)

    def test_notification_severity(self) -> None:
        self.assertEqual(
            "warning",
            tui._notification_severity((ValidationIssue("warning", "Careful"),)),
        )
        self.assertEqual(
            "error",
            tui._notification_severity((ValidationIssue("error", "Broken"),)),
        )


if __name__ == "__main__":
    unittest.main()
