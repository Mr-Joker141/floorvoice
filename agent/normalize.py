"""Transcript normalisation: Tamil/Sinhala/English number words -> digits, language detection."""

from __future__ import annotations

import re

TAMIL_DIGITS = str.maketrans("௦௧௨௩௪௫௬௭௮௯", "0123456789")

# Tamil number words (formal and colloquial spellings).
TA_UNITS: dict[str, int] = {
    "ஒன்று": 1, "ஒண்ணு": 1, "ஒரு": 1,
    "இரண்டு": 2, "ரெண்டு": 2, "இரெண்டு": 2,
    "மூன்று": 3, "மூணு": 3,
    "நான்கு": 4, "நாலு": 4,
    "ஐந்து": 5, "அஞ்சு": 5,
    "ஆறு": 6,
    "ஏழு": 7,
    "எட்டு": 8,
    "ஒன்பது": 9,
    "பூஜ்யம்": 0, "பூஜ்ஜியம்": 0, "சைபர்": 0,
}  # fmt: skip
TA_TENS: dict[str, int] = {
    "பத்து": 10,
    "இருபது": 20,
    "முப்பது": 30,
    "நாற்பது": 40,
    "ஐம்பது": 50,
    "அறுபது": 60,
    "எழுபது": 70,
    "எண்பது": 80,
    "தொண்ணூறு": 90,
    "நூறு": 100,
}
# Combining forms that take a following unit: இருபத்தி ஐந்து = 25.
TA_PREFIX: dict[str, int] = {
    "பதின": 10,
    "இருபத்தி": 20, "இருபத்து": 20,
    "முப்பத்தி": 30, "முப்பத்து": 30,
    "நாற்பத்தி": 40, "நாற்பத்து": 40,
    "ஐம்பத்தி": 50, "ஐம்பத்து": 50,
    "அறுபத்தி": 60, "அறுபத்து": 60,
    "எழுபத்தி": 70, "எழுபத்து": 70,
    "எண்பத்தி": 80, "எண்பத்து": 80,
    "தொண்ணூற்றி": 90, "தொண்ணூற்று": 90,
    "நூற்றி": 100, "நூற்று": 100,
}  # fmt: skip
# Case suffixes that may be glued to a number word ("இரண்டுக்கு" = "to two").
TA_SUFFIXES = ("", "க்கு", "கு", "ல", "ல்", "இல்", "ஐ", "ம்", "உம்", "ஆக", "ஆ")

EN_UNITS: dict[str, int] = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19,
}  # fmt: skip
EN_TENS: dict[str, int] = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}  # fmt: skip

_TA_SCRIPT = re.compile(r"[\u0B80-\u0BFF]")
_SI_SCRIPT = re.compile(r"[\u0D80-\u0DFF]")


def detect_language(text: str) -> str:
    """'ta' / 'si' if the utterance contains Tamil / Sinhala script, else 'en'."""
    if _SI_SCRIPT.search(text):
        return "si"
    return "ta" if _TA_SCRIPT.search(text) else "en"


def _split_suffix(
    word: str, table: dict[str, int], suffixes: tuple[str, ...] = ()
) -> tuple[int, str] | None:
    suffixes = suffixes or TA_SUFFIXES
    for base, value in table.items():
        if word.startswith(base):
            suffix = word[len(base) :]
            if suffix in suffixes or suffix.lstrip("-") in suffixes:
                return value, suffix
    return None


# Sinhala number words. Bare "එක" is left alone: it is also the definite article
# ("job card එක" = "the job card"); "එකක්" / "එකයි" are 1.
SI_UNITS: dict[str, int] = {
    "එකක්": 1, "එකයි": 1,
    "දෙක": 2, "තුන": 3, "හතර": 4, "පහ": 5, "හය": 6, "හත": 7, "අට": 8,
    "නවය": 9, "නමය": 9, "බිංදුව": 0, "බින්දුව": 0,
}  # fmt: skip
SI_TENS: dict[str, int] = {
    "දහය": 10, "එකොළහ": 11, "දොළහ": 12, "දහතුන": 13, "දාහතර": 14, "පහළොව": 15,
    "දාසය": 16, "දාහත": 17, "දහඅට": 18, "දහනවය": 19, "විස්ස": 20, "තිහ": 30,
    "හතළිහ": 40, "පනහ": 50, "හැට": 60, "හැත්තෑව": 70, "අසූව": 80, "අනූව": 90,
    "සීය": 100, "සියය": 100,
}  # fmt: skip
# Combining forms written before a unit, as one word or two: විසිපහ / විසි පහ = 25.
SI_PREFIX: dict[str, int] = {
    "විසි": 20, "තිස්": 30, "හතළිස්": 40, "පනස්": 50, "හැට": 60, "හැත්තෑ": 70,
    "අසූ": 80, "අනූ": 90, "එකසිය": 100,
}  # fmt: skip
# Endings glued to a number: දෙකක් (two of), දෙකට (to two), දෙකයි (just two), ...
SI_SUFFIXES = ("", "ක්", "යි", "ට", "කට", "ක්ම", "කුත්", "ක")
_SI_ZWJ = "\u200d"


def _si_number(tok: str) -> tuple[int, str] | None:
    tok = tok.replace(_SI_ZWJ, "")
    if hit := _split_suffix(tok, SI_TENS, SI_SUFFIXES) or _split_suffix(tok, SI_UNITS, SI_SUFFIXES):
        return hit
    for pre, value in SI_PREFIX.items():  # one-word compound: විසිපහක් = 25 + ක්
        if tok.startswith(pre) and (rest := _split_suffix(tok[len(pre) :], SI_UNITS, SI_SUFFIXES)):
            return value + rest[0], rest[1]
    return None


def _sinhala_numbers(tokens: list[str]) -> list[str]:
    out: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        # Digit-by-digit part numbers: "හය දෙක බිංදුව පහ" -> 6205.
        j, digits, last = i, "", None
        while j < len(tokens) and (last := _split_suffix(tokens[j], SI_UNITS, SI_SUFFIXES)):
            digits += str(last[0])
            j += 1
            if last[1]:
                break
        if len(digits) >= 2:
            out.append(digits + (last[1] if last else ""))
            i = j
            continue
        nxt = _split_suffix(tokens[i + 1], SI_UNITS, SI_SUFFIXES) if i + 1 < len(tokens) else None
        if tok in SI_PREFIX and nxt:
            out.append(f"{SI_PREFIX[tok] + nxt[0]}{nxt[1]}")
            i += 2
            continue
        hit = _si_number(tok)
        out.append(f"{hit[0]}{hit[1]}" if hit else tok)
        i += 1
    return out


def _tamil_numbers(tokens: list[str]) -> list[str]:
    out: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        # Digit-by-digit part numbers: "ஆறு இரண்டு பூஜ்யம் ஐந்து" -> 6205.
        j = i
        digits = ""
        while (
            j < len(tokens) and (hit := _split_suffix(tokens[j], TA_UNITS)) and tokens[j] != "ஒரு"
        ):
            digits += str(hit[0])
            j += 1
            if hit[1]:
                break
        if len(digits) >= 2:
            out.append(digits + (hit[1] if hit else ""))
            i = j
            continue
        if tok in TA_PREFIX and i + 1 < len(tokens):
            nxt = _split_suffix(tokens[i + 1], TA_UNITS)
            if nxt:
                out.append(f"{TA_PREFIX[tok] + nxt[0]}{nxt[1]}")
                i += 2
                continue
        hit = _split_suffix(tok, TA_TENS) or _split_suffix(tok, TA_UNITS)
        if hit:
            out.append(f"{hit[0]}{hit[1]}")
        else:
            out.append(tok)
        i += 1
    return out


EN_DIGITS = {w: v for w, v in EN_UNITS.items() if v < 10} | {"oh": 0}


def _english_numbers(tokens: list[str]) -> list[str]:
    out: list[str] = []
    i = 0
    while i < len(tokens):
        word = tokens[i].lower()
        # Digit-by-digit part numbers: "six two zero five" -> 6205.
        j = i
        while j < len(tokens) and tokens[j].lower() in EN_DIGITS:
            j += 1
        if j - i >= 2 and word != "oh":
            out.append("".join(str(EN_DIGITS[t.lower()]) for t in tokens[i:j]))
            i = j
            continue
        if word in EN_UNITS or word in EN_TENS:
            value = 0
            current = 0
            prev = ""
            j = i
            while j < len(tokens):
                w = tokens[j].lower()
                if w in EN_TENS:
                    if prev in EN_UNITS or prev in EN_TENS:
                        break  # "five twenty" is two numbers
                    current += EN_TENS[w]
                elif w in EN_UNITS:
                    if prev in EN_UNITS or (prev in EN_TENS and EN_UNITS[w] >= 10):
                        break  # "five ten", "twenty twelve"
                    current += EN_UNITS[w]
                elif w == "hundred" and current:
                    current *= 100
                elif w == "thousand" and current:
                    value += current * 1000
                    current = 0
                elif (
                    w == "and"
                    and j + 1 < len(tokens)
                    and (tokens[j + 1].lower() in EN_UNITS or tokens[j + 1].lower() in EN_TENS)
                ):
                    pass
                else:
                    break
                prev = w
                j += 1
            out.append(str(value + current))
            i = j
        else:
            out.append(tokens[i])
            i += 1
    return out


_TOKEN = re.compile(r"\S+")
# "two-க்கு" -> "two -க்கு" so the English word is parsed on its own.
_MIXED_SUFFIX = re.compile(r"([A-Za-z0-9])-(?=[\u0B80-\u0BFF\u0D80-\u0DFF])")
_TRAIL_PUNCT = re.compile(r"^(.*?)([.,!?;:]*)$")


def normalize_numbers(text: str) -> str:
    """Replace spelled-out Tamil/English numbers with digits; keep everything else."""
    text = _MIXED_SUFFIX.sub(r"\1 -", text.translate(TAMIL_DIGITS))
    raw = _TOKEN.findall(text)
    # Strip trailing punctuation for matching, then put it back on the last token of a run.
    words: list[str] = []
    punct: list[str] = []
    for tok in raw:
        m = _TRAIL_PUNCT.match(tok)
        assert m
        words.append(m.group(1))
        punct.append(m.group(2))
    result: list[str] = []
    # Process word by word so punctuation boundaries end a number run.
    run: list[str] = []
    for word, p in zip(words, punct, strict=True):
        run.append(word)
        if p:
            result += _english_numbers(_sinhala_numbers(_tamil_numbers(run)))
            result[-1] += p
            run = []
    if run:
        result += _english_numbers(_sinhala_numbers(_tamil_numbers(run)))
    return " ".join(result)


def token_count(text: str) -> int:
    return len(_TOKEN.findall(text))
