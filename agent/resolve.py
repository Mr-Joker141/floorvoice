"""Fuzzy resolution of spoken references against ERPNext masters (pure, no I/O)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

from erp.masters import is_open, split_aliases

MAX_RESULTS = 5
MIN_SCORE = 0.5

# Letters plus the Tamil block, so vowel signs/virama (not \w in Python) stay inside words.
_WORD = re.compile(r"[0-9]+|(?:[^\W\d_]|[\u0B80-\u0BFF\u0D80-\u0DFF])+")
_INDIC = re.compile(r"[\u0B80-\u0BFF\u0D80-\u0DFF]")  # Tamil, Sinhala
# "PO-JOB00005" and its amendment "PO-JOB00005-1" are both card 5.
_JOB_NO = re.compile(r"JOB0*(\d+)|(\d+)$")
# Filler words that should not count against a match.
STOPWORDS = {
    "the", "a", "an", "of", "some", "me", "please", "pcs", "pieces", "nos", "number", "no",
    "item", "items", "card", "job", "warehouse", "from", "to", "for", "mm",
}  # fmt: skip


def tokens(text: str) -> list[str]:
    """Lower-cased word/number tokens; letters and digits are split ("M8x30" -> m, 8, x, 30)."""
    # Zero-width joiners (Sinhala conjuncts like ස්ප්‍රින්ග්) are optional in ASR output.
    return [t.casefold() for t in _WORD.findall(text.replace("\u200d", ""))]


def _stem(tok: str) -> str:
    if len(tok) > 3 and tok.endswith("es") and tok[:-2].endswith(("sh", "ch", "x")):
        return tok[:-2]
    if len(tok) > 3 and tok.endswith("s") and not tok.endswith("ss"):
        return tok[:-1]
    return tok


def _tok_match(q: str, c: str) -> float:
    if q == c:
        return 1.0
    if q.isdigit() or c.isdigit():
        return 0.0  # numbers must match exactly
    qs, cs = _stem(q), _stem(c)
    if qs == cs:
        return 1.0
    if len(qs) >= 3 and cs.startswith(qs):
        return 0.85
    if len(cs) >= 3 and _INDIC.match(qs) and qs.startswith(cs):
        return 0.9  # case suffix glued on: "ஸ்டோர்ஸ்லிருந்து" = "from stores", "ලයින්වලට"
    if min(len(qs), len(cs)) < 4:
        return 0.0  # short words only match exactly ("to" must not match "two")
    ratio = SequenceMatcher(None, qs, cs).ratio()
    return ratio if ratio >= 0.8 else 0.0


def score(query: str, candidates: list[str]) -> float:
    """How well `query` matches the best of `candidates`, 0..1.

    Every significant query token must find a counterpart; digits must match exactly.
    """
    q_toks = [t for t in tokens(query) if t not in STOPWORDS]
    if not q_toks:
        return 0.0
    best = 0.0
    for cand in candidates:
        c_toks = tokens(cand)
        if not c_toks:
            continue
        if query.strip().casefold() == cand.strip().casefold():
            return 1.0
        total = 0.0
        for q in q_toks:
            m = max((_tok_match(q, c) for c in c_toks), default=0.0)
            if q.isdigit() and m == 0.0:
                total = 0.0  # a number in the query that the candidate lacks rules it out
                break
            total += m
        best = max(best, total / len(q_toks))
    return best


@dataclass
class Masters:
    items: list[dict[str, Any]] = field(default_factory=list)
    warehouses: list[dict[str, Any]] = field(default_factory=list)
    job_cards: list[dict[str, Any]] = field(default_factory=list)
    # item_code -> {warehouse name: qty}
    stock: dict[str, dict[str, float]] = field(default_factory=dict)

    def item(self, code: str) -> dict[str, Any] | None:
        return next((i for i in self.items if i["item_code"] == code), None)

    def warehouse(self, ref: str) -> dict[str, Any] | None:
        """Exact lookup by full name or short warehouse_name (case-insensitive)."""
        key = ref.strip().casefold()
        return next(
            (
                w
                for w in self.warehouses
                if key in (w["name"].casefold(), w["warehouse_name"].casefold())
            ),
            None,
        )

    def job_card(self, name: str) -> dict[str, Any] | None:
        return next((j for j in self.job_cards if j["name"] == name), None)


def _ranked(scored: list[tuple[float, dict[str, Any]]]) -> list[tuple[float, dict[str, Any]]]:
    hits = [(s, x) for s, x in scored if s >= MIN_SCORE]
    hits.sort(key=lambda p: -p[0])
    if hits and hits[0][0] == 1.0:
        # An exact hit beats partial ones, but keep equally-exact siblings (true collisions).
        hits = [h for h in hits if h[0] == 1.0]
    return hits[:MAX_RESULTS]


def search_items(m: Masters, query: str) -> list[dict[str, Any]]:
    """Fuzzy item search. If a spoken number matches no item (often an ASR mishearing,
    e.g. "bearing 2625Z" for 6205), fall back to the same item family without the number
    and flag the results so the agent asks which one was meant."""
    hits = _search_items(m, query)
    if hits:
        return hits
    words = [t for t in _WORD.findall(query) if not any(ch.isdigit() for ch in t)]
    if len(words) == len(_WORD.findall(query)) or not words:
        return []
    return [
        {**h, "match": min(h["match"], 0.6), "note": "spoken number matched no item"}
        for h in _search_items(m, " ".join(words))
    ]


OPTION_MIN_SCORE = 0.9


def option_items(m: Masters, options: list[str]) -> list[tuple[str, dict[str, Any]]]:
    """[(option, item)] when every option of a question names a distinct item, else [].

    Decides whether a question is over an item (and so whether a photo can answer it).
    """
    out: list[tuple[str, dict[str, Any]]] = []
    for opt in options:
        key = opt.strip().casefold()
        item = next((i for i in m.items
                     if key in (i["item_code"].casefold(), i["item_name"].casefold())), None)  # fmt: skip
        if item is None and m.warehouse(opt) is None:
            hits = _search_items(m, opt)
            clear = len(hits) == 1 or (len(hits) > 1 and hits[0]["match"] > hits[1]["match"])
            if hits and clear and hits[0]["match"] >= OPTION_MIN_SCORE:
                item = m.item(hits[0]["item_code"])
        if item is None or any(i["item_code"] == item["item_code"] for _, i in out):
            return []
        out.append((opt, item))
    return out if len(out) >= 2 else []


def _search_items(m: Masters, query: str) -> list[dict[str, Any]]:
    scored = [
        (
            score(query, [i["item_code"], i["item_name"], *split_aliases(i.get("voice_aliases"))]),
            i,
        )
        for i in m.items
    ]
    return [
        {
            "item_code": i["item_code"],
            "item_name": i["item_name"],
            "uom": i.get("stock_uom", ""),
            "stock_by_warehouse": m.stock.get(i["item_code"], {}),
            "match": round(s, 2),
        }
        for s, i in _ranked(scored)
    ]


def search_warehouses(m: Masters, query: str) -> list[dict[str, Any]]:
    scored = [
        (score(query, [w["name"], w["warehouse_name"], *split_aliases(w.get("voice_aliases"))]), w)
        for w in m.warehouses
    ]
    return [{"name": w["name"], "match": round(s, 2)} for s, w in _ranked(scored)]


def _pending(jc: dict[str, Any]) -> float:
    if not is_open(jc):
        return 0.0
    return float(jc.get("for_quantity") or 0) - float(jc.get("total_completed_qty") or 0)


def job_number(name: str) -> int | None:
    found = _JOB_NO.search(name)
    return int(found.group(1) or found.group(2)) if found else None


def code_numbers(item_code: str) -> set[str]:
    """Part numbers in an item code worth confusing (3+ digits): BRG-6205 -> {"6205"}."""
    return {t for t in tokens(item_code) if t.isdigit() and len(t) >= 3}


def confusable(m: Masters, number: str) -> set[str]:
    """Other item numbers one spoken digit away (6205 -> 6201, 6204, 6305)."""
    out: set[str] = set()
    for item in m.items:
        for n in code_numbers(item["item_code"]):
            if len(n) == len(number) and sum(a != b for a, b in zip(n, number, strict=True)) == 1:
                out.add(n)
    return out


def spoken_numbers(text: str) -> set[str]:
    return {t for t in tokens(text) if t.isdigit()}


def search_job_cards(m: Masters, query: str) -> list[dict[str, Any]]:
    nums = [int(t) for t in tokens(query) if t.isdigit()]
    scored: list[tuple[float, dict[str, Any]]] = []
    for jc in m.job_cards:
        if nums:
            # "job card 4" / "PO-JOB00004": match on the card's sequence number (open or not,
            # so the agent can say "already completed").
            s = 1.0 if job_number(jc["name"]) in nums else 0.0
        elif not is_open(jc):
            continue
        else:
            s = score(
                query,
                [
                    jc["name"],
                    jc.get("item_name") or "",
                    jc.get("production_item") or "",
                    jc.get("workstation") or "",
                    jc.get("operation") or "",
                ],
            )
        scored.append((s, jc))
    return [
        {
            "name": jc["name"],
            "item": jc.get("item_name") or jc.get("production_item"),
            "status": "Open" if is_open(jc) else "Completed",
            "pending_qty": _pending(jc),
            "workstation": jc.get("workstation"),
            "match": round(s, 2),
        }
        for s, jc in _ranked(scored)
    ]


def _coverage(text_toks: set[str], cand: str) -> float:
    """Fraction of the candidate's significant tokens heard in the utterance."""
    c_toks = [t for t in tokens(cand) if t not in STOPWORDS]
    if not c_toks:
        return 0.0
    hit = 0.0
    for c in c_toks:
        hit += max((_tok_match(t, c) for t in text_toks), default=0.0)
    # Small bonus per matched token so "பேரிங் 6205" outranks the generic "பேரிங்".
    return hit / len(c_toks) + 0.01 * hit


def _in_sequence(said: list[str], name: str) -> bool:
    """`name`'s words appear consecutively in the utterance ("line 2", not "2 kg ... line")."""
    want = tokens(name)
    if not want:
        return False
    for i in range(len(said) - len(want) + 1):
        if all(_tok_match(said[i + k], w) >= 0.85 for k, w in enumerate(want)):
            return True
    return False


def _top_tier(scored: list[tuple[float, dict[str, Any]]], floor: float) -> list[dict[str, Any]]:
    hits = [(s, x) for s, x in scored if s >= floor]
    if not hits:
        return []
    best = max(s for s, _ in hits)
    return [x for s, x in hits if s >= best - 1e-9][:MAX_RESULTS]


# ASR homophones for small numbers right after "card" ("job card for done" = job card 4).
CARD_HOMOPHONES = {"won": 1, "to": 2, "too": 2, "tu": 2, "for": 4, "fore": 4, "ate": 8}


def _card_numbers(utterance: str) -> set[int]:
    toks = tokens(utterance)
    nums: set[int] = set()
    for i, t in enumerate(toks[:-1]):
        if t in ("card", "கார்டு", "கார்ட்"):
            nxt = toks[i + 1]
            if nxt.isdigit():
                nums.add(int(nxt))
            elif nxt in CARD_HOMOPHONES:
                nums.add(CARD_HOMOPHONES[nxt])
    return nums


def candidates(m: Masters, utterance: str) -> dict[str, list[dict[str, Any]]]:
    """Masters the utterance most likely refers to, for the agent's first turn.

    Saves a search round-trip in the common case; ties are kept so collisions stay visible.
    """
    heard = set(tokens(utterance))
    items = _top_tier(
        [
            (
                max(
                    _coverage(heard, s)
                    for s in [i["item_name"], *split_aliases(i.get("voice_aliases"))]
                ),
                i,
            )
            for i in m.items
        ],
        0.5,
    )
    said = tokens(utterance)
    whs = [
        w
        for w in m.warehouses
        if any(
            _in_sequence(said, s)
            for s in [w["warehouse_name"], *split_aliases(w.get("voice_aliases"))]
        )
    ]
    cards = [j for j in m.job_cards if job_number(j["name"]) in _card_numbers(utterance)]
    return {
        "items": [
            {
                "item_code": i["item_code"],
                "item_name": i["item_name"],
                "uom": i.get("stock_uom", ""),
                "stock_by_warehouse": m.stock.get(i["item_code"], {}),
            }
            for i in items
        ],
        "warehouses": [w["name"] for w in whs],
        "job_cards": [
            {
                "name": j["name"],
                "item": j.get("item_name") or j.get("production_item"),
                "status": "Open" if is_open(j) else "Completed",
                "pending_qty": _pending(j),
            }
            for j in cards
        ],
    }
