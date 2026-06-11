"""Tolerant OFX/QFX parser (SGML-style, unclosed tags allowed).

Account numbers (ACCTID) are deliberately never extracted — security
invariant 2: nothing keyed by account number is ever stored.
"""

import re
from datetime import datetime
from pathlib import Path

from ledgerline.ingest.types import ParseError, ParsedTxn
from ledgerline.money import parse_amount_to_cents

# Blocks end at </STMTTRN>, at the next <STMTTRN> (SGML omits closing tags),
# at the end of the transaction list, or at end of file.
_STMTTRN_RE = re.compile(
    r"<STMTTRN>(.*?)(?=</STMTTRN>|<STMTTRN>|</BANKTRANLIST>|\Z)", re.S | re.I
)


def _tag(block: str, name: str) -> str | None:
    m = re.search(rf"<{name}>([^<\r\n]*)", block, re.I)
    if m:
        value = m.group(1).strip()
        return value or None
    return None


def parse_ofx(path: Path) -> tuple[list[ParsedTxn], list[ParseError]]:
    text = path.read_text(encoding="utf-8", errors="replace")
    txns: list[ParsedTxn] = []
    errors: list[ParseError] = []
    for m in _STMTTRN_RE.finditer(text):
        block = m.group(1)
        raw_line = re.sub(r"\s+", " ", block).strip()
        try:
            dtposted = _tag(block, "DTPOSTED")
            trnamt = _tag(block, "TRNAMT")
            name = _tag(block, "NAME") or _tag(block, "MEMO")
            fitid = _tag(block, "FITID")
            if not dtposted or not trnamt or not name:
                raise ValueError("missing DTPOSTED, TRNAMT, or NAME/MEMO")
            posted = datetime.strptime(dtposted[:8], "%Y%m%d").date()
            cents = parse_amount_to_cents(trnamt)
            txns.append(
                ParsedTxn(
                    posted_date=posted.isoformat(),
                    amount_cents=cents,
                    merchant_raw=name,
                    external_id=fitid,
                )
            )
        except ValueError as e:
            errors.append(ParseError(raw_line=raw_line, reason=str(e)))
    return txns, errors


def looks_like_ofx(path: Path) -> bool:
    if path.suffix.lower() in (".ofx", ".qfx"):
        return True
    head = path.read_text(encoding="utf-8", errors="replace")[:512].upper()
    return "OFXHEADER" in head or "<OFX>" in head
