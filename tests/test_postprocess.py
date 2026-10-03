import unittest
from wayvoice.postprocess import normalize

class PostprocessTests(unittest.TestCase):
    def test_russian_spoken_punctuation(self):
        text = normalize("привет запятая мир точка", spoken_punctuation=True)
        self.assertEqual(text, "Привет, мир.")

    def test_terminal_punctuation(self):
        self.assertEqual(normalize("привет мир"), "Привет мир.")

    # --- language-aware commands -----------------------------------------
    def test_english_spoken_punctuation(self):
        text = normalize("hello comma world full stop", spoken_punctuation=True, language="en")
        self.assertEqual(text, "Hello, world.")

    def test_english_punctuation_names(self):
        cases = [
            ("first line new line second line", "First line\nSecond line."),
            ("first line new paragraph second line", "First line\n\nSecond line."),
            ("really question mark", "Really?"),
            ("wow exclamation mark", "Wow!"),
            ("wait semicolon", "Wait;"),
            ("note colon", "Note:"),
            ("a comma b", "A, b."),
        ]
        for raw, expected in cases:
            self.assertEqual(normalize(raw, language="en"), expected, raw)

    def test_russian_commands_under_russian_language(self):
        text = normalize("строка два запятая строка три точка", language="ru")
        self.assertEqual(text, "Строка два, строка три.")

    def test_auto_applies_both_tables(self):
        # Nobody told us what was spoken, so both sets of commands have to be
        # honoured rather than leaving half of them in the text.
        self.assertEqual(normalize("привет comma мир"), "Привет, мир.")
        self.assertEqual(normalize("hello запятая world"), "Hello, world.")

    def test_language_without_a_table_still_drops_the_known_commands(self):
        # No German table exists; guessing one would be worse than the
        # alternative, which is the same behaviour as auto.
        self.assertEqual(normalize("hallo comma welt", language="de"), "Hallo, welt.")

    def test_commands_can_be_turned_off(self):
        self.assertEqual(normalize("hello comma world", spoken_punctuation=False), "Hello comma world.")

    def test_plain_text_is_untouched_by_the_commands(self):
        # "line" and "period" are ordinary words; nothing may be eaten.
        self.assertEqual(normalize("the train line is long"), "The train line is long.")
        self.assertEqual(normalize("a comma-separated value"), "A comma-separated value.")

    def test_language_keyword_is_optional(self):
        # Old callers pass no language at all and keep the old behaviour.
        self.assertEqual(normalize("привет запятая мир"), "Привет, мир.")


if __name__ == "__main__":
    unittest.main()
