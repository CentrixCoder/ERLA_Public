"""K3 / T-E: re-classify indexed documents (doc_type, authority_tier) from stored text.

No re-extraction, no file access in the library (G1.1). Uses the same head text the indexer
uses (first 2 chunks). Dry run reads only and reports the tier changes.

The owner's per-document overrides (site config/doc_overrides.yaml, erla/overrides.py; F13) win over the
rules: their tier replaces the inferred one, and their title and year are written too (the full-text title
column is rewritten with the title). `only_overrides` applies the override file alone: documents without an
entry, and the doc_type of those with one, are left as they are.
"""
from __future__ import annotations

import collections
import csv
from pathlib import Path

from . import classify, overrides, safeio, store

FIELDS = ["doc_id", "rel_path", "old_type", "new_type", "old_tier", "new_tier", "old_title", "new_title",
          "old_year", "new_year", "override"]


def reclassify(con, write: bool, out_csv: Path | None = None, log=print, heartbeat=None, guard=None,
               agent_root: Path | None = None, only_overrides: bool = False) -> dict:
    classify._load_yaml.cache_clear()
    ov = overrides.load(agent_root) if agent_root is not None else overrides.Overrides()
    for p in ov.problems:
        log(f"override skipped: {p}")
    docs = con.execute("SELECT doc_id, rel_path, path, ext, title, year, doc_type, authority_tier "
                       "FROM documents WHERE extract_status='ok'").fetchall()
    changes, moves = [], collections.Counter()
    matched: set[str] = set()
    fv = store.fts_version(con) if write else None
    for i, d in enumerate(docs):
        rel = d["rel_path"] or d["path"]
        entry = ov.get(rel)
        if entry:
            matched.add(overrides.key(rel))
        if only_overrides and not entry:
            continue
        title = d["title"] or Path(rel).stem
        if only_overrides:
            new_type, tier = d["doc_type"] or "other", d["authority_tier"]
        else:
            rows = con.execute("SELECT text FROM chunks WHERE doc_id=? ORDER BY seq LIMIT 2",
                               (d["doc_id"],)).fetchall()
            head = "\n".join(r["text"] or "" for r in rows)
            cls = classify.classify_document(rel, (d["ext"] or "").lstrip("."), title, head)
            new_type, tier = cls["doc_type"], cls["authority_tier"]
        new_title, new_year, new_tier = ov.apply(rel, d["title"], d["year"], tier)
        if (new_tier, new_type, new_title, new_year) == (d["authority_tier"], d["doc_type"] or "other",
                                                         d["title"], d["year"]):
            continue
        changes.append({"doc_id": d["doc_id"], "rel_path": rel, "old_type": d["doc_type"], "new_type": new_type,
                        "old_tier": d["authority_tier"], "new_tier": new_tier, "old_title": d["title"],
                        "new_title": new_title, "old_year": d["year"], "new_year": new_year,
                        "override": "yes" if entry else ""})
        if new_tier != d["authority_tier"]:
            moves[(d["authority_tier"], new_tier)] += 1
        if write:
            if new_title != d["title"]:          # the FTS title column holds the title: rewrite this document's rows
                rows = [(int(c["chunk_id"]), c["text"]) for c in
                        con.execute("SELECT chunk_id, text FROM chunks WHERE doc_id=? ORDER BY seq", (d["doc_id"],))]
                store.fts_delete(con, rows, d["title"], d["path"], d["rel_path"], fv)
                store.fts_insert(con, rows, new_title, d["path"], d["rel_path"], fv)
            con.execute("UPDATE documents SET doc_type=?, authority_tier=?, title=?, year=? WHERE doc_id=?",
                        (new_type, new_tier, new_title, new_year, d["doc_id"]))
        if heartbeat and i % 200 == 0:
            heartbeat()
    if write:
        con.commit()
    if out_csv:
        if guard is None:
            raise ValueError("out_csv needs guard=<Config> (G1.2)")
        with safeio.open_write(guard, out_csv, newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            w.writeheader()
            w.writerows(changes)
    unmatched = sorted(k for k in ov.entries if k not in matched)
    for k in unmatched:
        log(f"override matches no indexed document: {k!r}")
    summary = {"documents": len(docs), "changed": len(changes),
               "tier_changes": sum(moves.values()),
               "moves": {f"{a}->{b}": n for (a, b), n in sorted(moves.items())},
               "overrides": {"entries": len(ov.entries), "matched": len(matched), "unmatched": unmatched,
                             "problems": list(ov.problems)},
               "only_overrides": only_overrides,
               "written": write}
    log(f"reclassify: {summary}")
    return summary
