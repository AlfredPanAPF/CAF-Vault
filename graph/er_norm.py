"""Shared name normalization for ER blocking. Used by resolve_block.py and
fetch_gleif.py — both sides of a match must normalize identically."""
import re

# US + common international legal-form suffixes, stripped as trailing tokens
SUFFIXES = {
    "INC", "INCORPORATED", "CORP", "CORPORATION", "CO", "COMPANY", "LTD", "LIMITED",
    "PLC", "LLC", "LP", "SA", "NV", "AG", "SE", "HOLDINGS", "HOLDING", "GROUP",
    "SPA", "SRL", "AB", "ASA", "AS", "OYJ", "GMBH", "BV", "SAS", "SL", "PTE", "PTY",
    "KK", "KGAA", "AKTIENGESELLSCHAFT",
}


def norm(s: str) -> str:
    s = s.upper().replace("&", " AND ")
    s = re.sub(r"[^A-Z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def strip_suffix(s: str) -> str:
    """Drop trailing legal-form tokens. Trailing single letters also go — they are
    almost always punctuation debris from forms like 'A/S' or 'S.p.A.'."""
    toks = norm(s).split()
    while toks and (toks[-1] in SUFFIXES or len(toks[-1]) == 1):
        toks.pop()
    if toks and toks[0] == "THE":
        toks.pop(0)
    return " ".join(toks)
