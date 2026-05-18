import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

from phone_service import (
    PhoneTransferCancelled,
    _run_cancelable,
    delete_phone_album_dir,
    list_phone_album_tracks,
    lossy_album_match_key,
    phone_album_dir_exists,
)


class CancelableSubprocessTests(unittest.TestCase):
    def test_run_cancelable_preserves_returncode_and_stderr(self):
        proc = _run_cancelable(
            [sys.executable, "-c", "import sys; sys.stderr.write('nope'); sys.exit(7)"],
            timeout=5,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.assertEqual(proc.returncode, 7)
        self.assertEqual(proc.stderr, "nope")

    def test_run_cancelable_raises_when_cancelled(self):
        cancel_event = threading.Event()

        def cancel_later():
            time.sleep(0.3)
            cancel_event.set()

        thread = threading.Thread(target=cancel_later)
        thread.start()
        try:
            with self.assertRaises(PhoneTransferCancelled):
                _run_cancelable(
                    [sys.executable, "-c", "import time; time.sleep(5)"],
                    timeout=10,
                    cancel_event=cancel_event,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
        finally:
            thread.join()


class LossyAlbumMatchTests(unittest.TestCase):
    def test_matches_source_and_lossy_format_names(self):
        source = "Isaiah Rashad - IT'S BEEN AWFUL (2026) - WEB FLAC"
        lossy = "Isaiah Rashad - IT\\'S BEEN AWFUL (2026) - WEB V0"

        self.assertEqual(lossy_album_match_key(source), lossy_album_match_key(lossy))


class DeletePhoneAlbumTests(unittest.TestCase):
    def test_delete_phone_album_quotes_rel_dir_for_nested_ssh(self):
        rel_dir = "Weird Album; touch nope $(echo bad)"
        completed = subprocess.CompletedProcess(
            ["ssh"],
            0,
            stdout="deleted\tWeird Album; touch nope $(echo bad)",
            stderr="",
        )
        with mock.patch("phone_service._run_cancelable", return_value=completed) as run:
            result = delete_phone_album_dir("oldbeast", "phone", "/music", rel_dir)

        self.assertTrue(result["deleted"])
        cmd = run.call_args.args[0]
        transfer_cmd = cmd[-1]
        self.assertIn("ssh -o BatchMode=yes phone 'sh -c", transfer_cmd)
        self.assertIn("'\"'\"'Weird Album; touch nope $(echo bad)'\"'\"'", transfer_cmd)
        self.assertNotIn("input", run.call_args.kwargs)

    def test_delete_phone_album_requires_rel_dir(self):
        with self.assertRaises(ValueError):
            delete_phone_album_dir("oldbeast", "phone", "/music", "")


class PhoneAlbumExistsTests(unittest.TestCase):
    def test_phone_album_dir_exists_true_on_zero(self):
        completed = subprocess.CompletedProcess(["ssh"], 0, stdout="", stderr="")
        with mock.patch("phone_service._run_cancelable", return_value=completed):
            self.assertTrue(phone_album_dir_exists("oldbeast", "phone", "/music", "Album"))

    def test_phone_album_dir_exists_false_on_one(self):
        completed = subprocess.CompletedProcess(["ssh"], 1, stdout="", stderr="")
        with mock.patch("phone_service._run_cancelable", return_value=completed):
            self.assertFalse(phone_album_dir_exists("oldbeast", "phone", "/music", "Album"))


class PhoneAlbumTracksTests(unittest.TestCase):
    def test_list_phone_album_tracks_quotes_names_for_nested_ssh(self):
        completed = subprocess.CompletedProcess(
            ["ssh"],
            0,
            stdout="Album; nope\tAlbum; nope/01 Song.mp3\n",
            stderr="",
        )
        with mock.patch("subprocess.run", return_value=completed) as run:
            tracks = list_phone_album_tracks("oldbeast", "phone", "/music", ["Album; nope"])

        self.assertEqual(tracks, {"Album; nope": ["Album; nope/01 Song.mp3"]})
        cmd = run.call_args.args[0]
        transfer_cmd = cmd[-1]
        self.assertIn("ssh -o BatchMode=yes phone 'sh -c", transfer_cmd)
        self.assertIn("'\"'\"'Album; nope'\"'\"'", transfer_cmd)


if __name__ == "__main__":
    unittest.main()
