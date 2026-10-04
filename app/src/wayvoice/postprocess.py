from __future__ import annotations
import re

from . import languages

# Spoken punctuation, one table per language. A command only exists for the language
# it was spoken in, so a table belonging to another language can only fire on text
# the user meant literally - which is why "auto" can apply all of them at once.
#
# The patterns carry no word boundaries: _compile() adds guards that treat a hyphen
# as part of the word, so "comma-separated" does not become ",-separated".
RU_COMMANDS = [
    (r"нов(?:ая|ую)\s+строк(?:а|у)", "\n"),
    (r"нов(?:ый|ого)\s+абзац", "\n\n"),
    (r"вопросительн(?:ый|ого)\s+знак", "?"),
    (r"восклицательн(?:ый|ого)\s+знак", "!"),
    (r"точк(?:а|у)\s+с\s+запятой", ";"),
    (r"двоеточи(?:е|я)", ":"),
    (r"запят(?:ая|ую)", ","),
    (r"точк(?:а|у)", "."),
]

EN_COMMANDS = [
    (r"new\s+paragraph", "\n\n"),
    (r"new\s+line", "\n"),
    (r"question\s+mark", "?"),
    (r"exclamation\s+(?:mark|point)", "!"),
    (r"semi[ -]?colon", ";"),
    (r"colon", ":"),
    (r"comma", ","),
    (r"(?:full\s+stop|period)", "."),
]

COMMANDS_BY_LANGUAGE: dict[str, list[tuple[str, str]]] = {
    "ru": RU_COMMANDS,
    "en": EN_COMMANDS,
}

# Kept for callers that imported it: it is still the Russian table. Use
# COMMANDS_BY_LANGUAGE for anything language-aware.
COMMANDS = RU_COMMANDS

def _compile(table: list[tuple[str, str]]) -> list[tuple[re.Pattern[str], str]]:
    return [
        (re.compile(rf"(?<![\w-]){pattern}(?![\w-])", re.IGNORECASE), replacement)
        for pattern, replacement in table
    ]


_COMPILED: dict[str, list[tuple[re.Pattern[str], str]]] = {
    code: _compile(table) for code, table in COMMANDS_BY_LANGUAGE.items()
}


def _tables_for(language: str | None) -> list[tuple[re.Pattern[str], str]]:
    """Command tables to apply for a recognition language.

    ``auto`` means nobody said what was spoken, so all tables are applied: a spoken
    command needs a whole phrase to fire, so an extra table costs nothing, while
    guessing one would leave the other languages' commands in the text verbatim. A
    language with no table is treated the same way.
    """
    code = languages.normalize(language)
    if code in _COMPILED:
        return _COMPILED[code]
    return _COMPILED["ru"] + _COMPILED["en"]


def _spoken_punctuation(text: str, language: str | None = "auto") -> str:
    out = text
    for pattern, repl in _tables_for(language):
        out = pattern.sub(repl, out)
    return out

def _spacing(text: str) -> str:
    # spaces before punctuation
    text = re.sub(r"[ \t]+([,.;:!?])", r"\1", text)
    # exactly one space after punctuation, except before newline/end
    text = re.sub(r"([,;:])(?=[^\s\n])", r"\1 ", text)
    text = re.sub(r"([.!?])(?=[^\s\n])", r"\1 ", text)
    text = re.sub(r"[ \t]*\n[ \t]*", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()

def _capitalize_sentences(text: str) -> str:
    chars = list(text)
    capitalize_next = True
    for i, ch in enumerate(chars):
        if capitalize_next and ch.isalpha():
            chars[i] = ch.upper()
            capitalize_next = False
        if ch in ".!?\n":
            capitalize_next = True
        elif not ch.isspace() and ch not in "\"'«„(":
            if ch not in ".!?":
                capitalize_next = False
    return "".join(chars)

def normalize(
    text: str,
    *,
    spoken_punctuation: bool = True,
    ensure_terminal_punctuation: bool = True,
    language: str = "auto",
) -> str:
    text = text.strip()
    if not text:
        return ""
    if spoken_punctuation:
        text = _spoken_punctuation(text, language)
    text = _spacing(text)
    text = _capitalize_sentences(text)

    if ensure_terminal_punctuation and text:
        last = text.rstrip()[-1]
        if last not in ".!?…:;)]}»\"'":
            text += "."
    return text
