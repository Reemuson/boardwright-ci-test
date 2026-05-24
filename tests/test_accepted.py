import unittest

from boardwright.accepted import (
    AcceptedRun,
    evaluate_accepted_state,
    format_accepted_state,
    parse_accepted_runs,
)


class AcceptedMainTests(unittest.TestCase):
    def test_parse_accepted_runs(self) -> None:
        runs = parse_accepted_runs(
            """
            [
              {
                "databaseId": 11,
                "status": "completed",
                "conclusion": "success",
                "headBranch": "main",
                "headSha": "abc123",
                "createdAt": "2026-05-23T00:00:00Z",
                "displayTitle": "accepted"
              }
            ]
            """
        )

        self.assertEqual(1, len(runs))
        self.assertEqual("11", runs[0].database_id)
        self.assertEqual("abc123", runs[0].head_sha)

    def test_accepted_state_ready(self) -> None:
        run = _accepted_run("11", "completed", "success", "abcdef")

        state = evaluate_accepted_state((run,), "abcdef", "main-outputs.yaml")

        self.assertEqual("ready", state.state)
        self.assertTrue(state.ready)
        self.assertIn("Workflow: main-outputs.yaml", format_accepted_state(state))

    def test_accepted_state_stale(self) -> None:
        run = _accepted_run("11", "completed", "success", "old")

        state = evaluate_accepted_state((run,), "new", "main-outputs.yaml")

        self.assertEqual("stale", state.state)
        self.assertFalse(state.ready)

    def test_accepted_state_failed(self) -> None:
        run = _accepted_run("11", "completed", "failure", "abcdef")

        state = evaluate_accepted_state((run,), "abcdef", "main-outputs.yaml")

        self.assertEqual("failed", state.state)

    def test_accepted_state_running(self) -> None:
        run = _accepted_run("11", "in_progress", "", "abcdef")

        state = evaluate_accepted_state((run,), "abcdef", "main-outputs.yaml")

        self.assertEqual("running", state.state)

    def test_accepted_state_missing_without_origin_main(self) -> None:
        state = evaluate_accepted_state((), "", "main-outputs.yaml")

        self.assertEqual("missing", state.state)
        self.assertIn("origin/main", state.message)


def _accepted_run(
    database_id: str,
    status: str,
    conclusion: str,
    head_sha: str,
) -> AcceptedRun:
    return AcceptedRun(
        database_id=database_id,
        status=status,
        conclusion=conclusion,
        branch="main",
        head_sha=head_sha,
        created_at="2026-05-23T00:00:00Z",
        title="accepted",
    )


if __name__ == "__main__":
    unittest.main()
