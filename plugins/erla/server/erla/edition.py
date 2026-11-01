"""Edition rendering of protocol and plugin text: the full edition, or the free core edition (no specialists).

The source files carry specialist-only passages between markers (one source of truth, law L5):

    <!-- full -->            whole lines up to <!-- /full -->: full edition only
    <!--full-->text<!--/full-->   inline: full edition only
    <!--core:text-->         core edition only (replacement wording for what the full markers remove)

``render(text, core)`` resolves them. packaging/build.py renders every shipped protocol and plugin
markdown file, and the server renders a source checkout's files when it serves them, so no edition
ever shows a marker. packaging/build.py also checks the core edition for specialist details.
"""

from __future__ import annotations

import re

BLOCK = re.compile(r"^<!-- full -->[ \t]*\r?\n.*?^<!-- /full -->[ \t]*(?:\r?\n|\Z)", re.M | re.S)
BLOCK_MARK = re.compile(r"^<!-- /?full -->[ \t]*(?:\r?\n|\Z)", re.M)
INLINE = re.compile(r"<!--full-->(.*?)<!--/full-->", re.S)
# one line only: with re.S, ".*?" ran past a line end to the next "-->" and dropped the lines between
# (correctness audit 2026-10-31; no shipped file triggered it yet)
CORE_LINE = re.compile(r"^<!--core:([^\n]*?)-->[ \t]*(\r?\n|\Z)", re.M | re.S)
CORE = re.compile(r"<!--core:(.*?)-->", re.S)
MARKER = re.compile(r"<!--\s*/?full\s*-->|<!--core:")


def render(text: str, core: bool) -> str:
    if core:
        text = BLOCK.sub("", text)
        text = INLINE.sub("", text)
        text = CORE_LINE.sub(lambda m: m.group(1) + m.group(2), text)
        return CORE.sub(lambda m: m.group(1), text)
    text = BLOCK_MARK.sub("", text)
    text = INLINE.sub(lambda m: m.group(1), text)
    text = CORE_LINE.sub("", text)
    return CORE.sub("", text)

