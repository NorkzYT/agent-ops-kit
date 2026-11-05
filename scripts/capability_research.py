#!/usr/bin/env python3
"""Source helpers for the pm-competitor-research method adapter.

Pure functions, no I/O, no network: quote matching against captured source
text, derived-excerpt parsing, instruction-like text detection, price wording
rules (currency, unit, billing cadence), primary-source host matching and
freshness arithmetic. They decide whether a quoted vendor statement is really
in the captured page; they never fetch a page and never judge whether the
vendor's statement is true.

Method adapted from PM Skills (https://github.com/phuryn/pm-skills) at
2a12295a7717569baa7be181b4cfebe3ca38a6bb, MIT License, Copyright (c) 2026
Pawel Huryn; the notice and file list are in
hermes/skills/pm-competitor-research/references/.
"""

from __future__ import annotations

import datetime as _dt
import re
from typing import Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit

PM_SKILLS_REVISION = "2a12295a7717569baa7be181b4cfebe3ca38a6bb"

# Window (characters of normalized text) within which a price point's quotes and its plan name must sit.
QUOTE_SPAN = 600
PLAN_LOOKBACK = 300

# Text inside a source that tries to instruct the reader. It is data: listed in the report, never usable as evidence.
INJECTION = re.compile(
    r"\b(ignore|disregard|forget)\b[^.\n]{0,40}\b(previous|prior|above|earlier|all)\b[^.\n]{0,40}\b(instructions?|rules|prompts?)\b"
    r"|\b(you are|act as)\s+(now\s+)?(an?\s+)?(ai|assistant|language model|chatbot|agent)\b"
    r"|\bsystem prompt\b|\bnew instructions?\s*:"
    r"|\b(ai|assistant|language model|llm|agent)s?\b[^.\n]{0,40}\b(must|should)\s+(say|state|report|rank|recommend|write)\b", re.I)

# Market size/share/growth or leadership claims: only an observed statement with a cited source may carry them.
MARKET_CLAIM = re.compile(
    r"\bmarket[- ](size|share|leader|leadership)\b|\b(TAM|SAM|SOM)\b|\bfastest[- ]growing\b|\bmost popular\b"
    r"|\b(number one|#1|no\. 1)\b|\b\d+(\.\d+)?\s*%\s+of\s+(the\s+)?(market|teams|companies|users|customers)\b"
    r"|\b\d+(\.\d+)?\s*(million|billion|bn|m)\s+(users|customers|teams|in revenue|ARR)\b"
    r"|\bmarket (is|was) (worth|growing)\b|\bCAGR\b", re.I)

CURRENCY_MARKS = {"USD": ("$", "USD", "US$"), "EUR": ("€", "EUR"), "GBP": ("£", "GBP")}
UNIT_RE = {"per-seat": re.compile(r"\bseats?\b", re.I), "per-user": re.compile(r"\b(users?|members?)\b", re.I),
           "per-account": re.compile(r"\b(account|workspace|organi[sz]ation|location|site|practice)\b", re.I),
           "flat-fee": re.compile(r"\b(flat|per (month|year)|/\s*(month|mo|year|yr))\b", re.I),
           "usage-based": re.compile(r"\b(per (request|credit|run|call|unit|message|minute)|usage|pay-as-you-go)\b", re.I)}
ANNUAL_RE = re.compile(r"\b(billed annually|paid annually|billed yearly|annual (plan|price|billing)|per year|/\s*year|yearly)\b", re.I)
MONTHLY_BILLING_RE = re.compile(r"\b(billed monthly|paid monthly|monthly billing|month-to-month)\b", re.I)
CONTACT_RE = re.compile(r"\b(contact (sales|us)|custom pricing|talk to sales|request a quote)\b", re.I)
NUMBER_RE = re.compile(r"(?<![A-Za-z0-9_.])-?\d+(?:,\d{3})*(?:\.\d+)?")


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def numbers(text: str) -> List[float]:
    return [float(m.group(0).replace(",", "")) for m in NUMBER_RE.finditer(text)]


def same(a: float, b: float) -> bool:
    return abs(a - b) <= 1e-9 * max(1.0, abs(a), abs(b))


# ---------------------------------------------------------------- captures

def quote_space(capture: str, derived: bool) -> str:
    """Normalized text a quote may come from. A derived excerpt (the item has a snapshot_digest) offers only its
    '> ' quoted lines, one per line, never its attribution header; a full capture offers all of its text."""
    if not derived:
        return "\n".join(norm(line) for line in capture.splitlines())
    return "\n".join(norm(line[2:]) for line in capture.splitlines() if line.startswith("> "))


def quoted_lines(capture: str) -> List[str]:
    return [norm(line[2:]) for line in capture.splitlines() if line.startswith("> ") and norm(line[2:])]


def find(space: str, quote: str, start: int = 0) -> int:
    """Offset of the normalized quote in the space at or after ``start``, or -1. Lines are searched one by
    one, so a quote can never be stitched across two captured lines."""
    q = norm(quote)
    if not q:
        return -1
    off = 0
    for line in space.split("\n"):
        end = off + len(line)
        if end >= start:
            j = line.find(q, max(0, start - off))
            if j >= 0:
                return off + j
        off = end + 1
    return -1


def find_in_order(space: str, quotes: Sequence[str]) -> List[int]:
    """Positions of fragments read in order: each is looked up after the previous one (the nearest following
    occurrence), falling back to its first occurrence. -1 when a fragment is absent."""
    out: List[int] = []
    prev = 0
    for q in quotes:
        p = find(space, q, prev)
        if p < 0:
            p = find(space, q)
        out.append(p)
        if p >= 0:
            prev = p
    return out


def injection_hits(text: str) -> List[str]:
    return sorted({norm(m.group(0)) for m in INJECTION.finditer(text)})


def overlaps_injection(quote: str, capture_space: str, hits: Sequence[str]) -> Optional[str]:
    """The injected phrase a quote contains, or that sits on the same captured line as the quote."""
    q = norm(quote).lower()
    for h in hits:
        hl = h.lower()
        if hl in q:
            return h
        for line in capture_space.split("\n"):
            low = line.lower()
            if q in low and hl in low:
                return h
    return None


def plan_near(space: str, plan: str, first: int, quotes_text: str, other_plans: Sequence[str] = ()) -> bool:
    """The plan name appears in the quoted text, or is the last of the competitor's plan names within
    PLAN_LOOKBACK characters before the first quote with no Markdown heading in between (so a price under
    the next plan's heading never passes)."""
    def key(x: str) -> str:
        return norm(x).lower().replace("\\*", "").rstrip("*").strip()
    p = key(plan)
    if not p:
        return False
    if p in quotes_text.lower():
        return True
    window = space[max(0, first - PLAN_LOOKBACK):first].lower().replace("\\*", "")
    at = window.rfind(p)
    if at < 0:
        return False
    after = window[at + len(p):]
    if re.search(r"(^|\n)#{1,6} ", after):
        return False  # another heading sits between the plan name and the price
    return not any(key(o) and key(o) not in p and key(o) in after for o in other_plans)


def span_ok(positions: Sequence[int], quotes: Sequence[str]) -> bool:
    if not positions:
        return True
    end = max(p + len(norm(q)) for p, q in zip(positions, quotes))
    return end - min(positions) <= QUOTE_SPAN


# ---------------------------------------------------------------- price wording

def price_wording_errors(pp: dict, quote_text: str) -> List[str]:
    """What the quoted words must support for one price point (status, amount, currency, unit, cadence)."""
    pid, status, amount = pp["id"], pp["status"], pp["amount"]
    errs: List[str] = []
    if status == "unknown":
        if amount is not None or pp["currency"] is not None:
            errs.append(f"price {pid}: an unknown price has no amount or currency")
        if pp["quotes"]:
            errs.append(f"price {pid}: an unknown price cites no quote (record what is missing in its resolution)")
        if "resolution" not in pp:
            errs.append(f"price {pid}: an unknown price needs a resolution (how to retrieve it)")
        if pp["cadence"] != "not-applicable" or pp["unit"] != "not-applicable":
            errs.append(f"price {pid}: an unknown price has unit and cadence 'not-applicable'")
        return errs
    if not pp["quotes"]:
        return [f"price {pid}: a {status} price needs at least one verbatim quote"]
    if status == "contact-sales":
        if amount is not None or pp["currency"] is not None:
            errs.append(f"price {pid}: contact-sales pricing is not public; it has no amount or currency")
        if not CONTACT_RE.search(quote_text):
            errs.append(f"price {pid}: the quote does not say the price needs sales contact")
        if pp["cadence"] != "not-applicable":
            errs.append(f"price {pid}: contact-sales pricing has cadence 'not-applicable'")
        return errs
    if amount is None or pp["currency"] is None:
        return [f"price {pid}: a stated price needs an amount and a currency"]
    if not any(same(amount, n) for n in numbers(quote_text)):
        errs.append(f"price {pid}: amount {amount:g} is not in the quoted text")
    if not any(mark in quote_text for mark in CURRENCY_MARKS[pp["currency"]]):
        errs.append(f"price {pid}: the quoted text does not show currency {pp['currency']}")
    unit = pp["unit"]
    if unit != "not-applicable" and not UNIT_RE[unit].search(quote_text):
        errs.append(f"price {pid}: the quoted text does not say the price is {unit}")
    cad = pp["cadence"]
    annual, monthly = bool(ANNUAL_RE.search(quote_text)), bool(MONTHLY_BILLING_RE.search(quote_text))
    if cad == "billed-annually" and not annual:
        errs.append(f"price {pid}: cadence billed-annually but the quoted text does not say it is billed annually")
    elif cad == "billed-monthly" and not monthly:
        errs.append(f"price {pid}: cadence billed-monthly but the quoted text does not say it is billed monthly")
    elif cad == "unstated":
        if annual or monthly:
            errs.append(f"price {pid}: the quoted text states a billing cadence; record it instead of 'unstated'")
        if not pp["caveats"]:
            errs.append(f"price {pid}: an unstated billing cadence needs a caveat saying what is ambiguous")
    elif cad == "not-applicable" and amount != 0:
        errs.append(f"price {pid}: only a free (0) price has cadence 'not-applicable'")
    return errs


# ---------------------------------------------------------------- sources

def host_of(ref: str) -> str:
    try:
        return (urlsplit(ref).hostname or "").lower()
    except ValueError:
        return ""


def on_domain(ref: str, domain: str) -> bool:
    h = host_of(ref)
    return bool(h) and (h == domain or h.endswith("." + domain))


def retrieved_date(ts: str) -> _dt.date:
    return _dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(_dt.timezone.utc).date()


def age_days(ts: str, as_of: str) -> int:
    return (_dt.date.fromisoformat(as_of) - retrieved_date(ts)).days


def reverify_by(as_of: str, max_age: int, oldest: Optional[str]) -> str:
    """Last date the comparison is inside its freshness window (counted from the oldest cited retrieval)."""
    base = retrieved_date(oldest) if oldest else _dt.date.fromisoformat(as_of)
    return (base + _dt.timedelta(days=max_age)).isoformat()


def private_term_hits(texts: Sequence[str], terms: Sequence[str]) -> List[Tuple[str, str]]:
    out = []
    for t in texts:
        low = t.lower()
        for term in terms:
            if term.lower() in low:
                out.append((term, t))
    return out


def money(amount: Optional[float], currency: Optional[str]) -> str:
    if amount is None:
        return ""
    sym = {"USD": "$", "EUR": "€", "GBP": "£"}.get(currency or "", "")
    txt = f"{int(amount):,}" if amount == int(amount) else f"{amount:,.2f}"
    return f"{sym}{txt} {currency}"


def snapshot_errors(item_id: str, capture: str, snapshot: str) -> List[str]:
    """Every quoted line of a derived excerpt must occur in the full snapshot it claims to come from."""
    full = norm(snapshot)
    return [f"evidence {item_id}: quoted line not in the retrieved snapshot: {line[:120]!r}"
            for line in quoted_lines(capture) if line not in full]


def values_in_excerpt(item: dict) -> List[str]:
    nums = numbers(item["excerpt"])
    return [f"evidence {item['source_id']}: value {k}={v:g} is not in its excerpt"
            for k, v in item.get("values", {}).items() if not any(same(float(v), n) for n in nums)]


def excerpt_errors(item: dict, space: str) -> List[str]:
    """Each line of the pack's excerpt must be verbatim captured text (no unverifiable snippets)."""
    return [f"evidence {item['source_id']}: excerpt line is not in its captured file: {line[:120]!r}"
            for line in (norm(x) for x in item["excerpt"].split("\n")) if line and find(space, line) < 0]


def sources_table_row(it: dict) -> Dict[str, str]:
    return {"id": it["source_id"], "title": it["title"], "ref": it["ref"], "retrieved": it.get("retrieved_at", "not recorded"),
            "capture": it.get("digest", ""), "snapshot": it.get("snapshot_digest", "")}
