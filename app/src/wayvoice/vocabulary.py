"""Bounded, literal text replacements applied after recognition formatting."""
from __future__ import annotations

import re
from bisect import bisect_right
import unicodedata


MAX_RULES = 100
MAX_TEXT = 16384
MAX_PHRASE = 256


def parse_rules(raw: str) -> list[tuple[str, str]]:
    """Parse one ``source => target`` pair per nonempty line."""
    if not isinstance(raw, str) or len(raw) > MAX_TEXT:
        raise ValueError(f"Replacement rules must be text of at most {MAX_TEXT} characters")
    rules = []
    seen = []
    for line_number, line in enumerate(raw.split("\n"), 1):
        if any(unicodedata.category(char).startswith("C") for char in line):
            raise ValueError(f"Line {line_number}: control characters are not allowed")
        if not line.strip():
            continue
        if line.count("=>") != 1:
            raise ValueError(f"Line {line_number}: use source => target")
        source, target = (part.strip() for part in line.split("=>"))
        if not source or not target or max(len(source), len(target)) > MAX_PHRASE:
            raise ValueError(f"Line {line_number}: each phrase must contain 1–{MAX_PHRASE} characters")
        # Validation and matching must use the same Unicode case equivalence.
        if any(pattern.fullmatch(source) for pattern in seen):
            raise ValueError(f"Line {line_number}: duplicate source phrase")
        seen.append(re.compile(re.escape(source), re.IGNORECASE))
        rules.append((source, target))
        if len(rules) > MAX_RULES:
            raise ValueError(f"At most {MAX_RULES} replacement rules are allowed")
    return rules


def _protected(token: str) -> bool:
    # A conservative token guard keeps names inside technical text unchanged.
    return any(char in token for char in ("/", "\\", "@", "_", "`")) or bool(
        re.search(r"\w\.\w", token))


def apply_replacements(text: str, cfg: dict) -> str:
    """Replace whole literal phrases once, preserving exact replacement spelling."""
    enabled = cfg.get("vocabulary_enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("vocabulary_enabled must be a boolean")
    if not enabled:
        return text
    rules = parse_rules(cfg.get("vocabulary_rules", ""))
    if not rules or not text:
        return text
    # The callback selects the original rule rather than looking up casefolded
    # text: Python's Unicode IGNORECASE also matches some non-casefold equivalents.
    rules.sort(key=lambda rule: len(rule[0]), reverse=True)
    # Python \w excludes combining marks. Include every mark occurring in the
    # original text so boundaries cannot split Hindi words or NFD graphemes.
    marks = "".join(sorted({char for char in text
                            if unicodedata.category(char).startswith("M")}))
    word = r"[\w\-" + re.escape(marks) + "]"
    pattern = re.compile(
        r"(?<!" + word + ")(?:" + "|".join(
            f"(?P<r{index}>{re.escape(source)})" for index, (source, _) in enumerate(rules)
        ) + ")(?!" + word + ")", re.IGNORECASE)
    protected = [(match.start(), match.end()) for match in re.finditer(r"\S+", text)
                 if _protected(match.group())]

    ends = [end for _, end in protected]

    def replace(match: re.Match) -> str:
        index = bisect_right(ends, match.start())
        if index < len(protected) and protected[index][0] < match.end():
            return match.group()
        return rules[int(match.lastgroup[1:])][1]

    return pattern.sub(replace, text)
