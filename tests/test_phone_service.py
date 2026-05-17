import subprocess
import sys
import threading
import time
import unittest

from phone_service import PhoneTransferCancelled, _run_cancelable


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


if __name__ == "__main__":
    unittest.main()
