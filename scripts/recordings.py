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


# --- Parsers -----------------------------------------------------------------
import csv as _csv
import io as _io
from datetime import datetime

_HILLS_NAME = re.compile(r"^([DP])(\d{8})\d{2}id\.29$")
_HERN_NAME = re.compile(r"^(\d{2})-(\d{2})-(\d{2}|\d{4})\.csv$")
_HERN_PLACEHOLDER_LEGAL = "L Blk Un Sub S T R"


def _text(v):
    v = (v or "").strip()
    return v or None


def _money(v):
    v = (v or "").strip()
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _iso_from_mdy(date_s, time_s=None):
    """'08/21/2026' + '07:33' -> '2026-08-21T07:33'; '12/16/24 10:51:32 AM' ->
    '2024-12-16T10:51:32'. Returns None when the date does not parse."""
    date_s = (date_s or "").strip()
    if not date_s:
        return None
    for fmt in ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%y %I:%M:%S %p", "%m/%d/%Y", "%m/%d/%y"):
        try:
            dt = datetime.strptime(date_s, fmt)
            break
        except ValueError:
            continue
    else:
        return None
    if time_s:
        t = time_s.strip()
        if re.match(r"^\d{1,2}:\d{2}$", t):
            h, m = t.split(":")
            return dt.strftime("%Y-%m-%d") + f"T{int(h):02d}:{m}"
    if "%I" in fmt:
        return dt.strftime("%Y-%m-%dT%H:%M:%S")
    return dt.strftime("%Y-%m-%d")


def hillsborough_file_sets(names):
    """Pair D and P files by recording date. M files are not needed: the D
    file's description column is what categorize() reads."""
    d, p = {}, {}
    for n in names:
        m = _HILLS_NAME.match(n)
        if not m:
            continue
        (d if m.group(1) == "D" else p)[m.group(2)] = n
    return [(k, d[k], p[k]) for k in sorted(d) if k in p]


def parse_hillsborough(doc_bytes, party_bytes):
    """D file: Action|County|Instrument|DocType|DocDesc|Legal|BookType|Book|Page|
    Filler|PageCount|DateRecorded|TimeRecorded|Consideration (trailing pipe).
    P file: Action|County|Instrument|Seq|FRM/TO|Name (trailing pipe)."""
    recs = {}
    for line in doc_bytes.decode("latin-1").splitlines():
        f = line.split("|")
        if len(f) < 14 or not f[2].strip():
            continue
        inst = f[2].strip()
        recs[inst] = {
            "instrument_no": inst,
            "doc_type": _text(f[3]),
            "doc_desc": _text(f[4]),
            "category": categorize(f[4] or f[3]),
            "book": _text(f[7]),
            "page": _text(f[8]),
            "recorded_at": _iso_from_mdy(f[11], f[12]),
            "consideration": _money(f[13]),
            "legal_desc": _text(f[5]),
            "parties": [],
        }
    for line in party_bytes.decode("latin-1").splitlines():
        f = line.split("|")
        if len(f) < 6:
            continue
        inst = f[2].strip()
        rec = recs.get(inst)
        name = _text(f[5])
        if rec is None or not name:
            continue
        role = "grantor" if f[4].strip().upper() == "FRM" else "grantee"
        try:
            seq = int(f[3])
        except ValueError:
            seq = len(rec["parties"]) + 1
        rec["parties"].append((role, seq, name))
    return list(recs.values())


def hernando_file_date(name):
    m = _HERN_NAME.match(name)
    if not m:
        return None
    mm, dd, yy = m.groups()
    yyyy = yy if len(yy) == 4 else "20" + yy
    return f"{yyyy}-{mm}-{dd}"


def parse_hernando(data):
    """Columns: Grantor, Grantee, ClerkFileNumber, Book, PageNumber,
    DocumentTypeDescription, LegalDescription, DateRecorded. One row per
    grantor x grantee pair, so parties are de-duplicated per instrument."""
    recs = {}
    reader = _csv.reader(_io.StringIO(data.decode("latin-1")))
    for f in reader:
        if len(f) < 8 or not f[2].strip():
            continue
        inst = f[2].strip()
        rec = recs.get(inst)
        legal = _text(f[6])
        if legal == _HERN_PLACEHOLDER_LEGAL:
            legal = None
        if rec is None:
            rec = recs[inst] = {
                "instrument_no": inst,
                "doc_type": _text(f[5]),
                "doc_desc": re.sub(r"[0-9X]+$", "", (f[5] or "").strip()).strip() or None,
                "category": categorize(f[5]),
                "book": _text(f[3]),
                "page": _text(f[4]),
                "recorded_at": _iso_from_mdy(f[7]),
                "consideration": None,
                "legal_desc": None,
                "parties": [],
                "_seen": set(),
                "_legals": [],
            }
        # Rows repeat per grantor x grantee x legal line; keep each real legal once.
        if legal and legal not in rec["_legals"]:
            rec["_legals"].append(legal)
        for role, name in (("grantor", _text(f[0])), ("grantee", _text(f[1]))):
            if name and (role, name) not in rec["_seen"]:
                rec["_seen"].add((role, name))
                seq = 1 + sum(1 for r, _, _ in rec["parties"] if r == role)
                rec["parties"].append((role, seq, name))
    out = []
    for rec in recs.values():
        rec.pop("_seen")
        rec["legal_desc"] = "; ".join(rec.pop("_legals")) or None
        out.append(rec)
    return out
