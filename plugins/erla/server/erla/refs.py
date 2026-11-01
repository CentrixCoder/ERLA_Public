"""Citation graph (0.7.0, Phase 4.4): which documents cite which references, and which documents ARE them.

A reference is any of:

- standard: a standard, specification or handbook number - the classify.STD_PATTERNS (MMPDS, MIL-HDBK/STD,
  ASTM, AMS, SAE, ISO, EN, API, ASME, NASA, ESDU, CMH-17, AWS) plus MIL-PRF/DTL/SPEC, FED-STD, QQ, NAS/NASM/MS/AN
  hardware, SAE ARP/AIR/AS, DIN, VDI, BS, JIS, ANSI, IEEE, ECSS, NACE and NASA-HDBK numbers;
- regulation: 14 CFR / FAR parts, FAA advisory circulars, EASA CS, RTCA DO, DOT/FAA reports;
- book: a reference book or handbook by name, with its edition when printed ("Peterson, 3rd ed.", "Roark's
  Formulas for Stress and Strain, 7th Edition", "Shigley (10th ed.)", "Bruhn"). Names come from the site's
  synonyms.yaml `references:` section and `refs.references`, and any "<Name>, <n>th ed." citation is caught too;
- spec: a company, process, material or drawing specification ("Process Specification PS-10234",
  "per XYZ-SPEC-0042", "BMS 8-276", "BAC5300", "AIPS 02-01-001");
- site: the site's own numbering schemes, `refs.patterns: [{regex, kind}]` in library_config.yaml (a company's
  spec numbers are its own; this is how they are captured exactly).

Built from the text the index already holds (no library file is read): the indexer records each document's
citations and what it is (its title and file name) as it writes it, and `rebuild_if_stale` rebuilds
every document from stored chunks when REFS_VERSION changes. A document never cites itself.
"""

from __future__ import annotations

import re
import sqlite3
from functools import lru_cache
from pathlib import Path
from typing import Any

from . import store
from .classify import STD_PATTERNS, std_key

REFS_VERSION = "2"
MIN_COUNT = 1
EDITION_WINDOW = 120         # characters after a book name in which its edition is read

# ------------------------------------------------------------------ patterns
_EXTRA_STANDARDS = [
    r"\b(MIL-[A-Z]{1,4}-\d+)([A-Z])?\b",                       # MIL-PRF-23377, MIL-DTL-5541 (HDBK/STD too)
    r"\b(DOD-[A-Z]{1,4}-\d+)([A-Z])?\b",
    r"\b(FED-STD-\d+)([A-Z])?\b",
    r"\b(QQ-[A-Z]-\d{2,4}(?:/\d+)?)([A-Z])?\b",
    r"\b(NASM ?\d{3,6})(?!\d)()",                              # NASM, NAS and MS hardware: the base part
    r"\b(NAS ?\d{3,5})(?!\d)()",                               # number, dash and letter codes after it
    r"\b(MS ?\d{4,6})(?!\d)()",                                # (MS20470AD4); never 'MS 0.25' (4+ digits)
    r"\b(AN\d{1,4}-\d+)([A-Z])?\b",                             # AN3-5A
    r"\b((?:SAE )?(?:ARP|AIR|AS) ?\d{3,5})([A-Z])?\b",
    r"\b(DIN(?: EN)?(?: ISO)? \d{3,5}(?:-\d+)*)\b()",
    r"\b(VDI \d{4})\b()",
    r"\b(BS(?: EN)? \d{3,5}(?:-\d+)*)\b()",
    r"\b(JIS [A-Z] ?\d{4})\b()",
    r"\b(ANSI(?:/[A-Z]+)? [A-Z]?\d+(?:\.\d+)*M?)\b()",
    r"\b(IEEE (?:Std )?\d{3,5}(?:\.\d+)?)\b()",
    r"\b(ECSS-[A-Z]-[A-Z]{2}-\d{2}(?:-\d{2})?)([A-Z])?\b",
    r"\b(NACE [A-Z]{2}\d{4})\b()",
    r"\b(NASA-HDBK-\d+)([A-Z])?\b",
]
_REGULATIONS = [
    r"\b(14 CFR (?:Part )?\d+(?:\.\d+)?)()",
    r"\b(FAR (?:Part )?\d+(?:\.\d+)?)()",
    r"\b(AC \d{2,3}-\d+)([A-Z])?\b",
    r"\b(CS-\d{2,3}(?:\.\d+)?)()",
    r"\b((?:RTCA )?DO-\d{3})([A-Z])?\b",
    r"\b(DOT/FAA/[A-Z]{2,3}-\d{2}/\d+)()",
]
_SPECS = [
    # a number introduced as a specification, procedure or drawing
    r"\b(?:[Pp]rocess |[Mm]aterial |[Cc]ompany |[Ee]ngineering )?(?:[Ss]pecification|[Ss]pec|[Pp]rocedure|[Dd]rawing|"
    r"[Dd]wg)s?\.?\s*(?:[Nn]o\.?|[Nn]umber|#)?\s*:?\s*([A-Z]{0,8}[- ]?\d[\dA-Z]*(?:[-./]\d[\dA-Z]*)*)()",
    r"\b([A-Z]{2,6}-(?:SPEC|PS|MPS|DPS|EPS|QAS|MP|ES)-?\d{2,6}(?:-\d+)?[A-Z]?)()",   # XYZ-SPEC-0042
    r"\b((?:PS|MPS|DPS|EPS|QAS)-\d{3,6}[A-Z]?)\b()",
    r"\b(BMS ?\d{1,2}-\d{1,3}[A-Z]?)\b()",
    r"\b(BAC ?\d{4}[A-Z]?)\b()",
    r"\b((?:AIPS|AIMS|AIPI) ?\d{2}-\d{2}-\d{3})\b()",
    r"\b(ABS ?\d{4}[A-Z]?)\b()",
]
GENERIC_LOOKBACK = 110        # characters before an edition marker in which its book's name is found
_NAME = re.compile(r"\b([A-Z][A-Za-z'’\-]{2,}(?:\s(?:and|&)\s[A-Z][A-Za-z'’\-]{2,})?)(?:['’]s)?\b")
_NOT_A_NAME = frozenset("The This That These See Per Ref Reference References Table Figure Fig Section Chapter Vol "
                        "Volume Edition Appendix Page Revised Revision Issue Draft Second Third Fourth Fifth Sixth "
                        "Seventh Eighth Ninth Tenth First New Updated Proceedings Journal".split())
_ORDINAL_WORDS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7,
                  "eighth": 8, "ninth": 9, "tenth": 10, "eleventh": 11, "twelfth": 12}
_EDITION = re.compile(r"(?:(\d{1,2})(?:st|nd|rd|th)\s+(?:ed\b\.?|[Ee]dition\b)|[Ee]dition\s+(\d{1,2})\b|"
                      r"\b(" + "|".join(_ORDINAL_WORDS) + r")\s+[Ee]dition\b)", re.I)


def _compile(pats: list[str]) -> list[re.Pattern[str]]:
    return [re.compile(p) for p in pats]


_KINDS: list[tuple[str, list[re.Pattern[str]]]] = [
    ("standard", list(STD_PATTERNS) + _compile(_EXTRA_STANDARDS)),
    ("regulation", _compile(_REGULATIONS)),
    ("spec", _compile(_SPECS)),
]
_LITERALS = re.compile(r"[A-Z]{2,}")


def _triggers(pat: re.Pattern[str]) -> tuple[str, ...]:
    """Upper-case literals of a pattern: every match of these patterns contains at least one of them, so a text
    holding none is skipped with a fast substring test before the regular expression runs (0.23 -> MB/s x10)."""
    if pat.flags & re.I:
        return ()
    return tuple(dict.fromkeys(_LITERALS.findall(pat.pattern.replace("A-Z", "").replace("A-G", ""))))


_RUN: list[tuple[int, str, re.Pattern[str], tuple[str, ...]]] = [
    (rank, kind, pat, _triggers(pat)) for rank, (kind, pats) in enumerate(_KINDS) for pat in pats]
_SPEC_WORDS = ("spec", "procedure", "drawing", "dwg")
_EDITION_HINTS = ("ed.", "ed)", "ed,", " ed ", "dition")
_PREFIX = {"standard": "", "regulation": "", "spec": "SPEC:", "book": "REF:", "site": "SITE:"}


def ref_key(kind: str, ident: str) -> str:
    """Spelling-independent key: 'standard'/'regulation' keys are std_key (as documents.standard_id), so a citation
    joins the documents that are that standard; books are REF:<NAME>, specs SPEC:<ID>, site patterns SITE:<ID>."""
    if kind == "book":
        return "REF:" + re.sub(r"[^A-Z0-9&]+", " ", ident.upper().replace("’", "'").replace("'S ", " ")).strip()
    return _PREFIX.get(kind, "") + std_key(ident)


def _edition(text: str) -> str | None:
    m = _EDITION.search(text)
    if not m:
        return None
    if m.group(1) or m.group(2):
        return m.group(1) or m.group(2)
    return str(_ORDINAL_WORDS[m.group(3).lower()])


# ------------------------------------------------------------------ site configuration
@lru_cache(maxsize=8)
def _book_names(path: str, extra: tuple[str, ...]) -> tuple[tuple[re.Pattern[str], str, str], ...]:
    """(pattern, canonical name) for every known reference alias, longest first."""
    import yaml
    groups: list[list[str]] = []
    p = Path(path)
    if path and p.is_file():
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        groups = [[str(x) for x in g] for g in (raw.get("references") or [])]
    groups += [[e] for e in extra]
    out: list[tuple[re.Pattern[str], str, str]] = []
    for g in groups:
        canon = g[0]
        for alias in sorted(g, key=len, reverse=True):
            flags = 0 if " " not in alias else re.I       # a one-word name is matched with its capital
            out.append((re.compile(r"(?<![\w-])" + re.escape(alias) + r"(?:['’]s)?(?![\w-])", flags), canon,
                        alias.lower().split()[0]))
    out.sort(key=lambda x: -len(x[0].pattern))
    return tuple(out)


def site_rules(cfg: Any = None) -> dict[str, Any]:
    """Book names (synonyms.yaml `references:` + `refs.references`) and site patterns (`refs.patterns`)."""
    from .config import config_file
    rc = (getattr(cfg, "refs", None) or {}) if cfg is not None else {}
    try:
        syn = str(config_file("synonyms.yaml"))
    except Exception:
        syn = ""
    pats = []
    for p in rc.get("patterns") or []:
        rx = p.get("regex") if isinstance(p, dict) else str(p)
        if rx:
            pats.append((re.compile(rx), str((p.get("kind") if isinstance(p, dict) else None) or "site")))
    return {"books": _book_names(syn, tuple(str(x) for x in rc.get("references") or [])), "site": pats}


# ------------------------------------------------------------------ extraction
class _Spans:
    """Half-open character spans [a, b) of one text, with the three questions extract() asks answered by
    position marks instead of a scan of every span so far (that scan was O(n^2) per text; S11, 0.7.x).

    Exactly the answers of the scan over the added spans (tests/test_index_writer.py checks it against
    the scan on random spans): overlaps(a, b) = any(a < y and x < b), covers(p) = any(x <= p < y),
    starts_in(lo, hi) = any(lo <= x < hi)."""

    __slots__ = ("n", "occ", "starts", "spans", "points")

    def __init__(self, n: int) -> None:
        self.n = n
        self.occ: bytearray | None = None        # occ[i] = 1: position i lies inside a non-empty span
        self.starts: bytearray | None = None     # starts[i] = 1: a span starts at i
        self.spans: list[tuple[int, int]] = []
        self.points: list[int] = []              # empty spans (a == b): no position marks

    def add(self, a: int, b: int) -> None:
        if self.occ is None or self.starts is None:
            self.occ, self.starts = bytearray(self.n + 1), bytearray(self.n + 1)
        self.spans.append((a, b))
        if not 0 <= a <= b <= self.n:            # outside the text: the marks cannot hold it
            self.n = -1                          # answer every later question by the scan
        elif b > a:
            self.occ[a:b] = b"\x01" * (b - a)
        else:
            self.points.append(a)
        if self.n >= 0:
            self.starts[a] = 1

    def overlaps(self, a: int, b: int) -> bool:
        if not self.spans:
            return False
        if self.occ is None or self.n < 0 or not 0 <= a < b <= self.n:
            return any(a < y and x < b for x, y in self.spans)
        return self.occ.find(1, a, b) >= 0 or any(a < x < b for x in self.points)

    def covers(self, p: int) -> bool:
        if not self.spans:
            return False
        if self.occ is None or self.n < 0 or not 0 <= p < self.n:
            return any(x <= p < y for x, y in self.spans)
        return self.occ[p] == 1

    def starts_in(self, lo: int, hi: int) -> bool:
        if not self.spans:
            return False
        if self.starts is None or self.n < 0:
            return any(lo <= x < hi for x, _y in self.spans)
        lo, hi = max(lo, 0), min(hi, self.n + 1)
        return lo < hi and self.starts.find(1, lo, hi) >= 0


def extract(texts, rules: dict[str, Any] | None = None, max_chars: int = 2_000_000) -> dict[str, dict[str, Any]]:
    """Every reference printed in the texts: {key: {kind, id, n, editions}} (first spelling kept as id)."""
    rules = rules or site_rules()
    out: dict[str, dict[str, Any]] = {}

    def add(kind: str, ident: str, edition: str | None = None) -> None:
        ident = ident.strip().rstrip(".-/")
        if not ident or not any(c.isalnum() for c in ident):
            return
        key = ref_key(kind, ident)
        if key in ("", "REF:", "SPEC:", "SITE:"):
            return
        e = out.setdefault(key, {"kind": kind, "id": ident, "n": 0, "editions": []})
        e["n"] += 1
        if edition and edition not in e["editions"]:
            e["editions"].append(edition)

    seen = 0
    for t in texts:
        if not t:
            continue
        t = t[: max(0, max_chars - seen)]
        seen += len(t)
        cands: list[tuple[int, int, int, str, str, str | None]] = []
        tl = t.lower()
        for rank, kind, pat, trig in _RUN:
            if trig and not any(x in t for x in trig):
                continue
            if not trig and kind == "spec" and not any(w in tl for w in _SPEC_WORDS):
                continue
            for m in pat.finditer(t):
                if kind == "spec" and not any(ch.isdigit() for ch in m.group(1)):
                    continue
                rev = m.group(2) if (m.lastindex or 0) >= 2 and kind != "spec" else None
                cands.append((m.start(1), m.end(1), rank, kind, m.group(1), rev or None))
        taken = _Spans(len(t))
        for a, b, _r, kind, ident, rev in sorted(cands, key=lambda c: (-(c[1] - c[0]), c[2], c[0])):
            if taken.overlaps(a, b):
                continue                          # overlapping spans: the longest (DIN EN ISO 4762), then kind
            taken.add(a, b)
            add(kind, ident, rev)
        for pat, kind in rules.get("site", []):
            for m in pat.finditer(t):
                add(kind if kind in _PREFIX else "site", m.group(1) if pat.groups else m.group(0))
        named = _Spans(len(t))
        for pat, canon, low in rules.get("books", ()):
            if low not in tl:
                continue
            for m in pat.finditer(t):
                if named.covers(m.start()):
                    continue
                named.add(m.start(), m.end())
                add("book", canon, _edition(t[m.end(): m.end() + EDITION_WINDOW]))
        if any(h in tl for h in _EDITION_HINTS):
            for m in _EDITION.finditer(t):            # edition markers are rare: look back from each for its name
                lo = max(0, m.start() - GENERIC_LOOKBACK)
                seg = t[lo:m.start()]
                cut = max(seg.rfind(". "), seg.rfind(";"), seg.rfind(":"), seg.rfind("\n"))
                base = lo + (cut + 1 if cut >= 0 else 0)
                nm = _NAME.search(t, base, m.start())
                if nm is None:
                    continue
                name = nm.group(1)
                if (name.split()[0] in _NOT_A_NAME
                        or named.starts_in(base, m.end())                       # a known name in the citation
                        or taken.covers(nm.start(1))):
                    continue
                add("book", name, m.group(1) or m.group(2) or str(_ORDINAL_WORDS[m.group(3).lower()]))
        if seen >= max_chars:
            break
    return out


def identify(title: str | None, path: str | None, head: str = "", rules: dict[str, Any] | None = None) -> dict[str, dict]:
    """What a document IS: the references named in its title and file name. Its first page is not used: a first
    page cites as often as it names ("Kt from Peterson, 3rd ed." on page 1 of a report). Standards are also known
    by documents.standard_id (classify.detect_standard)."""
    name = f"{title or ''}\n{Path(path or '').stem.replace('_', ' ')}"
    return extract([name], rules)


# ------------------------------------------------------------------ index
def record_document(con: sqlite3.Connection, doc_id: int, texts: list[str], own_standard: str | None,
                    rules: dict[str, Any] | None = None, found: dict[str, dict[str, Any]] | None = None) -> int:
    """Write one document's citations and identity (writer connections). Returns how many references it cites.
    `found` is extract(texts, rules) when the caller computed it already (the indexer's workers do, S11)."""
    if not store.has_table(con, "refs"):
        return 0
    rules = rules or site_rules()
    d = con.execute("SELECT title, path FROM documents WHERE doc_id=?", (doc_id,)).fetchone()
    is_ = identify(d[0] if d else None, d[1] if d else None, texts[0] if texts else "", rules)
    own = {k for k in is_} | ({std_key(own_standard)} if own_standard else set())
    if found is None:
        found = extract(texts, rules)
    rows = [(doc_id, k, v["id"], v["n"], v["kind"], ",".join(v["editions"]) or None)
            for k, v in found.items() if k not in own and v["n"] >= MIN_COUNT]
    con.execute("DELETE FROM refs WHERE doc_id=?", (doc_id,))
    con.executemany("INSERT OR REPLACE INTO refs(doc_id, std_key, standard_id, n, kind, edition) VALUES (?,?,?,?,?,?)",
                    rows)
    if store.has_table(con, "ref_docs"):
        con.execute("DELETE FROM ref_docs WHERE doc_id=?", (doc_id,))
        con.executemany("INSERT OR REPLACE INTO ref_docs(doc_id, std_key, label, kind, edition) VALUES (?,?,?,?,?)",
                        [(doc_id, k, v["id"], v["kind"], ",".join(v["editions"]) or None) for k, v in is_.items()])
    return len(rows)


def rebuild_if_stale(con: sqlite3.Connection, log=print, cfg: Any = None) -> int:
    """Rebuild every document's rows from stored chunks when REFS_VERSION changed. Returns documents done."""
    if not store.has_table(con, "refs") or store.get_meta(con, "refs_version") == REFS_VERSION:
        return 0
    rules = site_rules(cfg)
    con.execute("DELETE FROM refs")
    if store.has_table(con, "ref_docs"):
        con.execute("DELETE FROM ref_docs")
    docs = con.execute("SELECT doc_id, standard_id FROM documents WHERE doc_id NOT IN "
                       "(SELECT doc_id FROM duplicates)").fetchall()
    for i, d in enumerate(docs, 1):
        texts = [r[0] or "" for r in con.execute("SELECT text FROM chunks WHERE doc_id=? ORDER BY seq", (d[0],))]
        record_document(con, d[0], texts, d[1], rules)
        if i % 1000 == 0:
            log(f"  refs: {i}/{len(docs)} documents")
            con.commit()
    store.set_meta(con, "refs_version", REFS_VERSION)
    con.commit()
    return len(docs)


# ------------------------------------------------------------------ readers
def _reference_docs(con: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
    """{key: [library documents that are that reference]}: standard numbers of documents and ref_docs rows."""
    out: dict[str, list[dict[str, Any]]] = {}
    dups = "doc_id NOT IN (SELECT doc_id FROM duplicates)"
    seen: set[tuple[str, int]] = set()
    for r in con.execute(f"SELECT doc_id, standard_id, revision, year, title FROM documents WHERE standard_id IS NOT NULL "
                         f"AND {dups}"):
        k = std_key(r[1])
        seen.add((k, r[0]))
        out.setdefault(k, []).append({"doc_id": r[0], "id": r[1], "kind": "standard", "edition": r[2], "year": r[3],
                                      "title": r[4]})
    if store.has_table(con, "ref_docs"):
        for r in con.execute(f"SELECT x.doc_id, x.std_key, x.label, x.kind, x.edition, d.year, d.title FROM ref_docs x "
                             f"JOIN documents d ON d.doc_id=x.doc_id WHERE x.{dups}"):
            if (r[1], r[0]) in seen:
                continue
            seen.add((r[1], r[0]))
            out.setdefault(r[1], []).append({"doc_id": r[0], "id": r[2], "kind": r[3], "edition": r[4], "year": r[5],
                                             "title": r[6]})
    return out


def cites_for(con: sqlite3.Connection, doc_ids: list[int], per_doc: int = 5) -> dict[int, list[dict[str, Any]]]:
    """For each document, the references it cites most that the library holds: [{id, kind, n, doc_ids}]."""
    if not doc_ids or not store.has_table(con, "refs"):
        return {}
    have = _reference_docs(con)
    if not have:
        return {}
    out: dict[int, list[dict[str, Any]]] = {}
    kind_sql = "kind" if store.has_column(con, "refs", "kind") else "'standard'"
    for i in range(0, len(doc_ids), 500):
        part = doc_ids[i:i + 500]
        for r in con.execute(f"SELECT doc_id, std_key, standard_id, n, {kind_sql} FROM refs WHERE doc_id IN "
                             f"({','.join('?' * len(part))}) ORDER BY doc_id, n DESC", part):
            docs = [d["doc_id"] for d in have.get(r[1], []) if d["doc_id"] != r[0]]
            if not docs:
                continue
            lst = out.setdefault(r[0], [])
            if len(lst) < per_doc:
                lst.append({"standard_id": r[2], "kind": r[4], "n": r[3], "doc_ids": docs[:5]})
    return out


def query_keys(text: str, rules: dict[str, Any] | None = None) -> list[str]:
    """Keys a user's reference string names: every reference extracted from it, else the string as a book name,
    a spec number and a standard number."""
    found = extract([text], rules)
    if found:
        return list(found)
    t = text.strip()
    return [k for k in dict.fromkeys([std_key(t), ref_key("book", t), ref_key("spec", t), ref_key("site", t)]) if k]


def library_refs(con: sqlite3.Connection, standard: str, k: int = 50, cfg: Any = None) -> dict[str, Any]:
    """The library documents that are a reference (every edition) and the documents that cite it. `standard` is
    a standard or spec number, a regulation, or a book name ('Peterson', 'Roark 7th ed.')."""
    keys = query_keys(standard, site_rules(cfg))
    if not keys:
        return {"standard": standard, "error": "no reference given"}
    have = _reference_docs(con)
    editions = [d for key in keys for d in have.get(key, [])]
    citing: list[dict[str, Any]] = []
    if store.has_table(con, "refs"):
        ed_sql = "r.edition" if store.has_column(con, "refs", "edition") else "NULL"
        marks = ",".join("?" * len(keys))
        for r in con.execute(f"SELECT r.doc_id, r.n, d.title, d.authority_tier, d.year, d.classification_flag, {ed_sql}, "
                             f"r.standard_id FROM refs r JOIN documents d ON d.doc_id=r.doc_id WHERE r.std_key IN ({marks}) "
                             "AND r.doc_id NOT IN (SELECT doc_id FROM duplicates) "
                             "ORDER BY r.n DESC LIMIT ?", (*keys, k)):
            citing.append({"doc_id": r[0], "mentions": r[1], "title": r[2], "tier": r[3], "year": r[4], "flag": r[5],
                           "cited_as": r[7], "editions_cited": r[6].split(",") if r[6] else []})
    built = store.get_meta(con, "refs_version") == REFS_VERSION
    return {"standard": standard, "keys": keys, "editions": editions, "cited_by": citing,
            "refs_built": built,
            "note": "An index of citations, not a source: open each document with library_show before citing it. "
                    "Company numbering schemes the patterns do not know: add them under refs.patterns in the site config."
                    + ("" if built else " The citation index may be incomplete until the next index_start (erla refresh).")}
