import unittest

from wayvoice import languages


class NormalizeTests(unittest.TestCase):
    def test_plain_codes_pass_through(self):
        self.assertEqual(languages.normalize("ru"), "ru")
        self.assertEqual(languages.normalize("en"), "en")
        self.assertEqual(languages.normalize("yue"), "yue")

    def test_spellings_of_the_same_language_agree(self):
        # Everything a locale, a config file or a hand-edited string can throw
        # at us has to land on the same code.
        for value in ("RU_ru", "ru-RU", " RU ", "Ru_RU.UTF-8"):
            self.assertEqual(languages.normalize(value), "ru")
        self.assertEqual(languages.normalize("de-DE"), "de")
        self.assertEqual(languages.normalize("ZH_HANS_CN"), "zh")

    def test_missing_and_unknown_become_auto(self):
        for value in ("", "   ", None, "xx", "klingon", "auto", "AUTO"):
            self.assertEqual(languages.normalize(value), languages.AUTO)


class ValidTests(unittest.TestCase):
    def test_known_codes_and_auto_are_valid(self):
        self.assertTrue(languages.is_valid("ru"))
        self.assertTrue(languages.is_valid("yue"))
        self.assertTrue(languages.is_valid(languages.AUTO))

    def test_unknown_codes_are_not_valid(self):
        for value in ("", None, "xx", "RU_ru"):
            self.assertFalse(languages.is_valid(value))


class TableTests(unittest.TestCase):
    def test_table_covers_whispers_languages(self):
        self.assertEqual(len(languages.CODES), 100)

    def test_every_code_appears_exactly_once(self):
        self.assertEqual(len(set(languages.CODES)), len(languages.CODES))
        self.assertEqual(set(languages.CODES), set(languages.NAMES))
        self.assertEqual(list(languages.CODES), list(languages.NAMES))

    def test_names_are_present_and_native_first(self):
        for code, (native, english) in languages.NAMES.items():
            self.assertTrue(native.strip(), code)
            self.assertTrue(english.strip(), code)
            self.assertEqual(languages.native_name(code), native)
            self.assertEqual(languages.english_name(code), english.capitalize())


class DisplayNameTests(unittest.TestCase):
    def test_english_ui_gets_both_names(self):
        self.assertEqual(languages.display_name("de", "en"), "Deutsch (German)")
        self.assertEqual(languages.display_name("ru", "en"), "Русский (Russian)")

    def test_other_ui_gets_the_native_name_only(self):
        # A Russian speaker looking for русский should not be shown
        # "Русский (Russian)" - the translation adds nothing for them.
        self.assertEqual(languages.display_name("de", "ru"), "Deutsch")
        self.assertEqual(languages.display_name("ru", "ru"), "Русский")

    def test_name_in_its_own_language_is_not_duplicated(self):
        self.assertEqual(languages.display_name("en", "en"), "English")

    def test_auto_has_no_name_of_its_own(self):
        self.assertEqual(languages.display_name(languages.AUTO, "en"), languages.AUTO)


class DetectFromLocaleTests(unittest.TestCase):
    def test_locale_names(self):
        self.assertEqual(languages.detect_from_locale(["de_DE.UTF-8"]), "de")
        self.assertEqual(languages.detect_from_locale(["ru_RU.UTF-8", "en_US"]), "ru")
        self.assertEqual(languages.detect_from_locale(["en_US.UTF-8", "ru_RU"]), "en")

    def test_territory_only_locale(self):
        self.assertEqual(languages.detect_from_locale(["pt_BR"]), "pt")

    def test_nothing_recognizable(self):
        for value in ([], None, [""], ["xx_XX.UTF-8"], [None]):
            self.assertEqual(languages.detect_from_locale(value), languages.AUTO)


if __name__ == "__main__":
    unittest.main()
