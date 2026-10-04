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
#: ``XDG_DATA_HOME`` is deliberately *not* redirected.  It holds the engine
#: runtime, which is a prepared virtual environment shared by every test that
#: touches the engine; pointing a test at an empty directory makes
#: ``engine_status`` answer "not prepared", and the daemon's constructor then
#: asks for the engine to be prepared - a real ``systemctl`` call or a detached
#: ``pip install`` from inside a unit test.  Reading the real runtime is safe:
#: nothing in a test run writes to it.  A test that really exercises the engine
#: setup has to redirect this one itself.
_XDG_VARS = (
    "XDG_CONFIG_HOME",
    "XDG_STATE_HOME",
    "XDG_CACHE_HOME",
    "XDG_RUNTIME_DIR",
    "HF_HOME",
    "HF_HUB_CACHE",
    "HUGGINGFACE_HUB_CACHE",
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
