import unittest
from unittest.mock import patch

from mpd_service import (
    append_current_song_to_playlist,
    delete_queue_album_occurrence,
    play_queue_album,
    queue_items_from_playlist,
    save_current_queue_as_playlist,
)


def song(pos, artist, album, file_path, albumartist=""):
    item = {
        "pos": str(pos),
        "artist": artist,
        "album": album,
        "file": file_path,
    }
    if albumartist:
        item["albumartist"] = albumartist
    return item


class FakeClient:
    def __init__(self, playlist):
        self._playlist = list(playlist)
        self.played = []
        self.deleted = []
        self.saved = []
        self.removed_playlists = []
        self.renamed = []
        self.playlist_added = []

    def playlistinfo(self):
        return list(self._playlist)

    def play(self, pos):
        self.played.append(pos)

    def delete(self, pos):
        self.deleted.append(pos)

    def save(self, name):
        self.saved.append(name)

    def rm(self, name):
        self.removed_playlists.append(name)

    def rename(self, old, new):
        self.renamed.append((old, new))

    def currentsong(self):
        return self._playlist[0] if self._playlist else {}

    def playlistadd(self, playlist, file_path):
        self.playlist_added.append((playlist, file_path))


class QueueOccurrenceTests(unittest.TestCase):
    def test_repeated_album_becomes_distinct_queue_items(self):
        playlist = [
            song(0, "Dagmar Zuniga", "Album", "a/01.flac"),
            song(1, "Dagmar Zuniga", "Album", "a/02.flac"),
            song(2, "Other Artist", "Other", "b/01.flac"),
            song(3, "Dagmar Zuniga", "Album", "a/01.flac"),
            song(4, "Dagmar Zuniga", "Album", "a/02.flac"),
        ]

        items = queue_items_from_playlist(playlist)

        self.assertEqual([item["positions"] for item in items], [[0, 1], [2], [3, 4]])

    def test_delete_occurrence_uses_only_selected_positions(self):
        playlist = [
            song(0, "Dagmar Zuniga", "Album", "a/01.flac"),
            song(1, "Dagmar Zuniga", "Album", "a/02.flac"),
            song(2, "Other Artist", "Other", "b/01.flac"),
            song(3, "Dagmar Zuniga", "Album", "a/01.flac"),
            song(4, "Dagmar Zuniga", "Album", "a/02.flac"),
        ]
        selected = queue_items_from_playlist(playlist)[2]
        client = FakeClient(playlist)

        result = delete_queue_album_occurrence(client, selected)

        self.assertEqual(result, {"ok": True, "stale": False, "deleted": 2})
        self.assertEqual(client.deleted, ["4", "3"])

    def test_delete_refuses_stale_positions(self):
        original = [
            song(0, "Dagmar Zuniga", "Album", "a/01.flac"),
            song(1, "Dagmar Zuniga", "Album", "a/02.flac"),
        ]
        selected = queue_items_from_playlist(original)[0]
        changed = [
            song(0, "Other Artist", "Other", "b/01.flac"),
            song(1, "Other Artist", "Other", "b/02.flac"),
        ]
        client = FakeClient(changed)

        result = delete_queue_album_occurrence(client, selected)

        self.assertEqual(result, {"ok": False, "stale": True, "deleted": 0})
        self.assertEqual(client.deleted, [])

    def test_play_occurrence_uses_first_position(self):
        playlist = [
            song(10, "Artist", "Album", "a/01.flac"),
            song(11, "Artist", "Album", "a/02.flac"),
        ]
        selected = queue_items_from_playlist(playlist)[0]
        client = FakeClient(playlist)

        self.assertTrue(play_queue_album(client, selected))
        self.assertEqual(client.played, ["10"])

    def test_save_current_queue_replaces_stored_playlist(self):
        client = FakeClient([])

        with patch("mpd_service.time.time", return_value=123.456):
            result = save_current_queue_as_playlist(client, " road trip ")

        self.assertEqual(result, {"ok": True, "name": "road trip"})
        self.assertEqual(client.saved, [".gridmode-save-123456"])
        self.assertEqual(client.removed_playlists, ["road trip"])
        self.assertEqual(client.renamed, [(".gridmode-save-123456", "road trip")])

    def test_append_current_song_to_playlist_uses_single_file(self):
        client = FakeClient([song(0, "Artist", "Album", "a/01.flac")])

        result = append_current_song_to_playlist(client, "sick_tunes")

        self.assertEqual(result, {"ok": True, "playlist": "sick_tunes", "file": "a/01.flac"})
        self.assertEqual(client.playlist_added, [("sick_tunes", "a/01.flac")])


if __name__ == "__main__":
    unittest.main()
