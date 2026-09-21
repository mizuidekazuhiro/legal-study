from __future__ import annotations

import re
import unicodedata

_JP_RE = re.compile(r"[\u3005-\u3007\u303b\u3040-\u30ff\u3400-\u9fff\uf900-\ufaff]")
_LOWER_LATIN_TOKEN_RE = re.compile(r"(?<![A-Za-z])[a-z]{2,12}(?![A-Za-z])")
_EXPECTED_LOWER_TOKENS = frozenset({"rank", "stock", "memo"})


def _is_expected_letter_or_number(c: str) -> bool:
    if _JP_RE.match(c):
        return True
    normalized = unicodedata.normalize("NFKC", c)
    if len(normalized) == 1 and normalized.isascii() and normalized.isalnum():
        return True
    # Parenthesized / circled list numbers are common in Japanese legal writing.
    # NFKC expands them to forms such as "(1)"; that expansion is expected text,
    # not evidence of a broken ToUnicode map.
    return bool(re.fullmatch(r"\(?[0-9]+\)?", normalized))


def is_suspicious_char(c: str) -> bool:
    if c.isspace():
        return False
    if c == "\ufffd":
        return True
    cp = ord(c)
    if 0xE000 <= cp <= 0xF8FF:
        return True
    cat = unicodedata.category(c)
    if cat.startswith(("L", "N")) and not _is_expected_letter_or_number(c):
        # Legal study sources are Japanese with occasional Latin letters/numbers.
        # Letters from unrelated scripts are a strong signal of a broken ToUnicode
        # map / embedded OCR layer (a common PDF extraction failure mode).
        return True
    return cat == "Cc"


def suspicious_char_count(text: str) -> int:
    return sum(1 for c in text if is_suspicious_char(c))


def native_text_quality(text: str) -> float:
    """Estimate whether a Japanese legal-study PDF text layer is trustworthy."""
    if not text or not text.strip():
        return 0.0

    chars = [c for c in text if not c.isspace()]
    if not chars:
        return 0.0

    suspicious = suspicious_char_count("".join(chars))
    printable = sum(1 for c in chars if not unicodedata.category(c).startswith("C"))
    base = printable / max(len(chars), 1)
    # Suspicious foreign-script glyphs are penalized strongly because they often
    # represent individual Japanese glyphs mapped to the wrong Unicode codepoint.
    penalty = min(0.85, (suspicious * 3.0) / max(len(chars), 1))
    return round(max(0.0, min(1.0, base - penalty)), 4)


def useful_char_count(text: str) -> int:
    return sum(1 for c in text if not c.isspace() and unicodedata.category(c) != "Cc")


def suspicious_token_count(text: str) -> int:
    """Count narrow lowercase-Latin noise tokens in Japanese source text."""
    return sum(
        1
        for match in _LOWER_LATIN_TOKEN_RE.finditer(text)
        if match.group(0).lower() not in _EXPECTED_LOWER_TOKENS
    )
