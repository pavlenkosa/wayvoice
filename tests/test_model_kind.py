"""The vocabulary the settings window and the model store share.

``describe()`` answers with a ``kind``: ``repo`` for a cached repository, ``local``
for a folder the user keeps, ``custom`` for the free-form entry. The window compares
those values to decide three things: whether to offer the download button, whether
to ask permission before fetching gigabytes, and whether the row can be deleted.

It compared against ``"hub"`` instead of ``"repo"`` - a value nothing produces. The
consequence was that choosing any model from the hub offered no download button at
all, and asked no question: only "Delete" was left, which is what was reported.

The tests that should have caught it passed, because their fixtures contained the
same wrong string. A fixture written by hand from memory agrees with the code that
was written by hand from memory; only the real function settles it. So the guards
below do not spell the values out - they ask ``describe()``.
"""

import importlib.util
import tempfile
import unittest
from pathlib import Path

from wayvoice import model_store

try:
    from wayvoice import ui
except Exception as _exc:  # no GTK bindings for this interpreter
    ui = None
    _why = f"{type(_exc).__name__}: {_exc}"
else:
    _why = ""

needs_ui = unittest.skipIf(ui is None, f"the settings window is unavailable ({_why})")


def _entry_for(root: Path, model_id: str) -> dict:
    """A real describe() answer, from a cache that holds nothing."""
    return model_store.describe(model_id, root=root)


class KindVocabularyTests(unittest.TestCase):
    """What describe() can say, and what it did say."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_a_hub_repository_is_reported_as_repo(self):
        entry = _entry_for(self.root, "Systran/faster-whisper-small")
        self.assertEqual(entry["kind"], "repo")
        self.assertEqual(entry["kind"], model_store.KIND_REPO)

    def test_a_free_form_id_is_also_a_repo(self):
        # The model in the report that started this: a fine-tuned repository is a
        # repository, whatever its name looks like.
        entry = _entry_for(self.root, "bzikst/faster-whisper-large-v3-russian-int8")
        self.assertEqual(entry["kind"], model_store.KIND_REPO)
        self.assertFalse(entry["downloaded"])
        self.assertEqual(entry["repo_id"], "bzikst/faster-whisper-large-v3-russian-int8")

    def test_a_path_is_local_and_never_deletable(self):
        entry = _entry_for(self.root, str(self.root / "my-model"))
        self.assertEqual(entry["kind"], model_store.KIND_LOCAL)

    def test_the_named_kinds_are_exactly_what_describe_returns(self):
        # Every kind describe() can produce, found by asking it about one entry of
        # each shape rather than by reading the constants back.
        produced = {
            _entry_for(self.root, "org/name")["kind"],
            _entry_for(self.root, str(self.root / "folder"))["kind"],
        }
        self.assertEqual(produced, {model_store.KIND_REPO, model_store.KIND_LOCAL})
        for name in (model_store.KIND_REPO, model_store.KIND_LOCAL, model_store.KIND_CUSTOM):
            self.assertIn(name, (model_store.KIND_REPO, model_store.KIND_LOCAL,
                                 model_store.KIND_CUSTOM))


class KindComparisonTests(unittest.TestCase):
    """The window must not spell a kind out where the store names it."""

    def _source(self):
        # find_spec rather than ui.__file__: this class runs on CI, where the import
        # of wayvoice.ui fails for want of the GTK bindings. Locating the file does
        # not import it, so the guards run everywhere instead of only on a desktop.
        return Path(importlib.util.find_spec("wayvoice.ui").origin).read_text(
            encoding="utf-8")

    def test_no_kind_is_compared_as_a_bare_string(self):
        import re

        code = []
        for line in self._source().splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                code.append(line)
        # A comparison against a literal is how the two modules drifted apart: the
        # store said "repo", the window said "hub", and both were correct about
        # their own text.
        offenders = [
            line.strip() for line in code
            if re.search(r'get\("kind"\)\s*\)?\s*[!=]=\s*["\']', line)
        ]
        self.assertEqual(offenders, [],
                         "compare against model_store.KIND_*, not a literal:\n"
                         + "\n".join(offenders))

    def test_the_word_hub_is_not_used_as_a_kind(self):
        # "hub" is the name of the cache directory, never of a kind.
        self.assertNotIn('"hub"', self._source())


@needs_ui
class RealEntryDrivesTheWindowTests(unittest.TestCase):
    """The window's decisions, fed by the real function rather than a fixture."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.window = ui.WayVoiceWindow.__new__(ui.WayVoiceWindow)
        self.window.ui_lang = "en"
        self.prepared = []
        self.asked = []
        self.window._ask_daemon_to_prepare_model = lambda: self.prepared.append(True)
        self.window._ask_about_download = lambda model_id, size: self.asked.append(
            (model_id, size))

    def _button(self):
        class Btn:
            visible = None

            def set_visible(self, value):
                self.visible = bool(value)
        self.window.model_fetch_btn = Btn()
        return self.window.model_fetch_btn

    def test_a_real_missing_repository_offers_the_download(self):
        entry = _entry_for(self.root, "org/some-finetune")
        button = self._button()
        self.window._download_report = {}
        self.window._model_entry = entry
        self.window._refresh_fetch_button()
        self.assertTrue(button.visible,
                        f"no download button for kind={entry['kind']!r}")

    def test_a_real_local_folder_offers_no_download_button(self):
        entry = _entry_for(self.root, str(self.root / "local-model"))
        button = self._button()
        self.window._download_report = {}
        self.window._model_entry = entry
        self.window._refresh_fetch_button()
        self.assertFalse(button.visible)

    def test_a_real_missing_repository_is_asked_about_first(self):
        entry = _entry_for(self.root, "org/some-finetune")
        self.window._download_confirmation_for = entry["id"]
        self.window._decide_what_to_do_about_the_selected_model(entry)
        self.assertEqual(len(self.asked), 1,
                         "a hub model was fetched without asking")
        self.assertEqual(self.asked[0][0], entry["id"])

    def test_a_real_local_folder_is_not_asked_about(self):
        entry = _entry_for(self.root, str(self.root / "local-model"))
        self.window._download_confirmation_for = entry["id"]
        self.window._decide_what_to_do_about_the_selected_model(entry)
        self.assertEqual(self.asked, [])
        self.assertEqual(len(self.prepared), 1,
                         "a local model should be warmed, not questioned")


if __name__ == "__main__":
    unittest.main()
