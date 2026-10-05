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

#: Tokens whose punctuation belongs to the token, not to the sentence.
#:
#: Splitting inside them is what produced "цена 3. 5 евро", "встреча в 12: 30",
#: "версия 1. 2. 3", "https: //example. Com/page" and "файл report. Pdf" - that
#: is, every dictated number, time, version, address, file name and e-mail came
#: out broken, and dictating those is most of what the application is for.
_INTACT = (
    re.compile(r"\S+://\S+"),                 # https://example.com/page
    re.compile(r"\S+@\S+\.\S+"),              # someone@example.com
    re.compile(r"\S*[/\\]\S*\.\S+"),          # /home/u/report.pdf, src/a.txt
    re.compile(r"[0-9][0-9.,:/-]*[0-9]"),     # 3.5, 12:30, 1.2.3, 10-20
    # A name with an extension, but only in Latin script: "report.pdf" is one word,
    # while "конец.начало" is two Russian words that the recognizer ran together -
    # and fixing that is what this function is for. Requiring Latin is what tells
    # the two apart without a list of every extension in the world.
    re.compile(r"[A-Za-z0-9_+-]+(\.[A-Za-z0-9_+-]+)*\.[A-Za-z]{1,6}\.?"),
)

#: Extensions protected whatever the rest of the name is written in. A short list
#: instead of "any letters", because in Russian "отчёт.pdf" is one word and
#: "конец.начало" is two, and only a known extension tells them apart.
_EXTENSIONS = frozenset((
    "pdf", "txt", "md", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "csv", "tsv",
    "json", "yaml", "yml", "toml", "ini", "conf", "log", "rtf", "odt",
    "py", "js", "ts", "tsx", "jsx", "sh", "bash", "c", "h", "cpp", "hpp", "rs", "go",
    "zip", "tar", "gz", "bz2", "xz", "7z", "deb", "rpm", "apk", "iso",
    "jpg", "jpeg", "png", "gif", "svg", "webp", "bmp", "tiff",
    "mp3", "mp4", "wav", "ogg", "flac", "mkv", "mov", "avi", "webm",
    "html", "htm", "css", "sql", "db", "sqlite",
))

_EXTENSION_AT_END = re.compile(r"^.+\.([A-Za-z0-9]{1,6})\.?$")


def _is_intact(token: str) -> bool:
    if any(pattern.fullmatch(token) for pattern in _INTACT):
        return True
    # A name in any script, as long as the extension is one people actually use.
    match = _EXTENSION_AT_END.match(token)
    return bool(match) and match.group(1).lower() in _EXTENSIONS


def _spacing(text: str) -> str:
    # Spaces before punctuation.
    text = re.sub(r"[ \t]+([,.;:!?])", r"\1", text)
    # One space after punctuation, except before a newline or the end. Splitting
    # runs per token rather than over the whole text, because a token is the unit
    # that is either one word or several.
    parts = re.split(r"(\s+)", text)
    for index in range(0, len(parts), 2):
        token = parts[index]
        if not token or _is_intact(token):
            continue
        token = re.sub(r"([,;:])(?=\S)", r"\1 ", token)
        token = re.sub(r"([.!?])(?=\S)", r"\1 ", token)
        parts[index] = token
    text = "".join(parts)
    text = re.sub(r"[ \t]*\n[ \t]*", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()

def _capitalize_sentences(text: str) -> str:
    """Capitalize the first letter of each sentence.

    Runs per token for the same reason as :func:`_spacing`: inside "example.com" or
    "report.pdf" a full stop is part of the word, and capitalizing after it turned a
    dictated address into "example.Com" and a file name into "report.Pdf".
    """
    parts = re.split(r"(\s+)", text)
    capitalize_next = True
    for index in range(0, len(parts), 2):
        token = parts[index]
        if not token:
            continue
        intact = _is_intact(token)
        chars = list(token)
        for position, ch in enumerate(chars):
            if intact:
                # Left alone entirely - including its first letter, so that a
                # sentence starting with a URL does not become "Https://" - but it
                # does consume the flag, or the word after it would be capitalized.
                capitalize_next = False
                continue
            if capitalize_next and ch.isalpha():
                chars[position] = ch.upper()
                capitalize_next = False
            if ch in ".!?":
                capitalize_next = True
            elif ch not in "\"'«„(":
                capitalize_next = False
        parts[index] = "".join(chars)
        # A newline ends a sentence whether or not a token follows it.
        if index + 1 < len(parts) and "\n" in parts[index + 1]:
            capitalize_next = True
    return "".join(parts)

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
