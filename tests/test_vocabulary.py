import unittest

from wayvoice.vocabulary import apply_replacements, parse_rules


class VocabularyTests(unittest.TestCase):
    def apply(self, text, rules):
        return apply_replacements(text, {"vocabulary_enabled": True, "vocabulary_rules": rules})

    def test_case_and_whole_phrase_boundaries(self):
        self.assertEqual(self.apply("ВЕЙ ВОЙС, вей войсик, вей войс-проект", "вей войс => WayVoice"),
                         "WayVoice, вей войсик, вей войс-проект")

    def test_longest_original_match_and_no_cascade(self):
        self.assertEqual(self.apply("alpha beta alpha gamma", "alpha => gamma\nalpha beta => AB\ngamma => delta"),
                         "AB gamma delta")

    def test_literal_patterns_and_outputs(self):
        self.assertEqual(self.apply("C++ (test)", "C++ => C#\n(test) => \\1$"), "C# \\1$")

    def test_protect_technical_tokens(self):
        text = "voice https://voice.org voice@example.org /voice/path voice.py voice_name `voice`"
        self.assertEqual(self.apply(text, "voice => speech"), text.replace("voice ", "speech ", 1))

    def test_toggle_and_empty_rules(self):
        self.assertEqual(apply_replacements("Hello", {"vocabulary_enabled": False, "vocabulary_rules": None}), "Hello")
        self.assertEqual(self.apply("Hello", "\n  \n"), "Hello")

    def test_parse_valid_and_invalid_rules(self):
        self.assertEqual(parse_rules("  first => FIRST  \n\nsecond => SECOND"),
                         [("first", "FIRST"), ("second", "SECOND")])
        for raw in (None, "x" * 16385, "a", "a =>", "=> b", "a => b => c",
                    "a => b\nA => c", "a\t => b", "a => b\r", "a => b\x00",
                    "x" * 257 + " => y", "a => " + "x" * 257,
                    "\n".join(f"source{i} => target" for i in range(101))):
            with self.subTest(raw=repr(raw)[:80]):
                with self.assertRaises(ValueError):
                    parse_rules(raw)

    def test_combining_marks_are_part_of_word_boundaries(self):
        for text, source in (("किताब", "क"), ("cafe\u0301", "cafe"),
                             ("\u0301cafe", "cafe"), ("a\U0001d185b", "a")):
            with self.subTest(text=text):
                self.assertEqual(self.apply(text, source + " => REPLACED"), text)
        self.assertEqual(self.apply("cafe\u0301", "cafe\u0301 => Exact"), "Exact")
        self.assertEqual(self.apply("alpha cafe\u0301", "alpha cafe => long\nalpha => short"),
                         "short cafe\u0301")

    def test_duplicates_use_the_same_unicode_matching_as_replacements(self):
        for first, second in (("i", "ı"), ("i", "İ"), ("s", "ſ"), ("k", "K"), ("σ", "ς")):
            with self.subTest(first=first, second=second):
                with self.assertRaises(ValueError):
                    parse_rules(f"{first} => first\n{second} => second")
                self.assertEqual(self.apply(second, f"{first} => Exact"), "Exact")

    def test_malformed_direct_config_is_reported(self):
        for cfg in ({"vocabulary_enabled": 1}, {"vocabulary_enabled": True, "vocabulary_rules": "invalid"}):
            with self.assertRaises(ValueError):
                apply_replacements("text", cfg)
