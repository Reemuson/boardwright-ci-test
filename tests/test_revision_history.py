import unittest

from boardwright.revision_history import build_revision_slots_from_text


class RevisionHistoryTests(unittest.TestCase):
    def test_builds_fixed_slots(self) -> None:
        text = (
            "# Changelog\n\n"
            "## [Unreleased]\n\n### Added\n\n- Current work\n\n"
            "## [0.1.0] - 2026-04-25\n\n### Fixed\n\n- First release\n"
        )

        slots = build_revision_slots_from_text(text, slot_count=3)

        self.assertEqual(3, len(slots))
        self.assertEqual("Unreleased", slots[0].version)
        self.assertEqual("0.1.0", slots[1].version)
        self.assertEqual("", slots[2].version)

    def test_previous_releases_shift_down(self) -> None:
        text = (
            "# Changelog\n\n"
            "## [Unreleased]\n\n"
            "## [0.3.0] - 2026-04-25\n\n### Added\n\n- Third\n\n"
            "## [0.2.0] - 2026-04-20\n\n### Changed\n\n- Second\n\n"
            "## [0.1.0] - 2026-04-10\n\n### Fixed\n\n- First\n"
        )

        slots = build_revision_slots_from_text(
            text,
            slot_count=4,
            include_unreleased=False,
        )

        self.assertEqual("0.3.0", slots[0].version)
        self.assertEqual("0.2.0", slots[1].version)
        self.assertEqual("0.1.0", slots[2].version)
        self.assertEqual("", slots[3].version)
        self.assertEqual("0.3.0 - 2026-04-25", slots[0].title)
        self.assertEqual("Changed:\n  - Second", slots[1].body)

    def test_visible_variables_are_title_and_body_only(self) -> None:
        text = "# Changelog\n\n## [0.1.0] - 2026-04-25\n\n### Added\n\n- First\n"

        slots = build_revision_slots_from_text(text, slot_count=1)

        self.assertEqual("0.1.0 - 2026-04-25", slots[0].title)
        self.assertEqual("Added:\n  - First", slots[0].body)

    def test_omits_empty_sections(self) -> None:
        text = (
            "# Changelog\n\n"
            "## [Unreleased]\n\n"
            "### Added\n\n"
            "### Fixed\n\n"
            "- Actual fix\n"
        )

        slots = build_revision_slots_from_text(text, slot_count=1)

        self.assertEqual("Fixed:\n  - Actual fix", slots[0].body)


if __name__ == "__main__":
    unittest.main()
