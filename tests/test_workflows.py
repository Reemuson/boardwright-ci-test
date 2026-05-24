import unittest
from pathlib import Path


class WorkflowTests(unittest.TestCase):
    def test_split_workflows_exist(self) -> None:
        expected = {
            "dev-preview.yaml": ("Boardwright Dev Preview", "Publish preview branch"),
            "main-outputs.yaml": ("Boardwright Main Outputs", "Commit accepted outputs"),
            "prepare-release.yaml": ("Boardwright Prepare Release", "Create and push tag"),
            "release.yaml": ("Boardwright Release", "Publish GitHub Release"),
        }

        for filename, markers in expected.items():
            workflow = Path(".github/workflows") / filename
            self.assertTrue(workflow.exists(), filename)
            text = workflow.read_text(encoding="utf-8")
            for marker in markers:
                self.assertIn(marker, text)

    def test_workflows_do_not_run_notes_as_standalone_target(self) -> None:
        for workflow in Path(".github/workflows").glob("*.yaml"):
            text = workflow.read_text(encoding="utf-8")

            self.assertNotIn("additional_args: --log kibot_preview_notes.log notes", text)
            self.assertNotIn("additional_args: --log kibot_main_notes.log notes", text)
            self.assertNotIn("additional_args: --log kibot_prepare_notes.log notes", text)
            self.assertNotIn("additional_args: --log kibot_release_notes.log notes", text)

    def test_workflows_clean_generated_outputs_after_kibot(self) -> None:
        for workflow in Path(".github/workflows").glob("*.yaml"):
            text = workflow.read_text(encoding="utf-8")

            self.assertIn("Clean generated outputs", text)
            self.assertIn("clean_generated_outputs.py", text)


if __name__ == "__main__":
    unittest.main()
