"""Test that UI widgets were extracted faithfully."""

import unittest
import os


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


try:
    from wayvoice.ui.widgets.labels import nearest_index, clip_subtitle, make_label
    from wayvoice.ui.widgets.language_picker import (
        language_choices,
        LanguagePicker,
        _enable_dropdown_search,
    )
except Exception as _exc:  # no GTK bindings for this interpreter
    nearest_index = clip_subtitle = make_label = None
    language_choices = LanguagePicker = _enable_dropdown_search = None
    _why = f"{type(_exc).__name__}: {_exc}"
else:
    _why = ""


@unittest.skipUnless(_gtk_available(), "no GTK display available")
class TestWidgetsExtracted(unittest.TestCase):

    def test_language_choices_cover_auto_and_supported_languages(self):
        from wayvoice import languages
        for language in ('en', 'ru'):
            codes, labels = language_choices(language)
            self.assertEqual(codes[0], languages.AUTO)
            self.assertIn('ru', codes)
            self.assertIn('en', codes)
            self.assertEqual(len(codes), len(labels))
            self.assertEqual(len(codes), len(set(codes)))
            self.assertTrue(all(isinstance(label, str) and label for label in labels))

    def test_language_picker_constructs_correctly(self):
        """Test that LanguagePicker constructs a real GTK widget."""
        # Create a simple test with basic language data
        codes = ['en', 'ru', 'de']
        labels = ['English', 'Russian', 'German']

        picker = LanguagePicker("Test Language", codes, labels, 0)

        # Should have a row attribute
        self.assertTrue(hasattr(picker, 'row'))

        # Should be able to get/set selected
        self.assertEqual(picker.get_selected(), 0)
        picker.set_selected(1)
        self.assertEqual(picker.get_selected(), 1)

        # Should be able to get selected code
        self.assertEqual(picker.selected_code(), 'ru')

        # Should be able to select code
        picker.select_code('en')
        self.assertEqual(picker.selected_code(), 'en')

    def test_enable_dropdown_search_functionality(self):
        """Test that _enable_dropdown_search works the same way."""
        # Create a dropdown to test with
        import gi
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        dropdown = Gtk.DropDown()

        # Test that it returns a boolean (this should not crash)
        result = _enable_dropdown_search(dropdown)
        self.assertIsInstance(result, bool)

    def test_nearest_index_function(self):
        """Test nearest_index function."""
        values = [30, 60, 90, 120, 180]
        self.assertEqual(nearest_index(values, 100), 2)  # Index of 90
        self.assertEqual(nearest_index(values, 150), 3)  # Index of 120
        self.assertEqual(nearest_index(values, 5), 0)   # Index of 30

    def test_clip_subtitle_function(self):
        """Test clip_subtitle function."""
        # Test default limit
        text = "A" * 121  # Longer than 120 chars
        result = clip_subtitle(text)
        # The function should truncate to 120 chars and add ellipsis
        self.assertLessEqual(len(result), 120)
        # Verify it ends with the Unicode ellipsis character
        self.assertTrue(result.endswith("…"))

        # Test shorter text
        short_text = "Short text"
        result = clip_subtitle(short_text)
        self.assertEqual(result, short_text)

        # Test with actual long text
        long_text = "way " * 40  # 160 chars, actually exceeds the 120-char limit
        result = clip_subtitle(long_text)
        # The function should truncate to 120 chars and add ellipsis
        self.assertLessEqual(len(result), 120)
        # Verify it ends with the Unicode ellipsis character
        self.assertTrue(result.endswith("…"))

    def test_make_label_function(self):
        """Test make_label function."""
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Pango", "1.0")
        from gi.repository import Gtk

        label = make_label("Test Label", "test-css-class", wrap=True, xalign=0.5)

        # Should be a Gtk.Label
        self.assertIsInstance(label, Gtk.Label)

        # Should have the right properties
        self.assertEqual(label.get_label(), "Test Label")
        self.assertTrue(label.has_css_class("test-css-class"))
        self.assertTrue(label.get_wrap())
        self.assertEqual(label.get_xalign(), 0.5)


class PublicWidgetImportTests(unittest.TestCase):
    def test_picker_keeps_public_import_contract(self):
        try:
            from wayvoice import ui
        except Exception as exc:
            self.skipTest(f"the settings window is unavailable ({exc})")
        self.assertIs(ui.LanguagePicker, LanguagePicker)
        codes, labels = ui.language_choices('en')
        self.assertIn('en', codes)
        self.assertEqual(len(codes), len(labels))


if __name__ == '__main__':
    unittest.main()
