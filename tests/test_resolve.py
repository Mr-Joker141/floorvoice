from agent.normalize import normalize_numbers
from agent.resolve import (
    Masters,
    candidates,
    score,
    search_items,
    search_job_cards,
    search_warehouses,
)


def codes(rows: list[dict]) -> list[str]:
    return sorted(r["item_code"] for r in rows)


def test_specific_item_is_unique(masters: Masters) -> None:
    assert codes(search_items(masters, "bearing 6205")) == ["BRG-6205"]
    assert codes(search_items(masters, "BRG-6205")) == ["BRG-6205"]
    assert codes(search_items(masters, "பேரிங் 6205")) == ["BRG-6205"]


def test_generic_item_is_a_collision(masters: Masters) -> None:
    assert codes(search_items(masters, "bearings")) == [
        "BRG-6201", "BRG-6204", "BRG-6205", "BRG-6305",
    ]  # fmt: skip
    assert codes(search_items(masters, "போல்ட்")) == ["BLT-M10X40", "BLT-M8X30"]


def test_numbers_must_match_exactly(masters: Masters) -> None:
    assert all("note" in h for h in search_items(masters, "bearing 6206"))
    assert score("bearing 6205", ["Ball Bearing 6204"]) == 0.0


def test_search_results_carry_stock(masters: Masters) -> None:
    [hit] = search_items(masters, "grease")
    assert hit["uom"] == "Kg"
    assert hit["stock_by_warehouse"]["Stores - FVD"] == 40


def test_warehouses(masters: Masters) -> None:
    names = lambda q: [w["name"] for w in search_warehouses(masters, q)]
    assert names("store") == ["Stores - FVD"]
    assert names("line two") == ["Line 2 - FVD"]
    assert names("லைன் 2") == ["Line 2 - FVD"]
    assert sorted(names("line")) == ["Line 1 - FVD", "Line 2 - FVD"]


def test_job_cards_by_number_and_item(masters: Masters) -> None:
    [jc] = search_job_cards(masters, "job card 4")
    assert jc["name"] == "PO-JOB00004"
    assert jc["pending_qty"] == 30
    assert [j["name"] for j in search_job_cards(masters, "gearbox")] == [
        "PO-JOB00002",
        "PO-JOB00005",
    ]


def test_candidates_code_switched(masters: Masters) -> None:
    text = normalize_numbers("இரண்டு bearing 6205 store-ல இருந்து line two-க்கு மாத்து")
    c = candidates(masters, text)
    assert [i["item_code"] for i in c["items"]] == ["BRG-6205"]
    assert sorted(c["warehouses"]) == ["Line 2 - FVD", "Stores - FVD"]


def test_candidates_pure_tamil_with_glued_suffixes(masters: Masters) -> None:
    text = normalize_numbers("இரண்டு பேரிங் 6205 ஸ்டோர்ஸ்லிருந்து லைன் இரண்டுக்கு மாற்று")
    c = candidates(masters, text)
    assert [i["item_code"] for i in c["items"]] == ["BRG-6205"]
    assert sorted(c["warehouses"]) == ["Line 2 - FVD", "Stores - FVD"]


def test_candidates_keep_collisions(masters: Masters) -> None:
    c = candidates(masters, "send me bearings")
    assert len(c["items"]) == 4


def test_candidates_to_is_not_two(masters: Masters) -> None:
    c = candidates(masters, "move 5 seals from stores to line 1")
    assert sorted(c["warehouses"]) == ["Line 1 - FVD", "Stores - FVD"]


def test_candidates_job_card_homophone(masters: Masters) -> None:
    c = candidates(masters, "job card for completed 30 pieces")
    assert [j["name"] for j in c["job_cards"]] == ["PO-JOB00004"]


def test_completed_job_card_found_by_number_only(masters: Masters) -> None:
    masters.job_cards[3].update(status="Completed", docstatus=1, total_completed_qty=30)
    [jc] = search_job_cards(masters, "job card 4")
    assert (jc["status"], jc["pending_qty"]) == ("Completed", 0)
    assert [j["name"] for j in search_job_cards(masters, "pump")] == ["PO-JOB00001"]


def test_unknown_number_falls_back_to_family(masters: Masters) -> None:
    hits = search_items(masters, "bearing 2625Z")
    assert codes(hits) == ["BRG-6201", "BRG-6204", "BRG-6205", "BRG-6305"]
    assert all(h["note"] == "spoken number matched no item" for h in hits)
    assert search_items(masters, "unobtainium 9999") == []
