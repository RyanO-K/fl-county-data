"""Clerk of Court official-records index feeds -> recorded_instruments,
instrument_parties, instrument_parcels.

Sources (see docs/superpowers/specs/2026-09-16-owner-recordings-design.md):
  hillsborough  daily D (documents) / P (parties) pipe-delimited files
  hernando      weekly comma-delimited CSV, one row per grantor x grantee
  broward       FTPS index; only active once FL_BROWARD_FTP_USER/PASS are set

Usage:
    python recordings.py                # every configured county
    python recordings.py hillsborough   # one county
    python recordings.py --link-only    # re-run the parcel link step only
"""
import re

NAME_KEY_LEN = 30
CATEGORIES = ("deed", "mortgage", "satisfaction", "assignment", "lien",
              "lis_pendens", "judgment", "release", "other")

_NON_ALNUM = re.compile(r"[^A-Z0-9]+")
_TRAILING = ("ET AL", "ETAL", "ET UX", "ETUX", "ET VIR", "ETVIR",
             "TRUSTEES", "TRUSTEE", "TR")


def norm_name(s):
    """Upper-case, punctuation to spaces, single spaces, trailing
    et-al / et-ux / trustee tokens removed. JR/SR are kept: the DOR roll
    keeps them too."""
    if not s:
        return ""
    n = _NON_ALNUM.sub(" ", str(s).upper()).strip()
    changed = True
    while changed and n:
        changed = False
        for t in _TRAILING:
            if n == t:
                return ""
            if n.endswith(" " + t):
                n = n[: -len(t) - 1].rstrip()
                changed = True
    return n


def name_key(s):
    """Index key: the statewide layer truncates OWN_NAME at 33 characters, so
    both sides compare on the first NAME_KEY_LEN characters."""
    return norm_name(s)[:NAME_KEY_LEN]


# Ordered: the first matching rule wins, so RELEASE/SATISFACTION beat the
# instrument they release, and ASSIGNMENT beats MORTGAGE.
_CATEGORY_RULES = (
    ("satisfaction", ("SATISFACTION",)),
    ("release", ("RELEASE",)),
    ("assignment", ("ASSIGNMENT",)),
    ("lis_pendens", ("LIS PENDENS", "LISPENDENS")),
    ("mortgage", ("MORTGAGE", "MTG")),
    ("deed", ("DEED",)),
    ("lien", ("LIEN",)),
    ("judgment", ("JUDGMENT", "JDGMNT", "JUDGEMENT")),
)


def categorize(doc_text):
    t = norm_name(doc_text)
    for cat, needles in _CATEGORY_RULES:
        if any(n in t for n in needles):
            return cat
    return "other"
