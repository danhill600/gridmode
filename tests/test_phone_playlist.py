import unittest
from unittest import mock

from phone_playlist import generate_playlist_from_config, m3u_text, order_phone_albums, write_phone_playlist


class PhonePlaylistTests(unittest.TestCase):
    def test_orders_phone_albums_by_matching_library_mtime(self):
        phone_albums = [
            {"name": "Older Artist - Older Album [WEB V0]", "mtime": 9999},
            {"name": "New Artist - New Album [WEB V0]", "mtime": 1},
        ]
        library_items = [
            {
                "artist": "Older Artist",
                "album": "Older Album",
                "rel_dir": "Older Artist - Older Album (2024) [WEB FLAC]",
                "mtime": 100,
            },
            {
                "artist": "New Artist",
                "album": "New Album",
                "rel_dir": "New Artist - New Album (2026) [WEB FLAC]",
                "mtime": 200,
            },
        ]

        ordered, unmatched = order_phone_albums(phone_albums, library_items)

        self.assertEqual(unmatched, [])
        self.assertEqual([item["phone"]["name"] for item in ordered], [
            "New Artist - New Album [WEB V0]",
            "Older Artist - Older Album [WEB V0]",
        ])

    def test_builds_relative_path_m3u(self):
        ordered = [
            {"phone": {"name": "Album B"}},
            {"phone": {"name": "Album A"}},
        ]
        tracks = {
            "Album A": ["Album A/01 One.mp3"],
            "Album B": ["Album B/01 First.mp3", "Album B/02 Second.mp3"],
        }

        self.assertEqual(
            m3u_text(ordered, tracks),
            "#EXTM3U\nAlbum B/01 First.mp3\nAlbum B/02 Second.mp3\nAlbum A/01 One.mp3\n",
        )

    def test_generate_playlist_from_config_writes_phone_copy(self):
        cfg = {
            "cache": {"dir": "/cache"},
            "music": {"ssh_host": "music-host"},
            "phone": {"ssh_host": "phone", "music_root": "/music"},
            "playlists": {"phone_recent": "recent.m3u"},
        }
        with (
            mock.patch("phone_playlist.load_library_index", return_value=[{
                "artist": "Artist",
                "album": "Album",
                "rel_dir": "Artist - Album [WEB FLAC]",
                "mtime": 20,
            }]),
            mock.patch("phone_playlist.list_phone_album_dirs", return_value=[
                {"name": "Artist - Album [WEB V0]", "mtime": 1},
            ]),
            mock.patch("phone_playlist.list_phone_album_tracks", return_value={
                "Artist - Album [WEB V0]": ["Artist - Album [WEB V0]/01 Song.mp3"],
            }),
            mock.patch("phone_playlist.write_phone_playlist") as write_playlist,
        ):
            result = generate_playlist_from_config(cfg)

        self.assertEqual(result["tracks"], 1)
        self.assertEqual(result["playlist"], "recent.m3u")
        write_playlist.assert_called_once()
        self.assertIn("Artist - Album [WEB V0]/01 Song.mp3", write_playlist.call_args.args[4])

    def test_write_phone_playlist_can_run_from_local_machine(self):
        completed = mock.Mock(returncode=0, stderr="")
        with mock.patch("subprocess.run", return_value=completed) as run:
            write_phone_playlist("", "phone", "/music", "recent.m3u", "#EXTM3U\n")

        cmd = run.call_args.args[0]
        self.assertEqual(cmd[:3], ["ssh", "-o", "BatchMode=yes"])
        self.assertEqual(cmd[3], "phone")
        self.assertEqual(run.call_args.kwargs["input"], "#EXTM3U\n")

    def test_generate_playlist_tolerates_missing_library_index(self):
        cfg = {
            "cache": {"dir": "/missing-cache"},
            "music": {"ssh_host": ""},
            "phone": {"ssh_host": "phone", "music_root": "/music"},
        }
        with (
            mock.patch("phone_playlist.list_phone_album_dirs", return_value=[
                {"name": "Album", "mtime": 1},
            ]),
            mock.patch("phone_playlist.list_phone_album_tracks", return_value={
                "Album": ["Album/01 Song.mp3"],
            }),
            mock.patch("phone_playlist.write_phone_playlist") as write_playlist,
        ):
            result = generate_playlist_from_config(cfg)

        self.assertEqual(result["tracks"], 1)
        write_playlist.assert_called_once()


if __name__ == "__main__":
    unittest.main()
