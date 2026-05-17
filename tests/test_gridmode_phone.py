import unittest

from gridmode import phone_folder_match_key


class PhoneFolderMatchTests(unittest.TestCase):
    def test_strips_reissue_and_format_suffixes(self):
        library_name = "Dan Reeder - Every Which Way (2020) {Oh Boy Records} [WEB FLAC]"
        phone_name = "Dan Reeder - Every Which Way"

        self.assertEqual(phone_folder_match_key(library_name), phone_folder_match_key(phone_name))

    def test_empty_name_stays_empty(self):
        self.assertEqual(phone_folder_match_key(""), "")


if __name__ == "__main__":
    unittest.main()
