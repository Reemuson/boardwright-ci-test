import unittest
from pathlib import Path

from boardwright.config import BoardwrightConfig
from boardwright.testbench import (
    _ignore_template_artifacts,
    build_testbench_plan,
    default_testbench_target,
    format_testbench_plan,
)


def _config() -> BoardwrightConfig:
    return BoardwrightConfig(
        root=Path("Boardwright").resolve(),
        project={"project": {"id": "TEST", "name": "Test"}},
        branches={"branches": {"development": "dev", "release": "main"}},
        legal={"legal": {}},
        revision_history={"revision_history": {}},
    )


class TestbenchTests(unittest.TestCase):
    def test_default_target_is_sibling(self) -> None:
        target = default_testbench_target(_config())

        self.assertEqual("Boardwright-testbench", target.name)

    def test_plan_includes_live_workflow_commands(self) -> None:
        plan = build_testbench_plan(_config(), github_repo="owner/live-test")
        text = format_testbench_plan(plan)

        self.assertIn("owner/live-test", text)
        self.assertIn("boardwright preview --variant PRELIMINARY --dispatch", text)
        self.assertIn("boardwright review --variant PRELIMINARY --fetch", text)
        self.assertIn("boardwright release 0.1.0", text)

    def test_copy_ignore_excludes_generated_outputs(self) -> None:
        ignored = _ignore_template_artifacts(
            "Boardwright",
            [
                ".git",
                "boardwright-preview",
                "Manufacturing",
                "src",
                "boardwright.egg-info",
                "boardwright-0.1.0-release.zip",
            ],
        )

        self.assertIn(".git", ignored)
        self.assertIn("boardwright-preview", ignored)
        self.assertIn("Manufacturing", ignored)
        self.assertIn("boardwright.egg-info", ignored)
        self.assertIn("boardwright-0.1.0-release.zip", ignored)
        self.assertNotIn("src", ignored)


if __name__ == "__main__":
    unittest.main()
