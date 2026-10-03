"""Keeping a test run off the machine it runs on.

Some parts of the daemon do real work outside their own process: it reads the
user's config, walks the user's model cache, and - since a daemon loads its
model at startup - starts a warm worker that sits there holding a model in
memory.  A test that constructs a daemon to check, say, its accept loop has no
business doing any of that: it depends on whatever the machine happens to have
installed, it can leave a real worker running after the run, and a failure then
looks like a defect in the code rather than in the test.

:func:`isolate_engine` cuts those paths for one test case.  It patches the
worker *start* rather than the warm-up above it, so ``ensure_worker``,
``warm_worker`` and ``prepare_model`` keep their real logic and only the thing
that would put a model in memory is taken away.
"""

from wayvoice import engine as _engine

#: What each hook is replaced with.  ``True``/``ready`` mean "there is nothing to
#: do here": the model counts as present and downloading it succeeds instantly,
#: which is what keeps every test on the path a user with a working installation
#: is on.
_ISOLATED = {
    # The one that would really spawn a process and load weights.
    "_start_worker": lambda cfg: False,
    "model_is_present": lambda cfg: True,
    "download_model": lambda cfg, on_progress=None, cancel_event=None: {
        "state": "ready", "error": "", "done": 0, "total": 0,
    },
}

#: What each name was before any test touched it.  Restoring these explicitly,
#: rather than through the patcher, keeps the cleanup correct when a test has
#: patched the same name itself: ``mock.patch`` restores whatever it found when
#: it stopped, which here would be this module's replacement rather than the
#: real function - and the leak would only show up in whichever module the
#: alphabetical ordering happened to run next.
_ORIGINALS = {name: getattr(_engine, name) for name in _ISOLATED}


def isolate_engine(test) -> None:
    """Stop a test's daemon from touching the real engine, cache or workers.

    Call from ``setUp``.  A test that needs different behaviour patches the same
    names afterwards and gets it for as long as its own patch lasts.
    """
    for name, replacement in _ISOLATED.items():
        test.addCleanup(setattr, _engine, name, _ORIGINALS[name])
        setattr(_engine, name, replacement)
