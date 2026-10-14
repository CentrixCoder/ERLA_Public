"""Query expansion (S5) and BM25 search with re-ranking and filters (S6)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

import json

from . import rerank as rr
from .classify import detect_standard
from .config import config_file

STOPWORDS = {
    "a", "an", "the", "of", "for", "in", "on", "and", "or", "to", "with", "what", "is", "are",
    "find", "all", "any", "me", "give", "show", "about", "from", "by", "at", "as", "be", "that",
    "this", "which", "documents", "document", "files", "file", "related", "information", "data",
}
TIER_BOOST = {"T1": 1.30, "T2": 1.20, "T3": 1.10, "T4": 1.00, "T5": 0.90, "T6": 0.80}
VALUE_WORDS = re.compile(r"\b(value|allowable|strength|modulus|kt|kf|fty|ftu|fsu|fbru|coefficient|factor|"
                         r"\d+(?:\.\d+)?)\b", re.I)
TOKEN_SPLIT = re.compile(r"[^\w\-./]+")


def synonym_groups() -> list[list[str]]:
    return _synonym_groups(str(config_file("synonyms.yaml")))


@lru_cache(maxsize=8)
def _synonym_groups(path: str) -> list[list[str]]:
    p = Path(path)
    if not p.is_file():
        return []
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    groups = []
    for section in raw.values():
        for g in section or []:
            groups.append([str(x) for x in g])
    return groups


def _fts_term(term: str) -> str:
    t = term.replace('"', " ").strip()
    return f'"{t}"' if t else ""


@dataclass
class Expansion:
    groups: list[list[str]] = field(default_factory=list)   # each group = OR'ed variants
    terms: list[str] = field(default_factory=list)           # leftover single terms

    def fts(self, mode: str = "and") -> str:
        parts = ["(" + " OR ".join(_fts_term(v) for v in g if _fts_term(v)) + ")" for g in self.groups]
        parts += [_fts_term(t) for t in self.terms if _fts_term(t)]
        parts = [p for p in parts if p and p != "()"]
        return (" AND " if mode == "and" else " OR ").join(parts)

    def describe(self) -> list[str]:
        return [" | ".join(g) for g in self.groups] + self.terms


def expand(query: str) -> Expansion:
    """Map query phrases onto synonym groups; remaining words become terms."""
    q = f" {query.lower()} "
    used = q
    exp = Expansion()
    for g in synonym_groups():
        for alias in sorted(g, key=len, reverse=True):
            a = alias.lower()
            pat = re.compile(r"(?<![\w-])" + re.escape(a) + r"(?![\w-])")
            if pat.search(used):
                exp.groups.append(g)
                used = pat.sub(" ", used)
                break
    for tok in TOKEN_SPLIT.split(used):
        tok = tok.strip(".-/")
        if len(tok) < 2 and not tok.isdigit():
            continue
        if tok in STOPWORDS:
            continue
        exp.terms.append(tok)
    return exp


@dataclass
class Hit:
    doc_id: int
    score: float
    title: str
    path: str
    rel_path: str
    ext: str
    tier: str
    flag: str | None
    year: int | None
    doc_type: str
    standard_id: str | None
    revision: str | None
    ocr_used: bool
    duplicates: int
    locators: list[str]
    snippets: list[str]
    rerank: float | None = None          # A1 cross-encoder relevance (0-1) when re-ranked
    warnings: list[str] = field(default_factory=list)   # A5/A6 quality flags of the cited chunks

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def _snippet(text: str, words: list[str], width: int = 320) -> str:
    low = text.lower()
    pos = -1
    for w in words:
        pos = low.find(w.lower())
        if pos >= 0:
            break
    start = max(0, (pos if pos >= 0 else 0) - width // 3)
    s = text[start:start + width].replace("\n", " ")
    return ("…" if start > 0 else "") + s + ("…" if start + width < len(text) else "")


def search(con, query: str, k: int = 20, types: list[str] | None = None, tiers: list[str] | None = None,
           year_min: int | None = None, discipline: str | None = None, exclude_flagged: bool = False,
           include_duplicates: bool = False, boosts: dict[int, float] | None = None,
           cfg=None, hybrid: bool = True, rerank: bool | None = None) -> tuple[list[Hit], Expansion, str]:
    exp = expand(query)
    words = [v for g in exp.groups for v in g] + exp.terms
    std_q, _ = detect_standard(query.upper())
    wants_value = bool(VALUE_WORDS.search(query))

    def run(mode: str):
        expr = exp.fts(mode)
        if not expr:
            return []
        return con.execute(
            "SELECT rowid, bm25(chunks_fts, 1.0, 2.5) AS s FROM chunks_fts WHERE chunks_fts MATCH ? "
            "ORDER BY s LIMIT 600", (expr,)).fetchall()

    mode = "and"
    rows = run("and")
    if len(rows) < k:
        rows_or = run("or")
        if len(rows_or) > len(rows):
            # keep AND hits first, then OR hits
            have = {r["rowid"] for r in rows}
            rows = list(rows) + [r for r in rows_or if r["rowid"] not in have]
            mode = "and+or"
    vec = []
    if hybrid and cfg is not None and cfg.embeddings.get("enabled"):
        from .embed import vector_search
        vec = vector_search(cfg, query)
    if not rows and not vec:
        return [], exp, mode

    and_ids = set()
    if mode == "and+or":
        and_ids = {r["rowid"] for r in run("and")}
    if vec:
        # Reciprocal rank fusion (k=60) of BM25 and dense rankings, scaled for readability
        raw = {}
        for rank, r in enumerate(rows, 1):
            raw[r["rowid"]] = raw.get(r["rowid"], 0.0) + 100.0 / (60 + rank)
        for rank, (cid, _cos) in enumerate(vec, 1):
            raw[cid] = raw.get(cid, 0.0) + 100.0 / (60 + rank)
        mode += "+vector"
        and_ids = set(raw)          # RRF already orders AND above OR; no extra penalty
    else:
        raw = {r["rowid"]: -r["s"] for r in rows}
    ids = list(raw)
    chunk_rows = []
    from .store import quality_expr
    qx = quality_expr(con)
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        chunk_rows += con.execute(
            f"SELECT c.chunk_id, c.doc_id, c.locator, c.text, c.has_table, {qx} FROM chunks c "
            f"WHERE c.chunk_id IN ({','.join('?' * len(part))})", part).fetchall()

    dup_of = {r["doc_id"]: r["canonical_doc_id"] for r in con.execute("SELECT doc_id, canonical_doc_id FROM duplicates")}
    dup_count: dict[int, int] = {}
    for d, c in dup_of.items():
        dup_count[c] = dup_count.get(c, 0) + 1

    docs: dict[int, dict] = {}
    for cr in chunk_rows:
        did = cr["doc_id"]
        if not include_duplicates and did in dup_of:
            # near-duplicate: its canonical copy carries (almost) the same text and is
            # cited instead; never re-map chunks to another file (locators would be wrong)
            continue
        sc = raw[cr["chunk_id"]]
        if mode == "and+or" and cr["chunk_id"] not in and_ids:
            sc *= 0.5
        if wants_value and cr["has_table"]:
            sc *= 1.10
        d = docs.setdefault(did, {"best": 0.0, "chunks": []})
        d["best"] = max(d["best"], sc)
        d["chunks"].append((sc, cr["locator"], cr["text"], cr["quality_json"]))

    if not docs:
        return [], exp, mode
    ids = list(docs)
    meta = {}
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        for r in con.execute(f"SELECT * FROM documents WHERE doc_id IN ({','.join('?' * len(part))})", part):
            meta[r["doc_id"]] = r

    from .classify import copy_group_flags, effective_flag
    groups = copy_group_flags(con)       # G2.5: a copy of a flagged document is labelled and filtered as one
    hits: list[Hit] = []
    texts: dict[int, list[str]] = {}
    base: dict[int, float] = {}
    for did, d in docs.items():
        m = meta.get(did)
        if m is None:
            continue
        flag = effective_flag(m["classification_flag"], did, groups)
        if types and m["ext"] not in types:
            continue
        if tiers and (m["authority_tier"] or "T5") not in tiers:
            continue
        if year_min and (m["year"] or 0) < year_min:
            continue
        if discipline and (m["discipline"] or "") != discipline:
            continue
        if exclude_flagged and flag:
            continue
        score = d["best"] * TIER_BOOST.get(m["authority_tier"] or "T5", 0.9)
        if std_q and m["standard_id"] and std_q.lower() in (m["standard_id"] or "").lower():
            score *= 1.5
        if m["ocr_used"]:
            score *= 0.95
        score *= (boosts or {}).get(did, 1.0)
        top = sorted(d["chunks"], key=lambda x: -x[0])[:3]
        texts[did] = [t[2] or "" for t in top[:2]]
        base[did] = d["best"]
        warn = []
        for t in top:
            q = _quality(t[3])
            if q.get("low_conf_numbers"):
                warn.append(f"{t[1]}: OCR_LOW_CONF {', '.join(q['low_conf_numbers'][:5])}")
            if q.get("ocr_confusions"):
                warn.append(f"{t[1]}: OCR_CONFUSION {', '.join(q['ocr_confusions'][:5])}")
        hits.append(Hit(
            doc_id=did, score=round(score, 3), title=m["title"] or Path(m["path"]).stem, path=m["path"],
            rel_path=m["rel_path"] or "", ext=m["ext"], tier=m["authority_tier"] or "T5",
            flag=flag, year=m["year"], doc_type=m["doc_type"] or "other",
            standard_id=m["standard_id"], revision=m["revision"], ocr_used=bool(m["ocr_used"]),
            duplicates=dup_count.get(did, 0), locators=[t[1] for t in top],
            snippets=[_snippet(t[2], words) for t in top], warnings=warn))
    hits.sort(key=lambda h: -h.score)
    use_rr = rerank if rerank is not None else rr.enabled(cfg)
    if use_rr and hits and cfg is not None:
        try:
            hits = _rerank(cfg, query, words, hits, texts, base)
            mode += "+rerank"
        except Exception as exc:          # model missing/offline: keep the fused ranking, say so
            mode += f"+rerank_failed({type(exc).__name__})"
    return hits[:k], exp, mode


def _quality(qj) -> dict:
    if not qj:
        return {}
    try:
        return json.loads(qj) or {}
    except ValueError:
        return {}


def _rerank(cfg, query: str, words: list[str], hits: list[Hit], texts: dict, base: dict) -> list[Hit]:
    """A1: re-order the top_n hits by cross-encoder relevance x the hit's own boosts (tier, standard
    number, OCR, feedback); hits below top_n keep their order beneath the re-ranked block."""
    r = cfg.rerank or {}
    n = int(r.get("top_n", 20))
    size = int(r.get("passage_chars", 1000))
    head, tail = hits[:n], hits[n:]
    passages, owner = [], []
    for i, h in enumerate(head):
        for t in texts.get(h.doc_id, [])[:2]:
            passages.append(rr.window(t, words, size=size))
            owner.append(i)
    scores = rr.score(cfg, query, passages)
    best: dict[int, float] = {}
    for o, s in zip(owner, scores):
        best[o] = max(best.get(o, 0.0), s)
    for i, h in enumerate(head):
        boost = h.score / max(base.get(h.doc_id, 0.0), 1e-9)
        h.rerank = round(best.get(i, 0.0), 4)
        h.score = round(100.0 * h.rerank * boost, 3)
    head.sort(key=lambda h: -h.score)
    if tail:
        floor = min(h.score for h in head)
        top_tail = max(h.score for h in tail) or 1.0
        for h in tail:
            h.score = round(floor * 0.99 * h.score / top_tail, 3)
    return head + tail
