"""Download a model into the hub cache, reporting progress as it goes.

Run by the *engine runtime* interpreter, not by the system Python, because
``huggingface_hub`` only exists in that virtual environment.  The module lives
inside the package and puts its parent on ``sys.path``, exactly like
``fw_runner.py``.

Why a separate process at all: the download can take minutes to hours, it must
be cancellable, and the daemon has to keep answering the hot key while it runs.
A child process gives all three for free, and it also means a download that
crashes the hub library takes nothing else with it.

The protocol on stdout is one line per event, all prefixed so that nothing the
library prints can be mistaken for it::

    WV-PROGRESS <done_bytes> <total_bytes>
    WV-READY <snapshot_path>
    WV-ERROR <message>

Progress lines are throttled: the hub calls ``update()`` per chunk, and a
4 GiB download would otherwise produce tens of thousands of lines for a
consumer that repaints twice a second.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _ensure_import_path() -> None:
    """Make the ``wayvoice`` package importable from this script.

    The runtime interpreter knows nothing about the application sources, and
    this module lives inside the package directory, so the import root is its
    parent -- the same layout for a source checkout and for the installed
    package.
    """
    root = Path(__file__).resolve().parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))


_ensure_import_path()

#: Printed between reports.  Fast enough for a progress bar to look alive,
#: sparse enough that a slow line of progress costs nothing.
REPORT_INTERVAL = 0.25


def expected_total(repo_id: str, patterns: list[str]) -> int:
    """Bytes the download will put on disk, or ``0`` when that is unknown.

    Read from the repository metadata instead of from the hub's progress bars.
    Those bars are two of them - one counting bytes written, one counting bytes
    that arrived over the network - and both are handed the size of every file,
    while the network one additionally invents its own total as it goes.  Adding
    them reported a 1.5 GB model as 3 GB and made the bar jump backwards.  The
    metadata is one request, and it is also the number that stays right when the
    files are already partly cached.

    ``0`` means "do not pretend to know": an offline machine, a rate limit or an
    unexpected answer all leave the caller with an indeterminate bar rather than
    a wrong percentage.
    """
    from fnmatch import fnmatch

    try:
        from huggingface_hub import HfApi

        info = HfApi().model_info(repo_id, files_metadata=True)
        total = 0
        for sibling in info.siblings or ():
            name = str(getattr(sibling, "rfilename", "") or "")
            size = int(getattr(sibling, "size", 0) or 0)
            if size > 0 and any(fnmatch(name, pattern) for pattern in patterns):
                total += size
        return total
    except Exception:
        return 0


class Reporter:
    """Turns the hub's progress bars into one line of bytes.

    The denominator is the size the repository says the model has; the numerator
    is the highest number of bytes any of the hub's byte counters has reached.
    Reporting is throttled: a download of a few gigabytes calls ``update()`` per
    chunk, and a consumer that repaints twice a second does not need tens of
    thousands of lines.
    """

    def __init__(self, total: int = 0, interval: float = REPORT_INTERVAL) -> None:
        self._interval = interval
        self._bars: list[object] = []
        self._total = max(0, int(total))
        self._last = -1.0

    @property
    def total(self) -> int:
        """Bytes expected, as far as it is known."""
        return self._total

    def register(self, bar) -> None:
        self._bars.append(bar)
        self.report(force=False)

    def counts(self) -> tuple[int, int]:
        """Bytes landed so far, and bytes expected.

        The hub counts the same bytes twice - once as they are written, once as
        they arrive - so the larger counter is taken rather than the sum.  Bars
        that never move are ignored by that maximum on their own, which is what
        drops the network bar when the download did not go through xet.
        """
        done = 0
        learned = 0
        for bar in self._bars:
            if getattr(bar, "unit", "") != "B":
                # The per-file bar of the thread map counts files, not bytes.
                continue
            done = max(done, int(getattr(bar, "n", 0) or 0))
            learned = max(learned, int(getattr(bar, "total", 0) or 0))
        if self._total <= 0:
            # Unknown before the download: use what the bars have learned.
            self._total = learned
        return done, self._total

    def report(self, force: bool = True) -> None:
        """Print the current counts, at most once per interval."""
        import time

        now = time.monotonic()
        if not force and (now - self._last) < self._interval:
            return
        self._last = now
        done, total = self.counts()
        print(f"WV-PROGRESS {done} {total}", flush=True)


def progress_tqdm_class(reporter: Reporter):
    """Build the progress-bar class the hub will instantiate.

    ``huggingface_hub`` takes any object with the ``tqdm`` shape, treats it as
    a context manager, feeds it with ``update(n)`` and then reads ``total``,
    ``n`` and a handful of cosmetic setters back out of it.  Nothing is drawn:
    this process' stdout is a pipe, and the only thing worth writing to it is
    a number the daemon can turn into a bar.

    A fresh class per call keeps the reporter private to one download, which
    matters because ``snapshot_download`` downloads several files concurrently
    and creates one bar for the transfer and one for the write.
    """

    class _ProgressBar:
        def __init__(self, *args, **kwargs) -> None:
            # ``total`` is 0 at creation and raised by the hub once the size of
            # the file is known, so it has to stay a plain attribute.
            self.n = int(kwargs.get("initial") or 0)
            self.total = int(kwargs.get("total") or 0)
            self.unit = str(kwargs.get("unit") or "")
            self.desc = str(kwargs.get("desc") or "")
            reporter.register(self)

        def update(self, amount=1):
            self.n += int(amount or 0)
            reporter.report(force=False)

        def update_transfer(self, _amount=1):
            # Bytes on the wire, which the hub also reports as written bytes.
            # Counting both would double the progress of every download.
            return

        def refresh(self):
            reporter.report(force=False)

        def set_postfix_str(self, *_args, **_kwargs):
            return

        def set_description(self, *_args, **_kwargs):
            return

        def set_description_str(self, *_args, **_kwargs):
            return

        def close(self):
            reporter.report(force=False)

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            self.close()
            return False

    return _ProgressBar


def download(repo_id: str, cache_dir: str = "", revision: str = "") -> str:
    """Fetch ``repo_id`` into the cache and return the snapshot directory.

    Raises whatever ``snapshot_download`` raises; the caller turns it into a
    message.  The snapshot path is printed only after the hub says it is
    complete, so a ``WV-READY`` line always means usable weights.
    """
    from huggingface_hub import snapshot_download

    from wayvoice import model_store

    patterns = list(model_store.FETCH_PATTERNS)
    reporter = Reporter(total=expected_total(repo_id, patterns))
    # The hub's own bars are silenced: they go to stderr and would drown the
    # protocol this process speaks on stdout.  Only the files that make the
    # model usable are fetched -- see model_store.FETCH_PATTERNS for why this is
    # not simply "everything in the repository".
    kwargs = {
        "tqdm_class": progress_tqdm_class(reporter),
        "allow_patterns": patterns,
    }
    if cache_dir:
        kwargs["cache_dir"] = cache_dir
    if revision:
        kwargs["revision"] = revision
    path = snapshot_download(repo_id, **kwargs)
    reporter.report(force=True)
    return str(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Download a Whisper model into the Hugging Face cache."
    )
    parser.add_argument("--repo", required=True, help="hub repository id")
    parser.add_argument("--cache-dir", default="", help="hub cache directory")
    parser.add_argument("--revision", default="", help="branch, tag or commit")
    args = parser.parse_args(argv)

    try:
        path = download(args.repo, cache_dir=args.cache_dir, revision=args.revision)
    except KeyboardInterrupt:
        print("WV-ERROR cancelled", flush=True)
        return 130
    except Exception as exc:  # the library raises a wide range of errors
        print(f"WV-ERROR {exc}", flush=True)
        return 1
    print(f"WV-READY {path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
