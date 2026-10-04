"""Keeping a test run off the machine it runs on.

Some parts of the daemon do real work outside their own process: they read the
user's config, walk the user's model cache, and start a warm worker that sits
there holding a model in memory.  A test that constructs a daemon to check, say,
its accept loop has no business doing any of that.  It depends on whatever the
machine happens to have installed, it can leave a real worker running after the
run, and - the worst of it - a test that writes a configuration from the test
process overwrites the user's own settings, because the temporary directories it
gives the *child* do nothing for the code running in the parent.

:func:`isolate_environment` closes that last one by pointing the XDG variables
at a per-test directory in the process that runs the test as well, and
:func:`isolate_engine` cuts the worker start, which is the call that really puts
a model in memory.
"""

import os
import shutil
import tempfile
from pathlib import Path
from unittest import mock

from wayvoice import engine as _engine

#: Every location the application may read or write on the user's behalf.
#: ``XDG_RUNTIME_DIR`` is in the list because it is where both sockets live, and
#: a test that binds the real one collides with a running daemon.
#:
#: ``XDG_DATA_HOME`` used to be left alone, because it holds the engine runtime
#: and an empty one makes ``engine_status`` answer "not prepared".  That was the
#: wrong trade: leaving it real makes every engine-dependent test depend on what
#: this machine happens to have installed - on a machine without the runtime they
#: failed, and six of them failed on CI, which is a machine without it by
#: definition.  The engine's readiness is now stated by the test instead (see
#: ``isolate_engine``), which is also the honest arrangement: a test about the
#: model logic should say what it assumes about the engine.
#:
#: ``WAYVOICE_RUNTIME`` is in the list because it takes precedence over both of
#: the locations above, so a value inherited from the environment would quietly
#: win over the redirect.
_XDG_VARS = (
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
    "XDG_CACHE_HOME",
    "XDG_RUNTIME_DIR",
    "HF_HOME",
    "HF_HUB_CACHE",
    "HUGGINGFACE_HUB_CACHE",
    "WAYVOICE_RUNTIME",
)

#: What each engine hook is replaced with.  ``True``/``ready`` mean "there is
#: nothing to do here": the model counts as present and downloading it succeeds
#: instantly, which keeps every test on the path a user with a working
#: installation is on.
_ISOLATED = {
    # The one that would really spawn a process and load weights.
    "_start_worker": lambda cfg: False,
    "model_is_present": lambda cfg: True,
    "download_model": lambda cfg, on_progress=None, cancel_event=None: {
        "state": "ready", "error": "", "done": 0, "total": 0,
    },
}

#: What the engine answers when a test's daemon asks whether it is ready.
#:
#: The engine's own status is a question about this machine: does a prepared
#: runtime exist, is a setup running, did one fail.  A test about model logic has
#: no business inheriting the answer - on a machine without the runtime it said
#: "not prepared" and the daemon refused to record, which is correct behaviour
#: and the wrong subject for those tests.  A test that really exercises the
#: engine patches the status itself.
_READY_ENGINE = {"state": "ready", "message": "Ready"}

#: Module globals a test run can move and must not leave moved.
#:
#: ``_worker_retry_after`` is a backoff deadline written whenever a worker
#: cannot be started.  A test that fails to start one therefore leaves it in the
#: future, and every test after it - in whichever module the alphabetical order
#: happens to put next - skips starting a worker without even trying.  The
#: failure it causes is silence, in a code path that is otherwise fine.
_ISOLATED_GLOBALS = {"_worker_retry_after": 0.0}

#: What each name was before any test touched it.  Restoring these explicitly,
#: rather than through the patcher, keeps the cleanup correct when a test has
#: patched the same name itself: ``mock.patch`` restores whatever it found when
#: it stopped, which here would be this module's replacement rather than the
#: real function - and the leak would only show up in whichever module the
#: alphabetical ordering happened to run next.
_ORIGINALS = {name: getattr(_engine, name) for name in _ISOLATED}
_ORIGINAL_GLOBALS = {
    name: getattr(_engine, name) for name in _ISOLATED_GLOBALS
}


def isolate_environment(test) -> Path:
    """Redirect every XDG location to a directory that dies with the test.

    Call from ``setUp`` and use the returned path when a test needs to write a
    file the code under test will read back.  Everything the application can
    touch on the user's behalf is inside it, so a test cannot overwrite a real
    configuration even by accident - which is exactly how a daemon test once
    replaced the user's settings with defaults.
    """
    root = Path(tempfile.mkdtemp(prefix="wayvoice-test-"))
    test.addCleanup(shutil.rmtree, root, ignore_errors=True)
    for name in _XDG_VARS:
        patcher = mock.patch.dict(os.environ, {name: str(root / name)})
        patcher.start()
        test.addCleanup(patcher.stop)
        # The runtime has to exist inside the redirect, or the code under test
        # answers "not prepared" instead of doing what the test is about.
        (root / name).mkdir(parents=True, exist_ok=True)
    return root


def isolate_engine(test) -> None:
    """Stop a test's daemon from touching the real engine or its workers.

    Call from ``setUp``.  A test that needs different behaviour patches the same
    names afterwards and gets it for as long as its own patch lasts.

    Three things are answered here rather than inherited from the machine: the
    worker is not started, the model counts as present, and the engine counts as
    ready.  That last one is the subtle one - see ``_READY_ENGINE``.

    The guarantees are checked by ``test_isolation.py``, which is worth reading
    before trusting this one: everything here is a promise to the tests that
    follow, and a promise that quietly stops being kept costs a code path its
    only exercise.
    """
    for name, replacement in _ISOLATED.items():
        test.addCleanup(setattr, _engine, name, _ORIGINALS[name])
        setattr(_engine, name, replacement)
    for name, value in _ISOLATED_GLOBALS.items():
        test.addCleanup(setattr, _engine, name, _ORIGINAL_GLOBALS[name])
        setattr(_engine, name, value)
    # The daemon imported the name, so patching the engine's does not reach it.
    from wayvoice import daemon as _daemon

    for module in (_engine, _daemon):
        name = "engine_status"
        original = getattr(module, name)
        test.addCleanup(setattr, module, name, original)
        setattr(module, name, lambda cfg: dict(_READY_ENGINE))
