"""Punctuation, and the words that contain it.

Every dictated number, time, version, address, file name and e-mail used to come
out broken, because the spacing pass inserted a space after every full stop and
comma, and the capitalizer treated the same full stops as sentence ends:

    "цена 3.5 евро"                   -> "Цена 3. 5 евро."
    "встреча в 12:30"                 -> "Встреча в 12: 30."
    "версия 1.2.3"                    -> "Версия 1. 2. 3."
    "открой https://example.com/page" -> "Открой https: //example. Com/page."
    "файл report.pdf"                 -> "Файл report. Pdf."
    "почта a@b.ru"                    -> "Почта a@b. Ru."

Dictating those is most of what the application is for, and the pass runs on every
dictation because auto-punctuation is on by default. It survived because no test
had a digit, a colon or a dot in it.

The hard half is telling "конец.начало" - two Russian words the recognizer ran
together, which this pass exists to fix - from "отчёт.pdf", which is one word. The
answer used here is the extension: a known one means a file name, an unknown one
means a sentence.
"""

import unittest

from wayvoice.postprocess import _is_intact, normalize


class IntactTokens(unittest.TestCase):
    def test_the_things_that_must_not_be_split(self):
        for token in ("3.5", "12:30", "1.2.3", "10-20", "1,5",
                      "https://example.com/page", "http://a.b/c?d=1",
                      "someone@example.com", "/home/u/report.pdf", "src/a.txt",
                      "report.pdf", "notes.MD", "отчёт.pdf", "заявка.docx"):
            with self.subTest(token=token):
                self.assertTrue(_is_intact(token), f"{token} would be split")

    def test_the_things_that_must_be_split(self):
        for token in ("конец.начало", "москва.питер", "привет,мир", "список:да",
                      "вопрос?да", "вот.и.е.пример", "слово!", "готово."):
            with self.subTest(token=token):
                self.assertFalse(_is_intact(token), f"{token} would be left alone")


class NumbersAndTimesSurvive(unittest.TestCase):
    """The cases that were reported, one dictation each."""

    CASES = {
        "цена 3.5 евро": "Цена 3.5 евро.",
        "встреча в 12:30": "Встреча в 12:30.",
        "версия 1.2.3": "Версия 1.2.3.",
        "открой https://example.com/page": "Открой https://example.com/page.",
        "файл report.pdf": "Файл report.pdf.",
        "почта a@b.ru": "Почта a@b.ru.",
        "путь /home/u/notes.md": "Путь /home/u/notes.md.",
        "1 000 000 рублей": "1 000 000 рублей.",
        "отчёт.pdf готов": "отчёт.pdf готов.",
        "заявка.docx подписать": "заявка.docx подписать.",
    }

    def test_each_case(self):
        for source, expected in self.CASES.items():
            with self.subTest(source=source):
                self.assertEqual(normalize(source), expected)

    def test_a_url_at_the_start_is_not_capitalized(self):
        # "Https://" would be worse than the lowercase it replaced.
        self.assertEqual(normalize("https://example.com"),
                         "https://example.com.")

    def test_a_filename_does_not_eat_the_next_sentence(self):
        self.assertEqual(normalize("отчёт.pdf готов.отправить"),
                         "отчёт.pdf готов. Отправить.")


class SentencesStillWork(unittest.TestCase):
    """The pass must still do the job it was written for."""

    def test_a_missing_space_is_inserted(self):
        self.assertEqual(normalize("привет,мир"), "Привет, мир.")
        self.assertEqual(normalize("конец.начало"), "Конец. Начало.")
        self.assertEqual(normalize("список: первый,второй"), "Список: первый, второй.")

    def test_a_space_before_punctuation_is_removed(self):
        self.assertEqual(normalize("привет , мир"), "Привет, мир.")

    def test_a_sentence_is_capitalized(self):
        self.assertEqual(normalize("вопрос?да!"), "Вопрос? Да!")
        self.assertEqual(normalize("первое предложение.второе предложение"),
                         "Первое предложение. Второе предложение.")

    def test_double_spaces_collapse_and_newlines_survive(self):
        self.assertEqual(normalize("два  слова.\nтретье"), "Два слова.\nТретье.")

    def test_an_ordinary_sentence_is_untouched(self):
        self.assertEqual(normalize("привет мир"), "Привет мир.")

    def test_an_unknown_extension_is_treated_as_a_sentence(self):
        # The rule that lets "отчёт.pdf" through must not let every dotted Russian
        # word through, or the pass stops fixing what it exists for.
        self.assertEqual(normalize("слово.другое"), "Слово. Другое.")


if __name__ == "__main__":
    unittest.main()
