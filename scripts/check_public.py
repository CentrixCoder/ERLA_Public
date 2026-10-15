#!/usr/bin/env python3
"""Fail if the public copy of ERLA holds anything that must not be public, or its manifests disagree.

Runs in this repo's CI, before every release, and in the private ERLA repo before it pushes here.

    python scripts/check_public.py .                 # scan plugins/ and .claude-plugin/
    python scripts/check_public.py . --tag v0.3.5    # also require the tag to match the plugin version
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

SCANNED = ("plugins", ".claude-plugin")
# The specialist agents are private (ERLA full edition only); the public core edition must never carry them.
SPECIALIST_FILES = re.compile(r"(?i)(^|/)(\w+_skills\.md|dtcalc\.py|jointcalc\.py|[\w-]+-agent\.md|commands/(dt|joint)\.md)$")
MAX_BYTES = 2_000_000

# Files that only exist on a working site: index, logs, site notes, recorded lessons/feedback, secrets.
BLOCKED_FILE = re.compile(
    r"(?i)(\.(db|sqlite3?|duckdb|parquet|pkl|npy|faiss|log|env|pem|key|pfx|p12)$"
    r"|^site_notes\.md$|^library_config\.yaml$|^lessons.*|^feedback.*|^requests?\.jsonl$)")

BLOCKED_TEXT = {
    "personal path": re.compile(r"(?i)\b[a-z]:\\{1,2}(users|documents and settings)\\{1,2}[^\\\s\"'`]+"
                                r"|/(Users|home)/[A-Za-z0-9_.-]+/"),
    "private repository link": re.compile(r"github\.com/CentrixCoder/ERLA\b"),
    "development machine name": re.compile(r"(?i)\bdavinci\b"),
    "email address": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[a-z]{2,}\b"),
    "secret or token": re.compile(r"sk-ant-[A-Za-z0-9_-]{10,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
                                  r"|AKIA[0-9A-Z]{16}|xox[abpr]-[A-Za-z0-9-]{10,}|-----BEGIN [A-Z ]*PRIVATE KEY-----"),
}


def scan(root: Path) -> list[str]:
    problems = []
    for top in SCANNED:
        for p in sorted((root / top).rglob("*")):
            if not p.is_file() or "__pycache__" in p.parts:
                continue
            rel = p.relative_to(root).as_posix()
            if SPECIALIST_FILES.search(rel):
                problems.append(f"{rel}: private specialist file")
                continue
            if BLOCKED_FILE.search(p.name):
                problems.append(f"{rel}: file type or name is site-only data")
                continue
            if p.stat().st_size > MAX_BYTES:
                problems.append(f"{rel}: larger than {MAX_BYTES // 1_000_000} MB")
                continue
            try:
                text = p.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                problems.append(f"{rel}: binary file")
                continue
            problems += _text_problems(rel, text)
    readme = root / "README.md"            # release notes are written into it at every release
    if readme.is_file():
        problems += _text_problems("README.md", readme.read_text(encoding="utf-8"))
    return problems


def _text_problems(rel: str, text: str) -> list[str]:
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        for label, rx in BLOCKED_TEXT.items():
            if m := rx.search(line):
                out.append(f"{rel}:{n}: {label}: {m.group(0)[:60]}")
    return out


def versions(root: Path, tag: str | None) -> list[str]:
    try:
        plugin = json.loads((root / "plugins/erla/.claude-plugin/plugin.json").read_text(encoding="utf-8"))
        market = json.loads((root / ".claude-plugin/marketplace.json").read_text(encoding="utf-8"))
        json.loads((root / "plugins/erla/.mcp.json").read_text(encoding="utf-8"))
        core = (root / "plugins/erla/server/erla/core/VERSION").read_text(encoding="utf-8").strip()
    except (OSError, ValueError) as e:
        return [f"manifest missing or not valid JSON: {e}"]
    found = {"plugin.json": plugin.get("version"), "marketplace.json": market.get("version"), "core/VERSION": core}
    badge = re.search(r"img\.shields\.io/badge/version-([0-9][0-9A-Za-z.]*)-blue",
                      (root / "README.md").read_text(encoding="utf-8")) if (root / "README.md").is_file() else None
    if badge:
        found["README badge"] = badge.group(1)
    if tag:
        found["tag"] = tag.removeprefix("v")
    if len(set(found.values())) != 1:
        return ["versions disagree: " + ", ".join(f"{k}={v}" for k, v in found.items())]
    if [p.get("source") for p in market.get("plugins", [])] != ["./plugins/erla"]:
        return ["marketplace.json must list exactly one plugin with source ./plugins/erla"]
    return []


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("root", type=Path)
    ap.add_argument("--tag", help="release tag that must match the plugin version, e.g. v0.3.5")
    args = ap.parse_args()
    problems = versions(args.root, args.tag) + scan(args.root)
    for p in problems:
        print(f"FAIL {p}")
    if problems:
        print(f"{len(problems)} problem(s): nothing may be published until they are fixed.")
        return 1
    print("public check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
