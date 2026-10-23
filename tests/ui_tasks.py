"""Display-independent GLib/task fixtures shared by UI tests."""
import importlib.util
import threading
from pathlib import Path

spec = importlib.util.spec_from_file_location('wayvoice_ui_async_tasks', Path(__file__).resolve().parents[1] / 'app/src/wayvoice/ui/async_tasks.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
TaskRunner = module.TaskRunner


class FakeGLib:
    def __init__(self):
        self.sources = {}
        self.removed = []
        self.counter = 0
        self.ready = threading.Event()

    def idle_add(self, callback):
        self.counter += 1
        self.sources[self.counter] = callback
        self.ready.set()
        return self.counter

    def timeout_add(self, _ms, callback):
        return self.idle_add(callback)

    def source_remove(self, source):
        self.removed.append(source)
        self.sources.pop(source, None)

    def fire(self, source):
        if not self.sources[source]():
            self.sources.pop(source, None)

    def drain(self):
        for source in list(self.sources):
            self.fire(source)

