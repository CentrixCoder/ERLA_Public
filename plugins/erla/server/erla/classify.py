"""Metadata inference, classification flags, authority tiers, web-query safety."""

from __future__ import annotations

import base64
import re
import sqlite3
import unicodedata
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path
from urllib.parse import unquote_plus, urlparse

import yaml

from .config import config_file

# ---------------------------------------------------------------- markings
MARKING_PATTERNS = [
    ("ITAR", re.compile(r"\bITAR\b|INTERNATIONAL TRAFFIC IN ARMS", re.I)),
    ("EAR", re.compile(r"\bEAR99\b|\bECCN\b|EXPORT ADMINISTRATION REGULATIONS", re.I)),
    ("EXPORT_CONTROLLED", re.compile(r"EXPORT[- ]CONTROLLED|EXPORT CONTROL(?:LED)? (?:INFORMATION|DATA)", re.I)),
    ("CUI", re.compile(r"\bCUI\b|CONTROLLED UNCLASSIFIED INFORMATION", re.I)),
    ("PROPRIETARY", re.compile(r"\bPROPRIETARY\b|COMPANY CONFIDENTIAL|\bCONFIDENTIAL\b", re.I)),
    ("DISTRIBUTION_LIMITED", re.compile(r"DISTRIBUTION STATEMENT [B-F]\b", re.I)),
]
# Phrases that mention the words without marking the document (reduce false flags)
BENIGN = re.compile(r"(?:non[- ]?proprietary|not (?:proprietary|export controlled)|"
                    r"distribution statement a|approved for public release)", re.I)


def detect_markings(text_head: str, text_tail: str = "") -> str | None:
    """Return the most restrictive marking found in the first/last pages, else None.
    G2.5: an explicit marking anywhere in head/tail flags the document."""
    sample = f"{text_head}\n{text_tail}"
    if not sample.strip():
        return None
    found = []
    for name, pat in MARKING_PATTERNS:
        m = pat.search(sample)
        if not m:
            continue
        window = sample[max(0, m.start() - 40): m.end() + 40]
        if name in ("PROPRIETARY", "DISTRIBUTION_LIMITED") and BENIGN.search(window):
            continue
        found.append(name)
    return most_restrictive(found)


# Most restrictive first. Site-defined flags (path rules) rank after these and before no flag.
FLAG_ORDER = ("ITAR", "EAR", "EXPORT_CONTROLLED", "CUI", "DISTRIBUTION_LIMITED", "PROPRIETARY")


def flag_rank(flag: str) -> tuple[int, str]:
    return (FLAG_ORDER.index(flag), "") if flag in FLAG_ORDER else (len(FLAG_ORDER), flag)


def most_restrictive(flags: Iterable[str | None]) -> str | None:
    present = [f for f in flags if f]
    return min(present, key=flag_rank) if present else None


def copy_group_flags(con: sqlite3.Connection) -> dict[int, str]:
    """G2.5: copies are one document for classification.

    Documents joined through table ``duplicates`` (exact or near, transitively) form a copy group.
    Returns, for every member of a group that holds a flagged document, the group's most restrictive
    flag; documents outside such a group are absent. Which copy became the canonical record depends on
    scan and extraction order, so the flag must not: an unflagged copy indexed before the flagged
    original carried the original's text unlabelled until 0.2.18. Read-only; the indexer writes the
    result (reconcile_copy_flags) and readers apply it before that refresh has run."""
    from .store import has_table
    if not has_table(con, "duplicates"):
        return {}
    parent: dict[int, int] = {}

    def find(x: int) -> int:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for doc_id, canonical in con.execute("SELECT doc_id, canonical_doc_id FROM duplicates"):
        a, b = find(doc_id), find(canonical)
        if a != b:
            parent[a] = b
    if not parent:
        return {}
    group_flag: dict[int, str] = {}
    for doc_id, flag in con.execute(
            "SELECT doc_id, classification_flag FROM documents WHERE classification_flag IS NOT NULL"):
        if doc_id in parent:
            root = find(doc_id)
            group_flag[root] = most_restrictive([group_flag.get(root), flag]) or flag
    return {d: group_flag[find(d)] for d in list(parent) if find(d) in group_flag}


def effective_flag(own: str | None, doc_id: int, groups: dict[int, str]) -> str | None:
    """A document's flag for labels and filters: its own, or its copy group's if more restrictive."""
    return most_restrictive([own, groups.get(doc_id)])


# ------------------------------------------------------------- standards
STD_PATTERNS = [
    re.compile(r"\b(MMPDS)[- ]?(\d{1,2}[A-Z]?)\b"),
    re.compile(r"\b(MIL-HDBK-\d+)([A-Z])?\b"),
    re.compile(r"\b(MIL-STD-\d+)([A-Z])?\b"),
    re.compile(r"\b(MIL-[A-Z]-\d+)([A-Z])?\b"),
    re.compile(r"\b(ASTM [A-G]\d{1,4})(?:[-/](\d{2,4}[a-z]?))?\b"),
    re.compile(r"\b(AMS[- ]?\d{4})([A-Z])?\b"),
    re.compile(r"\b(SAE [A-Z]{1,3}\d{2,5})([A-Z])?\b"),
    re.compile(r"\b(ISO \d{3,5}(?:-\d+)?)(?::(\d{4}))?\b"),
    re.compile(r"\b(EN \d{3,5}(?:-\d+)*)(?::(\d{4}))?\b"),
    re.compile(r"\b(API (?:STD |RP )?\d{2,4})\b()"),
    re.compile(r"\b(ASME (?:Y14\.\d+M?|B\d+(?:\.\d+)?M?|BPVC[^\n,;]{0,30}))\b()"),
    re.compile(r"\b(NASA-STD-\d+)([A-Z])?\b"),
    re.compile(r"\b(NASA[- ](?:TM|TN|TR|CR|SP)[- ]?[\dX-]+)\b()"),
    re.compile(r"\b(ESDU \d{5})\b()"),
    re.compile(r"\b(CMH-17)[- ]?(\d[A-Z]?)?\b"),
    re.compile(r"\b(AWS D\d+\.\d+)(?:[/:](\d{4}))?\b"),
]


def detect_standard(text: str) -> tuple[str | None, str | None]:
    for pat in STD_PATTERNS:
        m = pat.search(text)
        if m:
            rev = m.group(2) if m.lastindex and m.lastindex >= 2 else None
            return m.group(1).strip(), (rev or None)
    return None, None


YEAR_RE = re.compile(r"\b(19[5-9]\d|20[0-3]\d)\b")


def guess_year(*texts: str) -> int | None:
    for t in texts:
        if not t:
            continue
        years = [int(y) for y in YEAR_RE.findall(t)]
        if years:
            return max(years)
    return None


# ------------------------------------------------------------- doc type
DOC_TYPE_RULES = [
    ("solver_output", re.compile(r"\.(f06|op2)$", re.I)),
    ("solver_input", re.compile(r"\.(bdf|inp|dat)$", re.I)),
    ("standard", re.compile(r"\b(MIL-STD|MIL-HDBK|MMPDS|ASTM|AMS ?\d|SAE |ISO |ASME |NASA-STD|CMH-17|specification)\b", re.I)),
    ("handbook", re.compile(r"\b(handbook|manual|design guide|ESDU|data sheets?)\b", re.I)),
    ("textbook", re.compile(r"\b(textbook|edition|chapter \d|mcgraw|wiley|elsevier|springer)\b", re.I)),
    ("test_report", re.compile(r"\b(test report|test results|coupon test|qualification test)\b", re.I)),
    ("analysis_report", re.compile(r"\b(stress analysis|stress report|margin of safety|analysis report|stress check)\b", re.I)),
    ("paper", re.compile(r"\b(abstract|journal|proceedings|conference|doi:)\b", re.I)),
    ("datasheet", re.compile(r"\b(datasheet|data sheet|product data)\b", re.I)),
    ("presentation", re.compile(r"\.(ppt|pptx)$", re.I)),
    ("calculation", re.compile(r"\.(xls|xlsx|xlsm|csv)$", re.I)),
]

RAISING_TYPES = frozenset({"standard", "handbook", "textbook", "paper"})   # default tier T1/T2
# a keyword right after one of these words refers to another document ("consistent with the handbook",
# "tested per ASTM E8"); it must not raise this document's type and tier (integrity attacks, 0.3.6)
_REFERRING = re.compile(r"\b(?:the|per|with|see|from|to|by|under|using|than|in accordance with|according to)\s*$", re.I)


def _names_itself(pat: re.Pattern, probe: str) -> bool:
    """A raising-type keyword that is not a reference to some other document."""
    return any(not _REFERRING.search(probe[max(0, m.start() - 30):m.start()]) for m in pat.finditer(probe))

DISCIPLINE_KEYWORDS = {
    "fatigue": r"fatigue|S-N|crack growth|fracture|da/dN|endurance",
    "materials": r"material propert|allowable|tensile strength|yield strength|alloy|temper|heat treat",
    "joints": r"fastener|bolt|rivet|joint|lug|bearing stress|preload",
    "composites": r"composite|laminate|ply|CLT|fiber|matrix",
    "stability": r"buckling|crippling|stability|euler|postbuckl",
    "fea": r"finite element|FEA|FEM|nastran|abaqus|ansys|mesh",
    "dynamics": r"vibration|modal|natural frequency|random response|shock",
    "thermal": r"thermal|heat transfer|temperature distribution",
    "welding": r"weld",
    "structures": r"stress analysis|beam|plate|shell|frame|stiffener",
}


def authority_config() -> dict:
    return _load_yaml(str(config_file("source_authority.yaml")))


@lru_cache(maxsize=8)
def _load_yaml(path: str) -> dict:
    p = Path(path)
    if p.is_file():
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return {}


def classify_document(rel_path: str, ext: str, title: str, head_text: str) -> dict:
    """Infer doc_type, discipline, tier from path + title + first text."""
    auth = authority_config()
    parts = Path(rel_path).parts
    top = parts[0] if len(parts) > 1 else ""
    hint = (auth.get("folder_hints") or {}).get(top, {}) or {}
    probe = f"{rel_path}\n{title}\n{head_text[:4000]}"

    doc_type = None
    for name, pat in DOC_TYPE_RULES:
        if name in ("solver_output", "solver_input", "presentation", "calculation"):
            found = pat.search(rel_path) is not None
        elif name in RAISING_TYPES:
            found = _names_itself(pat, probe)
        else:
            found = pat.search(probe) is not None
        if found:
            doc_type = name
            break

    discipline = hint.get("discipline")
    if not discipline:
        best, score = None, 0
        for name, pat in DISCIPLINE_KEYWORDS.items():
            n = len(re.findall(pat, probe, re.I))
            if n > score:
                best, score = name, n
        discipline = best

    tier = resolve_tier(auth, rel_path, title, probe, doc_type, hint.get("tier"))
    return {"doc_type": doc_type or "other", "discipline": discipline, "authority_tier": tier, "top_folder": top}


TIER_ORDER = ["T1", "T2", "T3", "T4", "T5", "T6"]
DEFAULT_TIER_BY_TYPE = {"standard": "T1", "handbook": "T2", "textbook": "T2", "paper": "T2",
                        "test_report": "T3", "analysis_report": "T3", "datasheet": "T4",
                        "solver_output": "T5", "solver_input": "T5", "presentation": "T5",
                        "calculation": "T5"}


@lru_cache(maxsize=256)
def _rx(pattern: str) -> re.Pattern:
    return re.compile(pattern, re.I)


def _any(patterns, text: str) -> bool:
    return any(_rx(p).search(text) for p in (patterns or []))


def _worse(a: str, b: str) -> str:
    """Lower authority (higher tier number) of two tiers."""
    return a if TIER_ORDER.index(a) >= TIER_ORDER.index(b) else b


def resolve_tier(auth: dict, rel_path: str, title: str, probe: str,
                 doc_type: str | None, folder_tier: str | None) -> str:
    """K3 / T-D tier precedence (highest first):
      1. doc-type / path cap (T-C): solver files + calc tools -> T5 (T3 if under a signed-report path);
         analysis/test documents -> no better than T3.
      2. standard-ID pattern on path+title, doc_type standard/handbook (T-A) -> T1.
      3. authoritative-reference pattern on path+title (T-B) -> T2, never lowering a better tier.
      4. folder default (folder_hints), 5. publisher pattern, 6. default by doc_type.
    Patterns live in config/source_authority.yaml (tier_patterns, tier_caps)."""
    pats = auth.get("tier_patterns") or {}
    caps = auth.get("tier_caps") or {}
    name = f"{rel_path}\n{title}"

    # 4-6: base tier
    tier = folder_tier
    if not tier:
        pub = auth.get("publisher_patterns") or {}
        for t in ("T1", "T2"):
            if any(p.lower() in probe.lower() for p in pub.get(t, [])):
                tier = t
                break
    if not tier:
        tier = DEFAULT_TIER_BY_TYPE.get(doc_type or "", "T5")

    # 3: T2 references lift a worse tier
    if _any(pats.get("T2"), name) and TIER_ORDER.index(tier) > 1:
        tier = "T2"
    # 2: governing standards/specs
    if doc_type in set(caps.get("t1_doc_types") or ["standard", "handbook"]) and _any(pats.get("T1"), name):
        tier = "T1"

    # 1: caps (always win)
    # cap patterns match the path only: template-derived titles ("Engineering Report ...") on
    # company methods manuals must not demote them (dry run 2026-10-08)
    cap = None
    if doc_type in set(caps.get("t5_doc_types") or []) or _any(caps.get("t5_path_patterns"), rel_path):
        cap = "T3" if _any(caps.get("signed_report_patterns"), rel_path) else "T5"
    elif doc_type in set(caps.get("t3_doc_types") or []) or _any(caps.get("t3_path_patterns"), rel_path):
        cap = "T3"
    if cap:
        tier = _worse(tier, cap)
    return tier


def tier_for_url(url: str) -> str:
    """Authority tier for a web URL from source_authority.yaml (default T5)."""
    host = (urlparse(url).hostname or "").lower()
    tiers = authority_config().get("tiers") or {}
    for tier in ("T1", "T2", "T4", "T6"):
        for dom in tiers.get(tier, []) or []:
            if host == dom or host.endswith("." + dom):
                return tier
    if host.endswith(".gov") or host.endswith(".mil"):
        return "T2"
    if host.endswith(".edu"):
        return "T2"
    return "T5"


# --------------------------------------------------------- web-query safety
DASHES = "‐‑‒–—―−-"     # hyphen last: literal inside a character class
PN_RE = re.compile(rf"\b(?:P/?N|PART\s*(?:NO\b\.?|NUMBER|#))[:#.\s]*[\w{DASHES}]{{3,}}"
                   rf"|\b\d{{3,}}[{DASHES}]\d{{3,}}(?:[{DASHES}]\d+)?\b", re.I)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
# an address spelled out to get past a filter: jane.doe(at)corp.com, jane.doe at corp-example dot com
# (integrity attacks, 0.3.6); a plain "data at nasa.gov" is not an address and still passes
EMAIL_SPELLED_RE = re.compile(
    r"\b[\w.%+-]+\s*(?:\(at\)|\[at\])\s*[\w-]+(?:\s*(?:\.|\(dot\)|\[dot\])\s*[\w-]+)+"
    r"|\b[\w.%+-]+\s+at\s+[\w-]+(?:\s*(?:\.|\s+dot\s+|\(dot\)|\[dot\])\s*[\w-]+)*?"
    r"(?:\s+dot\s+|\(dot\)|\[dot\])\s*(?:com|org|net|gov|mil|edu|io|co|us|uk|de|fr|ca|au|info|biz)\b", re.I)

SHORT_TERM = 6      # folded terms shorter than this match only as whole words
COLLAPSED_MIN = 8   # long terms also match with every separator removed ("z e p h y r i n e", "a/b/c")

# Cyrillic and Greek letters that look like Latin ones ("zеphyrine" with a Cyrillic e)
_CONFUSABLES = str.maketrans({
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x", "і": "i", "ј": "j", "ѕ": "s",
    "ԁ": "d", "ԛ": "q", "ԝ": "w", "ү": "y", "А": "a", "В": "b", "Е": "e", "К": "k", "М": "m", "Н": "h",
    "О": "o", "Р": "p", "С": "c", "Т": "t", "Х": "x", "І": "i", "Ј": "j", "Ѕ": "s", "α": "a", "ε": "e",
    "ι": "i", "κ": "k", "ν": "v", "ο": "o", "ρ": "p", "τ": "t", "υ": "u", "χ": "x", "Α": "a", "Β": "b",
    "Ε": "e", "Ζ": "z", "Η": "h", "Ι": "i", "Κ": "k", "Μ": "m", "Ν": "n", "Ο": "o", "Ρ": "p", "Τ": "t",
    "Υ": "y", "Χ": "x"})
_B64_RE = re.compile(r"[A-Za-z0-9+/_-]{12,}={0,2}")
_TOKEN_RE = re.compile(r"[a-z0-9]+(?:[-.][a-z0-9]+)*")
# words of a classification marking itself; they identify no program, part or person
MARKING_WORDS = frozenset({"itar", "export", "controlled", "control", "information", "proprietary", "confidential",
                           "company", "distribution", "statement", "limited", "unclassified", "restricted",
                           "sensitive", "internal", "eccn", "ear99", "distribute", "outside"})


def _normalize(text: str) -> str:
    """NFKC, invisible format characters (zero-width etc.) removed, look-alike letters mapped to Latin."""
    t = unicodedata.normalize("NFKC", text)
    return "".join(ch for ch in t if unicodedata.category(ch) != "Cf").translate(_CONFUSABLES)


def _fold(text: str) -> str:
    return re.sub(r"[\W_]+", " ", _normalize(text).lower()).strip()


def expand_outbound(text: str) -> str:
    """The text plus every decoded form a filter could otherwise miss: repeated URL decoding
    (``%255F``) and base64 tokens in a query or URL (integrity attacks, 0.3.6)."""
    out, cur = [text], text
    for _ in range(4):
        nxt = unquote_plus(cur)
        if nxt == cur:
            break
        out.append(nxt)
        cur = nxt
    for tok in _B64_RE.findall(cur):
        for decode in (base64.urlsafe_b64decode, base64.b64decode):
            try:
                s = decode(tok + "=" * (-len(tok) % 4)).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                continue
            if s and sum(ch.isprintable() for ch in s) / len(s) > 0.9:
                out.append(s)
                break
    return "\n".join(dict.fromkeys(out))


def _term_in(t: str, q: str, qc: str | None = None) -> bool:
    """A flagged term in a folded query. Long terms match anywhere, also with all separators removed
    (``qc``, the collapsed query); short ones (an acronym file name such as "ZQXJ") only as whole
    words, so they neither leak nor block every query that happens to contain the letters (leak
    probes on a real site, 0.2.16)."""
    if len(t) >= SHORT_TERM:
        tc = t.replace(" ", "")
        return t in q or (qc is not None and len(tc) >= COLLAPSED_MIN and tc in qc)
    if len(t) < 3 or not re.search(r"[a-z]", t):
        return False    # "1", "a", "2019": no identifying content; numbers are left to PN_RE
    return re.search(rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])", q) is not None


def _candidates(q: str) -> list[str]:
    """Query words that could name something: words of 5+ letters, and codes with letters and digits
    ("kestrel-9", "lc-447"); a material designation such as "7050-t7451" has one letter and is not one."""
    found: set[str] = set()
    words = _TOKEN_RE.findall(q)
    # a code written apart ("lc 447") is also tried joined, as an index stores it ("lc-447")
    joined = [f"{a}-{b}" for a, b in zip(words, words[1:]) if a.isalpha() and len(a) <= 4 and b.isdigit()]
    for t in words + joined:
        for p in (t, *re.split(r"[-.]", t)):
            letters, digits = sum(c.isalpha() for c in p), sum(c.isdigit() for c in p)
            if (letters >= 5 and not digits) or (letters >= 2 and digits and len(p) >= 4):
                found.add(p)
    return sorted(found - MARKING_WORDS)[:40]


def _restricted_name(con: sqlite3.Connection, q: str, docs: list, flagged: set[int],
                     allowed: set[str]) -> str | None:
    """G2.2 for single words and for names inside flagged documents (integrity attacks, 0.3.6): a query
    word that names something - a code with letters and digits, a word the flagged text always writes
    capitalised, or a word only in a flagged file name - and that occurs in a flagged document but in no
    unflagged one. Returns the flagged file it came from, or None. Generic words ("forging") pass: the
    flagged text writes them in lower case or unflagged documents use them too."""
    for tok in _candidates(q):
        if tok in allowed:
            continue
        rx = re.compile(rf"(?<![a-z0-9]){re.escape(tok)}", re.I)
        if any(rx.search(_fold(f"{Path(r['path']).stem} {r['title'] or ''}"))
               for r in docs if r["doc_id"] not in flagged):
            continue                                    # an unflagged document is named with it
        match = f'"{tok}" OR "{tok}"*'
        rows = con.execute("SELECT c.doc_id, c.text FROM chunks_fts JOIN chunks c ON c.chunk_id = chunks_fts.rowid "
                           "WHERE chunks_fts MATCH ? LIMIT 400", (match,)).fetchall()
        unflagged = con.execute(
            "SELECT c.doc_id, c.text FROM chunks_fts JOIN chunks c ON c.chunk_id = chunks_fts.rowid "
            "JOIN documents d ON d.doc_id = c.doc_id WHERE chunks_fts MATCH ? AND d.classification_flag IS NULL "
            "LIMIT 50", (match,)).fetchall()
        if any(r["doc_id"] not in flagged and rx.search(r["text"] or "") for r in unflagged):
            continue                                    # used in unflagged text: generic
        occurrences = [m.group(0) for r in rows if r["doc_id"] in flagged for m in rx.finditer(r["text"] or "")]
        named = [r for r in docs if r["doc_id"] in flagged
                 and rx.search(_fold(f"{Path(r['path']).stem} {r['title'] or ''}"))]
        source = {r["doc_id"] for r in rows if r["doc_id"] in flagged} | {r["doc_id"] for r in named}
        if not source:
            continue
        name_like = (any(c.isdigit() for c in tok) or (named and not occurrences)
                     or bool(occurrences) and all(o[0].isupper() for o in occurrences))
        if name_like:
            doc = next(r for r in docs if r["doc_id"] in source)
            return Path(doc["path"]).name
    return None


def web_allow_terms(cfg: object | None) -> list[str]:
    """The site's ``web.allow_terms``: generic technical terms the user has decided may be sent
    even though a flagged document carries exactly that name (``erla web-allow``)."""
    web = getattr(cfg, "web", None) or {}
    terms = web.get("allow_terms") if isinstance(web, dict) else None
    return [str(t) for t in terms if isinstance(t, str) and t.strip()] if isinstance(terms, list) else []


def web_query_check(query: str, con: sqlite3.Connection | None = None,
                    allow: list[str] | None = None) -> list[str]:
    """Guardrail G2.2/G2.3/G7.1: return reasons a query must NOT be sent to the web.
    Empty list = safe. Checks part-number patterns, emails, and distinctive
    terms (file stems/titles) of flagged documents in the index.

    ``allow`` (the site's ``web.allow_terms``) exempts a flagged document's stem or title only
    when it IS that term after folding: allowing "Shear Lag" frees ``Shear Lag.pdf``
    but not a title such as "RPT-0042_Shear_Lag_revA", and it never removes words from the
    query, so a longer flagged name that contains the allowed term still blocks."""
    reasons = []
    # decoded and normalised first: double URL encoding, base64, zero-width characters and look-alike
    # letters reach the same checks as plain text (integrity attacks, 0.3.6)
    text = _normalize(expand_outbound(query))
    if PN_RE.search(text):
        reasons.append("contains a part-number-like token")
    if EMAIL_RE.search(text) or EMAIL_SPELLED_RE.search(text):
        reasons.append("contains an email address")
    if con is not None:
        # separators folded on both sides: "secret_widget", "secret-widget", "secret widget" and a
        # URL's "secret+widget" are the same term (adversarial eval, 0.2.15)
        q = _fold(text)
        qc = q.replace(" ", "")
        allowed = {_fold(a) for a in allow or []}
        # flagged documents and every member of their copy groups, whichever copy is the canonical
        # record (a renamed copy's file name identifies it as well; copies indexed before the next
        # refresh may carry no flag of their own)
        groups = copy_group_flags(con)
        docs = con.execute("SELECT doc_id, path, title, classification_flag FROM documents").fetchall()
        rows = [r for r in docs if r["classification_flag"] or r["doc_id"] in groups]
        for r in rows:
            for term in (Path(r["path"]).stem, r["title"] or ""):
                t = _fold(term)
                if t and t not in allowed and _term_in(t, q, qc):
                    reasons.append(f"contains a term from a flagged document ({Path(r['path']).name})")
                    break
        if not reasons:
            source = _restricted_name(con, q, docs, {r["doc_id"] for r in rows}, allowed)
            if source:
                reasons.append(f"contains a name or code found only in a flagged document ({source})")
    return reasons
