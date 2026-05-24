import unittest

from boardwright.accepted import AcceptedMainState
from boardwright import tui
from boardwright.config import load_config
from boardwright.preview import PreviewRun, evaluate_preview_state
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

    def test_issue_summary(self) -> None:
        self.assertEqual("validation ok", tui._issue_summary(()))
        self.assertIn(
            "warning",
            tui._issue_summary((ValidationIssue("warning", "Careful"),)),
        )

    def test_timeline_contains_release_steps(self) -> None:
        state = tui.collect_dashboard_state()
        text = tui._format_timeline(tui._workflow_steps(state)).plain

        self.assertIn("Edit in KiCad", text)
        self.assertIn("Record changes", text)
        self.assertIn("Preview CI", text)
        self.assertIn("Accept to main", text)
        self.assertEqual(tui._workflow_steps(state), state.workflow.steps)

    def test_inspector_shows_next_action(self) -> None:
        state = tui.collect_dashboard_state()
        text = tui._format_inspector(state)

        self.assertTrue(text.strip())
        self.assertIn("Latest CI", text)
        self.assertIn("Preview runs from dev pushes", text)
        self.assertIn("Stage:", text)

    def test_ci_status_shortens(self) -> None:
        self.assertEqual("CI not polled", tui._ci_status_short("CI not polled"))
        self.assertLessEqual(len(tui._ci_status_short("x" * 80)), 36)

    def test_top_status_is_rich_text(self) -> None:
        state = tui.collect_dashboard_state()

        self.assertTrue(tui._format_top_status(state.status, state.issues, "CI not polled").plain)

    def test_review_artifact_summary_contains_evidence(self) -> None:
        run = PreviewRun(
            database_id="42",
            status="completed",
            conclusion="success",
            branch="dev",
            head_sha="abcdef",
            created_at="2026-05-23T00:00:00Z",
            title="preview",
        )
        preview_state = evaluate_preview_state((run,), "abcdef", "CHECKED")

        text = tui._format_review_artifacts(preview_state, "Boardwright Dev Preview: completed/success")

        self.assertIn("Artifact: boardwright-preview-CHECKED", text)
        self.assertIn("Recent runs", text)
        self.assertIn("Run: 42", text)

    def test_review_artifact_summary_shows_selected_variant_artifact(self) -> None:
        run = PreviewRun(
            database_id="42",
            status="completed",
            conclusion="success",
            branch="dev",
            head_sha="abcdef",
            created_at="2026-05-23T00:00:00Z",
            title="preview",
        )
        preview_state = evaluate_preview_state((run,), "abcdef", "PRELIMINARY")

        text = tui._format_review_artifacts(preview_state, "Boardwright Dev Preview: completed/success")

        self.assertIn("Artifact: boardwright-preview-PRELIMINARY", text)

    def test_download_progress_text_mentions_variant(self) -> None:
        text = tui._download_progress_text("PRELIMINARY")

        self.assertIn("boardwright-preview-PRELIMINARY", text)
        self.assertIn("[##########]", text)

    def test_release_checklist_blocks_unready_accepted_outputs(self) -> None:
        accepted_state = AcceptedMainState(
            state="stale",
            workflow="main-outputs.yaml",
            expected_sha="abcdef",
            message="Accepted outputs are stale.",
        )

        checklist = tui.build_release_checklist(
            load_config(),
            "0.1.2",
            "RELEASED",
            "release",
            accepted_state,
        )

        text = tui._format_release_checklist(checklist)
        self.assertFalse(checklist.can_dispatch)
        self.assertIn("[ ] Accepted main outputs", text)
        self.assertIn("Resolve blocking items", text)

    def test_release_checklist_reports_invalid_release_inputs(self) -> None:
        accepted_state = AcceptedMainState(
            state="ready",
            workflow="main-outputs.yaml",
            expected_sha="abcdef",
            message="Accepted main outputs are fresh.",
        )

        checklist = tui.build_release_checklist(
            load_config(),
            "v0.1.2",
            "RELEASED",
            "release",
            accepted_state,
        )

        text = tui._format_release_checklist(checklist)
        self.assertFalse(checklist.can_dispatch)
        self.assertIn("Release version must use semantic form", text)
        self.assertIn("[ ] Release inputs", text)


if __name__ == "__main__":
    unittest.main()
