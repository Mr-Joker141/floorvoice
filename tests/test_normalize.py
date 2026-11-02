import pytest

from agent.normalize import detect_language, looks_roman_sinhala, normalize_numbers, token_count


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("இரண்டு bearing 6205 store-ல இருந்து line two-க்கு மாத்து",
         "2 bearing 6205 store -ல இருந்து line 2 -க்கு மாத்து"),
        ("பத்து போல்ட் வேண்டும்", "10 போல்ட் வேண்டும்"),
        ("இருபத்தி ஐந்து நட்", "25 நட்"),
        ("ரெண்டு பேரிங்", "2 பேரிங்"),
        ("லைன் இரண்டுக்கு", "லைன் 2க்கு"),
        ("௫ பேரிங்", "5 பேரிங்"),
        ("job card four completed, fifty pieces.", "job card 4 completed, 50 pieces."),
        ("move twenty five bolts", "move 25 bolts"),
        ("one hundred and twenty washers", "120 washers"),
        ("send me bearings", "send me bearings"),
        ("six two zero five, ten", "6205, 10"),
        ("bearing six two oh five", "bearing 6205"),
        ("ஆறு இரண்டு பூஜ்யம் ஐந்து பேரிங்", "6205 பேரிங்"),
        ("five ten", "5 10"),
        ("one hundred and five", "105"),
        ("move to bearing 6205", "move to bearing 6205"),  # homophones are left to the agent
    ],
)  # fmt: skip
def test_normalize_numbers(text: str, expected: str) -> None:
    assert normalize_numbers(text) == expected


def test_detect_language() -> None:
    assert detect_language("2 bearing store-ல") == "ta"
    assert detect_language("send me bearings") == "en"


def test_token_count() -> None:
    assert token_count("  send me  ") == 2


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("බෙයාරිං 6205 දෙකක් ස්ටෝර්ස් එකෙන් ලයින් දෙකට දාන්න",
         "බෙයාරිං 6205 2ක් ස්ටෝර්ස් එකෙන් ලයින් 2ට දාන්න"),
        ("Job card තුන ඉවරයි, කෑලි විස්සක්", "Job card 3 ඉවරයි, කෑලි 20ක්"),
        ("මට බෝල්ට් දහයක් එවන්න", "මට බෝල්ට් 10ක් එවන්න"),
        ("කෑලි විසිපහක්", "කෑලි 25ක්"),
        ("විසි පහ", "25"),
        ("බෙයාරිං හය දෙක බිංදුව පහ", "බෙයාරිං 6205"),
        ("job card එක ඉවරයි", "job card එක ඉවරයි"),  # එක = "the", not 1
        ("line two-ට", "line 2 -ට"),
    ],
)  # fmt: skip
def test_normalize_sinhala(text: str, expected: str) -> None:
    assert normalize_numbers(text) == expected


def test_detect_sinhala() -> None:
    assert detect_language("මට බෝල්ට් 10ක් එවන්න") == "si"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("බෙයාරිං හැට දෙක බිංදුව පහ දෙකක්", "බෙයාරිං 6205 2ක්"),  # 62|05, then qty
        ("බෙයාරිං හැටදෙක බිංදුව පහ", "බෙයාරිං 6205"),
        ("ලයින් දෙකට", "ලයින් 2ට"),
    ],
)
def test_sinhala_code_in_pairs(text: str, expected: str) -> None:
    assert normalize_numbers(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("job card tuna ivaray kalivisak.", "job card 3 ivaray kali 20."),
        ("M10 bolt dahayak evanna", "M10 bolt 10 evanna"),
        ("bearing heta deka binduwa paha dekak stores eken", "bearing 6205 2 stores eken"),
        ("bearing haya deka binduwa paha", "bearing 6205"),
        ("visipahak", "25"),
    ],
)
def test_sinhala_latin(text: str, expected: str) -> None:
    assert normalize_numbers(text, sinhala_latin=True) == expected


def test_sinhala_latin_only_when_chosen() -> None:
    assert normalize_numbers("job card tuna") == "job card tuna"


def test_reply_language_follows_choice() -> None:
    from agent.normalize import reply_language

    assert reply_language("job card 3 ivaray kali 20", "si") == "si"
    assert reply_language("மட்ட bolt 10 evan", "si") == "si"
    assert reply_language("job card five completed", "ta") == "en"
    assert reply_language("බෝල්ට් 10", "ta") == "ta"
    assert reply_language("இரண்டு bearing", "") == "ta"


def test_sinhala_misspellings() -> None:
    assert normalize_numbers("job card තුන ඉවරයි කැලිවිස") == "job card 3 ඉවරයි කැලි 20"
    assert normalize_numbers("line දකට") == "line 2ට"


@pytest.mark.parametrize(
    "text",
    [
        "mata bearing 10k ewanna",
        "mata bearing dahayak ewanna",
        "bearing 6205 dekak line 2 ta yawanna",
        "job card 3 iwarai kali 20k",
        "bearing dahayak oone",
    ],
)
def test_singlish_detected(text: str) -> None:
    assert looks_roman_sinhala(text)


@pytest.mark.parametrize(
    "text",
    [
        "send me ten bearings",
        "move 2 bearing 6205 from stores to line 2",
        "job card 3 completed 5 pieces",
        "I need one bolt",
        "give me 10k bolts",
        "மட bearing அனுப்பு",
        "මට බෙයාරිං එවන්න",
    ],
)
def test_singlish_not_detected(text: str) -> None:
    assert not looks_roman_sinhala(text)


def test_singlish_count_suffix() -> None:
    assert normalize_numbers("mata bearing 10k ewanna", sinhala_latin=True) == (
        "mata bearing 10 ewanna"
    )
    assert normalize_numbers("give me 10k bolts") == "give me 10k bolts"  # English: untouched
