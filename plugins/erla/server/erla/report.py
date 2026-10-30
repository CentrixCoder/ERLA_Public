"""Citation formatting, result rendering, retrieval log, feedback (S13, rules §7)."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import safeio
from .config import Config


def citation(hit: dict, sid: str) -> str:
    rev = f", rev {hit['revision']}" if hit.get("revision") else ""
    std = f"{hit['standard_id']}{rev} — " if hit.get("standard_id") else ""
    year = f", {hit['year']}" if hit.get("year") else ""
    ocr = " [OCR]" if hit.get("ocr_used") else ""
    flag = f" [{hit['flag']}]" if hit.get("flag") else ""
    loc = "; ".join(hit.get("locators", [])[:3])
    return f"[{sid}] {std}*{hit['title']}*{year}, {loc}{ocr}{flag}. Path: {hit['path']}"


def render_markdown(query: str, hits: list[dict], expanded: list[str], mode: str, index_date: str | None) -> str:
    lines = [f"## Search results — {query}",
             f"Index date: {index_date or 'unknown'}    Match mode: {mode}    Hits: {len(hits)}",
             f"Expanded queries: {'; '.join(expanded) or '(none)'}", "",
             "| Rank | ID | Doc | Title / Std / Rev | Type | Tier | Flag | Dup | Locators | Path |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for i, h in enumerate(hits, 1):
        std = f"{h['standard_id'] or ''} {h['revision'] or ''}".strip()
        title = (h["title"] or "")[:70].replace("|", "/")
        lines.append(f"| {i} | S{i} | {h['doc_id']} | {title}{' — ' + std if std else ''} | {h['ext']} | "
                     f"{h['tier']} | {h['flag'] or ''} | {h['duplicates'] or ''} | "
                     f"{'; '.join(h['locators'])[:60]} | {h['rel_path'][:90].replace('|', '/')} |")
    warn = [(i, w) for i, h in enumerate(hits, 1) for w in (h.get("warnings") or [])]
    rr = [(i, h["rerank"]) for i, h in enumerate(hits, 1) if h.get("rerank") is not None]
    if rr:
        lines.append("")
        lines.append("Re-ranker relevance (0-1): " + ", ".join(f"S{i} {v:.2f}" for i, v in rr))
    if warn:
        lines.append("")
        lines.append("### Data-quality warnings (A5/A6)")
        lines += [f"- S{i} {w}" for i, w in warn]
    lines.append("")
    lines.append("### Snippets")
    for i, h in enumerate(hits, 1):
        for loc, snip in zip(h["locators"], h["snippets"]):
            lines.append(f"- **S{i} {loc}**: {snip[:300].replace(chr(10), ' ')}")
    lines.append("")
    lines.append("### Citations")
    for i, h in enumerate(hits, 1):
        lines.append(citation(h, f"S{i}"))
    return "\n".join(lines)


def export(cfg: Config, name: str, payload: dict, hits: list[dict]) -> list[Path]:
    """Write <name>.json and <name>.csv to out/ (G1.2)."""
    jp = safeio.write_text(cfg, cfg.out_dir / f"{name}.json", json.dumps(payload, indent=2, default=str))
    cp = safeio.check(cfg, cfg.out_dir / f"{name}.csv")
    with safeio.open_at(cp, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["rank", "doc_id", "title", "standard_id", "revision", "ext", "tier", "flag",
                    "year", "duplicates", "locators", "path"])
        for i, h in enumerate(hits, 1):
            w.writerow([i, h["doc_id"], h["title"], h["standard_id"], h["revision"], h["ext"], h["tier"],
                        h["flag"], h["year"], h["duplicates"], "; ".join(h["locators"]), h["path"]])
    return [jp, cp]


def append_log(cfg: Config, record: dict[str, Any], name: str = "retrieval_log.jsonl") -> Path:
    record = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), **record}
    return safeio.append_jsonl(cfg, cfg.log_dir / name, record)


FEEDBACK_STEP = 0.05            # +5 % per 'useful', -5 % per 'not_useful'
FEEDBACK_CLAMP = (0.75, 1.25)
FEEDBACK_MIN_COVERAGE = 0.6     # share of a vote's topic terms the query must contain (as memory.MIN_COVERAGE)
_FB_CACHE: dict[str, Any] = {}


def feedback_record(con, doc_id: int, verdict: str, note: str = "", query: str = "") -> dict[str, Any]:
    """The feedback line to log (S15, 0.7.0): the document's content hash, so the vote follows the file
    and not its index row, and the question's topic terms, so the vote applies to that topic."""
    from . import memory
    rec: dict[str, Any] = {"doc_id": int(doc_id), "verdict": verdict, "note": note}
    if con is not None:
        row = con.execute("SELECT sha256 FROM documents WHERE doc_id=?", (int(doc_id),)).fetchone()
        if row and row[0]:
            rec["sha256"] = row[0]
    if query:
        rec["query"] = query
        rec["topic"] = sorted(memory.terms(query))
    return rec


def _feedback_lines(cfg: Config) -> list[dict[str, Any]]:
    p = Path(cfg.log_dir) / "feedback.jsonl"
    if not p.is_file():
        return []
    st = p.stat()
    key = f"{p}|{st.st_mtime_ns}|{st.st_size}"
    if _FB_CACHE.get("key") != key:
        rows = []
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(r, dict) and r.get("verdict") in ("useful", "not_useful"):
                rows.append(r)
        _FB_CACHE.update(key=key, rows=rows)
    return list(_FB_CACHE["rows"])


def feedback_boosts(cfg: Config, con=None, query: str | None = None) -> dict[int, float]:
    """Per-document multiplier from logged feedback (S15): +5 % per 'useful', -5 % per 'not_useful',
    clamped to [0.75, 1.25].

    0.7.0 (audit F5): a vote follows the document's content hash. A re-extracted document gets a new
    doc_id, so a doc_id-only vote either vanished or, when the row id was reused, moved to another file.
    A vote that names a topic counts only for queries covering FEEDBACK_MIN_COVERAGE of it. Votes logged
    before 0.7.0 (doc_id only) still count when `con` shows the same index row existed when the vote was
    logged (indexed_at not later than the vote); otherwise they are ignored."""
    rows = _feedback_lines(cfg)
    if not rows:
        return {}
    if con is None:                                   # no index to resolve against: the 0.6 behaviour
        score: dict[int, int] = {}
        for r in rows:
            try:
                d = int(r.get("doc_id", -1))
            except (TypeError, ValueError):
                continue
            score[d] = score.get(d, 0) + (1 if r["verdict"] == "useful" else -1)
        return {d: max(FEEDBACK_CLAMP[0], min(FEEDBACK_CLAMP[1], 1 + FEEDBACK_STEP * s)) for d, s in score.items()}
    from . import memory
    q = memory.terms(query or "")
    by_sha: dict[str, list[int]] = {}
    for d, s in con.execute("SELECT doc_id, sha256 FROM documents WHERE sha256 IS NOT NULL"):
        by_sha.setdefault(s, []).append(d)
    indexed = {d: t for d, t in con.execute("SELECT doc_id, indexed_at FROM documents")}
    score = {}
    for r in rows:
        topic = set(r.get("topic") or [])
        if topic and (not q or len(topic & q) / len(topic) < FEEDBACK_MIN_COVERAGE):
            continue
        if r.get("sha256"):
            docs = by_sha.get(str(r["sha256"]), [])
        else:
            try:
                d = int(r.get("doc_id", -1))
            except (TypeError, ValueError):
                continue
            when, ts = indexed.get(d), str(r.get("ts") or "")
            docs = [d] if when is not None and ts and str(when) <= ts else []
        for d in docs:
            score[d] = score.get(d, 0) + (1 if r["verdict"] == "useful" else -1)
    return {d: max(FEEDBACK_CLAMP[0], min(FEEDBACK_CLAMP[1], 1 + FEEDBACK_STEP * s)) for d, s in score.items()}
