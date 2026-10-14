"""Golden-query evaluation (S14): Recall@5/10, MRR, value presence."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import yaml

from . import safeio
from .config import Config
from .search import search


def _match(hit, expects: list[str]) -> bool:
    hay = f"{hit.title} {hit.path} {hit.standard_id or ''}".lower()
    return any(e.lower() in hay for e in expects)


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


def run_eval(con, cfg: Config, golden: Path, hybrid: bool = True, rerank: bool | None = None) -> dict:
    items = yaml.safe_load(golden.read_text(encoding="utf-8")) or []
    rows, r5, r10, mrr, vals, vtot = [], 0, 0, 0.0, 0, 0
    mode = ""
    for it in items:
        hits, _exp, mode = search(con, it["query"], k=10, cfg=cfg, hybrid=hybrid, rerank=rerank)
        rank = next((i for i, h in enumerate(hits, 1) if _match(h, it.get("expect_docs", []))), None)
        r5 += int(rank is not None and rank <= 5)
        r10 += int(rank is not None)
        mrr += (1 / rank) if rank else 0
        vhit = None
        if it.get("expect_value") not in (None, ""):
            vtot += 1
            vhit = _value_in(con, [h for h in hits[:5] if _match(h, it.get("expect_docs", []))],
                             str(it["expect_value"]))
            vals += int(bool(vhit))
        rows.append({"id": it.get("id"), "query": it["query"], "rank": rank, "value_found": vhit,
                     "top": [h.title[:60] for h in hits[:3]]})
    n = max(len(items), 1)
    result = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "n": len(items),
              "recall@5": round(r5 / n, 3), "recall@10": round(r10 / n, 3), "mrr": round(mrr / n, 3),
              "value_match": f"{vals}/{vtot}", "hybrid": hybrid, "mode": mode,
              "golden": str(golden), "rows": rows}
    hist = safeio.check(cfg, cfg.log_dir / "eval_history.jsonl")
    prev = None
    if hist.is_file():
        lines = hist.read_text(encoding="utf-8").strip().splitlines()
        if lines:
            prev = json.loads(lines[-1])
    if prev:
        result["delta_recall@10"] = round(result["recall@10"] - prev.get("recall@10", 0), 3)
        result["delta_mrr"] = round(result["mrr"] - prev.get("mrr", 0), 3)
    safeio.append_text_at(hist, json.dumps({k: v for k, v in result.items() if k != "rows"}) + "\n")
    return result
