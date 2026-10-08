"""Window-owned GLib sources and guarded background completions.

Workers may finish after closing; GTK callbacks must not run on a disposed view.
Domain owners retain cancellation policy for external operations.
"""
import logging
import threading


class TaskRunner:
    def __init__(self, glib=None):
        if glib is None:
            from gi.repository import GLib
            glib = GLib
        self.glib = glib
        self.closed = False
        self._sources = set()
        self._lock = threading.RLock()

    def _source(self, schedule, callback, args, repeat):
        with self._lock:
            if self.closed:
                return None
            source = None

            def dispatch():
                with self._lock:
                    if self.closed:
                        return False
                    if not repeat:
                        self._sources.discard(source)
                try:
                    result = callback(*args)
                except Exception:
                    logging.exception("WayVoice UI callback failed")
                    with self._lock:
                        self._sources.discard(source)
                    return False
                if not repeat or not result:
                    with self._lock:
                        self._sources.discard(source)
                    return False
                return True

            source = schedule(dispatch)
            self._sources.add(source)
            return source

    def idle(self, callback, *args):
        return self._source(self.glib.idle_add, callback, args, False)

    def every(self, milliseconds, callback):
        return self._source(lambda cb: self.glib.timeout_add(milliseconds, cb), callback, (), True)

    def run(self, work, done, failed=None):
        with self._lock:
            if self.closed:
                return False

        def worker():
            try:
                result = work()
            except Exception as exc:
                if failed is not None:
                    self.idle(failed, exc)
                else:
                    logging.exception('WayVoice UI background operation failed')
            else:
                self.idle(done, result)
        try:
            threading.Thread(target=worker, daemon=True).start()
        except RuntimeError as exc:
            if failed is not None:
                failed(exc)
            else:
                logging.exception('WayVoice UI worker could not start')
            return False
        return True

    def close(self):
        with self._lock:
            if self.closed:
                return
            self.closed = True
            sources, self._sources = self._sources, set()
        for source in sources:
            self.glib.source_remove(source)
