import unittest
from pathlib import Path

from boardwright.accepted import AcceptedRun, evaluate_accepted_state
from boardwright.config import BoardwrightConfig
from boardwright.preview import PreviewRun, evaluate_preview_state
from boardwright.status import ProjectStatus
from boardwright.validation import ValidationIssue
from boardwright.workflow_state import action_state, build_workflow_state


class WorkflowStateTests(unittest.TestCase):
    def test_validation_blocked(self) -> None:
        workflow = build_workflow_state(
            _config(),
            _status(),
            (ValidationIssue("error", "Broken"),),
            "ready for dry-run",
        )

        self.assertEqual("validation_blocked", workflow.stage)
        self.assertEqual("Fix validation", workflow.next_action)
        self.assertFalse(action_state(workflow, "Commit + Push").enabled)

    def test_needs_changelog(self) -> None:
        workflow = build_workflow_state(
            _config(),
            _status(dirty_count=1, unreleased_changes=False),
            (),
            "ready for dry-run",
        )

        self.assertEqual("needs_changelog", workflow.stage)
        self.assertEqual("Record Changes", workflow.next_action)

    def test_ready_to_commit(self) -> None:
        workflow = build_workflow_state(
            _config(),
            _status(dirty_count=1, unreleased_changes=True),
            (),
            "ready for dry-run",
        )

        self.assertEqual("ready_to_commit", workflow.stage)
        self.assertTrue(action_state(workflow, "Commit + Push").enabled)

    def test_needs_push(self) -> None:
        workflow = build_workflow_state(
            _config(),
            _status(ahead=1, unreleased_changes=True),
            (),
            "ready for dry-run",
        )

        self.assertEqual("needs_push", workflow.stage)
        self.assertTrue(action_state(workflow, "Commit + Push").enabled)

    def test_stale_preview(self) -> None:
        run = PreviewRun(
            database_id="1",
            status="completed",
            conclusion="success",
            branch="dev",
            head_sha="old",
            created_at="2026-05-23T00:00:00Z",
            title="preview",
        )
        preview = evaluate_preview_state((run,), "new", "CHECKED")
        workflow = build_workflow_state(
            _config(),
            _status(unreleased_changes=True),
            (),
            "ready for dry-run",
            preview,
        )

        self.assertEqual("preview_stale", workflow.stage)
        self.assertFalse(action_state(workflow, "Accept to Main").enabled)

    def test_reviewed_preview_enables_accept(self) -> None:
        run = PreviewRun(
            database_id="1",
            status="completed",
            conclusion="success",
            branch="dev",
            head_sha="same",
            created_at="2026-05-23T00:00:00Z",
            title="preview",
        )
        preview = evaluate_preview_state((run,), "same", "CHECKED")
        preview = type(preview)(**{**preview.__dict__, "reviewed": True})
        workflow = build_workflow_state(
            _config(),
            _status(unreleased_changes=True),
            (),
            "not ready yet",
            preview,
        )

        self.assertEqual("preview_reviewed", workflow.stage)
        self.assertTrue(action_state(workflow, "Accept to Main").enabled)

    def test_release_ready(self) -> None:
        run = PreviewRun(
            database_id="1",
            status="completed",
            conclusion="success",
            branch="dev",
            head_sha="same",
            created_at="2026-05-23T00:00:00Z",
            title="preview",
        )
        preview = evaluate_preview_state((run,), "same", "CHECKED")
        preview = type(preview)(**{**preview.__dict__, "reviewed": True})
        accepted_run = AcceptedRun(
            database_id="2",
            status="completed",
            conclusion="success",
            branch="main",
            head_sha="mainsha",
            created_at="2026-05-23T00:00:00Z",
            title="accepted",
        )
        accepted = evaluate_accepted_state((accepted_run,), "mainsha", "main-outputs.yaml")
        workflow = build_workflow_state(
            _config(),
            _status(unreleased_changes=True),
            (),
            "ready for dry-run",
            preview,
            accepted,
        )

        self.assertEqual("release_ready", workflow.stage)
        self.assertTrue(action_state(workflow, "Create Release").enabled)

    def test_reviewed_preview_without_accepted_main_does_not_enable_release(self) -> None:
        run = PreviewRun(
            database_id="1",
            status="completed",
            conclusion="success",
            branch="dev",
            head_sha="same",
            created_at="2026-05-23T00:00:00Z",
            title="preview",
        )
        preview = evaluate_preview_state((run,), "same", "CHECKED")
        preview = type(preview)(**{**preview.__dict__, "reviewed": True})
        workflow = build_workflow_state(
            _config(),
            _status(unreleased_changes=True),
            (),
            "ready for dry-run",
            preview,
        )

        self.assertEqual("preview_reviewed", workflow.stage)
        self.assertFalse(action_state(workflow, "Create Release").enabled)


def _config() -> BoardwrightConfig:
    return BoardwrightConfig(
        root=Path("."),
        project={
            "project": {"id": "TEST", "name": "Test"},
            "variants": {"dev_default": "DRAFT", "main_default": "CHECKED"},
            "outputs": {"main_workflow": "main-outputs.yaml"},
        },
        branches={"branches": {"development": "dev", "release": "main"}},
        legal={"legal": {}},
        revision_history={"revision_history": {}},
    )


def _status(
    dirty_count: int = 0,
    ahead: int = 0,
    behind: int = 0,
    unreleased_changes: bool = False,
    branch: str = "dev",
) -> ProjectStatus:
    return ProjectStatus(
        project_id="TEST",
        project_name="Test",
        branch=branch,
        dirty_count=dirty_count,
        ahead=ahead,
        behind=behind,
        latest_tag=None,
        unreleased_changes=unreleased_changes,
        variant="DRAFT",
    )


if __name__ == "__main__":
    unittest.main()
