import unittest
from wayvoice.models import PRESET_IDS, display_name, forced_language, preset_index

class ModelTests(unittest.TestCase):
    def test_language_specific_model(self):
        model = "bzikst/faster-whisper-large-v3-russian-int8"
        self.assertEqual(forced_language(model), "ru")
        self.assertEqual(PRESET_IDS[preset_index(model)], model)

    def test_display_name(self):
        self.assertEqual(display_name("small"), "Small")

if __name__ == "__main__":
    unittest.main()
