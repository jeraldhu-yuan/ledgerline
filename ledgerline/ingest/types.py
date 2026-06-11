"""Shared parse-result types (separate module to avoid circular imports)."""

from dataclasses import dataclass


@dataclass
class ParsedTxn:
    posted_date: str  # ISO 8601 date
    amount_cents: int  # negative = outflow
    merchant_raw: str
    external_id: str | None = None  # FITID / SimpleFIN id when the format has one


@dataclass
class ParseError:
    raw_line: str
    reason: str


@dataclass
class IngestResult:
    new: int = 0
    duplicates: int = 0
    failed: int = 0
