"""All money math is integer cents. Floats never touch amounts."""

from decimal import Decimal, InvalidOperation


def parse_amount_to_cents(text: str, sign: int = 1) -> int:
    """Parse a bank-export amount string into integer cents.

    Accepts "1,234.56", "$12.00", "(45.00)" (accounting negative), "-3.5".
    sign=-1 flips the convention for banks that export debits as positive.
    Raises ValueError on anything that does not parse to whole cents.
    """
    s = text.strip().replace(",", "").replace("$", "")
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]
    if not s:
        raise ValueError("empty amount")
    try:
        d = Decimal(s)
    except InvalidOperation:
        raise ValueError(f"unparseable amount: {text!r}") from None
    cents = d * 100
    if cents != cents.to_integral_value():
        raise ValueError(f"sub-cent precision in amount: {text!r}")
    return sign * int(cents)


def format_cents(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    return f"{sign}${abs(cents) // 100:,}.{abs(cents) % 100:02d}"
