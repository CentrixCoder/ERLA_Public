"""Ranking changes need recorded evidence (owner, 2026-10-29; master-ai-skills LL-0065, L1, L8, L13).

`docs/decisions/ranking_evidence.yaml` lists every ranking default (search, BM25F, re-ranker, evidence thresholds,
models, shipped query-expansion and tier data) and a fingerprint of every ranking function. `check()` compares
that register with the code:

* every default and function is registered, and nothing registered has gone;
* the recorded value or fingerprint equals the code's;
* the register's `baseline` equals the frozen baseline (`ranking_baseline.yaml`, the code at v0.7.0, ec4c222;
  `tests/test_ranking_evidence.py` re-derives it from git), so a value and its baseline cannot be rewritten
  together;
* a value or function that differs from its frozen baseline, or has none (a new knob), needs evidence: at least
  one opened source (title, url, locator, opened date, what it supports), at least one measurement on this
  library, and the owner's approval (L13);
* `status: cited` needs a valid opened source and `status: measured` a valid measurement, changed or not;
* a ranking function changed to fix a defect without changing how the golden set ranks can carry an
  `equivalence` record instead of the source and the measurement (release 0.7.2, owner 2026-10-31): two golden
  runs, on the baseline code and on today's, with the same settings and the same ranked hits on every
  question (`hits_sha256`); two runs on the cases the fix is for, where today's code ranks better (paired
  interval above 0); both pairs record the ranking-function fingerprints they ran (`methods`), and only
  baseline-to-today changes may differ between them; and the regression test the fix carries. The owner's
  signed approval is still required.

A measurement names two saved runs under `docs/evals/` (`erla eval --save`, reduced by `python
scripts/ranking_evidence.py run`): `base` (the alternative or the old value) and `run` (the registered value),
and the metric. `check()` recomputes it from the runs' rows: both on the same golden file (sha256), at least
`evaluate.MIN_GOLDEN` scored questions in at least `MIN_CLASSES` classes, and a paired bootstrap interval of
run - base that lies above 0. A typed `n`, `before` or `after` proves nothing (audit 2026-10-30, P5).

An approval is the commit that introduced the entry as it stands, signed with a key in `.github/allowed_signers`
(`git commit -S`; checked by `scripts/evidence_signatures.py`, which `check()` loads from the checkout). That
file starts with exactly the owner key pinned in that script's `TRUST_ROOT` and changes only in commits
signed by a key it already holds, so an agent that can type a name in `approved.by` still cannot approve (audit
2026-10-30, P6). `status: measured` needs the same approval, since a run file can be written by hand; each run's
sha256 is in the entry, so the signature covers the files too.

A function's fingerprint covers the module-level constants, helpers and classes it reaches, so editing
`TIER_BOOST` or a helper changes the fingerprint of every ranking function that uses it. Every module-level
constant of a ranking module is reached by a registered function, registered as a value, or listed in
`NOT_RANKING` with the reason.

`status: baseline` claims no evidence: the value is inherited, not justified. `tests/test_ranking_evidence.py`
runs `check()` in every `pytest` and `release.py check`; `python scripts/ranking_evidence.py update` records the
code's current state as `pending`, which fails until the evidence and approval are filled in.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import inspect
import json
import importlib.util
import math
import re
import textwrap
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
REG_REL = "docs/decisions/ranking_evidence.yaml"
SIGNERS_REL = ".github/allowed_signers"
REGISTER = ROOT / REG_REL
BASELINE = ROOT / "docs" / "decisions" / "ranking_baseline.yaml"
EVALS_REL = "docs/evals"
MIN_CLASSES = 2     # a default that ranks every kind of question is not measured on one kind (audit P5)
BASELINE_COMMIT = "ec4c222"         # v0.7.0: the code the frozen baseline describes
TEMPLATES = ROOT / "packaging" / "core_templates"
CONFIG_FILES = ("synonyms.yaml", "source_authority.yaml")     # shipped ranking data (query expansion, tiers)
AGENTS = re.compile(r"\b(claude|assistant|agent|gpt|model|bot|ai)\b", re.I)

# Ranking functions: an edit to any of these changes how documents are ranked.
METHODS = {
    "search": ["_keyword", "_candidates", "_fuse", "search", "prior_factor", "final_score", "_rerank", "_mmr",
               "collapse_editions", "tier_first", "evidence", "expand", "bm25_params", "settings"],
    "bm25": ["idf", "phrase_scores", "rank", "search", "params"],
    "sparse": ["search", "_merge"],
    "embed": ["vector_search", "chunk_inputs", "windows", "_dedupe", "embed_pending", "export_matrix"],
    "rerank": ["score", "window", "selected"],
}

# Module-level names registered as values in `code_defaults`: not folded into fingerprints, so a default changed
# with evidence on its own entry is not reported a second time through every function that reads it.
VALUE_NAMES = {
    "search": {"DEFAULTS", "PROFILES", "EVIDENCE_DEFAULTS"},
    "bm25": {"DEFAULTS"},
    "rerank": {"RERANKERS", "DEFAULT"},
    "embed": {"DEFAULT_MODEL"},
    "sparse": {"DEFAULT_MODEL"},
}

# Module-level constants of ranking modules that no registered ranking function reaches, and why they don't
# change a ranking. A new constant fails `check()` until a ranking function reaches it or it is listed here.
NOT_RANKING: dict[str, str] = {
    "search.INTENTS": "tuple(PROFILES): the intent names for argument checks; the profiles are registered values",
}


def _flat(prefix: str, v: Any, out: dict[str, Any]) -> None:
    if isinstance(v, dict):
        for k in sorted(v):
            _flat(f"{prefix}.{k}", v[k], out)
    else:
        out[prefix] = v


def file_hash(path: Path) -> str:
    """Hash of a shipped data file, line endings folded (checkouts may convert them)."""
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()[:16]


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
    for name in CONFIG_FILES:
        out[f"config.{name}"] = file_hash(TEMPLATES / name)
    return out


def fingerprint(fn: Any) -> str:
    """Hash of a function's syntax tree without docstrings: comments and layout don't count, code does."""
    tree = copy.deepcopy(fn) if isinstance(fn, ast.AST) else ast.parse(textwrap.dedent(inspect.getsource(fn)))
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


def _module_defs(tree: ast.Module) -> dict[str, ast.AST]:
    """Module-level functions, classes and assigned names of a parsed module."""
    defs: dict[str, ast.AST] = {}
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defs[n.name] = n
        elif isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    defs[t.id] = n
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            defs[n.target.id] = n
    return defs


def _closure(name: str, defs: dict[str, ast.AST], skip: set[str]) -> list[str]:
    """`name` and every module-level name it reaches through its body and its helpers' bodies."""
    seen: set[str] = set()
    todo = [name]
    while todo:
        x = todo.pop()
        if x in seen or x not in defs:
            continue
        seen.add(x)
        todo += [n.id for n in ast.walk(defs[x]) if isinstance(n, ast.Name) and n.id in defs and n.id not in skip]
    return sorted(seen)


def _modules(root: Path) -> dict[str, dict[str, ast.AST]]:
    return {mod: _module_defs(ast.parse((root / f"{mod}.py").read_text(encoding="utf-8"))) for mod in METHODS}


def code_methods(root: Path | None = None) -> dict[str, str]:
    """Fingerprints read from the source files (no import), so every Python version and platform agrees. Each
    covers the function and the module-level constants, helpers and classes it reaches (not other registered
    functions, which have their own entries, nor names registered as values)."""
    out = {}
    for mod, defs in _modules(root or Path(__file__).resolve().parent).items():
        for n in METHODS[mod]:
            if not isinstance(defs.get(n), (ast.FunctionDef, ast.AsyncFunctionDef)):
                raise KeyError(f"ranking function erla.{mod}.{n} not found (renamed? update METHODS)")
            skip = (VALUE_NAMES.get(mod, set()) | set(METHODS[mod])) - {n}
            parts = [f"{x}={fingerprint(defs[x])}" for x in _closure(n, defs, skip)]
            out[f"{mod}.{n}"] = hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]
    return out


def uncovered_constants(root: Path | None = None) -> list[str]:
    """Module-level constants of ranking modules that no registered function reaches and that are neither
    registered as values nor listed in NOT_RANKING."""
    out = []
    for mod, defs in _modules(root or Path(__file__).resolve().parent).items():
        reached: set[str] = set()
        for n in METHODS[mod]:
            reached |= set(_closure(n, defs, VALUE_NAMES.get(mod, set())))
        for x in sorted(defs):
            if (re.fullmatch(r"_?[A-Z][A-Z0-9_]*", x) and x not in reached and x not in VALUE_NAMES.get(mod, set())
                    and f"{mod}.{x}" not in NOT_RANKING):
                out.append(f"{mod}.{x}")
    return out


def load(path: Path = REGISTER) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def frozen(path: Path = BASELINE) -> dict[str, dict[str, Any]]:
    """The frozen baseline: {"defaults": {key: value}, "methods": {key: fingerprint}} at BASELINE_COMMIT."""
    b = load(path) if path.is_file() else {}
    return {"defaults": dict(b.get("defaults") or {}), "methods": dict(b.get("methods") or {})}


def _is_date(v: Any) -> bool:
    if isinstance(v, date):
        return True
    return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(v or "")))


def good_sources(e: dict[str, Any]) -> list[dict[str, Any]]:
    return [x for x in e.get("evidence") or [] if isinstance(x, dict) and x.get("kind") == "source"
            and str(x.get("url") or "").startswith(("http://", "https://")) and x.get("title")
            and str(x.get("locator") or "").strip() and _is_date(x.get("opened")) and x.get("supports")]


MEASURE_FORM = ("kind: measurement, base: docs/evals/<alternative>.json, base_sha256, run: docs/evals/<this value>.json, "
                "run_sha256, metric, date; see python scripts/ranking_evidence.py run")
MIN_PER_CLASS = 10  # questions a class needs before it counts toward MIN_CLASSES (owner bar, audit review 2026-10-30)


def run_sha256(path: Path) -> str:
    """The hash an entry records for a saved run: the committed bytes, line endings folded."""
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def _saved_run(ref: Any, digest: Any, root: Path) -> tuple[dict[str, Any] | None, str]:
    from .evaluate import METRICS, RUN_FORMAT
    if not ref:
        return None, "missing (a saved run under docs/evals/)"
    p = (root / str(ref)).resolve()
    if (root / EVALS_REL).resolve() not in p.parents:
        return None, f"{ref} is not a saved run under {EVALS_REL}/"
    if not p.is_file():
        return None, f"{ref} does not exist"
    if not isinstance(digest, str) or digest.lower() != run_sha256(p):
        return None, f"{ref}: its sha256 is not the one the entry records (so the approval does not cover it)"
    try:
        run = json.loads(p.read_text(encoding="utf-8"), parse_constant=lambda c: math.nan)
    except ValueError:
        return None, f"{ref} is not JSON"
    if not isinstance(run, dict) or run.get("format") != RUN_FORMAT or not isinstance(run.get("rows"), list):
        return None, f"{ref} is not a reduced eval run ({RUN_FORMAT}; python scripts/ranking_evidence.py run)"
    if not isinstance(run.get("settings"), dict):
        return None, f"{ref} records no settings, so nothing says what it measured"
    ids: set[str] = set()
    for i, row in enumerate(run["rows"]):
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
            return None, f"{ref}: row {i} has no id"
        if row["id"] in ids:
            return None, f"{ref}: question id {row['id']!r} appears twice"
        ids.add(row["id"])
        if not isinstance(row.get("class"), str):
            return None, f"{ref}: row {row['id']} has no class"
        if row.get("negative"):
            continue
        for m in METRICS:
            v = row.get(m)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= 1:
                return None, f"{ref}: row {row['id']} {m} = {v!r} is not a number in [0, 1]"
    for m in METRICS:
        v = run.get(m)
        if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)):
            return None, f"{ref}: summary {m} = {v!r} is not a finite number"
    return run, ""


def _dotted(d: Any, path: str) -> Any:
    for part in path.split("."):
        if not isinstance(d, dict) or part not in d:
            return KeyError
        d = d[part]
    return d


def _varied(b: dict[str, Any], n: dict[str, Any]) -> bool:
    return any(_norm(b.get(f)) != _norm(n.get(f)) for f in ("settings", "reranker", "hybrid", "mode", "intent"))


def measurement_problems(x: Any, key: str, value: Any, root: Path = ROOT) -> list[str]:
    """Why `x` does not show, on this library, that `value` of `key` ranks better than the alternative it was
    measured against; [] when it does. Everything is recomputed from the saved runs' rows; a malformed run is
    reported, never raised."""
    try:
        return _measurement_problems(x, key, value, root)
    except Exception as exc:                 # fail closed with a reason, not a traceback
        return [f"the measurement could not be checked ({type(exc).__name__}: {exc})"]


def _measurement_problems(x: Any, key: str, value: Any, root: Path) -> list[str]:
    from .evaluate import METRICS, MIN_GOLDEN, compare_runs
    if not isinstance(x, dict) or x.get("kind") != "measurement":
        return ["not a measurement"]
    bad: list[str] = []
    metric = x.get("metric")
    if metric not in METRICS:
        bad.append(f"metric {metric!r} is not one of {', '.join(METRICS)}")
    if not _is_date(x.get("date")):
        bad.append("no date (YYYY-MM-DD)")
    runs: dict[str, dict[str, Any]] = {}
    means: dict[str, float] = {}
    for side in ("base", "run"):
        r, why = _saved_run(x.get(side), x.get(f"{side}_sha256"), root)
        if r is None:
            bad.append(f"{side}: {why}")
            continue
        pos = [row for row in r["rows"] if not row.get("negative")]
        if len(pos) < MIN_GOLDEN:
            bad.append(f"{side}: {len(pos)} scored questions, fewer than {MIN_GOLDEN}")
        if metric in METRICS and pos:
            means[side] = round(sum(float(row[metric]) for row in pos) / len(pos), 3)
            if r.get(metric) is not None and abs(float(r[metric]) - means[side]) > 0.0005:
                bad.append(f"{side}: its {metric} summary {r[metric]} is not the mean of its rows ({means[side]})")
        runs[side] = r
    if len(runs) == 2 and metric in METRICS:
        b, n = runs["base"], runs["run"]
        if not n.get("golden_sha256") or b.get("golden_sha256") != n.get("golden_sha256"):
            bad.append("base and run were not measured on the same golden file (golden_sha256)")
        counts: dict[str, int] = {}
        for row in n["rows"]:
            if not row.get("negative") and row["class"] not in ("unclassified", "negative"):
                counts[row["class"]] = counts.get(row["class"], 0) + 1
        big = sorted(c for c, k in counts.items() if k >= MIN_PER_CLASS)
        if len(big) < MIN_CLASSES:
            bad.append(f"run: {len(big)} question class(es) with at least {MIN_PER_CLASS} questions, "
                       f"fewer than {MIN_CLASSES}")
        cmp = compare_runs(b, n)
        ci = cmp[metric]["ci95"]
        if cmp["n_paired"] < MIN_GOLDEN:
            bad.append(f"{cmp['n_paired']} questions are shared by base and run, fewer than {MIN_GOLDEN}")
        elif not ci or ci[0] <= 0:
            bad.append(f"the paired {metric} change {cmp[metric]['delta']} has ci95 {ci}, which does not lie above 0")
        for f, v in (("before", means.get("base")), ("after", means.get("run")), ("delta", cmp[metric]["delta"])):
            if f in x and v is not None:
                given = x[f]
                if isinstance(given, bool) or not isinstance(given, (int, float)) or abs(float(given) - v) > 0.0005:
                    bad.append(f"{f} {given!r} is not what the runs give ({v})")
        if not _varied(b, n):
            bad.append("base and run record the same settings, re-ranker and mode: nothing was varied")
        if key.startswith("search.") and not key.startswith(("search.profiles.", "search.evidence.")):
            sk = key[len("search."):]
            got, alt = _dotted(n["settings"], sk), _dotted(b["settings"], sk)
            if got is KeyError or alt is KeyError:
                bad.append(f"the runs' settings do not record {sk}, so they do not show what {key} was")
            elif _norm(got) != _norm(value):
                bad.append(f"run measured {key} = {got!r}, not the registered {value!r}")
            elif _norm(alt) == _norm(got):
                bad.append(f"base and run both have {key} = {got!r}: they do not compare this setting")
    return bad


def good_measurements(e: dict[str, Any], key: str = "", value: Any = None, root: Path = ROOT) -> list[dict[str, Any]]:
    return [x for x in e.get("evidence") or [] if isinstance(x, dict) and x.get("kind") == "measurement"
            and not measurement_problems(x, key, value, root)]


def _measurement_hint(e: dict[str, Any], key: str, value: Any, root: Path) -> str:
    tried = [x for x in e.get("evidence") or [] if isinstance(x, dict) and x.get("kind") == "measurement"]
    return f" ({'; '.join(measurement_problems(tried[0], key, value, root))})" if tried else f" ({MEASURE_FORM})"


EQUIV_FORM = ("kind: equivalence, base: docs/evals/<old code>.json, base_sha256, run: docs/evals/<this code>.json, "
              "run_sha256, fixed: {base, base_sha256, run, run_sha256, metric}, fixes: tests/<file>.py::<test>, date")
_SHA = re.compile(r"[0-9a-f]{64}")


def equivalence_problems(x: Any, key: str, code: dict[str, str], baseline: dict[str, Any],
                         root: Path = ROOT) -> list[str]:
    """Why `x` does not show that the change of ranking function `key` from its baseline to today's code is a
    correctness fix that ranks the golden set exactly as before; [] when it does. Everything is recomputed from
    the saved runs; a malformed record is reported, never raised."""
    try:
        return _equivalence_problems(x, key, code, baseline, root)
    except Exception as exc:                 # fail closed with a reason, not a traceback
        return [f"the equivalence could not be checked ({type(exc).__name__}: {exc})"]


def _code_pair(b: dict[str, Any], n: dict[str, Any], key: str, code: dict[str, str], baseline: dict[str, Any],
               label: str) -> list[str]:
    """`base` ran `key` at its baseline and `run` at today's code, and every ranking function that differs
    between the two runs is such a baseline-to-today change: nothing else was varied."""
    bm, nm = b.get("methods"), n.get("methods")
    if not isinstance(bm, dict) or not isinstance(nm, dict) or not bm or not nm:
        return [f"{label}: the runs record no ranking-function fingerprints (methods), so nothing says which code ran"]
    bad = []
    if nm.get(key) != code.get(key):
        bad.append(f"{label}: run ran {key} {nm.get(key)!r}, not today's code {code.get(key)!r}")
    if bm.get(key) != baseline.get(key):
        bad.append(f"{label}: base ran {key} {bm.get(key)!r}, not its baseline {baseline.get(key)!r}")
    for k in sorted(set(bm) | set(nm)):
        if bm.get(k) != nm.get(k) and not (bm.get(k) == baseline.get(k) and nm.get(k) == code.get(k)):
            bad.append(f"{label}: {k} differs between base and run but is not a change from its baseline to "
                       "today's code")
    return bad


def _equivalence_problems(x: Any, key: str, code: dict[str, str], baseline: dict[str, Any], root: Path) -> list[str]:
    from .evaluate import METRICS, MIN_GOLDEN, compare_runs
    if not isinstance(x, dict) or x.get("kind") != "equivalence":
        return ["not an equivalence"]
    bad: list[str] = []
    if not _is_date(x.get("date")):
        bad.append("no date (YYYY-MM-DD)")
    if key not in code or key not in baseline or code[key] == baseline[key]:
        bad.append(f"{key} is not a ranking function changed from its baseline")
    runs: dict[str, dict[str, Any]] = {}
    for side in ("base", "run"):
        r, why = _saved_run(x.get(side), x.get(f"{side}_sha256"), root)
        if r is None:
            bad.append(f"{side}: {why}")
        else:
            runs[side] = r
    if len(runs) == 2:
        b, n = runs["base"], runs["run"]
        if not n.get("golden_sha256") or b.get("golden_sha256") != n.get("golden_sha256"):
            bad.append("base and run were not measured on the same golden file (golden_sha256)")
        if _varied(b, n):
            bad.append("base and run differ in settings, re-ranker or mode: an equivalence compares code only")
        pos = [row for row in n["rows"] if not row.get("negative")]
        if len(pos) < MIN_GOLDEN:
            bad.append(f"run: {len(pos)} scored questions, fewer than {MIN_GOLDEN}")
        counts: dict[str, int] = {}
        for row in pos:
            if row["class"] not in ("unclassified", "negative"):
                counts[row["class"]] = counts.get(row["class"], 0) + 1
        if len([c for c, k in counts.items() if k >= MIN_PER_CLASS]) < MIN_CLASSES:
            bad.append(f"run: fewer than {MIN_CLASSES} question classes with at least {MIN_PER_CLASS} questions")
        brows, nrows = {row["id"]: row for row in b["rows"]}, {row["id"]: row for row in n["rows"]}
        if set(brows) != set(nrows):
            bad.append(f"base and run hold different questions ({len(set(brows) ^ set(nrows))} not in both)")
        unhashed = [i for i in nrows if not all(isinstance(r.get("hits_sha256"), str) and _SHA.fullmatch(r["hits_sha256"])
                                                for r in (brows.get(i) or {}, nrows[i]))]
        if unhashed:
            bad.append(f"{len(unhashed)} question(s) record no ranked-hits hash (hits_sha256), e.g. {unhashed[0]}")
        moved = sorted(i for i in set(brows) & set(nrows) if i not in unhashed and (
            brows[i].get("hits_sha256") != nrows[i].get("hits_sha256")
            or any(_norm(brows[i].get(m)) != _norm(nrows[i].get(m)) for m in METRICS)))
        if moved:
            bad.append(f"{len(moved)} of {len(nrows)} questions ranked differently, e.g. {', '.join(moved[:3])}: "
                       "not an equivalence (measure it instead)")
        bad += _code_pair(b, n, key, code, baseline, "golden runs")
    fx = x.get("fixed")
    if not isinstance(fx, dict):
        bad.append("fixed: missing (two saved runs on the cases the fix is for, where today's code ranks better)")
    else:
        metric = fx.get("metric")
        if metric not in METRICS:
            bad.append(f"fixed: metric {metric!r} is not one of {', '.join(METRICS)}")
        pair: dict[str, dict[str, Any]] = {}
        for side in ("base", "run"):
            r, why = _saved_run(fx.get(side), fx.get(f"{side}_sha256"), root)
            if r is None:
                bad.append(f"fixed {side}: {why}")
            else:
                pair[side] = r
        if len(pair) == 2 and metric in METRICS:
            fb, fn = pair["base"], pair["run"]
            if not fn.get("golden_sha256") or fb.get("golden_sha256") != fn.get("golden_sha256"):
                bad.append("fixed: base and run were not measured on the same case file (golden_sha256)")
            # inputs only: `mode` is the last question's outcome ('+rerank' once there are hits), which the fix
            # is meant to change on these cases
            if any(_norm(fb.get(f)) != _norm(fn.get(f)) for f in ("settings", "reranker", "hybrid", "intent")):
                bad.append("fixed: base and run differ in settings, re-ranker, hybrid or intent: only the code may "
                           "differ")
            cmp = compare_runs(fb, fn)
            ci = cmp[metric]["ci95"]
            if cmp["n_paired"] < MIN_GOLDEN:
                bad.append(f"fixed: {cmp['n_paired']} cases are shared by base and run, fewer than {MIN_GOLDEN}")
            elif not ci or ci[0] <= 0:
                bad.append(f"fixed: the paired {metric} change {cmp[metric]['delta']} has ci95 {ci}, which does not "
                           "lie above 0")
            bad += _code_pair(fb, fn, key, code, baseline, "fixed runs")
    ref = str(x.get("fixes") or "")
    m = re.fullmatch(r"(tests/[\w/.-]+\.py)::(test_\w+)", ref)
    if not m:
        bad.append("fixes: name the regression test as tests/<file>.py::<test_name>")
    else:
        p = (root / m.group(1)).resolve()
        if (root / "tests").resolve() not in p.parents or not p.is_file():
            bad.append(f"fixes: {m.group(1)} is not a test file of this repository")
        elif not re.search(rf"^\s*def {m.group(2)}\(", p.read_text(encoding="utf-8"), re.M):
            bad.append(f"fixes: {m.group(1)} has no test {m.group(2)}")
    return bad


Approver = Callable[[str, str, dict[str, Any]], list[str]]
SIGNATURES = ROOT / "scripts" / "evidence_signatures.py"


def signatures(path: Path = SIGNATURES) -> Any:
    """The approval checker (`scripts/evidence_signatures.py`: git and ssh-keygen); None outside a checkout."""
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("erla_evidence_signatures", path)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _approval_problems(section: str, key: str, e: dict[str, Any], approver: Approver, why: str) -> list[str]:
    ap = e.get("approved") or {}
    who = str(ap.get("by") or "").strip()
    if not who or AGENTS.search(who) or not _is_date(ap.get("date")):
        return [f"{key}: {why} without the owner's approval (approved: by <owner>, date); an agent cannot "
                "approve (L13)"]
    return approver(section, key, e)


def _evidence_problems(section: str, key: str, e: dict[str, Any], value: Any, root: Path,
                       approver: Approver, code: dict[str, Any] | None = None,
                       baseline: dict[str, Any] | None = None) -> list[str]:
    bad: list[str] = []
    eq = [x for x in e.get("evidence") or [] if isinstance(x, dict) and x.get("kind") == "equivalence"]
    if section == "methods" and eq and code is not None and baseline is not None:
        # a correctness fix that ranks the golden set exactly as before: the equivalence stands in for the
        # source and the measurement (there is no ranking choice to justify), never for the approval
        probs = [equivalence_problems(x, key, code, baseline, root) for x in eq]
        if any(not p for p in probs):
            return _approval_problems(section, key, e, approver, "changed from its baseline")
        bad.append(f"{key}: its equivalence record does not hold ({'; '.join(probs[0])})")
    if not good_sources(e):
        bad.append(f"{key}: changed from its baseline without an opened source "
                   "(kind: source, title, url, locator, opened: YYYY-MM-DD, supports)")
    if not good_measurements(e, key, value, root):
        bad.append(f"{key}: changed from its baseline without a measurement on this library"
                   + _measurement_hint(e, key, value, root))
    return bad + _approval_problems(section, key, e, approver, "changed from its baseline")


def _norm(v: Any) -> Any:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else v


def check(reg: dict[str, Any] | None = None, defaults: dict[str, Any] | None = None,
          methods: dict[str, str] | None = None, base: dict[str, dict[str, Any]] | None = None,
          uncovered: list[str] | None = None, root: Path | None = None,
          approver: Approver | None = None) -> list[str]:
    """Problems between the register and the code; [] when every change is backed by evidence. `root` is the
    repository the saved runs and the git history are read from; `approver` checks an owner approval (default:
    a signed commit, `git_approval`)."""
    reg = load() if reg is None else reg
    defaults = code_defaults() if defaults is None else defaults
    methods = code_methods() if methods is None else methods
    base = frozen() if base is None else base
    uncovered = uncovered_constants() if uncovered is None else uncovered
    root = ROOT if root is None else root
    approvals: list[str] = []

    sig: list[Any] = []

    def approve(section: str, key: str, e: dict[str, Any]) -> list[str]:
        approvals.append(key)
        if approver:
            return approver(section, key, e)
        if not sig:
            sig.append(signatures())
        if sig[0] is None:
            return [f"{key}: {SIGNATURES.name} is not available, so no approval can be verified (fail closed)"]
        return sig[0].git_approval(section, key, e, root)

    out: list[str] = [f"constants: {k} is reached by no registered ranking function; register the function that "
                      "uses it (METHODS) or list it in NOT_RANKING with the reason" for k in uncovered]
    for section, code, field in (("defaults", defaults, "value"), ("methods", methods, "fingerprint")):
        entries = reg.get(section) or {}
        fixed = base.get(section) or {}
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
            if _norm(e.get("baseline")) != _norm(fixed.get(k)):
                out.append(f"{section}: {k} has baseline {e.get('baseline')!r} in the register but "
                           f"{fixed.get(k)!r} in the frozen baseline ({BASELINE_COMMIT}); a baseline is never rewritten")
            if e.get("status") == "pending":
                out.append(f"{section}: {k} is pending: evidence and the owner's approval are missing")
            changed = k not in fixed or _norm(code[k]) != _norm(fixed[k])
            if changed:
                out += [f"{section}: {p}" for p in _evidence_problems(section, k, e, code[k], root, approve,
                                                                       code, fixed)]
            elif e.get("status") not in ("baseline", "cited", "measured"):
                out.append(f"{section}: {k} has status {e.get('status')!r} (baseline, cited or measured)")
            elif e.get("status") == "measured":     # a run file can be written by hand: the owner signs the claim
                out += [f"{section}: {p}" for p in _approval_problems(section, k, e, approve, "says measured")]
            if e.get("status") == "cited" and not good_sources(e):
                out.append(f"{section}: {k} says cited but lists no valid opened source "
                           "(kind: source, title, url, locator, opened, supports)")
            if e.get("status") == "measured" and not good_measurements(e, k, code[k], root):
                out.append(f"{section}: {k} says measured but lists no valid measurement on this library"
                           + _measurement_hint(e, k, code[k], root))
    if approvals and sig and sig[0] is not None:
        out += sig[0].signers_problems(root)
    return out


def updated(reg: dict[str, Any]) -> list[str]:
    """Record the code's current values and fingerprints in `reg` (in place; the file is written by
    scripts/ranking_evidence.py, since the package writes only through safeio). New or changed entries become
    `pending`, which fails `check()` until a person adds the evidence and approval; others are kept. A new
    entry's baseline is the frozen one (none for a new knob), never the current value."""
    changed = []
    base = frozen()
    for section, code, field in (("defaults", code_defaults(), "value"), ("methods", code_methods(), "fingerprint")):
        entries = reg.setdefault(section, {}) or {}
        reg[section] = entries
        for k, v in code.items():
            e = entries.get(k)
            if e is None:
                entries[k] = {field: v, "baseline": base[section].get(k), "status": "pending"}
                changed.append(k)
            elif _norm(e.get(field)) != _norm(v):
                e[field] = v
                e["status"] = "pending"
                changed.append(k)
        for k in [k for k in entries if k not in code]:
            entries[k]["status"] = "pending"
            changed.append(k)
    return changed
