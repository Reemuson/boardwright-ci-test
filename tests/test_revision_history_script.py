import unittest

from boardwright_resources.kibot.resources.scripts.get_revision_history_var import unquote


class RevisionHistoryScriptTests(unittest.TestCase):
    def test_unquote_decodes_newlines(self) -> None:
        self.assertEqual("- One\n- Two", unquote('"- One\\n- Two"'))


if __name__ == "__main__":
    unittest.main()
