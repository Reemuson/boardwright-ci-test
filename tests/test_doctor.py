import unittest
from pathlib import Path

from boardwright.config import BoardwrightConfig, load_config
from boardwright.doctor import (
    DoctorCheck,
    _check_workflow_inputs,
    doctor_exit_code,
    format_doctor_report,
)


def _config(root: Path) -> BoardwrightConfig:
    return BoardwrightConfig(
        root=root,
        project={
            "project": {"id": "TEST", "name": "Test"},
            "outputs": {
                "preview_workflow": "dev-preview.yaml",
                "main_workflow": "main-outputs.yaml",
                "prepare_release_workflow": "prepare-release.yaml",
                "release_workflow": "release.yaml",
            },
        },
        branches={"branches": {"development": "dev", "release": "main"}},
        legal={"legal": {}},
        revision_history={"revision_history": {}},
    )


class DoctorTests(unittest.TestCase):
    def test_report_marks_errors_as_blocking(self) -> None:
        checks = (
            DoctorCheck("ok", "Git", "found git."),
            DoctorCheck("error", "Repository", "origin remote is missing."),
        )

        report = format_doctor_report(checks)

        self.assertEqual(1, doctor_exit_code(checks))
        self.assertIn("[ERROR] Repository", report)
        self.assertIn("blocking issues", report)

    def test_report_allows_warnings(self) -> None:
        checks = (
            DoctorCheck("ok", "Git", "found git."),
            DoctorCheck("warning", "GitHub CLI", "gh is not installed."),
        )

        report = format_doctor_report(checks)

        self.assertEqual(0, doctor_exit_code(checks))
        self.assertIn("usable, with warnings", report)

    def test_workflow_input_check_accepts_expected_inputs(self) -> None:
        checks: list[DoctorCheck] = []

        _check_workflow_inputs(load_config(), "dev-preview.yaml", ("variant",), checks)

        self.assertEqual("ok", checks[0].level)
        self.assertIn("usable", checks[0].message)

    def test_workflow_input_check_reports_missing_inputs(self) -> None:
        checks: list[DoctorCheck] = []

        _check_workflow_inputs(load_config(), "dev-preview.yaml", ("missing_input",), checks)

        self.assertEqual("warning", checks[0].level)
        self.assertIn("missing_input", checks[0].message)


if __name__ == "__main__":
    unittest.main()
