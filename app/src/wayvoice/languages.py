"""Recognition languages.

Single source of truth for what can be dictated and how it is named.  The
codes and their English names are the ones Whisper itself uses, so anything
listed here can be handed to faster-whisper or to whisper.cpp unchanged.  The
native names come from ISO 639 and are what the settings window shows: a user
looking for their language finds it under the name they know it by, not under
an English translation.

``AUTO`` is not a language of its own.  Whisper detects the language per
utterance, which is why it is the default - a fixed default silently
transcribed Polish speech as Russian.
"""

from __future__ import annotations

AUTO = "auto"

# code -> (native name, English name)
NAMES: dict[str, tuple[str, str]] = {
    'en': ('English', 'english'),
    'zh': ('中文', 'chinese'),
    'de': ('Deutsch', 'german'),
    'es': ('Español', 'spanish'),
    'ru': ('Русский', 'russian'),
    'ko': ('한국어', 'korean'),
    'fr': ('Français', 'french'),
    'ja': ('日本語', 'japanese'),
    'pt': ('Português', 'portuguese'),
    'tr': ('Türkçe', 'turkish'),
    'pl': ('Język polski', 'polish'),
    'ca': ('Català', 'catalan'),
    'nl': ('Nederlands', 'dutch'),
    'ar': ('العربية', 'arabic'),
    'sv': ('Svenska', 'swedish'),
    'it': ('Italiano', 'italian'),
    'id': ('Bahasa Indonesia', 'indonesian'),
    'hi': ('हिन्दी', 'hindi'),
    'fi': ('Suomi', 'finnish'),
    'vi': ('Tiếng Việt', 'vietnamese'),
    'he': ('עברית', 'hebrew'),
    'uk': ('Українська', 'ukrainian'),
    'el': ('ελληνικά', 'greek'),
    'ms': ('Bahasa Melayu', 'malay'),
    'cs': ('čeština', 'czech'),
    'ro': ('Română', 'romanian'),
    'da': ('Dansk', 'danish'),
    'hu': ('Magyar', 'hungarian'),
    'ta': ('தமிழ்', 'tamil'),
    'no': ('Norsk', 'norwegian'),
    'th': ('ไทย', 'thai'),
    'ur': ('اردو', 'urdu'),
    'hr': ('Hrvatski', 'croatian'),
    'bg': ('Български', 'bulgarian'),
    'lt': ('Lietuvių', 'lithuanian'),
    'la': ('Latine', 'latin'),
    'mi': ('Te reo Māori', 'maori'),
    'ml': ('മലയാളം', 'malayalam'),
    'cy': ('Cymraeg', 'welsh'),
    'sk': ('Slovenčina', 'slovak'),
    'te': ('తెలుగు', 'telugu'),
    'fa': ('فارسی', 'persian'),
    'lv': ('Latviešu valoda', 'latvian'),
    'bn': ('বাংলা', 'bengali'),
    'sr': ('Српски', 'serbian'),
    'az': ('Azərbaycan dili', 'azerbaijani'),
    'sl': ('Slovenščina', 'slovenian'),
    'kn': ('ಕನ್ನಡ', 'kannada'),
    'et': ('Eesti', 'estonian'),
    'mk': ('Македонски', 'macedonian'),
    'br': ('Brezhoneg', 'breton'),
    'eu': ('Euskara', 'basque'),
    'is': ('Íslenska', 'icelandic'),
    'hy': ('Հայերեն', 'armenian'),
    'ne': ('नेपाली', 'nepali'),
    'mn': ('Монгол', 'mongolian'),
    'bs': ('Bosanski', 'bosnian'),
    'kk': ('қазақ тілі', 'kazakh'),
    'sq': ('Shqip', 'albanian'),
    'sw': ('Kiswahili', 'swahili'),
    'gl': ('Galego', 'galician'),
    'mr': ('मराठी', 'marathi'),
    'pa': ('ਪੰਜਾਬੀ', 'punjabi'),
    'si': ('සිංහල', 'sinhala'),
    'km': ('ខ្មែរ', 'khmer'),
    'sn': ('chiShona', 'shona'),
    'yo': ('Yorùbá', 'yoruba'),
    'so': ('Soomaaliga', 'somali'),
    'af': ('Afrikaans', 'afrikaans'),
    'oc': ('Occitan', 'occitan'),
    'ka': ('ქართული', 'georgian'),
    'be': ('беларуская мова', 'belarusian'),
    'tg': ('тоҷикӣ', 'tajik'),
    'sd': ('सिन्धी', 'sindhi'),
    'gu': ('ગુજરાતી', 'gujarati'),
    'am': ('አማርኛ', 'amharic'),
    'yi': ('ייִדיש', 'yiddish'),
    'lo': ('ພາສາລາວ', 'lao'),
    'uz': ('Oʻzbek', 'uzbek'),
    'fo': ('Føroyskt', 'faroese'),
    'ht': ('Kreyòl ayisyen', 'haitian creole'),
    'ps': ('پښتو', 'pashto'),
    'tk': ('Türkmen', 'turkmen'),
    'nn': ('Nynorsk', 'nynorsk'),
    'mt': ('Malti', 'maltese'),
    'sa': ('संस्कृतम्', 'sanskrit'),
    'lb': ('Lëtzebuergesch', 'luxembourgish'),
    'my': ('ဗမာစာ', 'myanmar'),
    'bo': ('བོད་ཡིག', 'tibetan'),
    'tl': ('Wikang Tagalog', 'tagalog'),
    'mg': ('Fiteny malagasy', 'malagasy'),
    'as': ('অসমীয়া', 'assamese'),
    'tt': ('Татарча', 'tatar'),
    'haw': ('ʻŌlelo Hawaiʻi', 'hawaiian'),
    'ln': ('Lingála', 'lingala'),
    'ha': ('هَوُسَ', 'hausa'),
    'ba': ('Башҡорт', 'bashkir'),
    'jw': ('Basa Jawa', 'javanese'),
    'su': ('Basa Sunda', 'sundanese'),
    'yue': ('粵語', 'cantonese'),
}

CODES: tuple[str, ...] = tuple(NAMES)


def normalize(value: str | None) -> str:
    """Return a usable language code.

    Anything unknown, empty or missing becomes :data:`AUTO`, because guessing a
    language for the model is worse than letting it detect one.
    """
    code = (value or "").strip().lower().replace("_", "-")
    if not code or code == AUTO:
        return AUTO
    base = code.split("-")[0]
    if base in NAMES:
        return base
    return AUTO


def is_valid(value: str | None) -> bool:
    code = (value or "").strip().lower()
    return code in NAMES or code == AUTO


def native_name(code: str) -> str:
    return NAMES.get(normalize(code), (code, code))[0]


def english_name(code: str) -> str:
    return NAMES.get(normalize(code), (code, code))[1].capitalize()


def display_name(code: str, ui_language: str = "en") -> str:
    """Name to show in the settings window.

    The native name always comes first.  The English name is appended only for
    an English interface, where it is the one that helps; in any other
    interface it would just be noise next to a name the user already reads.
    """
    entry = NAMES.get(normalize(code))
    if entry is None:
        return str(code)
    native, english = entry
    if ui_language == "en" and native.lower() != english.lower():
        return f"{native} ({english.capitalize()})"
    return native


def detect_from_locale(locales: "list[str] | tuple[str, ...] | None" = None) -> str:
    """Best Whisper code for a set of locale names such as ``["de_DE.UTF-8"]``.

    Used for the default of the recognition language.  Returns :data:`AUTO` when
    nothing matches, since the locale describes the interface, not necessarily
    the speech.
    """
    if locales is None:
        return AUTO
    for locale in locales:
        base = str(locale or "").split(".")[0].split("_")[0].split("-")[0].lower()
        if base in NAMES:
            return base
    return AUTO
