"""The recognition-language row must report what the user picked.

These tests need a real GTK, because the bug they guard against is the control's own
state: a :class:`Gtk.DropDown` changes its selection without telling anyone, so nothing
but reading it back can catch it.
"""

import os
import unittest


def _gtk_available() -> bool:
    """Whether this process can really open a GTK window.

    Not ``Gtk.init_check()``: with the bindings installed but no display - a build server, a
    plain ssh session - that still answers true, the tests go on to build widgets, and GTK
    takes the process down. A segfault is worse than a failure, because it takes the other
    four hundred tests with it.
    """
    if not (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")):
        return False
    try:
        import gi

        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Gtk

        return bool(Gtk.init_check())
    except Exception:
        return False


@unittest.skipUnless(_gtk_available(), "no GTK display available")
class LanguagePickerTests(unittest.TestCase):
    def setUp(self):
        import gi

        gi.require_version("Adw", "1")
        from gi.repository import Adw

        from wayvoice import ui

        Adw.init()
        self.ui = ui
        self.codes, self.labels = ui.language_choices("en")
        self.picker = ui.LanguagePicker("Language", self.codes, self.labels, 0)

    def test_choice_list_covers_every_language_plus_detection(self):
        import gi

        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        from wayvoice import languages

        self.assertEqual(len(self.codes), len(languages.CODES) + 1)
        self.assertEqual(self.codes[0], languages.AUTO)
        self.assertEqual(self.labels[0], "Auto")
        self.assertEqual(len(self.labels), len(self.codes))
        model = Gtk.DropDown.get_model(
            self.picker._dropdown if self.picker._dropdown is not None else self.picker.row
        )
        self.assertEqual(model.get_n_items(), len(self.codes))

    def test_user_selection_is_what_gets_reported(self):
        # The picker starts on detection; the user moves it to German.
        self.assertEqual(self.picker.selected_code(), "auto")
        self.picker._control().set_selected(self.codes.index("de"))
        self.assertEqual(self.picker.selected_code(), "de")
        self.assertEqual(
            self.picker.get_selected(),
            self.codes.index("de"),
        )

    def test_select_code_reports_whether_the_language_exists(self):
        self.assertTrue(self.picker.select_code("yue"))
        self.assertEqual(self.picker.selected_code(), "yue")
        # "klingon" normalizes to detection too, but saying "yes" here would claim a
        # language the list does not contain.
        self.assertFalse(self.picker.select_code("klingon"))
        self.assertEqual(self.picker.selected_code(), "auto")

    def test_out_of_range_index_does_not_break_saving(self):
        self.picker.set_selected(len(self.codes) + 5)
        self.assertEqual(self.picker.get_selected(), 0)
        self.assertEqual(self.picker.selected_code(), "auto")

    def test_forced_model_language_disables_the_row(self):
        self.picker.select_code("en")
        self.picker.set_sensitive(False)
        self.assertFalse(self.picker.row.get_sensitive())
        if self.picker._dropdown is not None:
            self.assertFalse(self.picker._dropdown.get_sensitive())
        self.assertEqual(self.picker.selected_code(), "en")


class LanguageChoiceTests(unittest.TestCase):
    """The label list itself: no widgets, but the module that builds it.

    ``wayvoice.ui`` imports GTK, so this skips rather than fails where the bindings are
    absent - the unit-test job runs under the interpreter setup-python installed, which
    cannot see the system ``python3-gi``. A module that fails to import fails the whole
    discovery run.
    """

    def setUp(self):
        try:
            from wayvoice import ui
        except Exception as exc:
            self.skipTest(f"the settings window is unavailable ({type(exc).__name__})")
        self.ui = ui

    def test_labels_are_localized_by_the_interface_language(self):
        from wayvoice import languages

        ui = self.ui
        codes, ru_labels = ui.language_choices("ru")
        _, en_labels = ui.language_choices("en")
        self.assertEqual(len(codes), len(ru_labels))
        ru_index = codes.index("de")
        en_index = codes.index("de")
        self.assertEqual(ru_labels[ru_index], "Deutsch")
        # An English interface adds the English name, because that is the one
        # the reader is looking for there.
        self.assertEqual(en_labels[en_index], "Deutsch (German)")
        self.assertEqual(ru_labels[codes.index("ru")], "Русский")
        self.assertEqual(
            len({label for label in ru_labels}),
            len(ru_labels),
            "two languages must not end up under the same label",
        )
        self.assertNotIn("", ru_labels)
        self.assertNotEqual(languages.AUTO, languages.CODES[0])


if __name__ == "__main__":
    unittest.main()
