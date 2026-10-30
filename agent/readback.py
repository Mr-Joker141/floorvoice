"""One-line confirmations built from posted documents (English and Tamil)."""

from __future__ import annotations

import re

from agent.tools import PostedDoc

DOCTYPE_SPOKEN = {
    "en": {
        "Stock Entry": "stock entry",
        "Job Card": "job card",
        "Material Request": "material request",
    },
    "ta": {
        "Stock Entry": "ஸ்டாக் என்ட்ரி",
        "Job Card": "ஜாப் கார்டு",
        "Material Request": "மெட்டீரியல் ரிக்வெஸ்ட்",
    },
}
_SEQ = re.compile(r"(\d+)$")


def spoken_number(doc_name: str) -> str:
    """'MAT-STE-2026-00012' -> '12' so speech synthesis reads it naturally."""
    m = _SEQ.search(doc_name)
    return str(int(m.group(1))) if m else doc_name


def readback(posted: list[PostedDoc], lang: str) -> tuple[str, str]:
    """Return (display text, speech text) for the posted documents."""
    lang = lang if lang in DOCTYPE_SPOKEN else "en"
    display: list[str] = []
    speech: list[str] = []
    for p in posted:
        dt = DOCTYPE_SPOKEN[lang][p.doctype]
        n = spoken_number(p.name)
        if lang == "ta":
            display.append(f"பதிவானது {p.name}: {p.summary}.")
            speech.append(f"{p.summary}. {dt} {n} பதிவானது.")
        else:
            display.append(f"Posted {p.name}: {p.summary}.")
            speech.append(f"Done. {p.summary[:1].upper()}{p.summary[1:]}. {dt.capitalize()} {n}.")
    return " ".join(display), " ".join(speech)
