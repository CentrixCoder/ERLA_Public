"""Exact BM25F keyword scoring over every matching chunk (0.7.0, owner decision 2026-10-29).

SQLite's built-in bm25() fixes k1 = 1.2 and b = 0.75, uses the classic IDF that goes negative for common terms
(clamped to 1e-6) and, for a contentless table, keeps counting deleted rows in N and the average length. ERLA
therefore scores keyword matches itself, from FTS5's own index statistics, for EVERY chunk that contains a query
phrase; only after exact scoring is the ranked list cut to `depth` for fusion.

Per query phrase q (a token or a phrase of tokens, as FTS5 matches it) and chunk d, BM25F (Robertson & Zaragoza):

    tf~(q,d) = sum_f  w_f * tf_f(q,d) / (1 - b_f + b_f * len_f(d) / avglen_f)
    score(q,d) = IDF(q) * tf~ * (k1 + 1) / (k1 + tf~)
    IDF(q) = ln(1 + (N - n(q) + 0.5) / (n(q) + 0.5))                     (Lucene / Elasticsearch)

With one field (w = 1) this is exactly IDF * TF (k1 + 1) / (TF + k1 (1 - b + b * len / avglen)). A chunk's score is
the sum over the query phrases. Fields: text, title (title + file stem), path (library-relative path).
N, len_f and avglen_f come from the live rows of `chunks_fts_docsize`. tf per field is counted by SQLite in C: a
column-filtered MATCH with bm25() weighted on that field alone is inverted exactly (_tf_from_sqlite; phrase matching
and tokenization are FTS5's own). Where the inversion does not give whole numbers (an uncommitted write), tf is
counted occurrence by occurrence from `chunks_fts_vi` (fts5vocab, instance), a phrase where its tokens sit at
consecutive offsets of one field. Measured on a 340,000-chunk index: 0.04-0.2 s for phrases and ordinary terms,
0.6-1.1 s for a term in 98 % of all chunks (SQLite's own bm25() needs 0.3 s for the same rows).

`mode="fts5"` reproduces SQLite's bm25() exactly (classic clamped IDF, field weights applied to tf before one
saturation over the whole-chunk length, N and averages from the FTS5 averages record) and exists so tests can
prove the statistics are read correctly.
"""

from __future__ import annotations

import math
import re
import sqlite3
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

import numpy as np

FIELDS = ("text", "title", "path")
VOCAB = "chunks_fts_vi"
DEFAULTS: dict[str, Any] = {
    "engine": "erla",                     # erla: exact BM25F here; fts5: SQLite's bm25() with LIMIT (0.6)
    "k1": 1.2,
    "b": {"text": 0.75, "title": 0.75, "path": 0.75},
    "weights": {"text": 1.0, "title": 2.5, "path": 0.5},
    "idf": "lucene",                      # lucene | classic (clamped at 1e-6, as SQLite)
    "depth": 600,                         # ranked chunks handed to fusion per AND / OR list, after exact scoring
}


def params(cfg_or_dict: Any) -> dict[str, Any]:
    """Effective BM25 parameters from `search.bm25` (a dict) merged over DEFAULTS. `b` may be one number."""
    raw = cfg_or_dict if isinstance(cfg_or_dict, dict) else (
        ((getattr(cfg_or_dict, "search", None) or {}).get("bm25") or {}) if cfg_or_dict is not None else {})
    p = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULTS.items()}
    for k, v in raw.items():
        if k in ("b", "weights") and isinstance(v, dict):
            p[k] = {**p[k], **{f: float(x) for f, x in v.items()}}
        elif k == "b":
            p["b"] = {f: float(v) for f in FIELDS}
        else:
            p[k] = v
    k1: Any = p["k1"]
    p["k1"] = float(k1)
    return p


# ------------------------------------------------------------------ varints (SQLite format)
def varints(blob: bytes | None) -> list[int]:
    out: list[int] = []
    if not blob:
        return out
    i, n = 0, len(blob)
    while i < n:
        v = 0
        for j in range(9):
            c = blob[i]
            i += 1
            if j == 8:
                v = (v << 8) | c
                break
            v = (v << 7) | (c & 0x7F)
            if not c & 0x80:
                break
        out.append(v)
    return out


# ------------------------------------------------------------------ index statistics
@dataclass
class Stats:
    ids: np.ndarray                 # live chunk rowids, sorted
    lens: np.ndarray                # [N, n_fields] token counts per field
    n_fields: int
    fts5_n: int                     # SQLite's averages record (counts deleted rows of a contentless table)
    fts5_tot: list[int]

    @property
    def n(self) -> int:
        return int(len(self.ids))

    def avg(self) -> np.ndarray:
        return self.lens.mean(axis=0) if self.n else np.ones(self.n_fields)


_STATS: dict[str, Stats] = {}


def available(con: sqlite3.Connection) -> bool:
    """The index has the version-2 FTS table and the vocabulary table the exact scorer reads."""
    from . import store
    return store.has_table(con, VOCAB) and store.fts_version(con) >= 2 and store.has_table(con, "chunks_fts_docsize")


def _db_key(con: sqlite3.Connection) -> str:
    row = con.execute("PRAGMA database_list").fetchone()
    return str(row[2] or id(con))


def stats(con: sqlite3.Connection) -> Stats:
    """Live per-chunk field lengths, cached until the FTS index changes (any insert changes the averages
    record; a delete changes the docsize row count)."""
    avg_blob = con.execute("SELECT block FROM chunks_fts_data WHERE id=1").fetchone()
    cnt, mx = con.execute("SELECT count(*), max(id) FROM chunks_fts_docsize").fetchone()
    key = f"{_db_key(con)}|{bytes(avg_blob[0]).hex() if avg_blob and avg_blob[0] else ''}|{cnt}|{mx}"
    st = _STATS.get(key)
    if st is not None:
        return st
    nf = len(FIELDS)
    ids = np.empty(cnt, dtype=np.int64)
    lens = np.zeros((cnt, nf), dtype=np.float64)
    i = 0
    for rid, sz in con.execute("SELECT id, sz FROM chunks_fts_docsize ORDER BY id"):
        v = varints(sz)
        ids[i] = rid
        lens[i, : min(nf, len(v))] = v[:nf]
        i += 1
    av = varints(avg_blob[0]) if avg_blob and avg_blob[0] else [0]
    st = Stats(ids=ids[:i], lens=lens[:i], n_fields=nf, fts5_n=av[0], fts5_tot=(av[1:] + [0] * nf)[:nf])
    for k in [k for k in _STATS if k.split("|", 1)[0] == key.split("|", 1)[0]]:
        _STATS.pop(k)                                    # one cached snapshot per index file
    _STATS[key] = st
    return st


# ------------------------------------------------------------------ query tokenization (exactly as the index)
_TOKENIZE = re.compile(r"tokenize\s*=\s*(\"[^\"]*\"|'(?:[^']|'')*')", re.I)


def tokenizer_spec(con: sqlite3.Connection) -> str:
    row = con.execute("SELECT sql FROM sqlite_master WHERE name='chunks_fts'").fetchone()
    m = _TOKENIZE.search(row[0] if row and row[0] else "")
    return m.group(1) if m else "'unicode61'"


_TOK_CONS: dict[str, sqlite3.Connection] = {}


@lru_cache(maxsize=4096)
def tokens(spec: str, text: str) -> tuple[str, ...]:
    """Tokens of `text` under the index's own tokenizer (an in-memory FTS5 table with the same spec), in order."""
    con = _TOK_CONS.get(spec)
    if con is None:
        from . import store
        con = store.connect_scratch()
        con.execute(f"CREATE VIRTUAL TABLE q USING fts5(x, tokenize={spec})")
        con.execute("CREATE VIRTUAL TABLE qv USING fts5vocab(q, instance)")
        _TOK_CONS[spec] = con
    con.execute("DELETE FROM q")
    con.execute("INSERT INTO q(rowid, x) VALUES (1, ?)", (text,))
    toks = tuple(t for t, _o in sorted(((r[0], r[1]) for r in con.execute("SELECT term, offset FROM qv")),
                                       key=lambda x: x[1]))
    con.execute("DELETE FROM q")
    return toks


# ------------------------------------------------------------------ phrase statistics
@dataclass
class Phrase:
    text: str
    toks: tuple[str, ...]
    rows: np.ndarray = field(default_factory=lambda: np.empty(0, np.int64))   # rowids with tf > 0 (live)
    tf: np.ndarray = field(default_factory=lambda: np.zeros((0, 3)))            # [len(rows), n_fields]


_COL = {f: i for i, f in enumerate(FIELDS)}


def _instances(con: sqlite3.Connection, term: str, docs: np.ndarray | None = None) -> np.ndarray:
    """[(doc, col, offset)] of a term (int64 [m, 3]), optionally only within `docs`."""
    out: list[tuple[int, int, int]] = []
    if docs is None:
        for d, c, o in con.execute(f"SELECT doc, col, offset FROM {VOCAB} WHERE term=?", (term,)):
            out.append((d, _COL.get(c, 0), o))
    else:
        dl = docs.tolist()
        for i in range(0, len(dl), 900):
            part = dl[i:i + 900]
            for d, c, o in con.execute(f"SELECT doc, col, offset FROM {VOCAB} WHERE term=? AND doc IN "
                                       f"({','.join('?' * len(part))})", (term, *part)):
                out.append((d, _COL.get(c, 0), o))
    return np.asarray(out, dtype=np.int64).reshape(-1, 3)


def _counts(con: sqlite3.Connection, term: str) -> tuple[np.ndarray, np.ndarray]:
    """Single-token phrase: (rows, tf[rows, fields]) counted inside SQLite."""
    agg: dict[int, np.ndarray] = {}
    for d, c, n in con.execute(f"SELECT doc, col, count(*) FROM {VOCAB} WHERE term=? GROUP BY doc, col", (term,)):
        a = agg.get(d)
        if a is None:
            a = agg[d] = np.zeros(len(FIELDS))
        a[_COL.get(c, 0)] += n
    rows = np.fromiter(agg.keys(), dtype=np.int64, count=len(agg))
    tf = np.vstack(list(agg.values())) if agg else np.zeros((0, len(FIELDS)))
    return rows, tf


def _fts_phrase(text: str) -> str:
    return '"' + text.replace('"', " ").strip() + '"'


def _tf_from_sqlite(con: sqlite3.Connection, text: str, st: Stats) -> tuple[np.ndarray, np.ndarray] | None:
    """Exact per-field term (phrase) frequencies of every matching chunk, counted by SQLite in C.

    For one field f, `field : "phrase"` matches the chunks holding the phrase in f, and bm25() with weight 1 on f
    and 0 elsewhere is idf_c * tf * (k1 + 1) / (tf + K) with SQLite's fixed k1 = 1.2, b = 0.75,
    K = k1 (1 - b + b D / avgD), idf_c the classic clamped IDF of that column-filtered phrase (n = rows returned),
    D the chunk's total length and avgD SQLite's averages. Every term but tf is known exactly (tests prove the
    reproduction to 1e-16), so tf = S K / (k1 + 1 - S) with S = score / idf_c. tf is an integer: a result more
    than 1e-6 away from one means the statistics do not hold (an uncommitted write), and None sends the caller to
    the occurrence-by-occurrence count."""
    if st.fts5_n <= 0:
        return None
    avg_d = sum(st.fts5_tot) / st.fts5_n
    k1, b = 1.2, 0.75
    per: dict[int, np.ndarray] = {}
    for fi, f in enumerate(FIELDS):
        w = ", ".join("1" if j == fi else "0" for j in range(len(FIELDS)))
        # one packed string per field: SQLite formats every (rowid, score) at full precision (%!.17g round-trips
        # a double exactly) and numpy parses it in C, 1.5x faster than fetching hundreds of thousands of tuples
        packed = con.execute(
            f"WITH t AS MATERIALIZED (SELECT rowid AS r, bm25(chunks_fts, {w}) AS s FROM chunks_fts "
            f"WHERE chunks_fts MATCH ?) SELECT group_concat(r || ' ' || printf('%!.17g', s), ' ') FROM t",
            (f"{f} : {_fts_phrase(text)}",)).fetchone()[0]
        a = np.fromstring(packed, sep=" ").reshape(-1, 2) if packed else np.zeros((0, 2))
        if not len(a):
            continue
        rid = a[:, 0].astype(np.int64)
        pos = np.searchsorted(st.ids, rid)
        if np.any(pos >= len(st.ids)) or np.any(st.ids[np.minimum(pos, len(st.ids) - 1)] != rid):
            return None
        n = len(rid)
        idf_c = math.log((st.fts5_n - n + 0.5) / (n + 0.5))
        idf_c = idf_c if idf_c > 0 else 1e-6
        kk = k1 * (1 - b + b * st.lens[pos].sum(axis=1) / avg_d)
        s = -a[:, 1] / idf_c
        tf = s * kk / (k1 + 1 - s)
        r = np.round(tf)
        if not len(tf) or np.abs(tf - r).max() > 1e-6 or r.min() < 1:
            return None
        per[fi] = np.stack([rid, r.astype(np.int64)])
    if not per:
        return np.empty(0, np.int64), np.zeros((0, len(FIELDS)))
    allr = np.sort(np.concatenate([v[0] for v in per.values()]), kind="stable")   # sorted merge, no hashing
    rows = allr[np.concatenate(([True], allr[1:] != allr[:-1]))] if len(allr) else allr
    tf = np.zeros((len(rows), len(FIELDS)))
    for fi, v in per.items():
        tf[np.searchsorted(rows, v[0]), fi] = v[1]
    return rows, tf


def phrase_stats(con: sqlite3.Connection, text: str, live: np.ndarray, spec: str | None = None,
                 st: Stats | None = None) -> Phrase:
    """Where a phrase occurs and how often per field, for every live chunk (no cap). Counted by SQLite through
    bm25() (fast, exact; _tf_from_sqlite), else occurrence by occurrence from the vocabulary table."""
    if st is not None:
        got = _tf_from_sqlite(con, text, st)
        if got is not None:
            return Phrase(text=text, toks=(), rows=got[0], tf=got[1])
    toks = tokens(spec or tokenizer_spec(con), text)
    ph = Phrase(text=text, toks=toks, tf=np.zeros((0, len(FIELDS))))
    if not toks:
        return ph
    if len(toks) == 1:
        rows, tf = _counts(con, toks[0])
    else:
        # chunks holding every token, rarest first, then consecutive offsets within one field
        docsets: list[np.ndarray] = []
        for t in dict.fromkeys(toks):
            d = np.fromiter((r[0] for r in con.execute(f"SELECT DISTINCT doc FROM {VOCAB} WHERE term=?", (t,))),
                            dtype=np.int64)
            docsets.append(d)
            if not len(d):
                return ph
        cand = docsets[0]
        for d in docsets[1:]:
            cand = np.intersect1d(cand, d, assume_unique=True)
        if not len(cand):
            return ph
        shift = np.int64(1 << 20)
        base = None
        for i, t in enumerate(toks):
            inst = _instances(con, t, cand)
            keys = (inst[:, 0] * 4 + inst[:, 1]) * shift + inst[:, 2] - i
            base = keys if base is None else np.intersect1d(base, keys)
        if base is None or not len(base):
            return ph
        doc_col = base // shift
        dc, n = np.unique(doc_col, return_counts=True)
        docs, cols = dc // 4, dc % 4
        rows = np.unique(docs)
        tf = np.zeros((len(rows), len(FIELDS)))
        np.add.at(tf, (np.searchsorted(rows, docs), np.minimum(cols, len(FIELDS) - 1)), n)
    keep = np.isin(rows, live, assume_unique=False)
    ph.rows, ph.tf = rows[keep], tf[keep]
    order = np.argsort(ph.rows)
    ph.rows, ph.tf = ph.rows[order], ph.tf[order]
    return ph


# ------------------------------------------------------------------ scoring
def idf(n_docs: float, n_hit: float, kind: str) -> float:
    if kind == "classic":
        v = math.log((n_docs - n_hit + 0.5) / (n_hit + 0.5)) if n_hit > 0 else 0.0
        return v if v > 0 else 1e-6
    return math.log(1.0 + (n_docs - n_hit + 0.5) / (n_hit + 0.5))


def phrase_scores(ph: Phrase, st: Stats, p: dict[str, Any], mode: str = "bm25f") -> np.ndarray:
    """Score of one phrase for each of its rows (aligned with ph.rows)."""
    if not len(ph.rows):
        return np.zeros(0)
    pos = np.searchsorted(st.ids, ph.rows)
    k1 = float(p["k1"])
    w = np.asarray([float(p["weights"].get(f, 0.0)) for f in FIELDS])
    if mode == "fts5":
        n_docs = st.fts5_n
        tot = np.asarray(st.fts5_tot, dtype=np.float64)
        avgd = tot.sum() / max(n_docs, 1)
        bb = float(p["b"]["text"])
        tfw = ph.tf @ w
        dl = st.lens[pos].sum(axis=1)
        return idf(n_docs, len(ph.rows), "classic") * tfw * (k1 + 1) / (tfw + k1 * (1 - bb + bb * dl / max(avgd, 1e-9)))
    b = np.asarray([float(p["b"].get(f, 0.75)) for f in FIELDS])
    avg = np.maximum(st.avg(), 1e-9)
    norm = 1 - b + b * st.lens[pos] / avg
    tfn = (ph.tf * w / norm).sum(axis=1)
    return idf(st.n, len(ph.rows), str(p.get("idf", "lucene"))) * tfn * (k1 + 1) / (k1 + tfn)


@dataclass
class Prepared:
    """A query's phrase statistics, reusable for any parameters (scripts/fit_ranker.py --bm25)."""
    groups: list[list[Phrase]]          # concept units: a synonym group or a leftover term (with its variants)
    stats: Stats


def prepare(con: sqlite3.Connection, units: list[list[str]]) -> Prepared:
    st = stats(con)
    spec = tokenizer_spec(con)
    cache: dict[str, Phrase] = {}
    groups: list[list[Phrase]] = []
    for u in units:
        g = []
        for v in u:
            if v not in cache:
                cache[v] = phrase_stats(con, v, st.ids, spec, st)
            g.append(cache[v])
        groups.append(g)
    return Prepared(groups=groups, stats=st)


def rank(prep: Prepared, p: dict[str, Any], k: int, mode: str = "bm25f",
         allowed: np.ndarray | None = None) -> tuple[list[tuple[int, float]], set[int], str]:
    """Every chunk holding any query phrase, scored exactly; AND chunks (every concept unit present) first,
    then, when fewer than k, the OR-only chunks (the 0.6 AND->OR rule). Each list is cut to `depth` only
    after scoring. Returns ([(rowid, score)] best first, AND rowids, 'and' | 'and+or').

    ``allowed``: the sorted chunk ids a search filter keeps (types, tiers, year, discipline, flags), applied
    before the cut. Filtering after it lost every allowed chunk ranked below `depth` (correctness audit
    2026-10-31: a .csv filter found nothing for a word in 701 chunks). None (no filter) changes nothing."""
    st = prep.stats
    if not prep.groups or not st.n:
        return [], set(), "and"
    total = np.zeros(st.n)
    present = np.zeros((len(prep.groups), st.n), dtype=bool)
    seen: set[int] = set()
    for gi, g in enumerate(prep.groups):
        for ph in g:
            if not len(ph.rows):
                continue
            pos = np.searchsorted(st.ids, ph.rows)
            present[gi, pos] = True
            if id(ph) in seen:                          # a phrase in two units scores once, as FTS5 does
                continue
            seen.add(id(ph))
            np.add.at(total, pos, phrase_scores(ph, st, p, mode))
    any_hit = present.any(axis=0)
    all_hit = present.all(axis=0)
    if allowed is not None:
        keep = np.isin(st.ids, allowed)
        any_hit &= keep
        all_hit &= keep
    depth = int(p.get("depth", 600))

    def top(mask: np.ndarray) -> list[tuple[int, float]]:
        idx = np.flatnonzero(mask)
        if not len(idx):
            return []
        sc = total[idx]
        if len(idx) > depth:
            cut = np.argpartition(-sc, depth - 1)[:depth]
            idx, sc = idx[cut], sc[cut]
        order = np.lexsort((st.ids[idx], -sc))       # score desc, rowid asc for ties
        return [(int(st.ids[idx[j]]), float(sc[j])) for j in order]

    and_rows = top(all_hit)
    and_ids = {r for r, _ in and_rows}
    and_all = {int(x) for x in st.ids[all_hit]}
    if len(and_rows) >= k:
        return and_rows, and_ids, "and"
    or_rows = top(any_hit & ~all_hit)
    if not or_rows:
        return and_rows, and_ids, "and"
    return and_rows + [r for r in or_rows if r[0] not in and_all], and_ids, "and+or"


def search(con: sqlite3.Connection, units: list[list[str]], p: dict[str, Any], k: int,
           mode: str = "bm25f", allowed: np.ndarray | None = None) -> tuple[list[tuple[int, float]], set[int], str]:
    return rank(prepare(con, units), p, k, mode, allowed)
