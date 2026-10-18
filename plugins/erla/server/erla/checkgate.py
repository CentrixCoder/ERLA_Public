"""Blind independent check, enforced in code (ADR-0002 Phase 3; laws L1, L3).

The lead opens a check with its draft (``open_check``) and gets back Part 1 of the Check Brief, which
holds none of its values. The checker records its own estimates (``estimate``) before it can see the
lead's values (``reveal``), and the comparison is computed here, not by a model. ``close`` records the
verdict and refuses one that is not consistent with that comparison. ``log_request`` and
``export_packet`` refuse a packet the user's check policy says must be checked unless it carries a
closed check (or a stated reason it was not checked).

Who may call what is a permission, not a promise: in the plugin the lead's tools allowlist leaves out
``check_estimate``, ``check_reveal`` and ``check_close``, which only the Professor holds. Where one
model holds every tool (an MCP client without sub-agents), the check is recorded as a self-check.

States: open -> estimated -> revealed -> closed. Records live in ``logs/checks/<check_id>.json``.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from . import safeio
from .config import Config, check_policy

VERDICTS = ("PASS", "PASS WITH FINDINGS", "FAIL")
CHECKERS = ("erla:professor", "self-check")
AGREE, EXPLAIN = 0.02, 0.05          # instructions Step 6: within 2 % agrees; 2-5 % needs a reason
OUT = ("out of tolerance", "units differ")
NUM = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")
ID = re.compile(r"^k-[0-9a-f]{12}$")


class CheckError(ValueError):
    """A request the check gate refuses; the message says what to do instead."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _path(cfg: Config, check_id: str) -> Path:
    if not ID.match(str(check_id)):
        raise CheckError(f"unknown check_id {check_id!r}")
    return cfg.log_dir / "checks" / f"{check_id}.json"


def load(cfg: Config, check_id: str) -> dict[str, Any]:
    p = _path(cfg, check_id)
    if not p.is_file():
        raise CheckError(f"unknown check_id {check_id!r}")
    return json.loads(p.read_text(encoding="utf-8"))


def _save(cfg: Config, rec: dict[str, Any]) -> None:
    safeio.write_text(cfg, _path(cfg, rec["check_id"]), json.dumps(rec, indent=1, ensure_ascii=False))


def _number(v: Any, what: str) -> float:
    if isinstance(v, bool):
        raise CheckError(f"{what}: a number is required, got {v!r}")
    if isinstance(v, (int, float)):
        return float(v)
    m = NUM.search(str(v))
    if not m:
        raise CheckError(f"{what}: a number is required, got {v!r}")
    return float(m.group(0))


def _obj(x: Any) -> dict[str, Any] | None:
    """``x`` as a JSON object, or None: tool arguments come from a model and are checked, not trusted."""
    return cast(dict[str, Any], x) if isinstance(x, dict) else None


def _list(x: Any) -> list[Any] | None:
    return cast(list[Any], x) if isinstance(x, list) else None


def open_check(cfg: Config, packet_in: Any) -> dict[str, Any]:
    """Store the lead's draft and return the check_id and Part 1 of the brief (no lead values)."""
    packet = _obj(packet_in)
    if packet is None or not str(packet.get("question") or "").strip():
        raise CheckError("packet needs 'question' and 'quantities'")
    qs = _list(packet.get("quantities"))
    if not qs:
        raise CheckError("packet needs at least one quantity: {name, value, units} for each value to check")
    names: list[str] = []
    for i, q_in in enumerate(qs):
        q = _obj(q_in)
        if q is None or not str(q.get("name") or "").strip() or not str(q.get("units") or "").strip():
            raise CheckError(f"quantity {i}: name and units are required")
        _number(q.get("value"), f"quantity {q['name']!r}")
        names.append(str(q["name"]).strip())
    if len(set(names)) != len(names):
        raise CheckError("quantity names must be unique")
    body = json.dumps(packet, sort_keys=True, ensure_ascii=False, default=str)
    cid = "k-" + hashlib.sha256(f"{body}|{_now()}".encode()).hexdigest()[:12]
    rec: dict[str, Any] = {"check_id": cid, "state": "open", "opened": _now(), "packet": packet, "estimates": [], "comparison": []}
    _save(cfg, rec)
    return {"check_id": cid, "brief_part1": brief_part1(packet),
            "next": "send check_id and brief_part1 to the professor agent; it calls check_estimate, "
                    "check_reveal and check_close"}


def brief_part1(packet: dict[str, Any]) -> dict[str, Any]:
    """Part 1 of the Check Brief: everything the checker needs to estimate, none of the lead's results."""
    keep = ("question", "inputs", "sources", "method", "discipline")
    out = {k: packet[k] for k in keep if k in packet}
    out["quantities"] = [{"name": q["name"], "units": q["units"]} for q in packet["quantities"]]
    return out


def estimate(cfg: Config, check_id: str, estimates: Any) -> dict[str, Any]:
    """The checker's own values, one per quantity (or ``not_checked`` with a reason), before the reveal."""
    rec = load(cfg, check_id)
    if rec["state"] not in ("open", "estimated"):
        raise CheckError(f"check {check_id} is {rec['state']}: estimates are fixed once the lead's values are revealed")
    want = [q["name"] for q in rec["packet"]["quantities"]]
    items = _list(estimates)
    if items is None:
        raise CheckError("estimates must be a list")
    got: dict[str, dict[str, Any]] = {}
    for e_in in items:
        e = _obj(e_in) or {}
        name = str(e.get("name") or "").strip()
        if name not in want:
            raise CheckError(f"estimate for unknown quantity {name!r}; quantities: {want}")
        if name in got:
            raise CheckError(f"two estimates for {name!r}")
        if str(e.get("not_checked") or "").strip():
            got[name] = {"name": name, "not_checked": str(e["not_checked"]).strip()}
            continue
        if not str(e.get("method") or "").strip() or not str(e.get("units") or "").strip():
            raise CheckError(f"estimate {name!r}: value, units and method are required (or not_checked with a reason)")
        got[name] = {"name": name, "value": _number(e.get("value"), f"estimate {name!r}"),
                     "units": str(e["units"]).strip(), "method": str(e["method"]).strip()}
    missing = [n for n in want if n not in got]
    if missing:
        raise CheckError(f"no estimate for {missing}: estimate each quantity or give not_checked with a reason")
    rec.update(state="estimated", estimates=[got[n] for n in want], estimated=_now())
    _save(cfg, rec)
    return {"check_id": check_id, "state": "estimated", "next": "check_reveal"}


def _compare(lead: dict[str, Any], est: dict[str, Any]) -> dict[str, Any]:
    from . import units
    row: dict[str, Any] = {"name": lead["name"], "lead": lead["value"], "units": lead["units"]}
    if "not_checked" in est:
        return {**row, "status": "not checked", "reason": est["not_checked"]}
    lv, ev = _number(lead["value"], lead["name"]), float(est["value"])
    if est["units"].strip() != str(lead["units"]).strip():
        try:
            ev = units.convert(ev, est["units"], str(lead["units"])).value
        except ValueError:
            return {**row, "estimate": est["value"], "estimate_units": est["units"], "status": "units differ"}
    diff = 0.0 if lv == ev else abs(ev - lv) / max(abs(lv), 1e-12)
    status = "agrees" if diff <= AGREE else ("explain" if diff <= EXPLAIN else "out of tolerance")
    return {**row, "estimate": round(ev, 10), "method": est["method"], "diff": round(diff, 5), "status": status}


def reveal(cfg: Config, check_id: str) -> dict[str, Any]:
    """The lead's values and conclusion, with the comparison computed here. Refused before estimates."""
    rec = load(cfg, check_id)
    if rec["state"] == "open":
        raise CheckError("record your own estimate of every quantity with check_estimate first; the lead's values "
                         "stay hidden until then")
    if rec["state"] == "estimated":
        est = {e["name"]: e for e in rec["estimates"]}
        rec["comparison"] = [_compare(q, est[q["name"]]) for q in rec["packet"]["quantities"]]
        rec.update(state="revealed", revealed=_now())
        _save(cfg, rec)
    p = rec["packet"]
    return {"check_id": check_id, "quantities": p["quantities"], "assumptions": p.get("assumptions", []),
            "conclusion": p.get("conclusion", ""), "comparison": rec["comparison"],
            "tolerances": {"agrees": AGREE, "explain": EXPLAIN}}


def close(cfg: Config, check_id: str, verdict: str, findings: Any = None,
          checker: str = "erla:professor", notes: Any = None) -> dict[str, Any]:
    """Record the verdict; refuse one that the computed comparison contradicts. ``notes`` are minor remarks
    that change nothing (a reason for a 2-5 % difference, a presentation point); they never make a PASS
    impossible, so PASS keeps its meaning (2026-10-17 calibration: no control got a clean PASS)."""
    rec = load(cfg, check_id)
    if rec["state"] != "revealed":
        raise CheckError(f"check {check_id} is {rec['state']}: close it after check_reveal")
    verdict = str(verdict).strip().upper()
    if verdict not in VERDICTS:
        raise CheckError(f"verdict must be one of {list(VERDICTS)}")
    if checker not in CHECKERS:
        raise CheckError(f"checker must be one of {list(CHECKERS)}")
    raw = _list(findings) if findings is not None else []
    fs = [f for f in (_obj(x) for x in raw or []) if f is not None]
    if raw is None or len(fs) != len(raw):
        raise CheckError("each finding is an object: {quantity?, item, changes_answer, evidence, action}")
    nraw = _list(notes) if notes is not None else []
    ns = [n for n in (_obj(x) for x in nraw or []) if n is not None and str(n.get("note") or "").strip()]
    if nraw is None or len(ns) != len(nraw):
        raise CheckError("each note is an object: {quantity?, note}")
    named = {str(f.get("quantity") or "").strip() for f in fs}
    noted = named | {str(n.get("quantity") or "").strip() for n in ns}
    cmp_ = rec["comparison"]
    unexplained = [c["name"] for c in cmp_ if c["status"] in OUT and c["name"] not in named]
    if unexplained:
        raise CheckError(f"{unexplained} differ beyond tolerance or in units: add a finding for each (quantity = its "
                         "name) saying which value is right and why")
    changes = [f for f in fs if f.get("changes_answer") is True]
    unreasoned = [c["name"] for c in cmp_ if c["status"] == "explain" and c["name"] not in noted]
    if unreasoned:
        raise CheckError(f"{unreasoned} differ by 2-5 %: give each a note or a finding (quantity = its name) "
                         "with the reason")
    if verdict == "PASS" and (fs or any(c["status"] not in ("agrees", "explain") for c in cmp_)):
        raise CheckError("PASS needs every quantity checked and within tolerance and no findings (minor remarks "
                         "go in notes); use PASS WITH FINDINGS or FAIL")
    if verdict == "PASS WITH FINDINGS" and (not fs or changes):
        raise CheckError("PASS WITH FINDINGS needs at least one finding and none that changes the answer "
                         "(that is a FAIL)")
    if verdict == "FAIL" and not changes:
        raise CheckError("FAIL needs at least one finding with changes_answer: true")
    rec.update(state="closed", closed=_now(), verdict=verdict, findings=fs, notes=ns, checker=checker)
    _save(cfg, rec)
    from .report import append_log
    append_log(cfg, {"cmd": "mcp.check", "check_id": check_id, "verdict": verdict, "checker": checker,
                     "n_quantities": len(cmp_), "n_out": sum(c["status"] in OUT for c in cmp_),
                     "n_not_checked": sum(c["status"] == "not checked" for c in cmp_), "n_findings": len(fs)})
    return {"check_id": check_id, "state": "closed", "verdict": verdict, "checker": checker,
            "comparison": cmp_, "findings": fs, "notes": ns}


def status(cfg: Config, check_id: str) -> dict[str, Any]:
    """What the lead may see: the state, and once closed the verdict, comparison and findings."""
    rec = load(cfg, check_id)
    out: dict[str, Any] = {"check_id": check_id, "state": rec["state"]}
    if rec["state"] == "closed":
        out.update(verdict=rec["verdict"], checker=rec["checker"], comparison=rec["comparison"],
                   findings=rec["findings"], notes=rec.get("notes", []))
    return out


def _calc_id_pattern() -> re.Pattern[str] | None:
    from .specialists import SPECIALISTS
    prefixes = sorted({re.escape(str(s["id_prefix"])) for s in SPECIALISTS.values() if s.get("id_prefix")})
    return re.compile(rf"\b(?:{'|'.join(prefixes)})[0-9a-f]{{12}}\b") if prefixes else None


def _verification_computed(cfg: Config, verification_id: str) -> bool:
    p = cfg.log_dir / "retrieval_log.jsonl"
    if not verification_id or not p.is_file():
        return False
    for line in reversed(p.read_text(encoding="utf-8", errors="replace").splitlines()[-5000:]):
        if verification_id in line:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("cmd") == "mcp.verify" and r.get("verification_id") == verification_id:
                return int(r.get("n_computed") or 0) > 0
    return False


def needs_check_record(cfg: Config, record: dict[str, Any]) -> bool:
    """Does the check policy require a check for the packet ``log_request`` is recording?"""
    pol = check_policy(cfg)
    if pol == "off" or record.get("values_reported") == 0:
        return False
    if pol == "always":
        return True
    return bool(record.get("discipline") or record.get("skills_loaded") or record.get("calc_ids")
                or record.get("design_value") or _verification_computed(cfg, str(record.get("verification_id") or "")))


def needs_check_text(cfg: Config, markdown: str) -> bool:
    """Does the check policy require a check for the packet ``export_packet`` is writing?"""
    pol = check_policy(cfg)
    if pol == "off":
        return False
    if pol == "always":
        return True
    pat = _calc_id_pattern()
    return bool(re.search(r"\bCOMPUTED\b", markdown) or (pat and pat.search(markdown)))


def gate(cfg: Config, needed: bool, check_id: str, unchecked_reason: str) -> dict[str, Any]:
    """What to record about the check, or raise when the policy requires one and none is given."""
    if check_id:
        st = status(cfg, check_id)
        if st["state"] != "closed":
            raise CheckError(f"check {check_id} is {st['state']}, not closed: finish it (instructions W9) first")
        return {"check_id": check_id, "check_verdict": st["verdict"], "checker": st["checker"]}
    if not needed:
        return {}
    if str(unchecked_reason or "").strip():
        return {"checked": False, "unchecked_reason": str(unchecked_reason).strip()}
    raise CheckError("the check policy requires an independent check for this packet (instructions W9): pass the "
                     "check_id of a closed check, or unchecked_reason saying why it could not run")
