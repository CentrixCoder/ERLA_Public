"""A1: local re-ranking of the top search candidates (no remote API: library text never leaves this computer).

A cross-encoder reads (query, passage) pairs jointly, which BM25 and bi-encoder vectors cannot do, so it
separates 'mentions the words' from 'answers the question'. It only re-orders the top `rerank.top_n`
documents; tier and standard-number boosts are re-applied afterwards (R1.1).

Re-rankers (`rerank.reranker` in the site config, or per search from the control panel):

  ms-marco    Xenova/ms-marco-MiniLM-L-12-v2 on the CPU through fastembed (the default; 0.12 GB, about 3.5 s per
              search). Kept as the default in 0.4.0 for speed (user decision 2026-10-16)
  bge-v2-m3   BAAI/bge-reranker-v2-m3 (int8 ONNX) on the CPU through fastembed (option; 0.57 GB, about 5x slower).
              int8 ranks like full precision (nDCG@10 0.774 vs 0.776 at temperature 2.0) at half the time and a
              quarter of the size; temperature 4.0 lifts it to 0.794, because the softer score leaves the tier and
              standard priors room to act (golden set, 2026-10-15)
  qwen3-4b    Qwen/Qwen3-Reranker-4B, LLM re-ranker on an NVIDIA GPU (PyTorch + CUDA, about 10 GB)
  custom      any fastembed cross-encoder named in `rerank.model`

Qwen3-Reranker-0.6B and bge-reranker-v2-gemma were evaluated and left out (user decision 2026-10-15: not
worth their setup cost next to these two).

A GPU re-ranker that cannot run here (no PyTorch, no CUDA, too little GPU memory) is never used silently:
`status()` says why and how to fix it, and a search that asked for it falls back to the default and says
so in its mode string.
"""

from __future__ import annotations

import math
import os
import re
import sys
from typing import Any, cast

os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")

DEFAULT = "ms-marco"
LEGACY_MODEL = "Xenova/ms-marco-MiniLM-L-12-v2"
# vram_gb: GPU memory the model needs in float16 with batch room (weights alone are about 2 bytes/parameter)
RERANKERS: dict[str, dict[str, Any]] = {
    "ms-marco": {"label": "ms-marco MiniLM (default, CPU, fast)", "kind": "fastembed",
                 "model": LEGACY_MODEL, "temperature": 4.0},
    "bge-v2-m3": {"label": "BGE reranker v2-m3 (CPU, slower)", "kind": "fastembed",
                  "model": "BAAI/bge-reranker-v2-m3-int8", "temperature": 4.0},   # int8 ONNX: same model, 0.57 GB
    "qwen3-4b": {"label": "Qwen3-Reranker 4B (GPU)", "kind": "qwen3", "model": "Qwen/Qwen3-Reranker-4B",
                 "vram_gb": 10.0},
}
TORCH_INDEX = "https://download.pytorch.org/whl/cu124"
_MODELS: dict[str, Any] = {}

# Qwen3-Reranker prompt, as on the model card (Qwen/Qwen3-Reranker-4B; the same for the whole family)
QWEN_PREFIX = ("<|im_start|>system\nJudge whether the Document meets the requirements based on the Query and the "
               "Instruct provided. Note that the answer can only be \"yes\" or \"no\".<|im_end|>\n<|im_start|>user\n")
QWEN_SUFFIX = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
QWEN_INSTRUCT = ("Given an engineering question, judge whether the passage from an engineering document gives the "
                 "value, method or requirement the question asks for")


def enabled(cfg) -> bool:
    return bool(cfg is not None and (cfg.rerank or {}).get("enabled", False))


def selected(cfg, requested: str | None = None) -> str:
    """The re-ranker key to use: the per-search request, else the site's `rerank.reranker`, else the default."""
    site = (cfg.rerank or {}).get("reranker") if cfg is not None else None
    key = str(requested or site or DEFAULT)
    return key if key in RERANKERS or key == "custom" else DEFAULT


def _gpu() -> dict[str, Any]:
    """What this computer offers a GPU re-ranker: PyTorch, transformers, a CUDA device and its memory."""
    import importlib.util
    info: dict[str, Any] = {"torch": importlib.util.find_spec("torch") is not None,
                            "transformers": importlib.util.find_spec("transformers") is not None,
                            "cuda": False, "device": None, "vram_gb": None}
    if info["torch"]:
        import torch
        if torch.cuda.is_available():
            p = torch.cuda.get_device_properties(0)
            info.update(cuda=True, device=p.name, vram_gb=round(p.total_memory / 2 ** 30, 1))
    return info


def status(key: str) -> dict[str, Any]:
    """Can `key` run here? {available, reason, fix} - the control panel shows reason and fix."""
    spec = RERANKERS.get(key)
    if spec is None:
        return {"key": key, "available": key == "custom", "reason": "" if key == "custom" else "unknown re-ranker"}
    out: dict[str, Any] = {"key": key, "label": spec["label"], "model": spec["model"], "available": True,
                           "reason": "", "fix": ""}
    if spec["kind"] == "fastembed":
        return out
    g = _gpu()
    out["gpu"] = g
    install = (f'"{sys.executable}" -m pip install torch --index-url {TORCH_INDEX}' + "\n"
               f'"{sys.executable}" -m pip install "transformers>=4.56" accelerate')
    if not g["torch"] or not g["transformers"]:
        missing = " and ".join(m for m in ("torch", "transformers") if not g[m])
        out.update(available=False,
                   reason=f"PyTorch with CUDA needs to be installed for this re-ranker ({missing} not found in "
                          "ERLA's Python).",
                   fix="Install PyTorch (CUDA build) and transformers into ERLA's Python, then restart ERLA:\n" + install)
    elif not g["cuda"]:
        out.update(available=False,
                   reason="PyTorch is installed but finds no CUDA GPU: check that the video card is an NVIDIA card "
                          "with a current driver and that the CUDA build of PyTorch is installed.",
                   fix="Reinstall the CUDA build of PyTorch:\n" + install)
    elif (g["vram_gb"] or 0) < spec["vram_gb"]:
        out.update(available=False,
                   reason=f"Video card {g['device']} has {g['vram_gb']} GB of memory; {spec['label']} needs about "
                          f"{spec['vram_gb']:g} GB. Check the video card specs, or choose a smaller re-ranker.",
                   fix="Use a CPU re-ranker: the default (ms-marco MiniLM) or BGE reranker v2-m3.")
    return out


def _fastembed(cfg, name: str):
    if name not in _MODELS:
        from fastembed.rerank.cross_encoder import TextCrossEncoder
        _MODELS[name] = TextCrossEncoder(model_name=name, cache_dir=str(cfg.agent_root / "models"))
    return _MODELS[name]


def _hf(cfg, key: str):
    """(tokenizer, model) on the GPU in float16, loaded once per process."""
    if key not in _MODELS:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer
        name = RERANKERS[key]["model"]
        cache = str(cfg.agent_root / "models")
        tok = AutoTokenizer.from_pretrained(name, cache_dir=cache, padding_side="left")
        model = cast(Any, AutoModelForCausalLM).from_pretrained(name, cache_dir=cache, dtype=torch.float16).cuda().eval()
        _MODELS[key] = (tok, model)
    return _MODELS[key]


def _qwen3(cfg, key: str, query: str, passages: list[str], batch: int) -> list[float]:
    """P(yes) per passage (model card: log-softmax over the 'no'/'yes' logits of the last position)."""
    import torch
    tok, model = _hf(cfg, key)
    yes, no = tok.convert_tokens_to_ids("yes"), tok.convert_tokens_to_ids("no")
    out: list[float] = []
    for i in range(0, len(passages), batch):
        texts = [f"{QWEN_PREFIX}<Instruct>: {QWEN_INSTRUCT}\n<Query>: {query}\n<Document>: {p}{QWEN_SUFFIX}"
                 for p in passages[i:i + batch]]
        enc = tok(texts, padding=True, truncation=True, max_length=8192, return_tensors="pt").to(model.device)
        with torch.no_grad():
            logits = model(**enc).logits[:, -1, :]
        pair = torch.stack([logits[:, no], logits[:, yes]], dim=1).float()
        out += torch.nn.functional.log_softmax(pair, dim=1)[:, 1].exp().tolist()
    return out


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


def score(cfg, query: str, passages: list[str], reranker: str | None = None) -> tuple[list[float], str]:
    """Relevance in (0, 1) per passage and the re-ranker key that produced it.

    Cross-encoder logits are squashed with sigmoid(logit / temperature) so the relevance differences
    stay visible before the tier and standard boosts (a plain sigmoid saturates near 1 for most
    technical passages). A requested GPU re-ranker that cannot run here falls back to the default."""
    if not passages:
        return [], selected(cfg, reranker)
    r = cfg.rerank or {}
    key = selected(cfg, reranker)
    if key in RERANKERS and RERANKERS[key]["kind"] != "fastembed" and not status(key)["available"]:
        key = DEFAULT + "(fallback)"
    batch = int(r.get("batch", 16))
    base = key.removesuffix("(fallback)")
    if base == "custom":
        name, t = str(r.get("model") or LEGACY_MODEL), float(r.get("temperature", 4.0))
    elif RERANKERS[base]["kind"] == "fastembed":
        name, t = RERANKERS[base]["model"], float(r.get("temperature", RERANKERS[base]["temperature"]))
    else:
        return _qwen3(cfg, base, query, passages, max(1, batch // 4)), key
    logits = list(_fastembed(cfg, name).rerank(query, passages, batch_size=batch))
    return [1.0 / (1.0 + math.exp(-float(x) / t)) for x in logits], key
