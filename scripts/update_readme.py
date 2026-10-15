#!/usr/bin/env python3
"""Set the README's version badge and put a release's notes at the top of its "What's new" section.

Run by the private ERLA release before it pushes here; re-running for the same version replaces that
version's notes. The three most recent versions are kept; older ones live on the Releases page.

    python scripts/update_readme.py README.md 0.3.6 notes.md
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

BADGE = re.compile(r"(https://img\.shields\.io/badge/version-)[0-9][0-9A-Za-z.]*(-blue)")
BLOCK = re.compile(r"(<!-- release-notes:start -->\n)(.*?)(<!-- release-notes:end -->)", re.S)
SECTION = re.compile(r"^### ", re.M)
KEEP = 3


def update(readme: str, version: str, notes: str) -> str:
    if not BADGE.search(readme) or not BLOCK.search(readme):
        raise SystemExit("README has no version badge or release-notes markers; nothing was changed")
    readme = BADGE.sub(rf"\g<1>{version}\g<2>", readme)

    def notes_block(m: re.Match) -> str:
        sections = [s for s in SECTION.split(m.group(2)) if s.strip()]
        sections = [s for s in sections if not s.startswith(f"{version}\n")]       # re-run: replace
        new = f"{version}\n\n{notes.strip()}\n\n"
        kept = [new, *sections][:KEEP]
        return m.group(1) + "".join(f"### {s.rstrip()}\n\n" for s in kept) + m.group(3)

    return BLOCK.sub(notes_block, readme, count=1)


def main() -> int:
    readme, version, notes = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3]).read_text(encoding="utf-8")
    readme.write_text(update(readme.read_text(encoding="utf-8"), version, notes), encoding="utf-8")
    print(f"README updated for {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
