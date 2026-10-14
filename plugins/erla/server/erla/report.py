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


def feedback_boosts(cfg: Config) -> dict[int, float]:
    """Per-document multiplier from logged feedback (S15): +5 % per 'useful',
    -5 % per 'not_useful', clamped to [0.75, 1.25]."""
    p = cfg.log_dir / "feedback.jsonl"
    if not p.is_file():
        return {}
    score: dict[int, int] = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        d = int(r.get("doc_id", -1))
        score[d] = score.get(d, 0) + (1 if r.get("verdict") == "useful" else -1 if r.get("verdict") == "not_useful" else 0)
    return {d: max(0.75, min(1.25, 1 + 0.05 * s)) for d, s in score.items()}
