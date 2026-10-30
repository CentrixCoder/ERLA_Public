"""Golden-query evaluation (S14): Recall@5/10, MRR, nDCG@10, primary-first, value presence.

A golden item names what a good answer cites, by substring of title, path or standard id:

    expect_docs: [...]   relevant documents (grade 2; grade 3 when no `primary` is given)
    primary: [...]       optional: the best source for the question (grade 3)
    related: [...]       optional: acceptable follow-up sources (grade 1)

Each group earns its gain once, at the first hit that matches it, so duplicate copies of one
document cannot inflate the score; the ideal ranking puts the groups in grade order. nDCG@10 uses
gain 2^grade - 1 and log2(rank + 1) discount. `primary@1` is the share of queries whose top hit is
the primary source (the expected documents when no primary is given).

Optional per item:

    class: value | conceptual | paraphrase | standard | ocr | negative   (any label; metrics per class)
    negative: true      the library holds no answer (also implied by class `negative`). Negatives are
                        left out of the ranking metrics and reported apart: the top hit's re-ranker
                        relevance next to that of the answerable questions. No pass/fail threshold
                        until one is calibrated on this report.

Every ranking metric carries a 95% bootstrap interval over the questions (`ci95`), and
`compare_runs` gives the paired difference of two runs on the same questions, so a change can be
told apart from the noise of a small golden set (L2).
"""

from __future__ import annotations

import json
import math
import random
import statistics
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


def _check_names(it: dict[str, Any]) -> None:
    """Document names must load as text: YAML reads an unquoted `07031_034` as the octal number 1847836 and
    `30378_05` as 3037805, which then matches nothing and scores a silent miss. Refuse instead."""
    for key in ("primary", "expect_docs", "related"):
        for x in it.get(key) or []:
            if not isinstance(x, str):
                raise ValueError(f"golden item {it.get('id') or it.get('query')!r}: {key} entry {x!r} is not text; "
                                 f"quote it in the YAML file (YAML turned the name into a number)")


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
    Runs recorded before nDCG@10 existed give ndcg@10 None, not 0 (no score is not a zero score).
    `n` counts the scored questions: negatives add no ranking evidence toward MIN_GOLDEN."""
    hist = Path(cfg.log_dir) / "eval_history.jsonl"
    try:
        lines = hist.read_text(encoding="utf-8").strip().splitlines() if hist.is_file() else []
        rec = json.loads(lines[-1]) if lines else None
    except (OSError, ValueError):
        return None
    if not isinstance(rec, dict):
        return None
    nd = rec.get("ndcg@10")
    return {"ts": rec.get("ts"), "n": int(rec.get("n_scored", rec.get("n")) or 0),
            "ndcg@10": float(nd) if isinstance(nd, (int, float)) else None,
            "recall@10": rec.get("recall@10"), "mrr": rec.get("mrr"), "reranker": rec.get("reranker"),
            "min_questions": MIN_GOLDEN}


def _loc_match(locators: list[str], want: list[str]) -> bool:
    """A hit's cited locators name an expected one (store.at_locator's word-boundary rule)."""
    from .store import at_locator
    rows: Any = [{"locator": loc} for loc in locators]
    return any(at_locator(rows, w) for w in want if w)


def _value_at(con, hit, locators: list[str], value: str) -> bool:
    """Citation validity (S14): the expected value is printed in the chunks at the hit's cited locators,
    not merely somewhere in the document."""
    from .store import at_locator
    from .verify import numbers
    rows = con.execute("SELECT locator, text FROM chunks WHERE doc_id=?", (hit.doc_id,)).fetchall()
    sel = [r for loc in locators for r in at_locator(rows, loc)]
    want = [n[0] for n in numbers(value)]
    for r in sel:
        text = r["text"] or ""
        if not want:
            if value.lower() in text.lower():
                return True
            continue
        got = [n[0] for n in numbers(text)]
        if all(any(abs(g - w) <= 1e-9 * max(1.0, abs(w)) for g in got) for w in want):
            return True
    return False


METRICS = ("recall@5", "recall@10", "mrr", "ndcg@10", "primary@1")
BOOT_SAMPLES = 2000
BOOT_SEED = 20261027      # fixed: the same run gives the same interval


def is_negative(it: dict[str, Any]) -> bool:
    return bool(it.get("negative")) or str(it.get("class") or "").lower() == "negative"


def bootstrap_ci(values: list[float], samples: int = BOOT_SAMPLES, seed: int = BOOT_SEED) -> list[float] | None:
    """95% percentile interval of the mean, resampling the questions with replacement; None below 2 values."""
    n = len(values)
    if n < 2:
        return None
    rng = random.Random(seed)
    means = sorted(sum(values[rng.randrange(n)] for _ in range(n)) / n for _ in range(samples))
    return [round(means[int(0.025 * samples)], 3), round(means[min(samples - 1, int(0.975 * samples))], 3)]


def _summary(rows: list[dict]) -> dict:
    """Mean of each ranking metric over the rows, with its 95% bootstrap interval."""
    n = max(len(rows), 1)
    out: dict[str, Any] = {m: round(sum(r[m] for r in rows) / n, 3) for m in METRICS}
    out["ci95"] = {m: bootstrap_ci([float(r[m]) for r in rows]) for m in METRICS}
    return out


def compare_runs(base: dict, new: dict, samples: int = BOOT_SAMPLES, seed: int = BOOT_SEED) -> dict:
    """Paired difference new - base per metric over the answerable questions both runs share (by id,
    else by query text), with a 95% bootstrap interval. An interval that spans 0 is not a change."""
    def key(r: dict) -> str:
        return str(r.get("id") or r["query"])
    b = {key(r): r for r in base.get("rows") or [] if not r.get("negative")}
    pairs = [(b[key(r)], r) for r in new.get("rows") or [] if not r.get("negative") and key(r) in b]
    out: dict[str, Any] = {"n_paired": len(pairs)}
    for m in METRICS:
        d = [float(nr[m]) - float(br[m]) for br, nr in pairs]
        out[m] = {"delta": round(sum(d) / len(d), 3) if d else None, "ci95": bootstrap_ci(d, samples, seed)}
    return out


def run_eval(con, cfg: Config, golden: Path, hybrid: bool = True, rerank: bool | None = None,
             reranker: str | None = None, record: bool = True, memory: bool = False,
             intent: str | None = None) -> dict:
    """`intent`: a ranking profile for every query; 'item' uses each item's own `intent` (property, research,
    locate). Items with `status: unreviewed` (candidates from `erla memory golden`) are skipped."""
    raw = yaml.safe_load(golden.read_text(encoding="utf-8")) or []
    items = [it for it in raw if str(it.get("status") or "") != "unreviewed"]
    for it in items:                      # before any search: a bad file fails at once, not after minutes
        _check_names(it)
    rows: list[dict] = []
    vals, vtot, lhit, ltot, cval, ctot = 0, 0, 0, 0, 0, 0
    secs = 0.0
    mode = ""
    calib: dict[str, dict[str, int]] = {}
    neg_verdicts: dict[str, int] = {}
    for it in items:
        it_intent = it.get("intent") if intent == "item" else intent
        t0 = time.perf_counter()
        kw: dict[str, Any] = {"cfg": cfg, "hybrid": hybrid, "rerank": rerank, "reranker": reranker, "memory": memory}
        if it_intent:
            kw["intent"] = it_intent
        hits, _exp, mode = search(con, it["query"], k=SEARCH_K, **kw)
        secs += time.perf_counter() - t0
        all_hits = hits
        hits = hits[:10]                  # scored @10; searched as ERLA searches (k changes the AND/OR candidate pool)
        verdict = None
        if con is not None and hasattr(con, "execute"):
            try:
                from .search import evidence
                verdict = evidence(con, it["query"], all_hits, mode, cfg)["verdict"]
            except Exception:
                verdict = None
        row: dict[str, Any] = {"id": it.get("id"), "query": it["query"],
                               "class": str(it.get("class") or it.get("intent") or "unclassified"),
                               "evidence": verdict, "top": [h.title[:60] for h in hits[:3]],
                               "top_rerank": getattr(hits[0], "rerank", None) if hits else None}
        if is_negative(it):               # no expected document: reported apart, with the verdict it drew
            row["negative"] = True
            if verdict:
                neg_verdicts[verdict] = neg_verdicts.get(verdict, 0) + 1
            rows.append(row)
            continue
        groups = _groups(it)
        relevant = [s for _, subs in groups for s in subs]
        rank = next((i for i, h in enumerate(hits, 1) if _match(h, relevant)), None)
        q_ndcg = ndcg(hits, groups)
        vhit = None
        if it.get("expect_value") not in (None, ""):
            vtot += 1
            vhit = _value_in(con, [h for h in hits[:5] if _match(h, relevant)], str(it["expect_value"]))
            vals += int(bool(vhit))
        want_loc = [str(x) for x in (it.get("expect_locator") or [])]
        if isinstance(it.get("expect_locator"), str):
            want_loc = [str(it["expect_locator"])]
        loc_ok = None
        if want_loc:
            ltot += 1
            loc_ok = any(_match(h, relevant) and _loc_match(h.locators, want_loc) for h in hits[:5])
            lhit += int(loc_ok)
        cite_ok = None
        if it.get("expect_value") not in (None, "") and con is not None:
            first = next((h for h in hits[:5] if _match(h, relevant)), None)
            if first is not None:
                ctot += 1
                cite_ok = _value_at(con, first, first.locators, str(it["expect_value"]))
                cval += int(cite_ok)
        if verdict:
            c = calib.setdefault(verdict, {"n": 0, "right_in_top5": 0})
            c["n"] += 1
            c["right_in_top5"] += int(rank is not None and rank <= 5)
        row.update({"rank": rank, "recall@5": int(rank is not None and rank <= 5), "recall@10": int(rank is not None),
                    "mrr": round(1 / rank, 4) if rank else 0.0, "ndcg@10": round(q_ndcg, 3),
                    "primary@1": int(bool(hits) and bool(groups) and _match(hits[0], groups[0][1])),
                    "value_found": vhit, "locator_hit@5": loc_ok, "citation_valid": cite_ok})
        rows.append(row)
    pos = [r for r in rows if not r.get("negative")]
    neg = [r for r in rows if r.get("negative")]
    n = max(len(items), 1)
    classes = sorted({r["class"] for r in pos})
    by_class = {c: {"n": len(cr), **_summary(cr)} for c in classes for cr in [[r for r in pos if r["class"] == c]]}
    result: dict[str, Any] = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "n": len(items),
                              "n_scored": len(pos), **_summary(pos),
                              "value_match": f"{vals}/{vtot}",
                              "locator_hit@5": round(lhit / ltot, 3) if ltot else None, "locator_items": ltot,
                              "citation_valid": f"{cval}/{ctot}",
                              "ndcg@10_by_class": {c: v["ndcg@10"] for c, v in by_class.items()},
                              "evidence_calibration": calib, "skipped_unreviewed": len(raw) - len(items),
                              "intent": intent, "sec_per_query": round(secs / n, 2), "hybrid": hybrid,
                              "mode": mode, "reranker": reranker or (cfg.rerank or {}).get("model"),
                              "golden": str(golden)}
    if classes != ["unclassified"]:
        result["by_class"] = by_class
    if neg:
        def med(rs: list[dict]) -> float | None:
            v = [float(r["top_rerank"]) for r in rs if r.get("top_rerank") is not None]
            return round(statistics.median(v), 3) if v else None
        result["negatives"] = {"n": len(neg), "top_rerank_median": med(neg), "answerable_top_rerank_median": med(pos),
                               "evidence": neg_verdicts}
    result["rows"] = rows
    if not record:
        return result
    hist = safeio.check(cfg, cfg.log_dir / "eval_history.jsonl")
    prev = None
    if hist.is_file():                    # the last run on the same questions: a delta against another set means nothing
        for line in reversed(hist.read_text(encoding="utf-8").strip().splitlines()):
            rec = json.loads(line) if line.strip() else None
            if isinstance(rec, dict) and rec.get("golden") == result["golden"]:
                prev = rec
                break
    if prev:
        result["delta_recall@10"] = round(result["recall@10"] - prev.get("recall@10", 0), 3)
        result["delta_mrr"] = round(result["mrr"] - prev.get("mrr", 0), 3)
        if "ndcg@10" in prev:
            result["delta_ndcg@10"] = round(result["ndcg@10"] - prev["ndcg@10"], 3)
    safeio.append_text_at(hist, json.dumps({k: v for k, v in result.items() if k != "rows"}) + "\n")
    return result
