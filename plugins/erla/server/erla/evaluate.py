"""Golden-query evaluation (S14): Recall@5/10, MRR, nDCG@10, primary-first, value presence.

A golden item names what a good answer cites, by substring of title, path or standard id:

    expect_docs: [...]   relevant documents (grade 2; grade 3 when no `primary` is given)
    primary: [...]       optional: the best source for the question (grade 3)
    related: [...]       optional: acceptable follow-up sources (grade 1)

Each group earns its gain once, at the first hit that matches it, so duplicate copies of one
document cannot inflate the score; the ideal ranking puts the groups in grade order. nDCG@10 uses
gain 2^grade - 1 and log2(rank + 1) discount. `primary@1` is the share of queries whose top hit is
the primary source (the expected documents when no primary is given).
"""

from __future__ import annotations

import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from . import safeio
from .config import Config
from .search import search

SEARCH_K = 20       # library_search's default k: the eval measures the search ERLA actually runs


def _match(hit, expects: list[str]) -> bool:
    # Golden files name documents by library-relative path with '/'; hit paths on Windows carry '\'
    hay = f"{hit.title} {hit.path} {hit.standard_id or ''}".lower().replace("\\", "/")
    return any(e.lower().replace("\\", "/") in hay for e in expects)


def _groups(it: dict[str, Any]) -> list[tuple[int, list[str]]]:
    """(grade, substrings) per judged group of a golden item, highest grade first."""
    primary = [str(x) for x in it.get("primary") or []]
    expect = [str(x) for x in it.get("expect_docs") or []]
    groups: list[tuple[int, list[str]]] = []
    if primary:
        groups.append((3, primary))
    if expect:
        groups.append((2 if primary else 3, expect))
    groups += [(1, [str(r)]) for r in it.get("related") or []]
    return groups


def ndcg(hits, groups: list[tuple[int, list[str]]], k: int = 10) -> float:
    if not groups:
        return 0.0
    seen: set[int] = set()
    dcg = 0.0
    for rank, h in enumerate(hits[:k], 1):
        for gi, (grade, subs) in enumerate(groups):
            if gi not in seen and _match(h, subs):
                seen.add(gi)
                dcg += (2 ** grade - 1) / math.log2(rank + 1)
                break
    ideal = sorted((g for g, _ in groups), reverse=True)[:k]
    idcg = sum((2 ** g - 1) / math.log2(i + 1) for i, g in enumerate(ideal, 1))
    return dcg / idcg if idcg else 0.0


def _value_in(con, hits, value: str) -> bool:
    """The expected value is printed, as a number, in an expected document among the top hits.
    A plain substring test ("1" in any text) proved nothing; numbers are compared numerically
    (verify.numbers: thousands commas, unicode minus, ranges), text values by substring."""
    from .verify import numbers
    want = [n[0] for n in numbers(value)]
    for h in hits:
        for (text,) in con.execute("SELECT text FROM chunks WHERE doc_id=?", (h.doc_id,)):
            text = text or ""
            if not want:
                if value.lower() in text.lower():
                    return True
                continue
            got = [n[0] for n in numbers(text)]
            if all(any(abs(g - w) <= 1e-9 * max(1.0, abs(w)) for g in got) for w in want):
                return True
    return False


MIN_GOLDEN = 30   # S14: fewer golden questions than this cannot tell re-rankers or changes apart


def last_run(cfg: Config) -> dict | None:
    """The latest recorded eval run, for the control panel's search-quality tile; None when there is none.
    Runs recorded before nDCG@10 existed give ndcg@10 None, not 0 (no score is not a zero score)."""
    hist = Path(cfg.log_dir) / "eval_history.jsonl"
    try:
        lines = hist.read_text(encoding="utf-8").strip().splitlines() if hist.is_file() else []
        rec = json.loads(lines[-1]) if lines else None
    except (OSError, ValueError):
        return None
    if not isinstance(rec, dict):
        return None
    nd = rec.get("ndcg@10")
    return {"ts": rec.get("ts"), "n": int(rec.get("n") or 0),
            "ndcg@10": float(nd) if isinstance(nd, (int, float)) else None,
            "recall@10": rec.get("recall@10"), "mrr": rec.get("mrr"), "reranker": rec.get("reranker"),
            "min_questions": MIN_GOLDEN}


def run_eval(con, cfg: Config, golden: Path, hybrid: bool = True, rerank: bool | None = None,
             reranker: str | None = None, record: bool = True, memory: bool = False) -> dict:
    items = yaml.safe_load(golden.read_text(encoding="utf-8")) or []
    rows, r5, r10, mrr, nd, p1, vals, vtot = [], 0, 0, 0.0, 0.0, 0, 0, 0
    secs = 0.0
    mode = ""
    for it in items:
        t0 = time.perf_counter()
        hits, _exp, mode = search(con, it["query"], k=SEARCH_K, cfg=cfg, hybrid=hybrid, rerank=rerank,
                                  reranker=reranker, memory=memory)
        secs += time.perf_counter() - t0
        hits = hits[:10]                  # scored @10; searched as ERLA searches (k changes the AND/OR candidate pool)
        groups = _groups(it)
        relevant = [s for _, subs in groups for s in subs]
        rank = next((i for i, h in enumerate(hits, 1) if _match(h, relevant)), None)
        r5 += int(rank is not None and rank <= 5)
        r10 += int(rank is not None)
        mrr += (1 / rank) if rank else 0
        q_ndcg = ndcg(hits, groups)
        nd += q_ndcg
        top_is_primary = bool(hits) and bool(groups) and _match(hits[0], groups[0][1])
        p1 += int(top_is_primary)
        vhit = None
        if it.get("expect_value") not in (None, ""):
            vtot += 1
            vhit = _value_in(con, [h for h in hits[:5] if _match(h, relevant)], str(it["expect_value"]))
            vals += int(bool(vhit))
        rows.append({"id": it.get("id"), "query": it["query"], "rank": rank, "ndcg@10": round(q_ndcg, 3),
                     "primary@1": top_is_primary, "value_found": vhit, "top": [h.title[:60] for h in hits[:3]]})
    n = max(len(items), 1)
    result = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "n": len(items),
              "recall@5": round(r5 / n, 3), "recall@10": round(r10 / n, 3), "mrr": round(mrr / n, 3),
              "ndcg@10": round(nd / n, 3), "primary@1": round(p1 / n, 3),
              "value_match": f"{vals}/{vtot}", "sec_per_query": round(secs / n, 2), "hybrid": hybrid,
              "mode": mode, "reranker": reranker or (cfg.rerank or {}).get("model"), "golden": str(golden),
              "rows": rows}
    if not record:
        return result
    hist = safeio.check(cfg, cfg.log_dir / "eval_history.jsonl")
    prev = None
    if hist.is_file():
        lines = hist.read_text(encoding="utf-8").strip().splitlines()
        if lines:
            prev = json.loads(lines[-1])
    if prev:
        result["delta_recall@10"] = round(result["recall@10"] - prev.get("recall@10", 0), 3)
        result["delta_mrr"] = round(result["mrr"] - prev.get("mrr", 0), 3)
        if "ndcg@10" in prev:
            result["delta_ndcg@10"] = round(result["ndcg@10"] - prev["ndcg@10"], 3)
    safeio.append_text_at(hist, json.dumps({k: v for k, v in result.items() if k != "rows"}) + "\n")
    return result
