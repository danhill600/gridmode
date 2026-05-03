import unittest

from gridmode_cache import album_records_from_items, cover_art_urls, release_mbid_from_key, title_similarity


class MusicBrainzCoverTests(unittest.TestCase):
    def test_album_record_keeps_musicbrainz_ids(self):
        records = album_records_from_items(
            [
                {
                    "artist": "Artist",
                    "albumartist": "Album Artist",
                    "album": "Album",
                    "file": "Artist/Album/01.flac",
                    "musicbrainz_albumid": "release-id",
                    "musicbrainz_releasegroupid": "release-group-id",
                }
            ]
        )

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].release_mbid, "release-id")
        self.assertEqual(records[0].release_group_mbid, "release-group-id")
        self.assertEqual(release_mbid_from_key(records[0].key), "release-id")

    def test_cover_art_urls_try_release_before_release_group(self):
        urls = list(cover_art_urls(["release-id"], ["group-id"]))

        self.assertEqual(
            urls,
            [
                "https://coverartarchive.org/release/release-id/front-500",
                "https://coverartarchive.org/release-group/group-id/front-500",
            ],
        )

    def test_title_similarity_allows_reissue_suffixes(self):
        self.assertGreaterEqual(title_similarity("Album", "Album (Remastered 2004)"), 0.7)
        self.assertLess(title_similarity("Album", "A Different Record"), 0.7)


if __name__ == "__main__":
    unittest.main()
