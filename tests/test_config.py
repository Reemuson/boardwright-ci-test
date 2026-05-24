import unittest

from boardwright.config import _read_simple_yaml


class ConfigTests(unittest.TestCase):
    def test_simple_yaml_reads_folded_text(self) -> None:
        parsed = _read_simple_yaml(
            """legal:
  safety_notice: >
    Verify isolation, creepage, and clearance.
    Confirm regulatory compliance.
"""
        )

        self.assertEqual(
            "Verify isolation, creepage, and clearance. Confirm regulatory compliance.",
            parsed["legal"]["safety_notice"],
        )


if __name__ == "__main__":
    unittest.main()
