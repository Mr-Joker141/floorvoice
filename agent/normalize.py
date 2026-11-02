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


def reply_language(text: str, speech: str = "") -> str:
    """Language to answer in. A worker who chose Sinhala gets Sinhala even when the
    recogniser wrote Latin or Tamil letters; Tamil and English follow the utterance."""
    if speech == "si":
        return "si"
    lang = detect_language(text)
    if speech == "ta" and lang == "si":
        return "ta"
    return lang


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
    "දෙක": 2, "දක": 2, "තුන": 3, "හතර": 4, "පහ": 5, "හය": 6, "හත": 7, "අට": 8,
    "නවය": 9, "නමය": 9, "බිංදුව": 0, "බින්දුව": 0,
}  # fmt: skip
SI_TENS: dict[str, int] = {
    "දහය": 10, "එකොළහ": 11, "දොළහ": 12, "දහතුන": 13, "දාහතර": 14, "පහළොව": 15,
    "දාසය": 16, "දාහත": 17, "දහඅට": 18, "දහනවය": 19, "විස්ස": 20, "විස": 20, "තිහ": 30,
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


# Misspellings Voice Transcribe produces ("දක" for දෙක, "විස" for විස්ස) are in the tables;
# "කැලිවිස" = කෑලි (pieces) + විස (20) glued together.
_SI_PIECES = re.compile(r"^(කෑලි|කැලි|කලි)(.+)$")


def _sinhala_numbers(tokens: list[str]) -> list[str]:
    split: list[str] = []
    for tok in tokens:
        m = _SI_PIECES.match(tok)
        split += [m.group(1), m.group(2)] if m and _si_number(m.group(2)) else [tok]
    tokens = split

    def digit(tok: str) -> tuple[int, str] | None:
        # "දෙකක්" (two of) / "දෙකට" (to two) are a quantity or a line, never a code digit.
        hit = _split_suffix(tok, SI_UNITS, SI_SUFFIXES)
        return hit if hit and hit[1] in ("", "යි") else None

    def digit_run(start: int) -> tuple[str, str, int]:
        j, digits, suffix = start, "", ""
        while j < len(tokens) and (d := digit(tokens[j])):
            digits += str(d[0])
            j += 1
            if d[1]:
                suffix = d[1]
                break
        return digits, suffix, j

    out: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        # Digit-by-digit part numbers: "හය දෙක බිංදුව පහ" -> 6205.
        digits, suffix, j = digit_run(i)
        if len(digits) >= 2:
            out.append(digits + suffix)
            i = j
            continue
        compound: tuple[int, str] | None = None
        j = i + 1
        nxt = _split_suffix(tokens[i + 1], SI_UNITS, SI_SUFFIXES) if i + 1 < len(tokens) else None
        if tok in SI_PREFIX and nxt:
            compound, j = (SI_PREFIX[tok] + nxt[0], nxt[1]), i + 2
        else:
            compound = _si_number(tok)
        if compound is not None:
            value, suffix = compound
            if not suffix and value >= 20:
                # Codes said in pairs: "හැට දෙක බිංදුව පහ" = 62|05.
                tail, tail_suffix, k = digit_run(j)
                if len(tail) >= 2:
                    out.append(f"{value}{tail}{tail_suffix}")
                    i = k
                    continue
            out.append(f"{value}{suffix}")
            i = j
            continue
        out.append(tok)
        i += 1
    return out


# Voice Transcribe often writes Sinhala speech in Latin letters ("job card tuna ivarai,
# kali visak"). Only used when the worker chose Sinhala: "tuna", "even", "ata" are English too.
SI_ROMAN_UNITS: dict[str, int] = {
    "ekak": 1, "ekai": 1, "ekay": 1,
    "deka": 2, "dekak": 2, "dekai": 2, "dekay": 2, "dekata": 2, "deke": 2,
    "tuna": 3, "thuna": 3, "tunak": 3, "thunak": 3, "tunai": 3, "tunay": 3, "tun": 3, "thun": 3,
    "hatara": 4, "hathara": 4, "hatarak": 4, "hatharak": 4,
    "paha": 5, "pahak": 5, "pahai": 5, "pahay": 5, "pahata": 5,
    "haya": 6, "hayak": 6, "hatha": 7, "hatak": 7, "hathak": 7, "ata": 8, "atak": 8,
    "namaya": 9, "navaya": 9, "binduwa": 0, "binduva": 0, "bindu": 0,
}  # fmt: skip
SI_ROMAN_TENS: dict[str, int] = {
    "dahaya": 10, "dahayak": 10, "dahai": 10, "dolaha": 12, "dolahak": 12, "pahalowa": 15,
    "visse": 20, "wisse": 20, "vissa": 20, "wissa": 20, "visak": 20, "wisak": 20,
    "vissak": 20, "wissak": 20, "tiha": 30, "thiha": 30, "tihak": 30, "panaha": 50,
    "panahak": 50, "siyaya": 100, "siyayak": 100,
}  # fmt: skip
SI_ROMAN_PREFIX: dict[str, int] = {
    "visi": 20, "wisi": 20, "tis": 30, "this": 30, "hatalis": 40, "panas": 50,
    "heta": 60, "haeta": 60, "hetta": 60, "asu": 80, "anu": 90,
}  # fmt: skip


# Bare digit words that can form a spoken part number; suffixed forms ("dekak") are quantities.
_SI_ROMAN_DIGITS = {"deka", "tuna", "thuna", "hatara", "hathara", "paha", "haya", "hatha",
                    "ata", "namaya", "navaya", "binduwa", "binduva", "bindu"}  # fmt: skip
# "kalivisak" = kali (pieces) + visak (20).
_SI_ROMAN_GLUED = re.compile(r"^(kali|keli|kaeli)(\w+)$", re.IGNORECASE)


# "10k" / "10ak" = 10 + the Sinhala indefinite "-ක්" ("dahayak").
_SI_ROMAN_COUNT = re.compile(r"^(\d+)(?:k|ak|kak)$", re.IGNORECASE)

# Typed "Singlish": Sinhala written in English letters ("mata bearing 10k ewanna").
# Words that are not English, so one of them marks the sentence as Sinhala.
SI_ROMAN_MARKERS = {
    "mata", "mage", "mge", "api", "apita", "oya", "oyata", "eka", "ekak", "eke", "ekata",
    "ewanna", "evanna", "ewanne", "yawanna", "yavanna", "denna", "ganna", "gannawa",
    "genna", "gennanna", "karanna", "kala", "kalaa", "iwarai", "ivarai", "iwara",
    "ona", "oone", "one", "onee", "ooni", "thiyenawa", "tiyenawa", "nadda", "neda", "tika",
    "kiyak", "kochchara", "kohenda", "kohen", "gabadawe", "gabadawa", "gabadaawe", "lain",
    "sthoor", "kali", "keli", "pahu", "aye", "ayeth", "eewa", "ewa", "meka", "ara", "hari",
    "nemei", "naha", "nehe", "baya", "hadanna", "watenna", "wenas", "wenaskaranna",
}  # fmt: skip
# Also English words: count them only next to an unambiguous marker.
_SI_ROMAN_WEAK = {"one", "ara", "hari", "ewa", "aye", "kala", "baya", "eke", "api"}
_LATIN_WORD = re.compile(r"[a-z]+")


def looks_roman_sinhala(text: str) -> bool:
    """True for Sinhala typed in Latin letters: no Sinhala/Tamil script, and at least one
    Sinhala-only word ("ewanna", "mata", "dahayak") or a "10k"-style count."""
    if _SI_SCRIPT.search(text) or _TA_SCRIPT.search(text):
        return False
    words = _LATIN_WORD.findall(text.lower())
    strong = [w for w in words if w in SI_ROMAN_MARKERS and w not in _SI_ROMAN_WEAK]
    numbers = [w for w in words if w in SI_ROMAN_TENS or (w in SI_ROMAN_UNITS and len(w) > 4)]
    counts = [t for t in text.split() if _SI_ROMAN_COUNT.match(t.strip(".,!?"))]
    return bool(strong) or (bool(numbers) and bool(counts or len(words) > 1))


def _roman_sinhala_numbers(tokens: list[str]) -> list[str]:
    def digit_run(start: int) -> tuple[str, int]:
        j, digits = start, ""
        while j < len(tokens) and tokens[j].lower() in _SI_ROMAN_DIGITS:
            digits += str(SI_ROMAN_UNITS[tokens[j].lower()])
            j += 1
        return digits, j

    split: list[str] = []
    for tok in tokens:
        if c := _SI_ROMAN_COUNT.match(tok):
            split.append(c.group(1))  # "10k" -> 10
            continue
        m = _SI_ROMAN_GLUED.match(tok)
        rest = m.group(2).lower() if m else ""
        if m and (rest in SI_ROMAN_TENS or rest in SI_ROMAN_UNITS):
            split += [m.group(1), m.group(2)]
        else:
            split.append(tok)
    tokens = split

    out: list[str] = []
    i = 0
    while i < len(tokens):
        low = tokens[i].lower()
        digits, j = digit_run(i)  # "hatha deka binduwa paha" = 6205
        if len(digits) >= 2:
            out.append(digits)
            i = j
            continue
        compound, j = None, i + 1
        nxt = tokens[i + 1].lower() if i + 1 < len(tokens) else ""
        if low in SI_ROMAN_PREFIX and nxt in SI_ROMAN_UNITS:
            compound, j = SI_ROMAN_PREFIX[low] + SI_ROMAN_UNITS[nxt], i + 2  # "visi paha" = 25
        else:
            for pre, value in SI_ROMAN_PREFIX.items():  # glued: "visipaha", "hetadeka"
                if low.startswith(pre) and low[len(pre) :] in SI_ROMAN_UNITS:
                    compound = value + SI_ROMAN_UNITS[low[len(pre) :]]
        if compound is not None:
            # Part numbers are often said in pairs: "heta deka binduwa paha" = 62|05.
            tail, k = digit_run(j)
            out.append(f"{compound}{tail}" if len(tail) >= 2 else str(compound))
            i = k if len(tail) >= 2 else j
            continue
        hit = SI_ROMAN_TENS.get(low)
        if hit is None:
            hit = SI_ROMAN_UNITS.get(low)
        out.append(str(hit) if hit is not None else tokens[i])
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


def normalize_numbers(text: str, *, sinhala_latin: bool = False) -> str:
    """Replace spelled-out Tamil/Sinhala/English numbers with digits; keep everything else.

    sinhala_latin: also read Sinhala number words written in Latin letters ("dahayak").
    """

    def numbers(run: list[str]) -> list[str]:
        run = _sinhala_numbers(_tamil_numbers(run))
        return _english_numbers(_roman_sinhala_numbers(run) if sinhala_latin else run)

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
            result += numbers(run)
            result[-1] += p
            run = []
    if run:
        result += numbers(run)
    return " ".join(result)


def token_count(text: str) -> int:
    return len(_TOKEN.findall(text))
