"""Source memory (0.4.0): which documents verified answers on which topics, so later searches rank them higher.

Every value or quote that verify_packet PASSes at a document's locator is evidence that the document answers
that kind of question. It is appended to ``logs/source_memory.jsonl`` (G1.2: logs only) as the topic terms of
the item (quantity, material, temper, form, direction, basis, row), the document's content hash and the
locator, with the verification_id as evidence. A search whose query covers a remembered topic raises the
matching documents' scores by at most MAX_BOOST.

What the memory can never do (tests/test_memory.py):
- add a document to the results: it only re-weights documents the search already found and kept;
- undo a filter: tier, type and restricted-document (G2) filters run before it;
- outlive its evidence: entries follow the content hash, so an edited or replaced file loses its memory,
  and ``erla memory forget <id>`` appends a tombstone the reader honours.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

FILE = "source_memory.jsonl"
MAX_BOOST = 0.30          # at most +30 % on a document's fused score
MIN_COVERAGE = 0.6        # share of a remembered topic's terms the query must contain
MIN_TERMS = 2
TOPIC_KEYS = ("quantity", "material", "temper", "condition", "form", "direction", "basis", "row")
STOP = frozenset("a an and are as at be by for from in is it of on or the to with what which how per vs".split())
TOKEN = re.compile(r"[a-z0-9][a-z0-9.\-/]*")
_CACHE: dict[str, Any] = {}


def terms(text: str) -> frozenset[str]:
    return frozenset(t.strip(".-/") for t in TOKEN.findall(text.lower())
                     if len(t.strip(".-/")) >= 2 and t not in STOP)


def _path(cfg) -> Path:
    return Path(cfg.log_dir) / FILE


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def record_verified(cfg, con, items: list[dict[str, Any]], results: list[dict[str, Any]],
                    verification_id: str) -> int:
    """Append one entry per PASSed value or quote item with a document locator; returns how many."""
    from . import safeio
    rows: list[str] = []
    for item, res in zip(items, results):
        if res.get("status") != "PASS" or res.get("kind") not in ("value", "quote"):
            continue
        doc_id, locator = item.get("doc_id"), str(item.get("locator") or "").strip()
        if doc_id is None or not locator:
            continue
        topic = terms(" ".join(str(item.get(k) or "") for k in TOPIC_KEYS))
        if len(topic) < MIN_TERMS:
            continue
        row = con.execute("SELECT sha256 FROM documents WHERE doc_id=?", (int(doc_id),)).fetchone()
        if not row or not row[0]:
            continue
        ts = _now()
        entry = {"ts": ts, "kind": "verified", "topic": sorted(topic), "sha256": row[0], "doc_id": int(doc_id),
                 "locator": locator, "evidence": verification_id}
        entry["id"] = hashlib.sha256(json.dumps(entry, sort_keys=True).encode()).hexdigest()[:10]
        rows.append(json.dumps(entry))
    if rows:
        safeio.append_text_at(safeio.check(cfg, _path(cfg)), "\n".join(rows) + "\n")
    return len(rows)


def forget(cfg, entry_id: str) -> bool:
    from . import safeio
    if not any(e["id"] == entry_id for e in load(cfg)):
        return False
    safeio.append_text_at(safeio.check(cfg, _path(cfg)),
                          json.dumps({"ts": _now(), "kind": "forget", "id": entry_id}) + "\n")
    return True


def load(cfg) -> list[dict[str, Any]]:
    """Live entries (tombstoned ones dropped), cached until the file changes."""
    p = _path(cfg)
    if not p.is_file():
        return []
    key = f"{p}|{p.stat().st_mtime_ns}|{p.stat().st_size}"
    if _CACHE.get("key") != key:
        live: dict[str, dict[str, Any]] = {}
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if not isinstance(e, dict) or not e.get("id"):
                continue
            if e.get("kind") == "forget":
                live.pop(str(e["id"]), None)
            elif e.get("kind") == "verified" and e.get("sha256") and e.get("topic"):
                live[str(e["id"])] = e
        _CACHE.update(key=key, entries=list(live.values()))
    return list(_CACHE["entries"])


def prior(cfg, con, query: str, doc_ids: list[int]) -> dict[int, dict[str, Any]]:
    """Score factor and evidence for each candidate document the memory knows on this query's topic."""
    entries = load(cfg) if cfg is not None else []
    if not entries or not doc_ids:
        return {}
    q = terms(query)
    sha_of = {}
    for i in range(0, len(doc_ids), 500):
        part = doc_ids[i:i + 500]
        sha_of.update({r[0]: r[1] for r in con.execute(
            f"SELECT doc_id, sha256 FROM documents WHERE doc_id IN ({','.join('?' * len(part))})", part)})
    by_sha: dict[str, list[int]] = {}
    for d, s in sha_of.items():
        if s:
            by_sha.setdefault(s, []).append(d)
    out: dict[int, dict[str, Any]] = {}
    for e in entries:
        docs = by_sha.get(e["sha256"])
        if not docs:
            continue
        topic = set(e["topic"])
        cover = len(topic & q) / len(topic)
        if cover < MIN_COVERAGE:
            continue
        for d in docs:
            m = out.setdefault(d, {"count": 0, "coverage": 0.0, "locators": [], "evidence": []})
            m["count"] += 1
            m["coverage"] = max(m["coverage"], cover)
            if e["locator"] not in m["locators"]:
                m["locators"].append(e["locator"])
            m["evidence"].append(e["evidence"])
    for m in out.values():
        m["factor"] = round(1 + MAX_BOOST * min(1.0, m["count"] / 3) * m["coverage"], 4)
        m["locators"], m["evidence"] = m["locators"][:5], sorted(set(m["evidence"]))[-5:]
    return out
