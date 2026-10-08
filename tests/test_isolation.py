"""The test harness has to be trustworthy too.

Every other test in this suite leans on :mod:`support` to keep a run off the
machine it runs on: no user's configuration, no user's cache, no real worker
holding a model in memory.  That is worth checking, because the failure mode of
a harness that leaks is silence - a test that stops exercising a path because a
test before it moved a global, or a run that rewrites the settings of the
machine it ran on.

These tests therefore test the harness.  They are the only ones here that are
about the tests rather than about the program.
"""

import os
import unittest
from pathlib import Path

from wayvoice import engine

from tests import support


class _Case(unittest.TestCase):
    """A test case that can assert on the state *after* its cleanup ran.

    Half of what is being checked here is that nothing is left behind, and that
    cannot be asserted from inside the test that still has its replacements in
    place.  ``doCleanups`` is the framework's own method: it runs and empties the
    list, so the teardown that follows finds nothing left to do.
    """

    def _after_cleanup(self) -> None:
        self.doCleanups()


class EnvironmentIsolationTests(_Case):
    """The XDG locations must point inside the test's own directory."""

    def test_every_location_is_redirected_and_the_originals_come_back(self):
        before = {name: os.environ.get(name) for name in support._XDG_VARS}
        root = support.isolate_environment(self)
        for name in support._XDG_VARS:
            redirected = os.environ[name]
            self.assertTrue(
                redirected.startswith(str(root)),
                f"{name} still points at {redirected}",
            )
            self.assertTrue(
                Path(redirected).is_dir(),
                f"{name} points somewhere that does not exist: {redirected}",
            )
        self._after_cleanup()
        for name, value in before.items():
            self.assertEqual(os.environ.get(name), value, f"{name} was not restored")

    def test_the_runtime_directory_is_where_the_daemon_socket_goes(self):
        # Two spellings of the same directory are how a test ends up talking to an empty
        # configuration: the child reads one path and the parent wrote the file into
        # another.
        root = support.isolate_environment(self)
        self.assertEqual(os.environ["XDG_RUNTIME_DIR"], str(root / "XDG_RUNTIME_DIR"))

    def test_the_data_home_is_redirected(self):
        # It holds the engine runtime, and leaving it real made every engine-dependent
        # test depend on what this machine has installed: six of them failed on CI,
        # which has no runtime by definition. isolate_engine states the readiness
        # instead, which is the honest arrangement - a test about model logic says what
        # it assumes about the engine.
        root = support.isolate_environment(self)
        self.assertIn("XDG_DATA_HOME", support._XDG_VARS)
        self.assertTrue(os.environ["XDG_DATA_HOME"].startswith(str(root)))

    def test_the_runtime_override_is_redirected_too(self):
        # WAYVOICE_RUNTIME takes precedence over both locations above, so a value
        # inherited from the environment would quietly win over the redirect.
        support.isolate_environment(self)
        self.assertIn("WAYVOICE_RUNTIME", support._XDG_VARS)


class EngineIsolationTests(_Case):
    """The hooks must be replaced for the test and only for the test."""

    def test_the_worker_start_is_replaced_and_comes_back(self):
        real = engine._start_worker
        support.isolate_engine(self)
        self.assertIsNot(engine._start_worker, real)
        self.assertFalse(engine._start_worker({}), "a test asked for a real worker")
        self._after_cleanup()
        self.assertIs(engine._start_worker, real)

    def test_a_model_is_reported_as_present_without_looking_at_the_cache(self):
        support.isolate_engine(self)
        self.assertTrue(engine.model_is_present({"model": "no-such-model"}))

    def test_the_worker_backoff_does_not_outlive_the_test(self):
        # The failure this prevents is silent: a worker that could not be started leaves
        # a deadline in the future, and every test after it skips starting one without
        # trying.
        moved = engine.time.monotonic() + 3600.0
        engine._worker_retry_after = moved
        self.addCleanup(setattr, engine, "_worker_retry_after", moved)
        support.isolate_engine(self)
        self.assertEqual(engine._worker_retry_after, 0.0)
        self._after_cleanup()
        self.assertEqual(engine._worker_retry_after, moved, "the deadline was left moved")

    def test_the_engine_is_reported_ready(self):
        # The answer to "is the engine prepared" is a question about this
        # machine, and a test about model logic has no business inheriting it.
        support.isolate_engine(self)
        from wayvoice import daemon, engine

        for module in (engine, daemon):
            self.assertEqual(module.engine_status({})["state"], "ready")

    def test_the_engine_status_is_restored_afterwards(self):
        from wayvoice import daemon, engine

        before = (engine.engine_status, daemon.engine_status)
        support.isolate_engine(self)
        self._after_cleanup()
        self.assertEqual(engine.engine_status, before[0])
        self.assertEqual(daemon.engine_status, before[1])

    def test_the_download_hook_succeeds_without_a_process(self):
        support.isolate_engine(self)
        self.assertEqual(
            engine.download_model("small")["state"], "ready",
        )


if __name__ == "__main__":
    unittest.main()
