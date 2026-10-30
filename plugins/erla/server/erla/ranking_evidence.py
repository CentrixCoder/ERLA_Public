"""Ranking changes need recorded evidence (owner, 2026-10-29; master-ai-skills LL-0065, L1, L8, L13).

`docs/decisions/ranking_evidence.yaml` lists every ranking default (search, BM25F, re-ranker, evidence thresholds,
models) and a fingerprint of every ranking function. `check()` compares that register with the code:

* every default and function is registered, and nothing registered has gone;
* the recorded value or fingerprint equals the code's;
* a value or function that differs from its `baseline` (the state when the register was created) needs
  evidence: at least one opened source (title, url, locator, opened date, what it supports), at least one
  measurement on this library (golden file, n, metric, before, after, date), and the owner's approval
  (`approved: {by, date}`; an agent cannot approve, L13).

`status: baseline` claims no evidence: the value is inherited, not justified. `tests/test_ranking_evidence.py`
runs `check()` in every `pytest` and `release.py check`; `python scripts/ranking_evidence.py update` records the
code's current state as `pending`, which fails until the evidence and approval are filled in.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import re
import textwrap
from datetime import date
from pathlib import Path
from typing import Any

import yaml

REGISTER = Path(__file__).resolve().parent.parent / "docs" / "decisions" / "ranking_evidence.yaml"
AGENTS = re.compile(r"\b(claude|assistant|agent|gpt|model|bot|ai)\b", re.I)

# Ranking functions: an edit to any of these changes how documents are ranked.
METHODS = {
    "search": ["_keyword", "_candidates", "_fuse", "search", "prior_factor", "final_score", "_rerank", "_mmr",
               "collapse_editions", "tier_first", "evidence", "expand", "bm25_params", "settings"],
    "bm25": ["idf", "phrase_scores", "rank", "search", "params"],
    "sparse": ["search", "_merge"],
    "embed": ["vector_search", "chunk_inputs", "windows", "_dedupe"],
    "rerank": ["score", "window", "selected"],
}


def _flat(prefix: str, v: Any, out: dict[str, Any]) -> None:
    if isinstance(v, dict):
        for k in sorted(v):
            _flat(f"{prefix}.{k}", v[k], out)
    else:
        out[prefix] = v


def code_defaults() -> dict[str, Any]:
    """Every tunable ranking default the code ships, flattened to dotted keys."""
    from . import bm25, embed, rerank, search, sparse
    out: dict[str, Any] = {}
    _flat("search", search.DEFAULTS, out)
    _flat("search.profiles", search.PROFILES, out)
    _flat("search.evidence", search.EVIDENCE_DEFAULTS, out)
    _flat("bm25", bm25.DEFAULTS, out)
    _flat("rerank.models", {k: {"model": v.get("model"), "temperature": v.get("temperature")}
                            for k, v in rerank.RERANKERS.items()}, out)
    out["rerank.default"] = rerank.DEFAULT
    out["embed.model"] = embed.DEFAULT_MODEL
    out["sparse.model"] = sparse.DEFAULT_MODEL
    return out


def fingerprint(fn: Any) -> str:
    """Hash of a function's syntax tree without docstrings: comments and layout don't count, code does."""
    tree = fn if isinstance(fn, ast.AST) else ast.parse(textwrap.dedent(inspect.getsource(fn)))
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and body
                and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant)
                and isinstance(body[0].value.value, str)):
            node.body = body[1:] or [ast.Pass()]
    return hashlib.sha256(_dump(tree).encode()).hexdigest()[:16]


def _dump(node: Any) -> str:
    """ast.dump that is the same on every Python version: ast.dump's output changed in 3.13 (empty and None
    fields omitted) and new fields appear (type_params in 3.12), so None and empty fields are always skipped."""
    if isinstance(node, ast.AST):
        parts = []
        for f in node._fields:
            v = getattr(node, f, None)
            if v is None or v == []:
                continue
            parts.append(f"{f}={_dump(v)}")
        return f"{type(node).__name__}({','.join(parts)})"
    if isinstance(node, list):
        return "[" + ",".join(_dump(x) for x in node) + "]"
    return repr(node)


def code_methods(root: Path | None = None) -> dict[str, str]:
    """Fingerprints read from the source files (no import), so every Python version and platform agrees."""
    root = root or Path(__file__).resolve().parent
    out = {}
    for mod, names in METHODS.items():
        tree = ast.parse((root / f"{mod}.py").read_text(encoding="utf-8"))
        defs = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        for n in names:
            if n not in defs:
                raise KeyError(f"ranking function erla.{mod}.{n} not found (renamed? update METHODS)")
            out[f"{mod}.{n}"] = fingerprint(defs[n])
    return out


def load(path: Path = REGISTER) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _is_date(v: Any) -> bool:
    if isinstance(v, date):
        return True
    return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(v or "")))


def _evidence_problems(key: str, e: dict[str, Any]) -> list[str]:
    ev = e.get("evidence") or []
    srcs = [x for x in ev if isinstance(x, dict) and x.get("kind") == "source"]
    meas = [x for x in ev if isinstance(x, dict) and x.get("kind") == "measurement"]
    bad: list[str] = []
    good_src = [x for x in srcs if str(x.get("url") or "").startswith(("http://", "https://")) and x.get("title")
                and str(x.get("locator") or "").strip() and _is_date(x.get("opened")) and x.get("supports")]
    if not good_src:
        bad.append(f"{key}: changed from its baseline without an opened source "
                   "(kind: source, title, url, locator, opened: YYYY-MM-DD, supports)")
    good_meas = [x for x in meas if x.get("golden") and isinstance(x.get("n"), int) and x["n"] > 0 and x.get("metric")
                 and x.get("before") is not None and x.get("after") is not None and _is_date(x.get("date"))]
    if not good_meas:
        bad.append(f"{key}: changed from its baseline without a measurement on this library "
                   "(kind: measurement, golden, n, metric, before, after, date)")
    ap = e.get("approved") or {}
    who = str(ap.get("by") or "").strip()
    if not who or AGENTS.search(who) or not _is_date(ap.get("date")):
        bad.append(f"{key}: changed from its baseline without the owner's approval (approved: by <owner>, date); "
                   "an agent cannot approve (L13)")
    return bad


def _norm(v: Any) -> Any:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else v


def check(reg: dict[str, Any] | None = None, defaults: dict[str, Any] | None = None,
          methods: dict[str, str] | None = None) -> list[str]:
    """Problems between the register and the code; [] when every change is backed by evidence."""
    reg = load() if reg is None else reg
    defaults = code_defaults() if defaults is None else defaults
    methods = code_methods() if methods is None else methods
    out: list[str] = []
    for section, code, field in (("defaults", defaults, "value"), ("methods", methods, "fingerprint")):
        entries = reg.get(section) or {}
        for k in sorted(set(code) - set(entries)):
            out.append(f"{section}: {k} is not in the register; add it with its evidence "
                       "(python scripts/ranking_evidence.py update, then fill in evidence and approval)")
        for k in sorted(set(entries) - set(code)):
            out.append(f"{section}: {k} is registered but no longer in the code; record the removal and its evidence")
        for k in sorted(set(code) & set(entries)):
            e = entries[k] or {}
            if _norm(e.get(field)) != _norm(code[k]):
                out.append(f"{section}: {k} is {code[k]!r} in the code but {e.get(field)!r} in the register")
                continue
            if e.get("status") == "pending":
                out.append(f"{section}: {k} is pending: evidence and the owner's approval are missing")
            if _norm(e.get(field)) != _norm(e.get("baseline")):
                out += [f"{section}: {p}" for p in _evidence_problems(k, e)]
            elif e.get("status") not in ("baseline", "cited", "measured"):
                out.append(f"{section}: {k} has status {e.get('status')!r} (baseline, cited or measured)")
            if e.get("status") in ("cited", "measured") and not (e.get("evidence") or []):
                out.append(f"{section}: {k} says {e['status']} but lists no evidence")
    return out


def updated(reg: dict[str, Any]) -> list[str]:
    """Record the code's current values and fingerprints in `reg` (in place; the file is written by
    scripts/ranking_evidence.py, since the package writes only through safeio). New or changed entries become
    `pending`, which fails `check()` until a person adds the evidence and approval; others are kept."""
    changed = []
    for section, code, field in (("defaults", code_defaults(), "value"), ("methods", code_methods(), "fingerprint")):
        entries = reg.setdefault(section, {}) or {}
        reg[section] = entries
        for k, v in code.items():
            e = entries.get(k)
            if e is None:
                entries[k] = {field: v, "baseline": v, "status": "pending"}
                changed.append(k)
            elif _norm(e.get(field)) != _norm(v):
                e[field] = v
                e["status"] = "pending"
                changed.append(k)
        for k in [k for k in entries if k not in code]:
            entries[k]["status"] = "pending"
            changed.append(k)
    return changed
