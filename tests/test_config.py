import unittest

from gridmode_config import config_to_mapping, parse_app_config


BASE = {
    "mpd": {"host": "localhost", "port": 6600, "password": ""},
    "lastfm": {"api_key": "", "api_secret": ""},
    "cache": {"dir": ".gridmode-cache"},
    "music": {"root": "", "ssh_host": ""},
    "ui": {"columns": 5, "cell_size": 180, "padding": 12, "font": "Helvetica 10"},
}


class ConfigTests(unittest.TestCase):
    def test_parse_minimal_config(self):
        config = parse_app_config(BASE)
        mapping = config_to_mapping(config)

        self.assertEqual(mapping["mpd"]["host"], "localhost")
        self.assertEqual(mapping["music"]["root"], "")
        self.assertEqual(mapping["music"]["ssh_host"], "")
        self.assertEqual(mapping["ui"]["nowplaying_cover_size"], 420)

    def test_requires_public_runtime_fields(self):
        broken = dict(BASE)
        broken["mpd"] = {"port": 6600}

        with self.assertRaisesRegex(ValueError, "mpd.host is required"):
            parse_app_config(broken)


if __name__ == "__main__":
    unittest.main()
