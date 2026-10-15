"""Progressive disclosure of protocol files (law L4: context is a budget).

A long protocol file stays one file (one source of truth, edited only under rules section 9);
an agent reads its compact index first and then only the sections the task needs:

  index(path)                        header, rules in full (binding on every task), section list,
                                     and one line per skill / workflow / lesson
  section(path, "S8")                one skill block;  a rule, lesson or workflow id the same way
                                     (prefixed ids such as "XX-S8" too);  "5" or "§5" a whole section

Works on any protocol file with '## N.' sections and '**ID ...**' / '- **ID ...**' blocks.
"""

from __future__ import annotations

import re
from pathlib import Path

from .edition import render

SECTION = re.compile(r"^## (\d+)\.", re.M)
BLOCK_HEAD = re.compile(r"^(?:- )?\*\*((?:[A-Z]+-)?[A-Z]{1,2}\d+)\b")


def _sections(text: str) -> list[tuple[str, int, int]]:
    """(number, start, end) of each '## N.' section."""
    marks = [(m.group(1), m.start()) for m in SECTION.finditer(text)]
    return [(n, s, marks[i + 1][1] if i + 1 < len(marks) else len(text)) for i, (n, s) in enumerate(marks)]


def _blocks(text: str) -> dict[str, str]:
    """ID -> block text. A block runs from its '**ID' line to the next block, heading or blank-line gap
    before a non-continuation line."""
    lines = text.splitlines()
    out: dict[str, str] = {}
    cur: str | None = None
    buf: list[str] = []
    def flush() -> None:
        if cur is not None:
            out.setdefault(cur, "\n".join(buf).strip())
    for line in lines:
        m = BLOCK_HEAD.match(line)
        if m or line.startswith("#"):
            flush()
            cur, buf = (m.group(1), [line]) if m else (None, [])
            continue
        if cur is not None:
            if line.startswith("- ") and buf and buf[0].startswith("- "):
                flush()                               # next bullet of a list that is not a block
                cur, buf = None, []
                continue
            buf.append(line)
    flush()
    return out


def _first_sentence(block: str) -> str:
    head = block.splitlines()[0]
    head = re.sub(r"^(?:- )?\*\*", "", head)
    return head.split("**")[0].strip()[:140]


def index(path: Path) -> str:
    text = render(path.read_text(encoding="utf-8"), core=False)
    secs = _sections(text)
    head = text[: secs[0][1]] if secs else text
    head = head.split("\n---", 1)[0].rstrip()
    blocks = _blocks(text)
    rules = [b for k, b in blocks.items() if re.match(r"[A-Z]+-R\d+$", k)]
    out = [head, "", f"Progressive index of {path.name}: read the rules below in full; fetch any skill, "
           "workflow, lesson or numbered section with protocol_section(file, key), e.g. a block id or '5'.",
           "", "## Sections"]
    for n, s, _ in secs:
        out.append("- " + text[s:text.index("\n", s)].lstrip("# ").strip())
    if rules:
        out += ["", "## Rules (binding, in full)"] + rules
    for label, pat in (("Skills", r"-S\d+$"), ("Workflows", r"-W\d+$"), ("Lessons (do-not list)", r"-L\d+$")):
        ids = [k for k in blocks if re.search(pat, k)]
        if ids:
            out += ["", f"## {label}"] + [f"- {k}: {_first_sentence(blocks[k]).removeprefix(k).strip(' .—-')}" for k in ids]
    return "\n".join(out) + "\n"


def section(path: Path, key: str) -> dict[str, str | list[str]]:
    text = render(path.read_text(encoding="utf-8"), core=False)
    k = key.strip().lstrip("§").strip()
    if k.isdigit():
        for n, s, e in _sections(text):
            if n == k:
                return {"file": path.name, "key": key, "text": text[s:e].strip()}
    blocks = _blocks(text)
    for bid, body in blocks.items():
        if bid.lower() == k.lower():
            return {"file": path.name, "key": bid, "text": body}
    return {"file": path.name, "key": key, "error": f"no section or block '{key}'",
            "available": [n for n, _, _ in _sections(text)] + list(blocks)}
