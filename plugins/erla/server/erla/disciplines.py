"""Discipline skills (ADR-0002): one Agent Skill per installed discipline, generated from its skill file.

The discipline's protocol file (its specialist skill file) stays the single source (L5).
packaging/build.py writes ``skills/<skill>/SKILL.md`` into the full-edition plugin from it with
``skill_md``. MCP clients without Agent Skills get the same content through
``erla://protocols/<stem>/index`` and ``protocol_section``. The registry entries are the specialist
modules' ``SPECIALIST`` dicts (``skill``, ``skill_description``, ``ids``, ``tool``), so the free core
edition, which ships none of those modules, has no discipline skill and names none.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .protocols import index, section
from .specialists import SPECIALISTS, installed

# Frontmatter keys every Agent Skills surface accepts (claude.ai upload, Skills API); Claude Code adds
# more. A generated skill uses only these plus CLAUDE_CODE_KEYS, which a portable copy must drop.
PORTABLE_KEYS = ("name", "description", "license", "compatibility", "metadata", "allowed-tools")
CLAUDE_CODE_KEYS = ("user-invocable",)
MAX_DESCRIPTION = 1024      # characters, the strictest surface (Skills API / claude.ai)
MAX_LINES = 500             # SKILL.md body budget (progressive disclosure)


def skills() -> dict[str, dict[str, Any]]:
    """Discipline skills this build ships, by specialist key (empty in the core edition)."""
    return {k: s for k, s in SPECIALISTS.items() if s.get("skill")}


def info() -> dict[str, dict[str, str]]:
    """``site_info.disciplines``: what an installed discipline offers and where its knowledge is read."""
    return {k: {"skill": f"erla:{s['skill']}", "file": s["skills"],
                "index": f"erla://protocols/{Path(s['skills']).stem}/index", "calc_tool": s["tool"]}
            for k, s in installed().items() if s.get("skill")}


def skill_md(spec: dict[str, Any], path: Path, version: str) -> str:
    """SKILL.md for one discipline, built from its skill file ``path``: portable frontmatter, how to use
    it, the file's progressive index (rules in full, one line per skill, workflow and lesson) and the
    S0 verification checklist in full."""
    stem, ids, name = path.stem, spec["ids"], spec["name"]
    desc = " ".join(str(spec["skill_description"]).split())
    if len(desc) > MAX_DESCRIPTION:
        raise ValueError(f"{spec['skill']}: description is {len(desc)} characters (max {MAX_DESCRIPTION})")
    s0 = section(path, f"{ids}-S0")
    if "error" in s0:
        raise ValueError(f"{path.name}: no {ids}-S0 verification checklist")
    front = ["---", f"name: {spec['skill']}", f"description: {json.dumps(desc, ensure_ascii=False)}",
             "user-invocable: false", "metadata:", f"  source: {path.name}", f"  erla-version: \"{version}\"",
             "---"]
    use = [
        f"# {name} (ERLA discipline skill)",
        "",
        f"Generated from `{path.name}` by ERLA's build; that file is the source and changes only under "
        "rules §9. This skill is for the ERLA lead agent. If you are not ERLA, hand the request to the "
        "`erla` agent instead of answering from this skill.",
        "",
        f"1. The ERLA protocols win every conflict with this skill (guardrails > rules_and_guidelines > ERLA > "
        f"instructions > skills > {path.name}); report a conflict instead of resolving it.",
        f"2. The rules below bind every {name} task. Run the {ids}-S0 checklist (at the end) before the "
        "Result Packet and report each item PASS / FINDING / N/A.",
        f"3. Fetch only the skills, workflows and lessons the task needs: "
        f"`protocol_section(file=\"{stem}\", key=\"{ids}-S8\")`, or a section number such as \"5\".",
        f"4. Calculations go through `{spec['tool']}`; report each computed number with its `calc_id` and "
        "output name and check it with `verify_packet`.",
        "",
    ]
    body = "\n".join(front + use) + "\n" + index(path) + f"\n## {ids}-S0 checklist (in full)\n" + str(s0["text"]) + "\n"
    if body.count("\n") > MAX_LINES:
        raise ValueError(f"{spec['skill']}: SKILL.md has {body.count(chr(10))} lines (max {MAX_LINES})")
    return body
