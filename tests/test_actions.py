import unittest
from pathlib import Path

from boardwright.actions import (
    build_prepare_release_action,
    build_promote_action,
)
from boardwright.config import BoardwrightConfig
from boardwright.errors import BoardwrightError


def _config() -> BoardwrightConfig:
    return BoardwrightConfig(
        root=Path("."),
        project={
            "project": {
                "id": "TEST",
                "name": "Test",
                "github_repo": "owner/repo",
            },
            "variants": {"dev_default": "DRAFT"},
            "outputs": {
                "main_workflow": "main-outputs.yaml",
                "prepare_release_workflow": "prepare-release.yaml",
            },
        },
        branches={"branches": {"release": "main"}},
        legal={"legal": {}},
        revision_history={"revision_history": {}},
    )


class ActionTests(unittest.TestCase):
    def test_build_promote_action(self) -> None:
        action = build_promote_action(_config(), "checked")

        self.assertEqual("promote", action.name)
        self.assertEqual("main-outputs.yaml", action.workflow)
        self.assertEqual("main", action.ref)
        self.assertIn(("variant", "CHECKED"), action.fields)
        self.assertIn(("commit_outputs", "true"), action.fields)
        self.assertIn("--repo", action.command)
        self.assertIn("owner/repo", action.command)

    def test_build_prepare_release_action(self) -> None:
        action = build_prepare_release_action(
            _config(),
            "0.1.2",
            "preliminary",
            "prerelease",
        )

        self.assertEqual("prepare-release.yaml", action.workflow)
        self.assertIn(("version", "0.1.2"), action.fields)
        self.assertIn(("variant", "PRELIMINARY"), action.fields)
        self.assertIn(("release_kind", "prerelease"), action.fields)

    def test_rejects_bad_release_kind(self) -> None:
        with self.assertRaises(BoardwrightError):
            build_prepare_release_action(_config(), "0.1.2", "CHECKED", "weird")


if __name__ == "__main__":
    unittest.main()
