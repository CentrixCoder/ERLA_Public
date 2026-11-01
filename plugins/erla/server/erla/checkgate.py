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
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from . import safeio
from .config import Config, check_policy
from .verify import numbers

VERDICTS = ("PASS", "PASS WITH FINDINGS", "FAIL")
CHECKERS = ("erla:professor", "self-check")
AGREE, EXPLAIN = 0.02, 0.05          # within 2 % agrees; under 5 % correlates with a reason; 5 % or more does not
OUT = ("out of tolerance", "units differ", "contradiction")
NOT_CONVERGED = (*OUT, "not checked")  # no result that ERLA and the Professor both support (owner, 2026-10-17)
DECISION = "decision"                 # units of a yes/no or label conclusion: critical / not critical, separates ...
STAGES = ("opus", "fable")            # the Professor on Opus first; Fable only when that check did not converge
FIRST_STAGE, LAST_STAGE = STAGES
MAX_ROUNDS = 3                        # the first check plus at most 2 rework rounds per stage
# Model currency (0.5.2): the Professor's agent file names model families (aliases), which Claude Code resolves to
# the newest model of each family. Every check records the exact model it ran on; a model without a passing
# calibration on the seeded-error set is flagged until professor_eval has measured it.
MODEL_ID = re.compile(r"^claude-[a-z]+(?:-[a-z0-9]+)*$")
CATCH_TARGET, FALSE_ALARM_MAX = 0.90, 0.10
CAL_MIN_FAULTY, CAL_MIN_CONTROLS = 10, 5
CALIBRATION_LOG = "professor_calibration.jsonl"
ID = re.compile(r"^k-[0-9a-f]{12}$")
# Coverage (rules R4.7, guardrails G3.8 and G6.3, instructions W9 step 3a; owner 2026-10-24): a check of a margin, a
# design value or a program audits the draft's coverage inventory, one row per configuration and failure mode. A PASS
# on values is not a PASS on completeness, so without an audited inventory such a check is at most PASS WITH FINDINGS.
COVERAGE_STATUS = ("COMPUTED", "NOT CHECKED", "REQUIRED INPUT")
MARGIN = re.compile(r"(?:\bMS\b|\bmargin)", re.IGNORECASE)


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
    # verify.numbers reads thousands commas and a unicode minus; a value with two numbers (a range, an alloy
    # name before the value) is refused, not read as its first digits (correctness audit 2026-10-31: "1,000"
    # was read as 1.0 and "−0.05" as +0.05, so a check could agree with the wrong number)
    found = numbers(str(v))
    if len(found) != 1:
        raise CheckError(f"{what}: give exactly one number (units apart), got {v!r}")
    return found[0][0]


def _obj(x: Any) -> dict[str, Any] | None:
    """``x`` as a JSON object, or None: tool arguments come from a model and are checked, not trusted."""
    return cast(dict[str, Any], x) if isinstance(x, dict) else None


def _list(x: Any) -> list[Any] | None:
    return cast(list[Any], x) if isinstance(x, list) else None


def open_check(cfg: Config, packet_in: Any, rework_of: str = "", escalate_from: str = "") -> dict[str, Any]:
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
        if str(q["units"]).strip() == DECISION:
            if not str(q.get("value") or "").strip():
                raise CheckError(f"quantity {q['name']!r}: a decision needs its label as value, e.g. 'not critical'")
        else:
            _number(q.get("value"), f"quantity {q['name']!r}")
        names.append(str(q["name"]).strip())
    if len(set(names)) != len(names):
        raise CheckError("quantity names must be unique")
    _coverage_rows(packet)
    stage, rnd, prior = _chain(cfg, rework_of, escalate_from)
    body = json.dumps(packet, sort_keys=True, ensure_ascii=False, default=str)
    # a random salt: two opens of one packet in the same second must not share (and overwrite) one record
    cid = "k-" + hashlib.sha256(f"{body}|{_now()}|{secrets.token_hex(8)}".encode()).hexdigest()[:12]
    rec: dict[str, Any] = {"check_id": cid, "state": "open", "opened": _now(), "packet": packet, "estimates": [],
                           "comparison": [], "stage": stage, "round": rnd, "prior": prior}
    _save(cfg, rec)
    return {"check_id": cid, "brief_part1": brief_part1(packet), "stage": stage, "round": rnd,
            "next": f"send check_id and brief_part1 to the professor agent on {stage.title()} (model "
                    f"'{stage}'); it calls check_estimate, check_reveal and check_close"}


def _chain(cfg: Config, rework_of: str, escalate_from: str) -> tuple[str, int, str]:
    """Stage, round and prior check of a new check: a first check is Opus round 1; a rework keeps its stage
    (at most MAX_ROUNDS per stage); Fable only follows an Opus check that did not converge."""
    if rework_of and escalate_from:
        raise CheckError("give rework_of or escalate_from, not both")
    if rework_of:
        prior = load(cfg, rework_of)
        if prior["state"] != "closed":
            raise CheckError(f"check {rework_of} is {prior['state']}: close it before a rework")
        rnd = int(prior.get("round") or 1) + 1
        if rnd > MAX_ROUNDS:
            nxt = ("escalate to the Professor on Fable (escalate_from)" if prior.get("stage", FIRST_STAGE) == FIRST_STAGE
                   else "report the result UNRESOLVED (outcome 'unresolved')")
            raise CheckError(f"{MAX_ROUNDS - 1} rework rounds are used up at this stage: {nxt}")
        return str(prior.get("stage") or FIRST_STAGE), rnd, rework_of
    if escalate_from:
        prior = load(cfg, escalate_from)
        if prior["state"] != "closed" or prior.get("stage", FIRST_STAGE) != FIRST_STAGE:
            raise CheckError("escalate only from a closed check of the Professor on Opus")
        if prior.get("converged"):
            raise CheckError(f"check {escalate_from} converged: no escalation to Fable")
        return LAST_STAGE, 1, escalate_from
    return FIRST_STAGE, 1, ""


def _coverage_rows(packet: dict[str, Any]) -> list[dict[str, Any]]:
    """The draft's coverage inventory, checked: rows {configuration, mode, status, source, reason}; a COMPUTED row
    names its source or method, the others their reason or missing input. [] when the draft has none."""
    raw = packet.get("coverage")
    if raw is None:
        return []
    rows_in = _list(raw)
    if rows_in is None:
        raise CheckError("coverage must be a list of rows {configuration, mode, status, source, reason}")
    rows: list[dict[str, Any]] = []
    for i, r_in in enumerate(rows_in):
        r = _obj(r_in)
        if r is None or not str(r.get("configuration") or "").strip() or not str(r.get("mode") or "").strip():
            raise CheckError(f"coverage row {i}: configuration and mode are required")
        st = str(r.get("status") or "").strip().upper()
        if st not in COVERAGE_STATUS:
            raise CheckError(f"coverage row {i}: status must be one of {list(COVERAGE_STATUS)}")
        if st == "COMPUTED" and not str(r.get("source") or "").strip():
            raise CheckError(f"coverage row {i}: a COMPUTED mode names its source or method")
        if st != "COMPUTED" and not str(r.get("reason") or "").strip():
            raise CheckError(f"coverage row {i}: a {st} mode gives its reason or the missing input")
        rows.append({**r, "status": st})
    return rows


def needs_coverage(packet: dict[str, Any]) -> bool:
    """Does this check report a margin, a design value or a program (W9 step 3a)? Declared by the lead
    (``reports_margins``) or seen in a quantity's name (MS, margin), so leaving the flag out does not avoid it."""
    if packet.get("reports_margins") is True or packet.get("program") is True:
        return True
    names = [str(q.get("name") or "") for q in (_obj(x) for x in _list(packet.get("quantities")) or []) if q is not None]
    return any(MARGIN.search(n) for n in names)


def brief_part1(packet: dict[str, Any]) -> dict[str, Any]:
    """Part 1 of the Check Brief: everything the checker needs to estimate, none of the lead's results. The
    coverage inventory is a list of what the draft covers, not a result, so the checker sees it first (W9 3a)."""
    keep = ("question", "inputs", "sources", "method", "discipline")
    out = {k: packet[k] for k in keep if k in packet}
    out["quantities"] = [{"name": q["name"], "units": q["units"]} for q in packet["quantities"]]
    rows = _coverage_rows(packet)
    if rows:
        out["coverage"] = rows
    elif needs_coverage(packet):
        out["coverage"] = "NONE GIVEN: audit not possible; the verdict is at most PASS WITH FINDINGS (guardrails G6.3)"
    return out


def estimate(cfg: Config, check_id: str, estimates: Any) -> dict[str, Any]:
    """The checker's own values, one per quantity (or ``not_checked`` with a reason), before the reveal."""
    rec = load(cfg, check_id)
    if rec["state"] not in ("open", "estimated"):
        raise CheckError(f"check {check_id} is {rec['state']}: estimates are fixed once the lead's values are revealed")
    want = [q["name"] for q in rec["packet"]["quantities"]]
    decisions = {q["name"]: str(q["units"]).strip() == DECISION for q in rec["packet"]["quantities"]}
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
        if decisions.get(name):
            if not str(e.get("value") or "").strip():
                raise CheckError(f"estimate {name!r}: give your own conclusion as the value")
            got[name] = {"name": name, "value": str(e["value"]).strip(), "units": DECISION,
                         "method": str(e["method"]).strip()}
            continue
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
    if str(lead["units"]).strip() == DECISION:
        same = " ".join(str(lead["value"]).lower().split()) == " ".join(str(est["value"]).lower().split())
        return {**row, "estimate": est["value"], "method": est["method"], "status": "agrees" if same else "contradiction"}
    lv, ev = _number(lead["value"], lead["name"]), float(est["value"])
    if est["units"].strip() != str(lead["units"]).strip():
        try:
            ev = units.convert(ev, est["units"], str(lead["units"])).value
        except ValueError:
            return {**row, "estimate": est["value"], "estimate_units": est["units"], "status": "units differ"}
    diff = 0.0 if lv == ev else abs(ev - lv) / max(abs(lv), 1e-12)
    if lv * ev < 0:                          # opposite signs (a margin, a load direction): opposite conclusions
        return {**row, "estimate": round(ev, 10), "method": est["method"], "diff": round(diff, 5),
                "status": "contradiction"}
    status = "agrees" if diff <= AGREE else ("explain" if diff < EXPLAIN else "out of tolerance")
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
          checker: str = "erla:professor", notes: Any = None, model: str = "",
          model_id: str = "", coverage_audited: bool = False) -> dict[str, Any]:
    """Record the verdict; refuse one that the computed comparison contradicts. ``notes`` are minor remarks
    that change nothing (a reason for a 2-5 % difference, a presentation point); they never make a PASS
    impossible, so PASS keeps its meaning (2026-10-17 calibration: no control got a clean PASS).
    ``coverage_audited``: the checker compared the draft's coverage inventory with the discipline's failure-mode
    catalogue (W9 step 3a). A check of a margin, design value or program without that audit is refused a PASS."""
    rec = load(cfg, check_id)
    if rec["state"] != "revealed":
        raise CheckError(f"check {check_id} is {rec['state']}: close it after check_reveal")
    verdict = str(verdict).strip().upper()
    if verdict not in VERDICTS:
        raise CheckError(f"verdict must be one of {list(VERDICTS)}")
    if checker not in CHECKERS:
        raise CheckError(f"checker must be one of {list(CHECKERS)}")
    stage = str(rec.get("stage") or FIRST_STAGE)
    if checker == "erla:professor" and str(model).strip().lower() != stage:
        raise CheckError(f"this check is the Professor on {stage.title()}: close it with model '{stage}' "
                         "(the model you run on)")
    mid = str(model_id).strip().lower()
    if checker == "erla:professor" and (not MODEL_ID.match(mid) or stage not in mid.split("-")):
        raise CheckError(f"close with model_id = the exact model you run on, of the {stage.title()} family "
                         f"(e.g. 'claude-{stage}-5-5'); got {model_id!r}")
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
    needs_cov = needs_coverage(rec["packet"])
    audited = bool(coverage_audited) and bool(_coverage_rows(rec["packet"]))
    if verdict == "PASS" and needs_cov and not audited:
        raise CheckError("this check reports a margin, design value or program and its coverage inventory was not "
                         "audited (none given, or coverage_audited not set): a PASS on values is not a PASS on "
                         "completeness (guardrails G6.3). Use PASS WITH FINDINGS with a finding 'coverage not "
                         "audited', or audit the inventory (W9 step 3a)")
    if verdict == "PASS WITH FINDINGS" and (not fs or changes):
        raise CheckError("PASS WITH FINDINGS needs at least one finding and none that changes the answer "
                         "(that is a FAIL)")
    if verdict == "FAIL" and not changes:
        raise CheckError("FAIL needs at least one finding with changes_answer: true")
    converged = not any(c["status"] in NOT_CONVERGED for c in cmp_)
    calibrated = None if checker != "erla:professor" else mid in calibrated_models(cfg)
    rec.update(state="closed", closed=_now(), verdict=verdict, findings=fs, notes=ns, checker=checker,
               model=str(model).strip().lower() or None, model_id=mid or None, calibrated=calibrated,
               converged=converged, coverage_audited=audited if needs_cov else None)
    _save(cfg, rec)
    from .report import append_log
    if calibrated is False:
        _open_model_trigger(cfg, mid)
    append_log(cfg, {"cmd": "mcp.check", "check_id": check_id, "verdict": verdict, "checker": checker,
                     "stage": stage, "round": rec.get("round", 1), "converged": converged,
                     "model_id": mid or None, "calibrated": calibrated,
                     "n_quantities": len(cmp_), "n_out": sum(c["status"] in OUT for c in cmp_),
                     "n_not_checked": sum(c["status"] == "not checked" for c in cmp_), "n_findings": len(fs),
                     "coverage_audited": rec["coverage_audited"]})
    return {"check_id": check_id, "state": "closed", "verdict": verdict, "checker": checker, "stage": stage,
            "model_id": mid or None, "calibrated": calibrated, "coverage_audited": rec["coverage_audited"],
            **({"calibration_note": f"checker model {mid} not yet calibrated: re-run scripts/professor_eval.py"}
               if calibrated is False else {}),
            "converged": converged, "comparison": cmp_, "findings": fs, "notes": ns,
            "next": "" if converged else _next_step(rec)}


def calibrated_models(cfg: Config) -> set[str]:
    """Model IDs whose latest calibration on the seeded-error set passed both targets."""
    p = cfg.log_dir / CALIBRATION_LOG
    latest: dict[str, bool] = {}
    if p.is_file():
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                r = _obj(json.loads(line))
            except ValueError:
                continue
            if r is not None and r.get("model_id"):
                latest[str(r["model_id"])] = bool(r.get("passed"))
    return {m for m, ok in latest.items() if ok}


def record_calibration(cfg: Config, model_id: str, summary: dict[str, Any], source: str = "") -> dict[str, Any]:
    """Append one calibration result. It passes only on a full set (at least CAL_MIN_FAULTY faulty and
    CAL_MIN_CONTROLS correct packets) that meets both targets: a smoke run never calibrates a model."""
    mid = str(model_id).strip().lower()
    if not MODEL_ID.match(mid):
        raise CheckError(f"calibration needs an exact model id, got {model_id!r}")
    faulty, controls = int(summary.get("faulty") or 0), int(summary.get("controls") or 0)
    catch = summary.get("catch_rate")
    false_alarm = summary.get("false_alarm_rate")
    passed = (faulty >= CAL_MIN_FAULTY and controls >= CAL_MIN_CONTROLS and catch is not None
              and false_alarm is not None and float(catch) >= CATCH_TARGET and float(false_alarm) <= FALSE_ALARM_MAX)
    rec = {"ts": _now(), "model_id": mid, "faulty": faulty, "controls": controls, "catch_rate": catch,
           "false_alarm_rate": false_alarm, "targets": {"catch": CATCH_TARGET, "false_alarm": FALSE_ALARM_MAX},
           "passed": passed, "source": source}
    safeio.append_jsonl(cfg, cfg.log_dir / CALIBRATION_LOG, rec)
    return rec


def _open_model_trigger(cfg: Config, model_id: str) -> None:
    """One protocol-review trigger per uncalibrated checker model (shown by site_info / protocol_status)."""
    p = cfg.log_dir / "protocol_review_log.jsonl"
    if p.is_file() and f'"model_id": "{model_id}"' in p.read_text(encoding="utf-8", errors="replace"):
        return
    safeio.append_jsonl(cfg, p, {"ts": _now(), "type": "trigger_open", "trigger": "model_change",
                                 "model_id": model_id,
                                 "detail": f"the Professor ran on {model_id}, which has no passing calibration; "
                                           "run scripts/professor_eval.py on the seeded-error set"})


def _next_step(rec: dict[str, Any]) -> str:
    """What ERLA does after a check that did not converge (owner's rule, 2026-10-17)."""
    unresolved = ("report UNRESOLVED - no solution can be established from the available sources; never "
                  "contrive one")
    more = int(rec.get("round") or 1) < MAX_ROUNDS
    if str(rec.get("stage") or FIRST_STAGE) == FIRST_STAGE:
        return ("not converged: fix the draft and open a rework check (rework_of)" if more else
                "not converged after the rework rounds: escalate to the Professor on Fable (escalate_from)")
    return (f"not converged on Fable: re-check (rework_of) only with a fix the sources support; otherwise {unresolved}"
            if more else f"not converged on Fable either: {unresolved}")


def status(cfg: Config, check_id: str) -> dict[str, Any]:
    """What the lead may see: the state, and once closed the verdict, comparison and findings."""
    rec = load(cfg, check_id)
    out: dict[str, Any] = {"check_id": check_id, "state": rec["state"], "stage": rec.get("stage", FIRST_STAGE),
                           "round": rec.get("round", 1), "prior": rec.get("prior", "")}
    if rec["state"] == "closed":
        out.update(verdict=rec["verdict"], checker=rec["checker"], comparison=rec["comparison"],
                   findings=rec["findings"], notes=rec.get("notes", []), converged=rec.get("converged", True),
                   model_id=rec.get("model_id"), calibrated=rec.get("calibrated"),
                   coverage_audited=rec.get("coverage_audited"))
        if not out["converged"]:
            out["next"] = _next_step(rec)
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


def gate(cfg: Config, needed: bool, check_id: str, unchecked_reason: str, unresolved: bool = False) -> dict[str, Any]:
    """What to record about the check, or raise when the policy requires one and none is given. A check that did
    not converge is never recorded as a solution: only as UNRESOLVED, and only after the Fable stage."""
    if check_id:
        st = status(cfg, check_id)
        if st["state"] != "closed":
            raise CheckError(f"check {check_id} is {st['state']}, not closed: finish it (instructions W9) first")
        rec = {"check_id": check_id, "check_verdict": st["verdict"], "checker": st["checker"], "stage": st["stage"],
               "converged": st["converged"], "model_id": st.get("model_id"), "calibrated": st.get("calibrated")}
        if st["converged"]:
            return rec
        if st["stage"] != LAST_STAGE or st["checker"] != "erla:professor":
            if st["checker"] == "self-check" and unresolved:
                return {**rec, "outcome": "unresolved"}
            raise CheckError(f"check {check_id} did not converge ({st.get('next', '')}); it cannot be recorded as a "
                             "solution")
        if not unresolved:
            raise CheckError(f"check {check_id} did not converge on Fable either: this packet must be reported "
                             "UNRESOLVED (no solution from the available sources), not as a solution")
        return {**rec, "outcome": "unresolved"}
    if not needed:
        return {}
    if str(unchecked_reason or "").strip():
        return {"checked": False, "unchecked_reason": str(unchecked_reason).strip()}
    raise CheckError("the check policy requires an independent check for this packet (instructions W9): pass the "
                     "check_id of a closed check, or unchecked_reason saying why it could not run")
