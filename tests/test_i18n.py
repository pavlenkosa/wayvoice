import unittest
from unittest import mock
from wayvoice.i18n import _EN, _RU, resolve_language, tr

class I18nTests(unittest.TestCase):
    def test_explicit_languages(self):
        self.assertEqual(resolve_language("ru"), "ru")
        self.assertEqual(resolve_language("en"), "en")

    def test_translation_exists(self):
        self.assertEqual(tr("nav.home", "ru"), "Главная")
        self.assertEqual(tr("nav.home", "en"), "Home")

    def test_every_key_is_translated_in_both_languages(self):
        # A key added to only one dictionary silently falls back to English in
        # the Russian UI, which is easy to miss in a screenshot-free review.
        self.assertEqual(sorted(_RU), sorted(_EN))
        self.assertEqual(len(_RU), len(_EN))

    def test_locale_is_used_for_auto(self):
        with mock.patch("wayvoice.i18n.locale.getlocale", return_value=("ru_RU", "UTF-8")):
            self.assertEqual(resolve_language("auto"), "ru")
        with mock.patch("wayvoice.i18n.locale.getlocale", return_value=("de_DE", "UTF-8")):
            self.assertEqual(resolve_language(None), "en")

    def test_explicit_choice_beats_the_locale(self):
        with mock.patch("wayvoice.i18n.locale.getlocale", return_value=("ru_RU", "UTF-8")):
            self.assertEqual(resolve_language("en"), "en")


if __name__ == "__main__":
    unittest.main()
