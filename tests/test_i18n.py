import unittest
from wayvoice.i18n import resolve_language, tr

class I18nTests(unittest.TestCase):
    def test_explicit_languages(self):
        self.assertEqual(resolve_language("ru"), "ru")
        self.assertEqual(resolve_language("en"), "en")

    def test_translation_exists(self):
        self.assertEqual(tr("nav.home", "ru"), "Главная")
        self.assertEqual(tr("nav.home", "en"), "Home")

if __name__ == "__main__":
    unittest.main()
