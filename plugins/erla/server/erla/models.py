"""Typed record shapes shared across modules (Step 1 hardening).

These are ``TypedDict``s, not classes: records stay plain dicts at run time (same JSON, same
SQLite rows, no behaviour change) while pyright checks every producer and consumer.
``tests/test_models.py`` keeps them in step with the SQLite column lists.
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict


class PropertyRecord(TypedDict):
    """One material-property value parsed from a table chunk (A3). Column order = props.COLS."""

    quantity: str                 # canonical quantity, e.g. "Ftu", "E", "Kt"
    value: float                  # as printed (before any printed multiplier)
    value_text: str               # the cell text the value came from (G3.2 traceability)
    units: str | None             # printed units, "10^3 ksi"-style when a multiplier applies
    value_si: float | None        # value * multiplier converted to SI (pint), if dimensionally valid
    units_si: str | None
    material: str | None
    temper: str | None
    form: str | None
    thickness: str | None
    direction: str | None
    temperature: str | None
    basis: str | None             # A / B / S / typical (G3.6: never dropped)
    row_label: str | None
    col_label: str | None
    caption: str | None
    flags: list[str]              # A5/A6 quality flags, FOOTNOTE:<n>
    ocr: int                      # 1 if the source chunk is OCR text (G3.5)


class ChunkRecord(TypedDict):
    """A chunk as stored in the ``chunks`` table (subset used by search and props)."""

    chunk_id: int
    doc_id: int
    locator: str
    text: str
    quality_json: str | None


JobState = Literal["running", "done", "failed", "stopped_unexpectedly", "never_run"]


class IndexJobStatus(TypedDict, total=False):
    """``logs/index_job.json`` written by the MCP background index job (K2)."""

    state: JobState
    pid: int
    started: str
    finished: str
    phase: str | None
    full: bool
    embed: bool
    ocr: bool
    error: str
    index_result: dict[str, Any]
    ocr_result: dict[str, Any]
    tables_result: dict[str, Any]
    pruned_duplicate_vectors: int
    embedded: int
    vector_rows: int


PROPERTY_FIELDS: tuple[str, ...] = tuple(PropertyRecord.__annotations__)
