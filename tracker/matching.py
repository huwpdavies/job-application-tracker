"""Normalisation and fuzzy matching of company names and role titles."""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field

from rapidfuzz import fuzz

_LEGAL = {
    "ltd", "limited", "plc", "llp", "llc", "inc", "incorporated", "co", "company", "corp", "corporation",
    "gmbh", "ag", "bv", "sa", "uk", "group", "holdings", "the", "and", "io", "com", "net",
}
_SENIORITY = {"senior", "sr", "junior", "jr", "lead", "principal", "head", "graduate", "intern", "associate", "chief", "director", "manager"}
_NOISE = re.compile(r"\((?:[^)]*)\)|\[(?:[^\]]*)\]|\b[mfd]/[mfd](?:/[mfd])?\b", re.IGNORECASE)
_PUNCT = re.compile(r"[^a-z0-9 ]+")
_TERMINAL = ("Rejected", "Offer", "Withdrawn", "No response")

COMPANY_RATIO = 88
ROLE_RATIO = 80


def norm_company(name: str | None) -> str:
    s = (name or "").lower().replace("&", " and ")
    s = _PUNCT.sub(" ", s)
    toks = [t for t in s.split() if t not in _LEGAL]
    return " ".join(toks) or _PUNCT.sub(" ", (name or "").lower()).strip()


def norm_role(title: str | None) -> str:
    s = _NOISE.sub(" ", (title or "").lower().replace("&", " and "))
    return " ".join(_PUNCT.sub(" ", s).split())


def norm_ref(ref: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (ref or "").lower())


def company_match(a: str, b: str) -> bool:
    """a, b already normalised."""
    if not a or not b:
        return False
    if a == b or fuzz.ratio(a, b) >= COMPANY_RATIO:
        return True
    ta, tb = a.split(), b.split()
    short, long_ = (a, tb) if len(ta) <= len(tb) else (b, ta)
    # "northbridge" vs "northbridge insurance": one name is the leading words of the other
    return len(short) >= 4 and long_[: len(short.split())] == short.split()


def role_score(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 100.0
    if (set(a.split()) & _SENIORITY) != (set(b.split()) & _SENIORITY):
        return 0.0
    ta, tb = set(a.split()), set(b.split())
    short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    if len(short) >= 2 and short <= long_:
        return 90.0  # one title is the other plus extra words ("... Program: Learn. Hack. Secure!")
    return float(fuzz.token_sort_ratio(a, b))


@dataclass
class Match:
    app_id: int | None = None
    reason: str = ""
    candidates: list[int] = field(default_factory=list)  # same-company applications, if any
    ambiguous: bool = False


def find_match(conn: sqlite3.Connection, company: str, role: str | None, job_ref: str | None) -> Match:
    cn, rn, ref = norm_company(company), norm_role(role), norm_ref(job_ref)
    cands = [
        r
        for r in conn.execute("SELECT id, company_norm, role_norm, job_reference, status FROM applications")
        if company_match(cn, r["company_norm"])
    ]
    if not cands:
        return Match()
    ids = [r["id"] for r in cands]
    if ref:
        same = [r for r in cands if norm_ref(r["job_reference"]) == ref]
        if len(same) == 1:
            return Match(same[0]["id"], "job reference", ids)
    if rn:
        scored = sorted(((role_score(rn, r["role_norm"]), r["id"]) for r in cands), reverse=True)
        good = [s for s in scored if s[0] >= ROLE_RATIO]
        if good:
            return Match(good[0][1], f"role match {good[0][0]:.0f}", ids)
        blank = [r for r in cands if not r["role_norm"]]
        if len(blank) == 1:  # an application recorded without a role can take the first email that names one
            return Match(blank[0]["id"], "application has no role recorded", ids)
        return Match(None, "same company, different role", ids)
    if len(cands) == 1:
        return Match(cands[0]["id"], "only application at this company", ids)
    active = [r for r in cands if r["status"] not in _TERMINAL]
    if len(active) == 1:
        return Match(active[0]["id"], "only active application at this company", ids)
    return Match(None, "several applications at this company and no role given", ids, ambiguous=True)
