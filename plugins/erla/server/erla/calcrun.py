"""Calculation runs of the installed specialist tools (law L1: the model proposes, code verifies).

A specialist module's ``run(task, params)`` returns its results; ``record`` stores the run under a
``calc_id`` in ``logs/<tool>_runs.jsonl`` so verify_packet can check every reported number against it.
Only the server writes these logs (no model-facing write).
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable
from pathlib import Path
from typing import Any, cast


NOT_RESULTS = ("inputs", "units", "path", "convergence", "equations", "warnings", "label", "module")


def numeric_outputs(out: dict[str, Any]) -> dict[str, float]:
    """The result numbers of a run, flattened to names (``a_crit``, ``N``, ``rows[2].S_residual``).

    verify_packet checks a reported COMPUTED value against these (0.2.19), so a number the model
    computed by hand cannot pass as a calculation result."""
    flat: dict[str, float] = {}

    def walk(name: str, v: Any) -> None:
        if isinstance(v, bool) or v is None:
            return
        if isinstance(v, (int, float)):
            if math.isfinite(float(v)):
                flat[name] = float(v)
        elif isinstance(v, dict):
            d = cast(dict[str, Any], v)
            for k, x in d.items():
                walk(f"{name}.{k}" if name else str(k), x)
        elif isinstance(v, (list, tuple)):
            seq = list(cast(Iterable[Any], v))
            for i, x in enumerate(seq):
                walk(f"{name}[{i}]", x)

    for k, v in out.items():
        if k not in NOT_RESULTS:
            walk(k, v)
    return flat


def log_name(spec: dict[str, Any]) -> str:
    return f"{spec['tool']}_runs.jsonl"


def record(cfg: Any, spec: dict[str, Any], task: str, params: dict[str, Any]) -> dict[str, Any]:
    """Run ``spec``'s calculation and record it; the result carries ``calc_id`` and ``verify_with``."""
    from . import report
    out: dict[str, Any] = spec["run"](task, params)
    if "error" in out:
        return out
    outputs = numeric_outputs(out)
    calc_id = spec["id_prefix"] + hashlib.sha256(json.dumps(
        {"task": task, "inputs": out.get("inputs"), "units": out.get("units"), "outputs": outputs},
        sort_keys=True, default=str).encode()).hexdigest()[:12]
    report.append_log(cfg, {"cmd": f"mcp.{spec['tool']}", "calc_id": calc_id, "task": task,
                            "inputs": out.get("inputs"), "units": out.get("units"), "module": out.get("module"),
                            "outputs": outputs}, log_name(spec))
    out["calc_id"] = calc_id
    out["verify_with"] = {"kind": "computed", "calc_id": calc_id, "calc_output": sorted(outputs)[:40],
                          "note": spec.get("verify_note") or "verify_packet item per reported number: "
                                                             "calc_output is one of these names"}
    return out


def runs(log_dir: Path, specs: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Recorded runs of the given specialists' tools, by calc_id."""
    found: dict[str, dict[str, Any]] = {}
    for spec in specs:
        p = log_dir / log_name(spec)
        if not p.is_file():
            continue
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict) and cast(dict[str, Any], rec).get("calc_id"):
                found[str(cast(dict[str, Any], rec)["calc_id"])] = cast(dict[str, Any], rec)
    return found
