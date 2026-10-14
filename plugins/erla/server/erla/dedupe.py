"""Exact (sha256) and near-duplicate (64-bit simhash) detection."""

from __future__ import annotations

import hashlib
import os
import re
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any

TOKEN_RE = re.compile(r"[a-z0-9]{3,}")


def sha256_file(path: Path, bufsize: int = 4 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:          # read-only (G1)
        while True:
            b = fh.read(bufsize)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _load_native() -> Callable[[str, int], str | None] | None:
    """Rust core (rust/erla_core, Step 3) when installed and not disabled by ERLA_PURE_PYTHON=1."""
    if os.environ.get("ERLA_PURE_PYTHON") == "1":
        return None
    import importlib
    try:
        mod: Any = importlib.import_module("erla_core")
    except ImportError:
        return None
    fn: Callable[[str, int], str | None] = mod.simhash
    return fn


_NATIVE = _load_native()
BACKEND = "rust" if _NATIVE else "python"


def simhash(text: str, max_chars: int = 300_000) -> str | None:
    """64-bit simhash over word 3-shingles of normalised text (hex string).

    Uses the Rust core when available; it is bit-identical to :func:`_simhash_py`
    (tests/test_rust_parity.py). Text Rust cannot accept (lone surrogates) uses Python."""
    if _NATIVE is not None:
        try:
            return _NATIVE(text, max_chars)
        except (UnicodeError, ValueError):
            pass
    return _simhash_py(text, max_chars)


def _simhash_py(text: str, max_chars: int = 300_000) -> str | None:
    """Reference implementation (authoritative)."""
    toks = TOKEN_RE.findall(text[:max_chars].lower())
    if len(toks) < 30:
        return None
    v = [0] * 64
    for i in range(len(toks) - 2):
        h = int.from_bytes(hashlib.blake2b(" ".join(toks[i:i + 3]).encode(), digest_size=8).digest(), "big")
        for b in range(64):
            v[b] += 1 if (h >> b) & 1 else -1
    out = 0
    for b in range(64):
        if v[b] > 0:
            out |= 1 << b
    return f"{out:016x}"


def hamming(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def near_duplicate_pairs(items: list[tuple[int, str]], max_dist: int = 3) -> list[tuple[int, int, int]]:
    """Return (doc_a, doc_b, distance) for simhashes within ``max_dist`` bits.

    Uses 4 x 16-bit bands: with <=3 differing bits, at least one band is equal
    (pigeonhole), so banding finds every qualifying pair."""
    buckets: dict[tuple[int, str], list[tuple[int, str]]] = defaultdict(list)
    for doc_id, sh in items:
        for band in range(4):
            buckets[(band, sh[band * 4:(band + 1) * 4])].append((doc_id, sh))
    seen: set[tuple[int, int]] = set()
    out: list[tuple[int, int, int]] = []
    for group in buckets.values():
        if len(group) < 2 or len(group) > 500:
            continue
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                key = (min(a[0], b[0]), max(a[0], b[0]))
                if key in seen:
                    continue
                seen.add(key)
                d = hamming(a[1], b[1])
                if d <= max_dist:
                    out.append((key[0], key[1], d))
    return out
