import pytest

from agent.normalize import detect_language, normalize_numbers, token_count


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
