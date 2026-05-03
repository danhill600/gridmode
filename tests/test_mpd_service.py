import unittest

from mpd_service import delete_queue_album_occurrence, play_queue_album, queue_items_from_playlist


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

    def playlistinfo(self):
        return list(self._playlist)

    def play(self, pos):
        self.played.append(pos)

    def delete(self, pos):
        self.deleted.append(pos)


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


if __name__ == "__main__":
    unittest.main()
