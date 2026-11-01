"""Query expansion (S5) and hybrid search with re-ranking, filters, profiles and evidence (S6).

Pipeline (0.7.0; every new stage is a setting, the defaults keep the 0.6 ranking except where the 0.6
behaviour was a defect - see the RAG audit of 2026-10-28):

  expand -> BM25 (FTS5, version-2 text) + dense vectors (+ optional sparse) -> RRF -> chunk -> document
  (max, optional aggregation) -> filters -> priors (tier, standard, OCR, feedback, source memory)
  -> cross-encoder on the top documents (title + query-centred passages) -> prior mode (power | bounded |
  additive) -> optional tier_first -> optional diversity (editions | mmr) -> top k

`intent` selects a profile (property | research | locate) whose settings override the site's; `queries`
adds sub-queries that are fused by document RRF and re-ranked against the original question; `evidence()`
grades how well the hits support the question (STRONG / PARTIAL / WEAK / NONE).
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from . import rerank as rr
from .classify import detect_standard, same_standard, std_key
from .config import config_file

STOPWORDS = {
    "a", "an", "the", "of", "for", "in", "on", "and", "or", "to", "with", "what", "is", "are",
    "find", "all", "any", "me", "give", "show", "about", "from", "by", "at", "as", "be", "that",
    "this", "which", "documents", "document", "files", "file", "related", "information", "data",
}
TIER_BOOST = {"T1": 1.30, "T2": 1.20, "T3": 1.10, "T4": 1.00, "T5": 0.90, "T6": 0.80}
# re-ranked score = relevance x prior^BOOST_POWER (`rerank.boost_power` overrides): 1.5 since 0.4.0, after the
# ranking fit and the eval improved both golden sets over 1.0 (scripts/fit_ranker.py; 2026-10-16)
BOOST_POWER = 1.5
VALUE_WORDS = re.compile(r"\b(value|allowable|strength|modulus|kt|kf|fty|ftu|fsu|fbru|coefficient|factor|"
                         r"\d+(?:\.\d+)?)\b", re.I)
TOKEN_SPLIT = re.compile(r"[^\w\-./]+")
_COMPOUND = re.compile(r"[-./]")
_NUMERIC = re.compile(r"[\d.]+")

# ------------------------------------------------------------------ settings and profiles
# Every key can be set in the site config (`rerank:` for the re-rank stage, `search:` for the rest) and per
# profile (`search.profiles.<intent>`). Defaults reproduce the 0.6 ranking, apart from the defect fixes.
DEFAULTS: dict[str, Any] = {
    # re-rank stage (`rerank:`)
    "top_n": 20, "passage_chars": 1000, "passages_per_doc": 2, "passage_pick": "fused", "neighbors": False,
    "title_in_passage": True, "boost_power": BOOST_POWER, "prior_mode": "power", "prior_clip_low": 0.693,
    "prior_clip_high": 0.693, "prior_lambda": 0.15, "tier_first": False,
    # retrieval stage (`search:`)
    "doc_agg_weight": 0.0, "diversity": "none", "mmr_lambda": 0.7, "path_weight": 0.5, "rrf_k": 60,
    "rrf_weights": {"bm25": 1.0, "vector": 1.0, "sparse": 1.0},
}
RERANK_KEYS = ("top_n", "passage_chars", "passages_per_doc", "passage_pick", "neighbors", "title_in_passage",
               "boost_power", "prior_mode", "prior_clip_low", "prior_clip_high", "prior_lambda", "tier_first")
SEARCH_KEYS = ("doc_agg_weight", "diversity", "mmr_lambda", "path_weight", "rrf_k", "rrf_weights")
# Built-in profiles (Phase 3.5). Only settings with a measurement behind them are changed here:
# tier_first helps property lookups and costs research queries (README 0.4.0); locate is a file inventory.
# A profile never picks a re-ranker on its own (the user chooses it); `search.profiles.<intent>.reranker` may.
PROFILES: dict[str, dict[str, Any]] = {
    "property": {"tier_first": True},
    "research": {"tier_first": False, "diversity": "editions"},
    "locate": {"rerank": False},
}
INTENTS = tuple(PROFILES)


def settings(cfg, intent: str | None = None) -> dict[str, Any]:
    """Effective ranking settings: defaults <- site `rerank:` / `search:` <- built-in profile <- site profile."""
    s: dict[str, Any] = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULTS.items()}
    r = (getattr(cfg, "rerank", None) or {}) if cfg is not None else {}
    q = (getattr(cfg, "search", None) or {}) if cfg is not None else {}
    for k in RERANK_KEYS:
        if k in r:
            s[k] = r[k]
    for k in SEARCH_KEYS:
        if k in q:
            s[k] = q[k]
    s["profile"] = None
    if intent:
        if intent not in PROFILES and intent not in (q.get("profiles") or {}):
            s["profile"] = f"unknown:{intent}"
            return s
        s.update(PROFILES.get(intent, {}))
        s.update((q.get("profiles") or {}).get(intent) or {})
        s["profile"] = intent
    return s


# ------------------------------------------------------------------ query expansion
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


def _variants(term: str) -> list[str]:
    """A term and, for a non-numeric compound (7075-T651, Ti-6Al-4V), its spaced form: text that prints
    'high strength' then matches a query for 'high-strength' (Phase 1.1)."""
    out = [term]
    if _COMPOUND.search(term) and not _NUMERIC.fullmatch(term):
        spaced = " ".join(p for p in _COMPOUND.split(term) if p)
        if spaced and spaced != term:
            out.append(spaced)
    return out


def _or(variants: list[str]) -> str:
    parts = []
    for v in variants:
        for x in _variants(v):
            t = _fts_term(x)
            if t and t not in parts:
                parts.append(t)
    if not parts:
        return ""
    return parts[0] if len(parts) == 1 else "(" + " OR ".join(parts) + ")"


@dataclass
class Expansion:
    groups: list[list[str]] = field(default_factory=list)   # each group = OR'ed variants
    terms: list[str] = field(default_factory=list)           # leftover single terms

    def fts(self, mode: str = "and") -> str:
        parts = [_or(g) for g in self.groups] + [_or([t]) for t in self.terms]
        parts = [p for p in parts if p and p != "()"]
        return (" AND " if mode == "and" else " OR ").join(parts)

    def describe(self) -> list[str]:
        return [" | ".join(g) for g in self.groups] + self.terms

    def units(self) -> list[list[str]]:
        """Concept units for coverage: each synonym group, and each leftover term."""
        return [list(g) for g in self.groups] + [[t] for t in self.terms]


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
    memory: dict | None = None           # 0.4.0 source memory: verified answers on this topic (erla.memory)
    boost: float | None = None           # 0.4.0 prior multiplier (tier, standard, OCR, feedback, memory) when re-ranked
    editions: list[int] | None = None    # 0.7.0 other editions of the same standard folded into this hit (diversity)
    cites: list[dict] | None = None      # 0.7.0 standards this document cites that the library holds (erla.refs)

    def as_dict(self) -> dict:
        d = dict(self.__dict__)
        for k in ("editions", "cites"):          # new optional fields only when set
            if d.get(k) is None:
                d.pop(k, None)
        return d


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


def _texts(con, chunk_ids: list[int]) -> dict[int, str]:
    out: dict[int, str] = {}
    ids = list(dict.fromkeys(chunk_ids))
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        for r in con.execute(f"SELECT chunk_id, text FROM chunks WHERE chunk_id IN ({','.join('?' * len(part))})", part):
            out[r[0]] = r[1] or ""
    return out


# ------------------------------------------------------------------ candidate retrieval
@dataclass
class _Cands:
    hits: list[Hit]
    exp: Expansion
    mode: str
    words: list[str]
    base: dict[int, float]                      # document score before priors
    chunks: dict[int, list[tuple]]              # doc -> [(fused score, chunk_id, locator, seq, channel rank)] best first
    and_docs: set[int]


def _bm25_sql(con, s: dict[str, Any]) -> str:
    from .store import fts_version
    if fts_version(con) >= 2:
        return f"bm25(chunks_fts, 1.0, 2.5, {float(s.get('path_weight', 0.5))})"
    return "bm25(chunks_fts, 1.0, 2.5)"


def bm25_params(cfg, s: dict[str, Any]) -> dict[str, Any]:
    """`search.bm25` (and a profile's `bm25`) over erla.bm25.DEFAULTS; `search.path_weight` sets the path
    field weight unless `search.bm25.weights.path` does."""
    from . import bm25
    site = dict(((getattr(cfg, "search", None) or {}).get("bm25") or {}) if cfg is not None else {})
    prof = s.get("bm25") or {}
    merged = {**site, **{k: v for k, v in prof.items() if k not in ("b", "weights")}}
    for key in ("b", "weights"):
        a, b_ = site.get(key), prof.get(key)
        if isinstance(a, dict) or isinstance(b_, dict):
            merged[key] = {**(a if isinstance(a, dict) else {}), **(b_ if isinstance(b_, dict) else {})}
        elif b_ is not None:
            merged[key] = b_
    p = bm25.params(merged)
    if not (isinstance(merged.get("weights"), dict) and "path" in merged["weights"]):
        p["weights"]["path"] = float(s.get("path_weight", 0.5))
    return p


def keyword_units(exp: Expansion) -> list[list[str]]:
    """Concept units with every variant FTS5 would OR together (synonyms and compound spellings)."""
    out = []
    for u in exp.units():
        vs: list[str] = []
        for v in u:
            for x in _variants(v):
                x = x.replace('"', " ").strip()
                if x and x not in vs:
                    vs.append(x)
        if vs:
            out.append(vs)
    return out


def _allowed_docs(con, types, tiers, year_min, discipline, exclude_flagged) -> set[int] | None:
    """Ids of the documents a search filter keeps, or None when no filter is set. The same tests as the
    post-filter in _candidates (G2.5: a copy of a flagged document is filtered as flagged)."""
    if not (types or tiers or year_min or discipline or exclude_flagged):
        return None
    from .classify import copy_group_flags, effective_flag
    groups = copy_group_flags(con) if exclude_flagged else {}
    return {r["doc_id"] for r in con.execute(
        "SELECT doc_id, ext, authority_tier, year, discipline, classification_flag FROM documents")
        if not (types and r["ext"] not in types)
        and not (tiers and (r["authority_tier"] or "T5") not in tiers)
        and not (year_min and (r["year"] or 0) < year_min)
        and not (discipline and (r["discipline"] or "") != discipline)
        and not (exclude_flagged and effective_flag(r["classification_flag"], r["doc_id"], groups))}


def _allowed_chunks(con, docs: set[int] | None):
    """Sorted chunk ids of `docs` (from _allowed_docs), or None when no filter is set."""
    if docs is None:
        return None
    import numpy as np

    keep = sorted(docs)
    ids: list[int] = []
    for i in range(0, len(keep), 500):
        part = keep[i:i + 500]
        ids += [r[0] for r in con.execute(f"SELECT chunk_id FROM chunks WHERE doc_id IN ({','.join('?' * len(part))})",
                                          part)]
    return np.array(sorted(ids), dtype=np.int64)


def _keyword(con, exp: Expansion, k: int, cfg, s: dict[str, Any],
             allowed=None) -> tuple[list[tuple[int, float]], set[int], str]:
    """Keyword channel: [(chunk_id, score)] best first, the AND chunk ids, and 'and' | 'and+or'.

    engine 'erla' (default): exact BM25F with Lucene IDF over EVERY matching chunk (erla.bm25), cut to
    `depth` only after scoring. engine 'fts5', or an index without the version-2 FTS and vocabulary tables:
    SQLite's bm25() (k1 1.2, b 0.75, classic IDF) with LIMIT 600, the 0.6 behaviour, noted in the mode."""
    from . import bm25
    p = bm25_params(cfg, s)
    if str(p.get("engine", "erla")) == "erla" and bm25.available(con):
        units = keyword_units(exp)
        if not units:
            return [], set(), "and"
        return bm25.search(con, units, p, k, allowed=allowed)
    bm = _bm25_sql(con, s)
    keep = set(int(x) for x in allowed) if allowed is not None else None

    def run(m: str):
        expr = exp.fts(m)
        if not expr:
            return []
        if keep is None:
            return [(r[0], -r[1]) for r in con.execute(
                f"SELECT rowid, {bm} AS s FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY s LIMIT 600", (expr,))]
        out: list[tuple[int, float]] = []              # filtered: walk the ranking until 600 allowed rows
        for r in con.execute(f"SELECT rowid, {bm} AS s FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY s", (expr,)):
            if r[0] in keep:
                out.append((r[0], -r[1]))
                if len(out) >= 600:
                    break
        return out

    rows_and = run("and")
    rows = list(rows_and)
    and_ids = {r[0] for r in rows_and}                  # F9: the AND set is not queried twice
    mode = "and"
    if len(rows) < k:
        rows_or = run("or")
        if len(rows_or) > len(rows):
            rows = rows + [r for r in rows_or if r[0] not in and_ids]    # AND hits first, then OR hits
            mode = "and+or"
    if str(p.get("engine", "erla")) == "erla":
        mode += "+bm25_fts5"                              # the exact scorer needs a refresh of this index
    return rows, and_ids, mode


def _candidates(con, query: str, k: int, types, tiers, year_min, discipline, exclude_flagged, include_duplicates,
                boosts, cfg, hybrid: bool, memory: bool, s: dict[str, Any]) -> _Cands:
    exp = expand(query)
    words = [v for g in exp.groups for v in g] + exp.terms
    std_q, _ = detect_standard(query.upper())
    wants_value = bool(VALUE_WORDS.search(query))
    keep_docs = _allowed_docs(con, types, tiers, year_min, discipline, exclude_flagged)
    allowed = _allowed_chunks(con, keep_docs)       # the keyword channel filters before its cut (audit item 16)
    rows, and_ids, mode = _keyword(con, exp, k, cfg, s, allowed)
    or_mode = mode.startswith("and+or")  # before any channel note joins the mode string (review finding 1)
    vec: list[tuple[int, float]] = []
    if hybrid and cfg is not None and (getattr(cfg, "embeddings", None) or {}).get("enabled"):
        from . import embed
        stale = embed.vector_mismatch(cfg, con)
        if stale:
            mode += "+vector_stale"
        else:
            vec = embed.vector_search(cfg, query)
    sp: list[tuple[int, float]] = []
    if hybrid and cfg is not None and (getattr(cfg, "sparse", None) or {}).get("enabled"):
        from . import sparse
        try:
            sp = sparse.search(cfg, con, query)
        except Exception as exc:                       # model missing / offline: keep the other channels
            mode += f"+sparse_failed({type(exc).__name__})"
    if not rows and not vec and not sp:
        return _Cands([], exp, mode, words, {}, {}, set())

    chan_rank: dict[int, int] = {}
    for ranked in ([c for c, _ in rows], [c for c, _ in vec], [c for c, _ in sp]):
        for rank, cid in enumerate(ranked, 1):
            chan_rank[cid] = min(chan_rank.get(cid, rank), rank)
    if vec or sp:
        # Reciprocal rank fusion of BM25, dense (and sparse) rankings, scaled for readability
        kk = float(s.get("rrf_k", 60))
        w = {"bm25": 1.0, "vector": 1.0, "sparse": 1.0, **(s.get("rrf_weights") or {})}
        raw: dict[int, float] = {}
        for rank, (cid, _sc) in enumerate(rows, 1):
            raw[cid] = raw.get(cid, 0.0) + w["bm25"] * 100.0 / (kk + rank)
        for rank, (cid, _cos) in enumerate(vec, 1):
            raw[cid] = raw.get(cid, 0.0) + w["vector"] * 100.0 / (kk + rank)
        for rank, (cid, _sc) in enumerate(sp, 1):
            raw[cid] = raw.get(cid, 0.0) + w["sparse"] * 100.0 / (kk + rank)
        mode += "+vector" if vec else ""
        mode += "+sparse" if sp else ""
        penalize = False                 # RRF already orders AND above OR; no extra penalty
    else:
        raw = {cid: sc for cid, sc in rows}
        penalize = or_mode
    ids = list(raw)
    chunk_rows = []
    from .store import quality_expr
    qx = quality_expr(con)
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        chunk_rows += con.execute(
            f"SELECT c.chunk_id, c.doc_id, c.seq, c.locator, c.has_table, {qx} FROM chunks c "
            f"WHERE c.chunk_id IN ({','.join('?' * len(part))})", part).fetchall()

    dup_of = {r["doc_id"]: r["canonical_doc_id"] for r in con.execute("SELECT doc_id, canonical_doc_id FROM duplicates")}
    dup_count: dict[int, int] = {}
    for d, c in dup_of.items():
        dup_count[c] = dup_count.get(c, 0) + 1

    docs: dict[int, dict] = {}
    and_docs: set[int] = set()
    for cr in chunk_rows:
        did = cr["doc_id"]
        if not include_duplicates and did in dup_of and (keep_docs is None or dup_of[did] in keep_docs):
            # near-duplicate: its canonical copy carries (almost) the same text and is
            # cited instead; never re-map chunks to another file (locators would be wrong).
            # A copy whose canonical the filter drops stands in for it, or the filter finds
            # neither; the copy carries the canonical's marking (G2.5, effective_flag below).
            continue
        cid = cr["chunk_id"]
        sc = raw[cid]
        if penalize and cid not in and_ids:
            sc *= 0.5
        if cid in and_ids:
            and_docs.add(did)
        if wants_value and cr["has_table"]:
            sc *= 1.10
        d = docs.setdefault(did, {"chunks": []})
        d["chunks"].append((sc, cid, cr["locator"], cr["seq"], chan_rank.get(cid, 10 ** 6), cr["quality_json"]))

    if not docs:
        return _Cands([], exp, mode, words, {}, {}, set())
    agg_w = float(s.get("doc_agg_weight") or 0.0)
    for d in docs.values():
        d["chunks"].sort(key=lambda x: -x[0])
        d["best"] = d["chunks"][0][0] + (agg_w * sum(x[0] for x in d["chunks"][1:3]) if agg_w else 0.0)
    if agg_w:
        mode += "+agg"
    ids = list(docs)
    meta = {}
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        for r in con.execute(f"SELECT * FROM documents WHERE doc_id IN ({','.join('?' * len(part))})", part):
            meta[r["doc_id"]] = r

    from .classify import copy_group_flags, effective_flag
    groups = copy_group_flags(con)       # G2.5: a copy of a flagged document is labelled and filtered as one
    hits: list[Hit] = []
    base: dict[int, float] = {}
    chunks: dict[int, list[tuple]] = {}
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
        if std_q and same_standard(std_q, m["standard_id"]):        # F4: never ASTM E8 -> ASTM E83
            score *= 1.5
        if m["ocr_used"]:
            score *= 0.95
        score *= (boosts or {}).get(did, 1.0)
        top = d["chunks"][:3]
        chunks[did] = [c[:5] for c in d["chunks"][:8]]
        base[did] = d["best"]
        warn = []
        for t in top:
            q = _quality(t[5])
            if q.get("low_conf_numbers"):
                warn.append(f"{t[2]}: OCR_LOW_CONF {', '.join(q['low_conf_numbers'][:5])}")
            if q.get("ocr_confusions"):
                warn.append(f"{t[2]}: OCR_CONFUSION {', '.join(q['ocr_confusions'][:5])}")
        hits.append(Hit(
            doc_id=did, score=round(score, 3), title=m["title"] or Path(m["path"]).stem, path=m["path"],
            rel_path=m["rel_path"] or "", ext=m["ext"], tier=m["authority_tier"] or "T5",
            flag=flag, year=m["year"], doc_type=m["doc_type"] or "other",
            standard_id=m["standard_id"], revision=m["revision"], ocr_used=bool(m["ocr_used"]),
            duplicates=dup_count.get(did, 0), locators=[t[2] for t in top], snippets=[], warnings=warn))
    if memory and cfg is not None and hits:
        from . import memory as mm
        known = mm.prior(cfg, con, query, [h.doc_id for h in hits])
        for h in hits:                    # only documents already found and kept by the filters above
            km = known.get(h.doc_id)
            if km:
                h.score = round(h.score * km["factor"], 3)
                h.memory = km
        if known:
            mode += "+memory"
    hits.sort(key=lambda h: -h.score)
    return _Cands(hits, exp, mode, words, base, chunks, and_docs)


def _fuse(cands: list[_Cands], kk: float = 60.0) -> _Cands:
    """Document-level RRF of several sub-query candidate lists (Phase 4.1). A document keeps its prior
    (score / base) and chunks from the sub-query where it ranked best."""
    fused: dict[int, float] = {}
    best_rank: dict[int, tuple[int, int]] = {}
    for qi, c in enumerate(cands):
        for rank, h in enumerate(c.hits, 1):
            fused[h.doc_id] = fused.get(h.doc_id, 0.0) + 100.0 / (kk + rank)
            if h.doc_id not in best_rank or rank < best_rank[h.doc_id][0]:
                best_rank[h.doc_id] = (rank, qi)
    hits: list[Hit] = []
    base: dict[int, float] = {}
    chunks: dict[int, list[tuple]] = {}
    and_docs: set[int] = set()
    for did, (_rank, qi) in best_rank.items():
        c = cands[qi]
        h = next(x for x in c.hits if x.doc_id == did)
        prior = h.score / max(c.base.get(did, 0.0), 1e-9)
        base[did] = fused[did]
        h.score = round(fused[did] * prior, 4)
        hits.append(h)
        chunks[did] = c.chunks.get(did, [])
        if did in c.and_docs:
            and_docs.add(did)
    hits.sort(key=lambda h: -h.score)
    exp = Expansion()
    for c in cands:
        for g in c.exp.groups:
            if g not in exp.groups:
                exp.groups.append(g)
        for t in c.exp.terms:
            if t not in exp.terms:
                exp.terms.append(t)
    words = list(dict.fromkeys(w for c in cands for w in c.words))
    modes = sorted({c.mode for c in cands})
    return _Cands(hits, exp, f"multi[{len(cands)}]:" + "|".join(modes), words, base, chunks, and_docs)


# ------------------------------------------------------------------ search
def search(con, query: str, k: int = 20, types: list[str] | None = None, tiers: list[str] | None = None,
           year_min: int | None = None, discipline: str | None = None, exclude_flagged: bool = False,
           include_duplicates: bool = False, boosts: dict[int, float] | None = None,
           cfg=None, hybrid: bool = True, rerank: bool | None = None,
           reranker: str | None = None, memory: bool = True, intent: str | None = None,
           queries: list[str] | None = None) -> tuple[list[Hit], Expansion, str]:
    s = settings(cfg, intent)
    subs = [query] + [q for q in dict.fromkeys(queries or []) if q and q.strip() and q != query]
    cands = [_candidates(con, q, k, types, tiers, year_min, discipline, exclude_flagged, include_duplicates,
                         boosts, cfg, hybrid, memory, s) for q in subs]
    c = cands[0] if len(cands) == 1 else _fuse(cands, float(s.get("rrf_k", 60)))
    hits, exp, mode = c.hits, c.exp, c.mode
    if s["profile"]:
        mode += f"+profile[{s['profile']}]"
    if not hits:
        return [], exp, mode
    want_rr = s.get("rerank", True)
    use_rr = (rerank if rerank is not None else rr.enabled(cfg)) and want_rr is not False
    if use_rr and cfg is not None:
        try:
            texts = _passages(con, c, hits[: int(s["top_n"])], s)
            hits, used = _rerank(cfg, query, c.words, hits, texts, c.base, reranker or s.get("reranker"), s)
            mode += f"+rerank[{used}]"
            if s.get("tier_first"):
                hits = tier_first(hits)
                mode += "+tier_first"
        except Exception as exc:          # model missing/offline: keep the fused ranking, say so
            mode += f"+rerank_failed({type(exc).__name__})"
    div = str(s.get("diversity") or "none")
    if div in ("editions", "mmr"):
        hits = collapse_editions(hits)
        mode += "+editions"
    if div == "mmr":
        hits = _mmr(con, c, hits, k, float(s.get("mmr_lambda", 0.7)))
        mode += "+mmr"
    hits = hits[:k]
    texts = _texts(con, [x[1] for h in hits for x in c.chunks.get(h.doc_id, [])[:3]])
    for h in hits:
        h.snippets = [_snippet(texts.get(x[1], ""), c.words) for x in c.chunks.get(h.doc_id, [])[:3]]
    return hits, exp, mode


def _passages(con, c: _Cands, head: list[Hit], s: dict[str, Any]) -> dict[int, list[str]]:
    """Passage texts the cross-encoder reads per head document (Phase 3.2): the top `passages_per_doc`
    chunks by fused score ('fused', the 0.6 rule) or by their best single-channel rank ('channel'), each
    extended by the next chunk of the same document when it is short and `neighbors` is on."""
    n = max(1, int(s.get("passages_per_doc", 2)))
    pick: dict[int, list[tuple]] = {}
    for h in head:
        ch = list(c.chunks.get(h.doc_id, []))
        if s.get("passage_pick") == "channel":
            ch.sort(key=lambda x: x[4])
        pick[h.doc_id] = ch[:n]
    texts = _texts(con, [x[1] for v in pick.values() for x in v])
    size = int(s.get("passage_chars", 1000))
    nb: dict[tuple[int, int], str] = {}
    if s.get("neighbors"):
        want = [(did, x[3] + 1) for did, v in pick.items() for x in v if len(texts.get(x[1], "")) < size // 2]
        for did, seq in want:
            row = con.execute("SELECT text FROM chunks WHERE doc_id=? AND seq=?", (did, seq)).fetchone()
            if row:
                nb[(did, seq - 1)] = row[0] or ""
    out: dict[int, list[str]] = {}
    for did, v in pick.items():
        out[did] = [texts.get(x[1], "") + ("\n" + nb[(did, x[3])] if (did, x[3]) in nb else "") for x in v]
    return out


RELEVANT_SHARE = 0.5      # a re-ranked hit is "relevant" at >= half the top hit's re-ranker relevance


def relevant_set(hits: list[Hit]) -> list[Hit]:
    """Re-ranked hits whose relevance is at least RELEVANT_SHARE of the best one (none without re-ranking)."""
    scored = [h for h in hits if h.rerank is not None]
    if not scored:
        return []
    top = max(h.rerank or 0.0 for h in scored)
    return [h for h in scored if (h.rerank or 0.0) >= RELEVANT_SHARE * top and top > 0]


def tier_first(hits: list[Hit]) -> list[Hit]:
    """R1.1 within the relevant set: tier first, then score; the other hits keep their order below it."""
    rel = relevant_set(hits)
    ids = {id(h) for h in rel}
    order = {f"T{i}": i for i in range(1, 7)}
    rel.sort(key=lambda h: (order.get(h.tier, 9), -h.score))
    return rel + [h for h in hits if id(h) not in ids]


def tiers_view(hits: list[Hit]) -> dict:
    """Primary source and the T1/T2 sources that corroborate it (two-tier results, 0.4.0)."""
    rel = relevant_set(hits) or hits[:1]
    if not rel:
        return {"primary": None, "corroborating": []}
    primary = hits[0] if hits[0] in rel else rel[0]
    corro = [h.doc_id for h in rel if h is not primary and h.tier in ("T1", "T2")][:5]
    return {"primary": primary.doc_id, "corroborating": corro}


def collapse_editions(hits: list[Hit]) -> list[Hit]:
    """Phase 3.4: one hit per standard number - the best-ranked edition stays, the others are listed in
    its `editions` so the agent can open them (never dropped silently)."""
    seen: dict[str, Hit] = {}
    out: list[Hit] = []
    for h in hits:
        key = std_key(h.standard_id)
        if key and key in seen:
            first = seen[key]
            first.editions = (first.editions or []) + [h.doc_id]
            continue
        if key:
            seen[key] = h
        out.append(h)
    return out


_WORDS = re.compile(r"[a-z0-9]{3,}")


def _mmr(con, c: _Cands, hits: list[Hit], k: int, lam: float) -> list[Hit]:
    """Maximal marginal relevance over the top 3k hits: relevance = score share of the best, redundancy =
    word Jaccard of the documents' top chunks. The order below the re-ordered block is kept."""
    pool = hits[: max(k * 3, k)]
    rest = hits[len(pool):]
    if len(pool) < 3:
        return hits
    texts = _texts(con, [x[1] for h in pool for x in c.chunks.get(h.doc_id, [])[:2]])
    bag = {h.doc_id: set(_WORDS.findall(" ".join(texts.get(x[1], "") for x in c.chunks.get(h.doc_id, [])[:2]).lower()))
           for h in pool}
    top = max(h.score for h in pool) or 1.0
    chosen: list[Hit] = []
    left = list(pool)
    while left:
        def gain(h: Hit) -> float:
            if not chosen:
                return h.score / top
            red = max((len(bag[h.doc_id] & bag[x.doc_id]) / max(1, len(bag[h.doc_id] | bag[x.doc_id]))) for x in chosen)
            return lam * h.score / top - (1 - lam) * red
        best = max(left, key=gain)
        chosen.append(best)
        left.remove(best)
    return chosen + rest


def _quality(qj) -> dict:
    if not qj:
        return {}
    try:
        return json.loads(qj) or {}
    except ValueError:
        return {}


def prior_factor(boost: float, s: dict[str, Any]) -> float:
    """How the prior (tier, standard, OCR, feedback, memory multipliers) enters the re-ranked score.

    power    (0.4 rule): prior^p. With p = 1.5 the factor spans 0.43-5.6, wider than the relevance itself,
             so a weakly relevant T1 hit can pass a strongly relevant T5 hit (audit F3).
    bounded  exp(clip(p ln prior, -low, +high)): the same shape near 1, but the priors can move a score by at
             most e^high up and e^low down (defaults: a factor of 2 either way).
    additive used by `_rerank` as relevance + lambda ln prior (a tie-breaker scale)."""
    b = max(boost, 1e-9)
    mode = str(s.get("prior_mode") or "power")
    p = float(s.get("boost_power", BOOST_POWER))
    if mode == "bounded":
        x = p * math.log(b)
        return math.exp(max(-float(s.get("prior_clip_low", 0.693)), min(float(s.get("prior_clip_high", 0.693)), x)))
    return b ** p


def final_score(rel: float, boost: float, s: dict[str, Any]) -> float:
    if str(s.get("prior_mode") or "power") == "additive":
        return 100.0 * max(0.0, rel + float(s.get("prior_lambda", 0.15)) * math.log(max(boost, 1e-9)))
    return 100.0 * rel * prior_factor(boost, s)


def _rerank(cfg, query: str, words: list[str], hits: list[Hit], texts: dict, base: dict,
            reranker: str | None = None, s: dict[str, Any] | None = None) -> tuple[list[Hit], str]:
    """A1: re-order the top_n hits by cross-encoder relevance x the hit's own boosts (tier, standard
    number, OCR, feedback); hits below top_n keep their order beneath the re-ranked block.
    Each passage starts with the document title (`title_in_passage`, audit F6: the re-ranker never saw the
    title, which was the deciding evidence for handbooks and methods documents)."""
    s = s or settings(cfg)
    n = int(s.get("top_n", 20))
    size = int(s.get("passage_chars", 1000))
    with_title = bool(s.get("title_in_passage", True))
    head, tail = hits[:n], hits[n:]
    passages, owner = [], []
    for i, h in enumerate(head):
        for t in texts.get(h.doc_id, [])[: max(1, int(s.get("passages_per_doc", 2)))]:
            w = rr.window(t, words, size=size)
            passages.append(f"{h.title}\n{w}" if with_title and h.title else w)
            owner.append(i)
    scores, used = rr.score(cfg, query, passages, reranker)
    best: dict[int, float] = {}
    for o, sc in zip(owner, scores):
        best[o] = max(best.get(o, 0.0), sc)
    for i, h in enumerate(head):
        boost = h.score / max(base.get(h.doc_id, 0.0), 1e-9)
        h.rerank = round(best.get(i, 0.0), 4)
        h.boost = round(boost, 4)
        h.score = round(final_score(h.rerank, boost, s), 3)
    head.sort(key=lambda h: -h.score)
    if tail:
        floor = min(h.score for h in head)
        top_tail = max(h.score for h in tail) or 1.0
        for h in tail:
            h.score = round(floor * 0.99 * h.score / top_tail, 3)
    if str(s.get("prior_mode") or "power") != "power":
        used += f",prior={s.get('prior_mode')}"
    return head + tail, used


# ------------------------------------------------------------------ evidence (Phase 4.1)
EVIDENCE_DEFAULTS = {"strong_relevance": 0.6, "partial_relevance": 0.35, "strong_coverage": 0.8,
                     "partial_coverage": 0.5}
NEXT_STEP = {
    "STRONG": "Open the primary hit and corroborate it (library_show, then verify_packet).",
    "PARTIAL": "Open the top hits; if they do not answer, search again with aliases, standard-number variants or "
               "sub-queries (library_search `queries`).",
    "WEAK": "The library hits do not cover the question well: reformulate once (aliases, broader terms, "
            "sub-queries); if still WEAK, report the gap or go to the web gate (web_query_check).",
    "NONE": "Nothing in the library matched: report the gap or go to the web gate (web_query_check).",
}


def _covers(text: str, variants: list[str]) -> bool:
    low = text.lower()
    for v in variants:
        for x in _variants(v.lower()):
            if re.search(r"(?<![\w])" + re.escape(x) + r"(?![\w])", low):
                return True
    return False


def evidence(con, query: str, hits: list[Hit], mode: str, cfg=None) -> dict[str, Any]:
    """How well the hits support the question (CRAG-style sufficiency signal, unfitted defaults that the
    site can set under `search.evidence`; `erla eval` reports how each verdict did on the golden set).

    coverage = share of the question's concept units (synonym groups and leftover terms) printed in the
    hit's title or top chunks; relevance = the cross-encoder's score of the top hit (when re-ranked)."""
    th = {**EVIDENCE_DEFAULTS, **(((getattr(cfg, "search", None) or {}).get("evidence") or {}) if cfg is not None else {})}
    if not hits:
        return {"verdict": "NONE", "next_step": NEXT_STEP["NONE"], "top_relevance": None, "coverage": 0.0,
                "relevant_hits": 0, "all_terms_matched": False}
    units = expand(query).units()
    covs: list[float] = []
    for h in hits[:3]:
        cids = [r[0] for r in con.execute(
            "SELECT chunk_id FROM chunks WHERE doc_id=? AND locator IN (%s)" % ",".join("?" * len(h.locators)),
            (h.doc_id, *h.locators))] if h.locators else []
        txt = h.title + "\n" + "\n".join(_texts(con, cids).values())
        covs.append(sum(1 for u in units if _covers(txt, u)) / len(units) if units else 0.0)
    cov = covs[0] if covs else 0.0
    rel = hits[0].rerank
    and_match = "+or" not in mode.split("+rerank")[0] or cov >= 1.0
    if rel is not None:
        if rel >= th["strong_relevance"] and cov >= th["strong_coverage"]:
            verdict = "STRONG"
        elif rel >= th["partial_relevance"] and cov >= th["partial_coverage"]:
            verdict = "PARTIAL"
        else:
            verdict = "WEAK"
    else:
        verdict = "STRONG" if cov >= th["strong_coverage"] and and_match else (
            "PARTIAL" if cov >= th["partial_coverage"] else "WEAK")
    return {"verdict": verdict, "next_step": NEXT_STEP[verdict], "top_relevance": rel,
            "coverage": round(cov, 3), "best_coverage_top3": round(max(covs), 3) if covs else 0.0,
            "relevant_hits": len(relevant_set(hits)), "all_terms_matched": bool(and_match),
            "thresholds": th}
