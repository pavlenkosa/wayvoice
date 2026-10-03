"""The download helper and the protocol it speaks.

A model download is the longest thing WayVoice ever waits for, and the window
shows it as a progress bar.  That bar is fed by lines this module prints, so
the interesting questions are what counts as progress, what is ignored, and
what the caller gets told when the download fails.

``huggingface_hub`` is faked throughout: these tests are about the reporting,
not about the network.
"""

import io
import sys
import unittest
from unittest import mock

from wayvoice import model_fetch


class FakeHub:
    """The part of ``huggingface_hub`` the helper touches."""

    def __init__(self, *, error: Exception | None = None, siblings=None):
        self.error = error
        self.siblings = siblings or []
        self.calls: list[dict] = []

    def snapshot_download(self, repo_id, **kwargs):
        self.calls.append({"repo_id": repo_id, **kwargs})
        if self.error is not None:
            raise self.error
        return f"/cache/{repo_id}/snapshots/abc"

    def model_info(self, repo_id, files_metadata=False):
        return mock.Mock(siblings=self.siblings)


def _fake_hub(hub: FakeHub):
    """Install ``hub`` as ``huggingface_hub`` for the duration of a test."""
    module = mock.Mock()
    module.snapshot_download = hub.snapshot_download
    module.HfApi = lambda: mock.Mock(model_info=hub.model_info)
    return mock.patch.dict(sys.modules, {"huggingface_hub": module})


class FakeSibling:
    def __init__(self, name: str, size: int):
        self.rfilename = name
        self.size = size


class ReporterTests(unittest.TestCase):
    """What the bar on the screen is made of."""

    def setUp(self):
        self.out = io.StringIO()
        self.patch = mock.patch.object(sys, "stdout", self.out)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def lines(self) -> list[str]:
        return [line for line in self.out.getvalue().splitlines() if line.strip()]

    def test_bytes_landed_are_reported(self):
        reporter = model_fetch.Reporter(total=1000)
        bars = model_fetch.progress_tqdm_class(reporter)
        bar = bars(total=1000, initial=0, unit="B")
        bar.update(400)
        self.assertEqual(reporter.counts(), (400, 1000))

    def test_the_same_bytes_counted_twice_are_not_added(self):
        # The hub has one counter for bytes written and one for bytes received,
        # and both are given the size of every file. Summing them reported a
        # 1.5 GB model as 3 GB and made the bar sit at 50% when it was done.
        reporter = model_fetch.Reporter(total=1000)
        bars = model_fetch.progress_tqdm_class(reporter)
        written = bars(total=1000, unit="B")
        received = bars(total=1000, unit="B")
        written.update(600)
        received.update(600)
        self.assertEqual(reporter.counts(), (600, 1000))

    def test_a_bar_that_never_moved_is_not_counted(self):
        reporter = model_fetch.Reporter(total=1000)
        bars = model_fetch.progress_tqdm_class(reporter)
        bars(total=1000, unit="B")  # created, never fed: xet's network bar
        bars(total=500, unit="B").update(500)
        self.assertEqual(reporter.counts(), (500, 1000))

    def test_the_per_file_bar_is_not_bytes(self):
        # snapshot_download also draws a bar for its thread map, counting files.
        reporter = model_fetch.Reporter(total=0)
        bars = model_fetch.progress_tqdm_class(reporter)
        files = bars(total=4, unit="it")
        files.update(4)
        self.assertEqual(reporter.counts(), (0, 0))

    def test_without_a_known_total_the_bars_supply_one(self):
        reporter = model_fetch.Reporter(total=0)
        bars = model_fetch.progress_tqdm_class(reporter)
        bars(total=777, unit="B").update(10)
        self.assertEqual(reporter.counts(), (10, 777))

    def test_resumed_bytes_are_counted_from_the_start(self):
        reporter = model_fetch.Reporter(total=1000)
        bars = model_fetch.progress_tqdm_class(reporter)
        bar = bars(total=1000, initial=250, unit="B")
        self.assertEqual(reporter.counts(), (250, 1000))
        bar.update(10)
        self.assertEqual(reporter.counts(), (260, 1000))

    def test_reporting_is_throttled_but_the_last_one_always_comes(self):
        reporter = model_fetch.Reporter(total=100, interval=1000.0)
        bars = model_fetch.progress_tqdm_class(reporter)
        bar = bars(total=100, unit="B")
        bar.update(1)
        bar.update(1)
        bar.update(1)
        reporter.report(force=True)
        # One line at most from the throttled updates, one from the forced call.
        self.assertLessEqual(len(self.lines()), 2)
        self.assertTrue(self.lines()[-1].startswith("WV-PROGRESS"))

    def test_a_progress_line_is_well_formed(self):
        reporter = model_fetch.Reporter(total=2048)
        bars = model_fetch.progress_tqdm_class(reporter)
        bars(total=2048, unit="B").update(512)
        reporter.report(force=True)
        self.assertEqual(self.lines()[-1], "WV-PROGRESS 512 2048")

    def test_the_cosmetic_setters_the_hub_calls_all_exist(self):
        # The hub reads these back after creating a bar; a missing one is an
        # AttributeError in the middle of a multi-gigabyte download.
        reporter = model_fetch.Reporter(total=10)
        bar = model_fetch.progress_tqdm_class(reporter)(total=10, unit="B")
        for name in (
            "update", "update_transfer", "refresh", "set_postfix_str",
            "set_description", "set_description_str", "close",
        ):
            self.assertTrue(callable(getattr(bar, name)), name)
        with bar as entered:
            self.assertIs(entered, bar)
        bar.update_transfer(5)
        bar.set_postfix_str("1 MB/s")
        bar.set_description("Downloading bytes")
        bar.set_description_str("done")
        bar.refresh()
        bar.close()


class ExpectedTotalTests(unittest.TestCase):
    """The denominator, which must be right or the bar is a lie."""

    def test_the_size_of_the_files_we_actually_fetch(self):
        hub = FakeHub(siblings=[
            FakeSibling("model.bin", 700),
            FakeSibling("tokenizer.json", 100),
            FakeSibling("model.safetensors", 900),  # not in FETCH_PATTERNS
            FakeSibling("README.md", 10),
        ])
        with _fake_hub(hub):
            total = model_fetch.expected_total(
                "org/name", ["model.bin", "tokenizer.json"]
            )
        self.assertEqual(total, 800)

    def test_an_unknown_size_is_not_guessed(self):
        hub = FakeHub(siblings=[FakeSibling("model.bin", 0)])
        with _fake_hub(hub):
            self.assertEqual(model_fetch.expected_total("org/name", ["model.bin"]), 0)

    def test_a_failing_metadata_call_leaves_it_unknown(self):
        # Offline, rate limited or simply a different hub: an indeterminate bar
        # is better than a percentage that is not true.
        hub = FakeHub()
        hub.model_info = mock.Mock(side_effect=OSError("offline"))
        with _fake_hub(hub):
            self.assertEqual(model_fetch.expected_total("org/name", ["model.bin"]), 0)


class MainTests(unittest.TestCase):
    """What the caller of the helper finds out."""

    def setUp(self):
        self.out = io.StringIO()
        patch = mock.patch.object(sys, "stdout", self.out)
        patch.start()
        self.addCleanup(patch.stop)

    def _run(self, hub, *args):
        with _fake_hub(hub):
            code = model_fetch.main(list(args))
        return code

    def test_a_finished_download_says_where_it_landed(self):
        hub = FakeHub()
        code = self._run(hub, "--repo", "org/name")
        self.assertEqual(code, 0)
        self.assertIn("WV-READY /cache/org/name/snapshots/abc", self.out.getvalue())

    def test_only_the_files_a_model_needs_are_fetched(self):
        # The Systran repositories carry weights WayVoice cannot use; fetching
        # everything doubles the download for tiny and wastes gigabytes on
        # large-v3.
        hub = FakeHub()
        self._run(hub, "--repo", "org/name")
        patterns = hub.calls[0]["allow_patterns"]
        self.assertIn("model.bin", patterns)
        self.assertIn("config.json", patterns)
        self.assertEqual(patterns, ["config.json", "preprocessor_config.json",
                                    "model.bin", "tokenizer.json", "vocabulary.*"])

    def test_the_cache_directory_is_passed_on(self):
        hub = FakeHub()
        self._run(hub, "--repo", "org/name", "--cache-dir", "/tmp/hub")
        self.assertEqual(hub.calls[0]["cache_dir"], "/tmp/hub")

    def test_a_failed_download_says_why_and_fails(self):
        hub = FakeHub(error=RuntimeError("404 Client Error"))
        code = self._run(hub, "--repo", "org/name")
        self.assertEqual(code, 1)
        self.assertIn("WV-ERROR 404 Client Error", self.out.getvalue())
        self.assertNotIn("WV-READY", self.out.getvalue())

    def test_an_interrupt_is_a_cancellation_and_not_a_crash(self):
        hub = FakeHub(error=KeyboardInterrupt())
        code = self._run(hub, "--repo", "org/name")
        self.assertEqual(code, 130)
        self.assertIn("WV-ERROR cancelled", self.out.getvalue())

    def test_a_missing_repository_argument_is_refused(self):
        with self.assertRaises(SystemExit):
            model_fetch.main([])


if __name__ == "__main__":
    unittest.main()
