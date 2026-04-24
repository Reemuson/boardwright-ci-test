import unittest
from pathlib import Path

from boardwright.config import BoardwrightConfig
from boardwright.validation import validate_project


class ValidationTests(unittest.TestCase):
    def test_validates_variant_values(self) -> None:
        config = BoardwrightConfig(
            root=Path("."),
            project={
                "project": {
                    "id": "TEST",
                    "name": "Test",
                    "company": "Company",
                    "designer": "Designer",
                },
                "variants": {
                    "dev_default": "FAST",
                    "preview_default": "CHECKED",
                    "main_default": "CHECKED",
                    "release_default": "RELEASED",
                },
                "outputs": {"preview_engine": "github-actions"},
            },
            branches={"branches": {}},
            legal={"legal": {}},
            revision_history={"revision_history": {}},
        )

        issues = validate_project(config)

        self.assertTrue(
            any("Unsupported variants.dev_default" in issue.message for issue in issues)
        )


if __name__ == "__main__":
    unittest.main()
