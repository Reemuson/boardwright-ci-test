import unittest

from boardwright.errors import BoardwrightError
from boardwright.release import _validate_version


class ReleaseTests(unittest.TestCase):
    def test_validates_semver(self) -> None:
        _validate_version("0.1.0")
        with self.assertRaises(BoardwrightError):
            _validate_version("v0.1.0")


if __name__ == "__main__":
    unittest.main()
