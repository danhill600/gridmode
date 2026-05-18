import unittest

from gridmode import items_have_mtimes, phone_folder_match_key


class PhoneFolderMatchTests(unittest.TestCase):
    def test_strips_reissue_and_format_suffixes(self):
        library_name = "Dan Reeder - Every Which Way (2020) {Oh Boy Records} [WEB FLAC]"
        phone_name = "Dan Reeder - Every Which Way"

        self.assertEqual(phone_folder_match_key(library_name), phone_folder_match_key(phone_name))

    def test_empty_name_stays_empty(self):
        self.assertEqual(phone_folder_match_key(""), "")


class LibraryMtimeTests(unittest.TestCase):
    def test_detects_cached_mtimes(self):
        self.assertFalse(items_have_mtimes([{"mtime": None}, {}]))
        self.assertTrue(items_have_mtimes([{"mtime": None}, {"mtime": 123.0}]))


if __name__ == "__main__":
    unittest.main()
