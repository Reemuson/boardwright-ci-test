import unittest
import subprocess
import sys
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

from boardwright import cli


class CliTests(unittest.TestCase):
    def test_plain_boardwright_opens_tui(self) -> None:
        with patch.object(cli, "_tui", return_value=0) as mocked_tui:
            result = cli.main([])

        self.assertEqual(0, result)
        mocked_tui.assert_called_once_with()

    def test_python_module_entrypoint_help(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-m", "boardwright", "--help"],
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(0, completed.returncode)
        self.assertIn("usage: boardwright", completed.stdout)
        self.assertIn("accepted", completed.stdout)
        self.assertIn("doctor", completed.stdout)
        self.assertIn("review", completed.stdout)
        self.assertIn("testbench", completed.stdout)
        self.assertIn("outputs", completed.stdout)

    def test_outputs_clean_command_prints_summary(self) -> None:
        with patch.object(cli, "clean_generated_outputs", return_value=object()), patch.object(
            cli,
            "format_cleanup_summary",
            return_value="cleanup ok",
        ), redirect_stdout(StringIO()) as stdout:
            result = cli.main(["outputs", "clean"])

        self.assertEqual(0, result)
        self.assertIn("cleanup ok", stdout.getvalue())

    def test_doctor_command_prints_report(self) -> None:
        with patch.object(cli, "load_config", return_value=object()), patch.object(
            cli,
            "run_doctor",
            return_value=(),
        ), patch.object(cli, "format_doctor_report", return_value="Doctor OK"), redirect_stdout(
            StringIO()
        ):
            result = cli.main(["doctor"])

        self.assertEqual(0, result)

    def test_review_command_prints_preview_state(self) -> None:
        state = type("State", (), {"ready": False})()
        with patch.object(cli, "load_config", return_value=object()), patch.object(
            cli,
            "build_preview_state",
            return_value=state,
        ), patch.object(cli, "format_preview_state", return_value="Preview ready"), redirect_stdout(
            StringIO()
        ):
            result = cli.main(["review"])

        self.assertEqual(1, result)

    def test_testbench_plan_command_prints_plan(self) -> None:
        with patch.object(cli, "load_config", return_value=object()), patch.object(
            cli,
            "build_testbench_plan",
            return_value=object(),
        ), patch.object(cli, "format_testbench_plan", return_value="Plan"), redirect_stdout(
            StringIO()
        ):
            result = cli.main(["testbench", "plan"])

        self.assertEqual(0, result)


if __name__ == "__main__":
    unittest.main()
