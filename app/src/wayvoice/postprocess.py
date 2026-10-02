from __future__ import annotations
import re

COMMANDS = [
    (r"\bнов(?:ая|ую)\s+строк(?:а|у)\b", "\n"),
    (r"\bнов(?:ый|ого)\s+абзац\b", "\n\n"),
    (r"\bвопросительн(?:ый|ого)\s+знак\b", "?"),
    (r"\bвосклицательн(?:ый|ого)\s+знак\b", "!"),
    (r"\bточк(?:а|у)\s+с\s+запятой\b", ";"),
    (r"\bдвоеточи(?:е|я)\b", ":"),
    (r"\bзапят(?:ая|ую)\b", ","),
    (r"\bточк(?:а|у)\b", "."),
]

def _spoken_punctuation(text: str) -> str:
    out = text
    for pattern, repl in COMMANDS:
        out = re.sub(pattern, repl, out, flags=re.IGNORECASE)
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
) -> str:
    text = text.strip()
    if not text:
        return ""
    if spoken_punctuation:
        text = _spoken_punctuation(text)
    text = _spacing(text)
    text = _capitalize_sentences(text)

    if ensure_terminal_punctuation and text:
        last = text.rstrip()[-1]
        if last not in ".!?…:;)]}»\"'":
            text += "."
    return text
