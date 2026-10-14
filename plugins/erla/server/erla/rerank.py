"""A1: local cross-encoder re-ranking of the top search candidates (fastembed / ONNX, no remote API).

The cross-encoder reads (query, passage) pairs jointly, which BM25 and bi-encoder vectors cannot do,
so it separates 'mentions the words' from 'answers the question'. It only re-orders the top
`rerank.top_n` documents; tier and standard-number boosts are re-applied afterwards (R1.1).
"""

from __future__ import annotations

import math
import os
import re

os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")

DEFAULT_MODEL = "Xenova/ms-marco-MiniLM-L-12-v2"
_MODELS: dict = {}


def enabled(cfg) -> bool:
    return bool(cfg is not None and (cfg.rerank or {}).get("enabled", False))


def model(cfg):
    name = (cfg.rerank or {}).get("model", DEFAULT_MODEL)
    if name not in _MODELS:
        from fastembed.rerank.cross_encoder import TextCrossEncoder
        _MODELS[name] = TextCrossEncoder(model_name=name, cache_dir=str(cfg.agent_root / "models"))
    return _MODELS[name]


def window(text: str, words: list[str], size: int = 1500) -> str:
    """The `size`-char passage of a chunk centred on the densest cluster of query words.
    Chunks are up to ~6000 chars; the model reads ~512 tokens, so the passage must contain the match."""
    if len(text) <= size:
        return text
    low = text.lower()
    hits = sorted(m.start() for w in words if len(w) > 1
                  for m in re.finditer(re.escape(w.lower()), low))
    if not hits:
        return text[:size]
    best, best_n, j = hits[0], 0, 0
    for i, h in enumerate(hits):
        while hits[j] < h - size // 2:
            j += 1
        if i - j + 1 > best_n:
            best, best_n = h, i - j + 1
    start = max(0, min(best - size // 2, len(text) - size))
    return text[start:start + size]


def score(cfg, query: str, passages: list[str]) -> list[float]:
    """Cross-encoder relevance in (0, 1) per passage: sigmoid(logit / temperature).

    ms-marco logits span roughly -11..+11; a plain sigmoid saturates at 0.99-1.00 for most technical
    passages, which hands the ordering back to the tier boost. Dividing by `temperature` (default 4)
    keeps the relevance differences visible before tier/standard boosts are applied."""
    if not passages:
        return []
    r = cfg.rerank or {}
    logits = list(model(cfg).rerank(query, passages, batch_size=int(r.get("batch", 16))))
    t = float(r.get("temperature", 4.0))
    return [1.0 / (1.0 + math.exp(-float(x) / t)) for x in logits]
