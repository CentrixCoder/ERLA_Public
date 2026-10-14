"""Shared maintenance chain (K2): index -> deferred OCR -> PDF table backfill -> embeddings.

Used by ``erla refresh`` (cli) and by the MCP ``index_start`` background job, so both run the
same steps in the same order (skills S1 step 8). The caller owns the single-instance lock
(``logs/refresh.lock``) and passes its heartbeat; this module only does the work.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from . import store
from .config import Config


def run_chain(cfg: Config, *, full: bool = False, ocr_inline: bool = False, run_ocr: bool = True,
              run_tables: bool = True, run_embed: bool = True, ocr_limit: int | None = None,
              heartbeat: Callable[[], None] | None = None, log: Callable[[str], None] = print,
              on_phase: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Run the chain and return a summary dict (JSON-serialisable apart from ``default=str`` values)."""
    from . import specialists
    from .indexer import Indexer

    def phase(name: str) -> None:
        if on_phase:
            on_phase(name)

    summary: dict[str, Any] = {}
    phase("index")
    idx = Indexer(cfg, log=log, heartbeat=heartbeat)
    before, since = specialists.sha_snapshot(idx.con), specialists.now()
    st = idx.run(full=full, ocr=ocr_inline)
    summary["index"] = {k: v for k, v in st.as_dict().items() if k != "failures"}
    summary["index_failures"] = st.failures[:50]
    # S17: new documents on a specialist's topic open a review trigger for its skill file
    summary["specialist_additions"] = specialists.open_addition_triggers(
        cfg, specialists.topic_additions(idx.con, before, since))
    if run_ocr and cfg.ocr.enabled:
        phase("ocr")
        so = idx.run_ocr(limit=ocr_limit)
        summary["ocr"] = {k: v for k, v in so.as_dict().items() if k != "failures"}
        summary["ocr_failures"] = so.failures[:50]
    idx.close()

    # A3: a new property parser rebuilds every document's records from the stored table text
    # (index only, no library file is read; seconds for a large library)
    from . import props
    con = store.connect_writer(cfg)
    try:
        rebuilt = props.rebuild_if_stale(con, log=log)
    finally:
        con.close()
    if rebuilt:
        summary["properties_rebuilt"] = rebuilt

    budget = float((cfg.tables or {}).get("refresh_budget_minutes", 60))
    if run_tables and (cfg.tables or {}).get("enabled", True) and budget > 0:
        from . import tables
        phase("tables")
        con = store.connect_writer(cfg)
        try:
            res = tables.backfill(con, cfg, log=log, heartbeat=heartbeat, max_seconds=budget * 60)
        finally:
            con.close()
        summary["tables"] = {k: v for k, v in res.items() if k != "failures"}

    if run_embed and cfg.embeddings.get("enabled"):
        from .embed import embed_pending, export_matrix, prune_duplicate_embeddings
        phase("embed")
        con = store.connect_writer(cfg)
        try:
            summary["pruned_duplicate_embeddings"] = prune_duplicate_embeddings(con)
            summary["embedded_chunks"] = embed_pending(con, cfg, limit=None, log=log, progress=heartbeat)
            summary["vector_index"] = export_matrix(con, cfg, progress=heartbeat)
        finally:
            con.close()
    return summary
