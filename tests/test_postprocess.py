import unittest
from wayvoice.postprocess import normalize

class PostprocessTests(unittest.TestCase):
    def test_russian_spoken_punctuation(self):
        text = normalize("привет запятая мир точка", spoken_punctuation=True)
        self.assertEqual(text, "Привет, мир.")

    def test_terminal_punctuation(self):
        self.assertEqual(normalize("привет мир"), "Привет мир.")

if __name__ == "__main__":
    unittest.main()
