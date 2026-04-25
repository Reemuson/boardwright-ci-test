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


if __name__ == "__main__":
    unittest.main()
