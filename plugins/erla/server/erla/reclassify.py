"""K3 / T-E: re-classify indexed documents (doc_type, authority_tier) from stored text.

No re-extraction, no file access in the library (G1.1). Uses the same head text the indexer
uses (first 2 chunks). Dry run reads only and reports the tier changes.
"""
from __future__ import annotations

import collections
import csv
from pathlib import Path

from . import classify, safeio


def reclassify(con, write: bool, out_csv: Path | None = None, log=print, heartbeat=None, guard=None) -> dict:
    classify._load_yaml.cache_clear()
    docs = con.execute("SELECT doc_id, rel_path, path, ext, title, doc_type, authority_tier "
                       "FROM documents WHERE extract_status='ok'").fetchall()
    changes, moves = [], collections.Counter()
    for i, d in enumerate(docs):
        rows = con.execute("SELECT text FROM chunks WHERE doc_id=? ORDER BY seq LIMIT 2", (d["doc_id"],)).fetchall()
        head = "\n".join(r["text"] or "" for r in rows)
        rel = d["rel_path"] or d["path"]
        title = d["title"] or Path(rel).stem
        cls = classify.classify_document(rel, (d["ext"] or "").lstrip("."), title, head)
        if cls["authority_tier"] != d["authority_tier"] or cls["doc_type"] != (d["doc_type"] or "other"):
            changes.append({"doc_id": d["doc_id"], "rel_path": rel, "old_type": d["doc_type"], "new_type": cls["doc_type"],
                            "old_tier": d["authority_tier"], "new_tier": cls["authority_tier"]})
            if cls["authority_tier"] != d["authority_tier"]:
                moves[(d["authority_tier"], cls["authority_tier"])] += 1
            if write:
                con.execute("UPDATE documents SET doc_type=?, authority_tier=? WHERE doc_id=?",
                            (cls["doc_type"], cls["authority_tier"], d["doc_id"]))
        if heartbeat and i % 200 == 0:
            heartbeat()
    if write:
        con.commit()
    if out_csv:
        if guard is None:
            raise ValueError("out_csv needs guard=<Config> (G1.2)")
        with safeio.open_write(guard, out_csv, newline="") as f:
            w = csv.DictWriter(f, fieldnames=["doc_id", "rel_path", "old_type", "new_type", "old_tier", "new_tier"])
            w.writeheader()
            w.writerows(changes)
    summary = {"documents": len(docs), "changed": len(changes),
               "tier_changes": sum(moves.values()),
               "moves": {f"{a}->{b}": n for (a, b), n in sorted(moves.items())},
               "written": write}
    log(f"reclassify: {summary}")
    return summary
