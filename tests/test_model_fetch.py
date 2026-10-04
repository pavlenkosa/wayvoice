"""The download helper and the protocol it speaks.

A model download is the longest thing WayVoice ever waits for, and the window
shows it as a progress bar.  That bar is fed by lines this module prints, so
the interesting questions are what counts as progress, what is ignored, and
what the caller gets told when the download fails.

``huggingface_hub`` is faked throughout: these tests are about the reporting,
not about the network.
"""

import io
import json
import os
import subprocess
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


# --------------------------------------------------------------------------
# Compatibility with the installed huggingface_hub
# --------------------------------------------------------------------------
# The bar in this module is not a tqdm: it is whatever shape the hub expects,
# and the hub is the only thing that decides that shape.  Both tests below read
# the requirement out of the *installed library* - one from its source, one by
# driving it - because a test that enumerates the methods of our own class
# cannot fail for anything the library actually calls.  That is how an
# AttributeError could ship in the middle of a real download.
def _hub_runtime() -> str | None:
    """The engine runtime interpreter, if it can import huggingface_hub.

    Not merely "a file called bin/python exists": an earlier version of this
    check looked no further, and a leftover directory from an interrupted setup
    - or one a test created - satisfied it.  The probe then failed with
    ModuleNotFoundError and the test failed, on a machine with no library to be
    incompatible with.  One extra subprocess settles it.
    """
    from wayvoice import engine

    python = engine.faster_runtime() / "bin/python"
    if not python.exists():
        return None
    probe = subprocess.run(
        [str(python), "-c", "import huggingface_hub"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    return str(python) if probe.returncode == 0 else None


def _hub_probe(source: str) -> str:
    """Run a snippet under the runtime interpreter and return its stdout."""
    import subprocess

    from wayvoice import engine
    from wayvoice.paths import app_src_dir

    proc = subprocess.run(
        [_hub_runtime(), "-c", source],
        capture_output=True,
        text=True,
        timeout=180,
        cwd=str(app_src_dir().parent),
        env={**os.environ, "PYTHONPATH": str(app_src_dir())},
    )
    if proc.returncode != 0:
        raise AssertionError(f"the runtime could not run the probe: {proc.stderr.strip()}")
    return proc.stdout


class HubCompatibilityTests(unittest.TestCase):
    """The progress bar has to be the shape the installed hub actually uses."""

    def setUp(self):
        if _hub_runtime() is None:
            # Not a failure: these two check compatibility with a library that
            # only exists once the engine runtime is installed, and a machine
            # without it has nothing to be incompatible with.
            self.skipTest(
                "no engine runtime with huggingface_hub: the bar is checked "
                "against the library where it is installed"
            )

    def test_the_bar_offers_every_attribute_the_hub_touches(self):
        # Read out of the library's own source: every attribute it reaches for
        # on a progress-bar object, in the xet reporter and in the two snapshot
        # download classes that feed it.  A name that appears there and not on
        # our bar is an AttributeError waiting for the first chunk of a download.
        required = _hub_probe(
            "import ast, inspect, json\n"
            "from huggingface_hub.utils import _xet_progress_reporting as xpr\n"
            "from huggingface_hub import _snapshot_download as snap\n"
            "names = set()\n"
            "def collect(source):\n"
            "    tree = ast.parse(source)\n"
            "    for func in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef,))]:\n"
            "        params = {a.arg for a in func.args.args + func.args.kwonlyargs}\n"
            "        for node in ast.walk(func):\n"
            "            if not isinstance(node, ast.Attribute):\n"
            "                continue\n"
            "            target = node.value\n            # a parameter named like a bar...\n"
            "            if isinstance(target, ast.Name) and target.id in params and 'bar' in target.id:\n"
            "                names.add(node.attr)\n"
            "            # ...or self.reconstruction_bar / self.transfer_bar\n"
            "            if (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)\n"
            "                    and target.value.id == 'self' and target.attr.endswith('_bar')):\n"
            "                names.add(node.attr)\n"
            "collect(inspect.getsource(xpr))\n"
            "collect(inspect.getsource(snap))\n"
            "print(json.dumps(sorted(names)))\n"
        )
        wanted = set(json.loads(required))
        self.assertTrue(
            {"total", "n", "update"} <= wanted,
            f"the probe did not find the obvious ones: {sorted(wanted)}",
        )
        reporter = model_fetch.Reporter(total=100)
        bar = model_fetch.progress_tqdm_class(reporter)(total=100, unit="B")
        missing = sorted(name for name in wanted if not hasattr(bar, name))
        self.assertEqual(
            missing, [],
            "the installed hub uses bar." + missing[0] + " and the bar has none"
            if missing else "",
        )

    def test_the_installed_xet_reporter_can_report_into_the_bar(self):
        # The behavioural version: hand the real reporter a real bar and push a
        # progress report through it.  This is the path a multi-gigabyte model
        # takes, and it is where the AttributeError was raised.
        output = _hub_probe(
            "import json\n"
            "from types import SimpleNamespace\n"
            "from huggingface_hub.utils._xet_progress_reporting import XetDownloadProgressReporter\n"
            "from wayvoice import model_fetch\n"
            "reporter_holder = model_fetch.Reporter(total=1000)\n"
            "bar = model_fetch.progress_tqdm_class(reporter_holder)(total=1000, unit='B')\n"
            "xet = XetDownloadProgressReporter(\n"
            "    reconstruction_desc='Reconstructing', log_level=20,\n"
            "    external_reconstruction_bar=bar,\n"
            ")\n"
            "report = SimpleNamespace(\n"
            "    total_bytes_completed=400, total_transfer_bytes_completed=350,\n"
            "    total_bytes_completion_rate=100.0, total_transfer_bytes_completion_rate=90.0,\n"
            "    total_bytes=1000,\n"
            ")\n"
            "xet.update_progress(report)\n"
            "xet.update_progress(SimpleNamespace(\n"
            "    total_bytes_completed=900, total_transfer_bytes_completed=800,\n"
            "    total_bytes_completion_rate=100.0, total_transfer_bytes_completion_rate=90.0,\n"
            "    total_bytes=1000,\n"
            "))\n"
            "xet.close()\n"
            "reporter_holder.report(force=True)\n"
        )
        self.assertIn("WV-PROGRESS", output, output)
        done, total = output.strip().splitlines()[-1].split()[1:]
        # Bytes written to disk are what a progress bar shows; the network
        # counter is the hub's own business.
        self.assertEqual(total, "1000")
        self.assertEqual(int(done), 900)
