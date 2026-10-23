"""Test that UI widgets were extracted faithfully."""

import unittest
from tests.ui_support import GTK_AVAILABLE, gtk_display_available

if GTK_AVAILABLE:
    from gi.repository import Gtk
    from wayvoice.ui.widgets.labels import nearest_index, clip_subtitle, make_label
    from wayvoice.ui.widgets.language_picker import (
        language_choices, LanguagePicker, _enable_dropdown_search,
    )


@unittest.skipUnless(gtk_display_available(), "no GTK display available")
class TestWidgetsExtracted(unittest.TestCase):


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

    def test_enable_dropdown_search_enables_supported_widget(self):
        dropdown = Gtk.DropDown()
        result = _enable_dropdown_search(dropdown)
        if hasattr(dropdown, 'set_enable_search'):
            self.assertTrue(result)
            self.assertTrue(dropdown.get_enable_search())
        else:
            self.assertFalse(result)

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


@unittest.skipUnless(GTK_AVAILABLE, "GTK/libadwaita bindings unavailable")
class WidgetValueTests(unittest.TestCase):
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


    def test_search_falls_back_when_widget_lacks_search_api(self):
        self.assertFalse(_enable_dropdown_search(object()))

    def test_picker_keeps_public_import_contract(self):
        from wayvoice import ui
        self.assertIs(ui.LanguagePicker, LanguagePicker)
        codes, labels = ui.language_choices('en')
        self.assertIn('en', codes)
        self.assertEqual(len(codes), len(labels))


if __name__ == '__main__':
    unittest.main()
