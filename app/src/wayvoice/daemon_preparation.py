from __future__ import annotations

from copy import deepcopy
from types import MappingProxyType
import threading
import traceback
import uuid

from .engine import engine_from_config, model_state, prepare_model, TranscriptionCancelled


class ModelPreparationController:
    """Own one addressed model operation and its published progress snapshot.

    The shared lifecycle lock makes starting preparation atomic with daemon model
    maintenance and shutdown. All slow preparation work runs outside that lock.
    """

    def __init__(self, lock, shutdown, unavailable):
        self._lock = lock
        self._shutdown = shutdown
        self._unavailable = unavailable
        self.report = {"state": "idle", "model": "", "done_bytes": 0,
                       "total_bytes": 0, "error": "", "warming": False}
        self.thread = None
        self.running = False
        self.cancel_event = threading.Event()

    def snapshot(self) -> dict:
        """Read progress without waiting behind daemon lifecycle I/O."""
        return dict(self.report)

    def start(self, cfg: dict, download: bool = True) -> str:
        """Get the selected model ready: fetch it if asked to, then warm it.

        Returns the phase that was *started* - ``"downloading"`` or ``"warming"`` - read
        before the thread has had a chance to finish. Reporting the state the background work
        had reached by then would be a race, and a race that answers "ready" for a load that
        has just begun.

        Only one run at a time: two downloads of the same 3 GB file would fight over the same
        cache, and the second one's progress would be the first one's failure.

        A model that is already on disk is never fetched - the daemon asks this on every start
        and the answer must not involve the network - so ``download=False`` is what startup
        uses. Asking to prepare a model that *is* on disk warms it rather than reporting
        "already done", which used to leave someone who had just picked a different model
        facing a slow first dictation with nothing having told them it was coming.
        """
        if self._shutdown.is_set() or self._unavailable():
            return ""
        settings = MappingProxyType(deepcopy(dict(cfg)))
        engine = engine_from_config(settings)
        state = model_state(engine, settings)
        if not state["supported"]:
            return ""
        if state["present"]:
            # Nothing to fetch. Warming it is still the point of being asked, and it
            # needs a worker to warm into - the user may have turned that off.
            if not settings.get("engine_worker", True):
                return ""
            download = False
        elif not download:
            # Asked to warm up a model that is not there.
            return ""
        with self._lock:
            if self._shutdown.is_set() or self.running or self._unavailable():
                return ""
            self.running = True
            self.cancel_event.clear()
            phase = "downloading" if download else "warming"
            self.report = {
                "state": phase,
                "operation_id": uuid.uuid4().hex,
                "engine": engine.id,
                "model": state["model"],
                "done_bytes": 0,
                "total_bytes": 0,
                "error": "",
                "warming": not download,
            }
            try:
                thread = threading.Thread(
                    target=self.run,
                    args=(settings, dict(self.report), download),
                    daemon=True,
                )
            except Exception as exc:
                self.running = False
                self.report = {**self.report, "state": "error", "error": str(exc),
                               "warming": False}
                return ""
            self.thread = thread
        try:
            with self._lock:
                if self._shutdown.is_set():
                    self.running = False
                    self.thread = None
                    self.report = {**self.report, "state": "cancelled", "warming": False}
                    return ""
            thread.start()
        except Exception as exc:
            with self._lock:
                self.running = False
                self.thread = None
                self.report = {**self.report, "state": "error",
                               "done_bytes": 0, "total_bytes": 0,
                               "error": str(exc), "warming": False}
            return ""
        return phase

    def run(self, cfg: dict, reported: dict, download: bool) -> None:
        """Fetch the model when asked to, then let the warm worker hold it.

        Runs on its own thread and holds the lifecycle lock only to publish progress: a dictation may be
        waiting on the hot key while three gigabytes come down.
        """
        result = {"state": "cancelled"}

        def on_progress(done: int, total: int) -> None:
            with self._lock:
                # Never resurrect a download the user cancelled, and never overwrite
                # the error of one that has already finished.
                if self.report.get("operation_id") != reported.get("operation_id") or self.report.get("state") != "downloading":
                    return
                self.report = {**self.report, "done_bytes": int(done), "total_bytes": int(total)}

        def on_warming() -> None:
            with self._lock:
                if self.report.get("operation_id") == reported.get("operation_id") and self.report.get("state") == "downloading":
                    # From the user's side this is the same wait: the weights are down and
                    # the model is going into memory. Reporting it only in the final
                    # answer would leave the window at 100% for the whole load.
                    self.report = {**self.report, "warming": True}

        try:
            if self._shutdown.is_set() or self.cancel_event.is_set():
                raise TranscriptionCancelled("Model preparation cancelled")
            engine = engine_from_config(cfg)
            result = prepare_model(
                engine, cfg, on_progress, self.cancel_event, on_warming, download
            )
        except TranscriptionCancelled:
            # A cancelled preparation is not a failure: "cancelled" is a state the
            # engine's own contract lists, and the window shows it as such.
            result = {"state": "cancelled"}
        except Exception as exc:
            # prepare_model is documented to answer with a dict, but a value it does
            # not expect - a hand-edited config, a helper that died in a way it does
            # not recognise - raises instead. Without this the thread ended there,
            # and because _prepare_running was only cleared on the success path it
            # stayed set: the settings button and `wayvoice model --download` were
            # then refused for the rest of the session, and the window's spinner
            # turned over a download that was not happening.
            traceback.print_exc()
            result = {"state": "error", "error": str(exc)}
        finally:
            with self._lock:
                if self.report.get("operation_id") != reported.get("operation_id"):
                    return
                if not isinstance(result, dict):
                    result = {"state": "error", "error": str(result)}
                try:
                    done = int(result.get("done") or 0)
                    total = int(result.get("total") or 0)
                except (TypeError, ValueError, OverflowError) as exc:
                    result = {"state": "error", "error": str(exc)}
                    done = total = 0
                self.report = {
                    "state": str(result.get("state") or "ready"),
                    "model": reported.get("model", ""),
                    "operation_id": reported.get("operation_id", ""),
                    "engine": reported.get("engine", ""),
                    "done_bytes": done,
                    "total_bytes": total,
                    "error": str(result.get("error") or ""),
                    # Whether the warm-up succeeded, not whether one is running: the
                    # reply ends the work, and a window still saying "preparing"
                    # would never stop.
                    "warming": False,
                }
                # In the finally, so that no path out of this thread can leave the
                # daemon permanently unable to start another preparation.
                self.running = False
                self.thread = None

    def cancel(self, operation_id=None) -> str:
        with self._lock:
            if operation_id is not None and operation_id != self.report.get("operation_id"):
                return "stale"
            if not self.running:
                return "nothing"
            self.cancel_event.set()
            return "warming" if self.report.get("warming") or self.report.get("state") == "warming" else "download"
