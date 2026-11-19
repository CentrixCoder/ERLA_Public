"""Claim attribution: does a cited passage support a sentence? (ADR-0006; laws L3, L8, L16).

The model writes sentences and cites sources; this module checks, in code, what code can check about a sentence:

* ``anchors``: the sentence's checkable terms - numbers, identifiers that mix letters and digits (7075-T7351, T6,
  H1150, E399, MIL-HDBK-5J), acronyms (MMPDS, ASTM, LEFM), property symbols (Ftu, Fty, KIc), grain directions
  (L-T, ST) and A/B/S-basis.
* ``support``: the sentence is supported by the cited text when every anchor is printed in it and at least
  ``MIN_COVERAGE`` of the sentence's content words are. A missing anchor is a swapped alloy, temper, standard,
  direction or number: the claim fails. Few content words found, or nothing checkable at all, leaves the claim
  unconfirmed: code cannot judge a paraphrase. A pass is about terms, not meaning; a negated or reversed sentence
  with the same terms passes (ADR-0006 rows C8-C10, NOT CHECKED).
* ``citation_scores``: ALCE's citation recall and precision (Gao et al. 2023, arXiv 2305.14627 §3.3) with this judge
  in place of their NLI model: a statement scores recall 1 when it has a citation and its cited passages together
  support it; a citation is irrelevant when it alone does not support the statement and the other citations do.
* ``ablation_scores``: a ContextCite-style attribution (Cohen-Wang et al. 2024, arXiv 2409.00729 §3: random sentence
  ablations, a LASSO surrogate with alpha 0.01, 32 ablations as in its §4 comparisons), scored by a local
  cross-encoder instead of the generator's token log-probabilities, which ERLA cannot get. That substitution is a
  HYPOTHESIS (the paper does not discuss a proxy scorer); the scores are shown to the reader and never change a status.

Standard library and numpy only; the scorer is injected, so nothing here loads a model.
"""

from __future__ import annotations

import math
import random
import re
from collections.abc import Callable, Sequence
from typing import Any

MAX_CITATIONS = 3          # ALCE §2 fn 4: at most three citations per statement
MIN_COVERAGE = 0.5         # HYPOTHESIS (ADR-0006): share of content words printed in the cited text; measured on evals
N_ABLATIONS = 32           # ContextCite §4, Fig. 2: the headline comparisons use 32 ablations
LASSO_ALPHA = 0.01         # ContextCite App. A.4 (scikit-learn Lasso alpha)
MAX_SOURCE_SENTENCES = 48  # sentences of the cited text offered to the ablation (the ones sharing most words)

Scorer = Callable[[str, list[str]], list[float]]

TAG = re.compile(r"\[\s*(S\d+(?:\s*[,;]\s*S?\d+)*)\s*\]")
NUMBER = re.compile(r"(?<![\w.])([-−–]?)(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+\.\d*|\.\d+|\d+)(?:[eE]([-+]?\d+))?(?!\d)")
IDENT = re.compile(r"(?<![\w-])(?=[\w./-]*\d)(?=[\w./-]*[A-Za-z])[A-Za-z0-9]+(?:[-/.][A-Za-z0-9]+)*(?![\w-])")
NUM_WITH_UNIT = re.compile(r"^\d[\d.,]*(?:[a-z]{1,4}|%)$")          # 1.0in, 25ksi: a number, not an identifier
ACRONYM = re.compile(r"(?<![\w-])[A-Z]{2,}(?![\w-])")
SYMBOL = re.compile(r"(?<![\w-])(?:Ftu|Fty|Fcy|Fsu|Fbru|Fbry|KIc|KIC|KIscc|Kc|Kapp|Kt|Kf|Kbr)(?![\w-])")
DIRECTION = re.compile(r"(?<![\w-])[LTS]-[LTS](?![\w-])")
BASIS = re.compile(r"(?<![\w-])[ABS]-basis\b", re.I)
LOCATOR_BEFORE = re.compile(r"(?:\bp|\bpp|\bpage|\btable|\bfig|\bfigure|\bsection|\bsec|§|\beq|\bequation|\brev|"
                            r"\bref|\bchapter|\bch|\bno|#|\bstep|\bitem)[ \t.:]*$", re.I)
NOT_ANCHORS = frozenset({"NOT", "VERIFIED", "UNVERIFIED", "FOUND", "UNRESOLVED", "ESTIMATE", "COMPUTED", "PASS", "FAIL",
                         "UNCONFIRMED", "OCR", "AND", "OR", "THE", "SEE", "NOTE"})
STOP = frozenset("""a about above after again against all also although among an and any are as at be because been
before being below between both but by can could did do does doing done down during each either else for from
further given gives had has have having here how however if in into is it its itself just less like made make many
may more most much must near no nor not now of off often on once only or other our out over own per same shall should
since so some such than that the their them then there these they this those through thus to too under until up upon
used uses using very via was were what when where whether which while who whom why will with within without would
yet""".split())
ABBREV = re.compile(r"\b(?:e\.g|i\.e|fig|figs|eq|eqs|no|nos|ref|refs|vs|approx|ca|cf|al|vol|sec|ch|pp|p|min|max|"
                    r"incl|resp|etc)\.$", re.I)
# a sentence ends at . ! ? (with the [S#] tags right after it) before a capital, a digit, a bracket or a parenthesis;
# never before a tag, which belongs to the sentence before it
SPLIT = re.compile(r"[.!?](?:\s*\[\s*S\d[^\]]*\])*(?=\s+(?:[(A-Z0-9]|\[(?!\s*S\d)))")


# ---------------------------------------------------------------------------------------------------- text units

def strip_tags(text: str) -> str:
    """The sentence without its [S#] tags."""
    return re.sub(r"\s+([.,;:!?])", r"\1", re.sub(r"\s+", " ", TAG.sub(" ", text))).strip()


def tags(text: str) -> list[str]:
    """The [S#] tags of a sentence, in order, without repeats: '[S1, S3]' and '[S1][S3]' give ['S1', 'S3']."""
    out: list[str] = []
    for m in TAG.finditer(text):
        for t in re.split(r"\s*[,;]\s*", m.group(1)):
            t = t if t.upper().startswith("S") else "S" + t
            t = "S" + t[1:].strip()
            if t not in out:
                out.append(t)
    return out


def sentences(text: str) -> list[str]:
    """Sentences of a passage or an answer. A table row is one unit; prose is split after . ! ? before a capital,
    a digit or a bracket, not after common abbreviations (e.g., Fig., No.) or inside decimals."""
    out: list[str] = []
    para: list[str] = []

    def flush() -> None:
        if para:
            out.extend(_split(" ".join(para)))
            para.clear()

    for line in text.splitlines():
        s = line.strip()
        if not s:
            flush()
        elif s.startswith("|"):
            flush()
            if not re.fullmatch(r"[\s|:-]+", s):
                out.append(s)
        elif s.startswith("#") or re.match(r"^(?:[-*•]|\d+[.)])\s+", s):
            flush()
            out.extend(_split(re.sub(r"^(?:#+|[-*•]|\d+[.)])\s*", "", s)))
        else:
            para.append(s)
    flush()
    return [s for s in out if s.strip()]


def _split(text: str) -> list[str]:
    parts: list[str] = []
    start = 0
    for m in SPLIT.finditer(text):
        end = m.end()
        if ABBREV.search(text[start:m.start() + 1]):
            continue
        parts.append(text[start:end].strip())
        start = end
    parts.append(text[start:].strip())
    return [p for p in parts if p]


# ---------------------------------------------------------------------------------------------------- anchors

def _seq(text: str) -> str:
    """Ordered lowercase tokens: 'L-T' -> 'l t', '7075-T7351' -> '7075 t7351' (as erla.verify reads labels)."""
    s = text.lower().replace("–", "-").replace("—", "-").replace("−", "-").replace("-", " ").replace("/", " ")
    return " ".join(t for t in (w.strip(".") for w in re.findall(r"[a-z0-9.]+", s)) if t)


def numbers(text: str) -> list[tuple[float, int, int, str]]:
    out: list[tuple[float, int, int, str]] = []
    for m in NUMBER.finditer(text):
        sign, digits, exp = m.group(1), m.group(2).replace(",", ""), m.group(3)
        try:
            v = float(digits + (f"e{exp}" if exp else ""))
        except ValueError:
            continue
        # a dash right after a number ('65-75', '65 –75') separates a range; anywhere else it is a minus sign
        minus = bool(sign) and not re.search(r"\d[ \t]*$", text[:m.start()])
        out.append((-v if minus else v, m.start(), m.end(), m.group(0)))
    return out


def anchors(sentence: str) -> dict[str, list[Any]]:
    """The checkable terms of a sentence: {'terms': [identifiers, acronyms, symbols, directions, bases],
    'numbers': [(value, as written)]}. Locator numbers (p. 12, Table 3) and [S#] tags are not anchors."""
    text = strip_tags(sentence)
    terms: list[str] = []
    spans: list[tuple[int, int]] = []

    def add(term: str, a: int, b: int) -> None:
        if any(x <= a < y or x < b <= y for x, y in spans):
            return
        spans.append((a, b))
        if _seq(term) and _seq(term) not in [_seq(t) for t in terms]:
            terms.append(term)

    for rx in (BASIS, DIRECTION, SYMBOL):
        for m in rx.finditer(text):
            add(m.group(0), m.start(), m.end())
    for m in IDENT.finditer(text):
        tok = m.group(0)
        if NUM_WITH_UNIT.match(tok) or LOCATOR_BEFORE.search(text[:m.start()]):
            continue
        add(tok, m.start(), m.end())
    for m in ACRONYM.finditer(text):
        if m.group(0) not in NOT_ANCHORS:
            add(m.group(0), m.start(), m.end())
    masked = "".join(" " if any(a <= i < b for a, b in spans) else ch for i, ch in enumerate(text))
    nums: list[tuple[float, str]] = []
    for v, a, _b, written in numbers(masked):
        if LOCATOR_BEFORE.search(masked[:a]):
            continue
        if not any(math.isclose(v, x, rel_tol=1e-9, abs_tol=1e-12) for x, _ in nums):
            nums.append((v, written))
    return {"terms": terms, "numbers": nums}


def _stem(word: str) -> str:
    for suf in ("ings", "ing", "ies", "ed", "es", "s", "ly"):
        if word.endswith(suf) and len(word) - len(suf) >= 4:
            return word[:-len(suf)]
    return word


def content_words(text: str) -> set[str]:
    """Lowercase stems of the words of four letters or more that carry content (not stop words)."""
    return {_stem(w) for w in re.findall(r"[a-z]{4,}", strip_tags(text).lower()) if w not in STOP}


# ---------------------------------------------------------------------------------------------------- support

def support(sentence: str, passages: Sequence[str]) -> dict[str, Any]:
    """Whether ``passages`` (the cited text, together) support ``sentence``. Returns
    {'verdict': 'supported' | 'unsupported' | 'unknown', 'missing': [anchors printed in no passage], 'checked': [...],
    'coverage': share of content words found, 'reason'}."""
    found = anchors(sentence)
    joined = " \n ".join(passages)
    seq = f" {_seq(joined)} "
    have = [n for n, *_ in numbers(joined)]
    missing: list[str] = [t for t in found["terms"] if f" {_seq(t)} " not in seq]
    missing += [w for v, w in found["numbers"]
                if not any(math.isclose(v, x, rel_tol=1e-9, abs_tol=1e-12) for x in have)]   # the sign is part of it
    checked = [*found["terms"], *(w for _, w in found["numbers"])]
    words = content_words(sentence) - {_stem(_seq(t)) for t in found["terms"]}
    pool = {_stem(w) for w in re.findall(r"[a-z]{4,}", joined.lower())}
    coverage = 1.0 if not words else sum(1 for w in words if w in pool) / len(words)
    out: dict[str, Any] = {"checked": checked, "missing": missing, "coverage": round(coverage, 3)}
    if missing:
        out.update(verdict="unsupported", reason=f"not printed in the cited text: {', '.join(missing)}")
    elif not checked and not words:
        out.update(verdict="unknown", reason="the sentence has no term code can check")
    elif coverage < MIN_COVERAGE:
        out.update(verdict="unknown", reason=(f"only {coverage:.0%} of the sentence's content words are printed in the "
                                              f"cited text (at least {MIN_COVERAGE:.0%} needed): code cannot judge a "
                                              "paraphrase"))
    elif not checked:
        out.update(verdict="supported", reason="its words are printed in the cited text (no number or identifier to check)")
    else:
        out.update(verdict="supported", reason=f"{', '.join(checked)} printed in the cited text")
    return out


def irrelevant(sentence: str, passages: Sequence[str]) -> list[bool]:
    """ALCE precision (§3.3): passage i is irrelevant when it alone does not support the sentence and the others do."""
    if len(passages) < 2:
        return [False] * len(passages)
    out: list[bool] = []
    for i, p in enumerate(passages):
        rest = [q for j, q in enumerate(passages) if j != i]
        out.append(support(sentence, [p])["verdict"] != "supported" and support(sentence, rest)["verdict"] == "supported")
    return out


def citation_scores(statements: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """ALCE citation recall and precision over statements {'cites': [...], 'supported': bool, 'irrelevant': [bool]}:
    recall = share of statements with a citation whose cited passages support them; precision = share of citations
    on supported statements that are not irrelevant. None when there is nothing to score."""
    n = len(statements)
    cites = sum(len(s.get("cites") or []) for s in statements)
    recalled = [bool(s.get("cites")) and bool(s.get("supported")) for s in statements]
    good = sum(sum(1 for x in (s.get("irrelevant") or [False] * len(s.get("cites") or [])) if not x)
               for s, r in zip(statements, recalled) if r)
    return {"statements": n, "citations": cites,
            "citation_recall": round(sum(recalled) / n, 4) if n else None,
            "citation_precision": round(good / cites, 4) if cites else None}


# ---------------------------------------------------------------------------------------------------- ContextCite proxy

def _lasso(x: Any, y: Any, alpha: float, iters: int = 500, tol: float = 1e-7) -> Any:
    """Coordinate-descent LASSO with an intercept: min (1/2n)||y - Xw - b||^2 + alpha ||w||_1 (scikit-learn's form)."""
    import numpy as np
    n, d = x.shape
    xm, ym = x.mean(axis=0), y.mean()
    xc, yc = x - xm, y - ym
    norms = (xc ** 2).sum(axis=0) / n
    w = np.zeros(d)
    r = yc.copy()
    for _ in range(iters):
        delta = 0.0
        for j in range(d):
            if norms[j] == 0:
                continue
            old = w[j]
            rho = float(xc[:, j] @ (r + xc[:, j] * old)) / n
            new = math.copysign(max(abs(rho) - alpha, 0.0), rho) / norms[j]
            if new != old:
                r -= xc[:, j] * (new - old)
                w[j] = new
                delta = max(delta, abs(new - old))
        if delta < tol:
            break
    return w


def _to_logit(p: float) -> float:
    p = min(max(float(p), 1e-6), 1 - 1e-6)
    return math.log(p / (1 - p))


def anchored(claim: str, units: Sequence[str]) -> list[int]:
    """Indexes of the sentences that print the most of the claim's anchors (all of them when the claim has none, or
    when no sentence prints one). Code narrows the candidates before the scorer ranks them: on near-miss sources (the
    same sentence with another number or temper) the ablation surrogate alone found the right sentence in 0 of 8
    cases, because near-duplicates are redundant to a linear model (docs/evals/attribution-proxy-2026-11-18.json)."""
    a = anchors(claim)
    if not a["terms"] and not a["numbers"]:
        return list(range(len(units)))
    counts: list[int] = []
    for u in units:
        seq, have = f" {_seq(u)} ", [n for n, *_ in numbers(u)]
        counts.append(sum(1 for t in a["terms"] if f" {_seq(t)} " in seq) + sum(
            1 for v, _ in a["numbers"] if any(math.isclose(v, x, rel_tol=1e-9, abs_tol=1e-12) for x in have)))
    best = max(counts, default=0)
    return [i for i, c in enumerate(counts) if c == best] if best else list(range(len(units)))


def pick_sentences(claim: str, units: Sequence[str], limit: int = MAX_SOURCE_SENTENCES) -> list[str]:
    """The cited text's sentences to attribute over: those printing the claim's anchors (``anchored``), then at most
    ``limit`` of them, the ones sharing most words with the claim (kept in document order)."""
    units = [units[i] for i in anchored(claim, units)]
    if len(units) <= limit:
        return list(units)
    want = content_words(claim) | {_seq(t) for t in anchors(claim)["terms"]}
    ranked = sorted(range(len(units)), key=lambda i: -len(want & (content_words(units[i]) | set(_seq(units[i]).split()))))
    keep = sorted(ranked[:limit])
    return [units[i] for i in keep]


def ablation_scores(claim: str, units: Sequence[str], scorer: Scorer, n: int = N_ABLATIONS,
                    alpha: float = LASSO_ALPHA, seed: int = 0) -> list[float]:
    """ContextCite-style weights, one per source sentence: score the claim against ``n`` random ablations of the
    sentences (each kept with probability 1/2), take the logit of each score, fit a LASSO surrogate. ``scorer(query,
    passages)`` returns a probability-like score per passage."""
    import numpy as np
    d = len(units)
    if d == 0:
        return []
    rng = random.Random(seed)
    masks = [[rng.random() < 0.5 for _ in range(d)] for _ in range(n)]
    texts = [" ".join(u for u, keep in zip(units, m) if keep) for m in masks]
    ys = [_to_logit(p) for p in scorer(claim, texts)]
    w = _lasso(np.array(masks, dtype=float), np.array(ys, dtype=float), alpha)
    return [float(v) for v in w]


def leave_one_out(claim: str, units: Sequence[str], scorer: Scorer) -> list[float]:
    """Baseline (ContextCite §4): the drop in the claim's score when one sentence is removed."""
    if not units:
        return []
    full = _to_logit(scorer(claim, [" ".join(units)])[0])
    drops = scorer(claim, [" ".join(u for j, u in enumerate(units) if j != i) for i in range(len(units))])
    return [full - _to_logit(p) for p in drops]


def direct(claim: str, units: Sequence[str], scorer: Scorer) -> list[float]:
    """Baseline (ContextCite §4, semantic similarity): each sentence scored against the claim on its own."""
    return [_to_logit(p) for p in scorer(claim, list(units))] if units else []


def top(units: Sequence[str], weights: Sequence[float], k: int = 3) -> list[dict[str, Any]]:
    """The k sentences with the largest positive weights, for display."""
    order = sorted(range(len(units)), key=lambda i: -weights[i])
    return [{"sentence": re.sub(r"\s+", " ", units[i])[:240], "weight": round(weights[i], 3)}
            for i in order[:k] if weights[i] > 0]
