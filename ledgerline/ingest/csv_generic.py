"""Configurable column-mapping CSV reader."""

import csv
from datetime import datetime
from pathlib import Path

from ledgerline.ingest.types import ParseError, ParsedTxn
from ledgerline.money import parse_amount_to_cents


def parse_csv(path: Path, profile: dict) -> tuple[list[ParsedTxn], list[ParseError]]:
    txns: list[ParsedTxn] = []
    errors: list[ParseError] = []
    cols = profile["columns"]
    with open(path, newline="", encoding="utf-8-sig") as f:
        for _ in range(profile.get("skip_rows", 0)):
            next(f, None)
        reader = csv.DictReader(f)
        for row in reader:
            raw_line = ",".join((v or "") for v in row.values())
            try:
                date_s = (row.get(cols["date"]) or "").strip()
                amount_s = (row.get(cols["amount"]) or "").strip()
                desc = (row.get(cols["description"]) or "").strip()
                if not date_s or not amount_s or not desc:
                    raise ValueError("missing date, amount, or description")
                posted = datetime.strptime(date_s, profile["date_format"]).date()
                cents = parse_amount_to_cents(amount_s, sign=profile.get("sign", 1))
                ext_col = profile.get("external_id_column")
                ext_id = (row.get(ext_col) or "").strip() or None if ext_col else None
                txns.append(
                    ParsedTxn(
                        posted_date=posted.isoformat(),
                        amount_cents=cents,
                        merchant_raw=desc,
                        external_id=ext_id,
                    )
                )
            except (ValueError, KeyError) as e:
                errors.append(ParseError(raw_line=raw_line, reason=str(e)))
    return txns, errors
