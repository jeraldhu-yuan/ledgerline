"""Deterministic merchant string cleaning. Pure functions, no LLM, reproducible."""

import re

# Processor/POS prefixes that precede the actual merchant name
_PREFIXES = [
    r"SQ \*",
    r"TST\*\s?",
    r"PAYPAL \*",
    r"PYPL\s?\*",
    r"PP\*",
    r"POS DEBIT\s+",
    r"POS PURCHASE\s+",
    r"POS\s+",
    r"DEBIT CARD PURCHASE\s+",
    r"CHECKCARD\s+\d{4,}\s+",
    r"CHECKCARD\s+",
    r"ACH (?:DEBIT|CREDIT)\s+",
    r"RECURRING PAYMENT\s+",
]
_PREFIX_RE = re.compile(r"^(?:" + "|".join(_PREFIXES) + r")", re.IGNORECASE)

# Store/reference numbers: "#1234" anywhere, standalone digit runs of 3+
# ("CHIPOTLE 1278"), and trailing processor reference codes ("*RT4Y7")
_HASH_NUM_RE = re.compile(r"#\s*\d+")
_DIGIT_RUN_RE = re.compile(r"\b\d{3,}\b")
_TRAILING_REF_RE = re.compile(r"\*\s?\w+$")

_US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC",
}

# State codes that double as common merchant-name words ("SUPPLY CO",
# "DINE IN", "BAR OR GRILL") — never stripped, even in trailing position
_AMBIGUOUS_STATES = {"CO", "IN", "OR", "DE", "OH", "OK", "ME", "LA"}

# Words that should keep their casing when title-casing
_CASE_EXCEPTIONS = {"of", "the", "and"}


def clean_merchant(raw: str) -> str:
    """merchant_raw -> merchant_clean. Deterministic rules only."""
    s = raw.strip()

    # Strip processor prefixes (may stack, e.g. "POS DEBIT SQ *CAFE")
    while True:
        stripped = _PREFIX_RE.sub("", s).strip()
        if stripped == s or not stripped:
            break
        s = stripped

    # Strip reference codes and store numbers
    s = _TRAILING_REF_RE.sub("", s)
    s = _HASH_NUM_RE.sub(" ", s)
    s = _DIGIT_RUN_RE.sub(" ", s)
    s = s.strip()

    # Strip a trailing 2-letter US state code (keep at least two leading tokens
    # so "GA POWER" style names survive)
    tokens = s.split()
    if (
        len(tokens) >= 3
        and tokens[-1].upper() in _US_STATES
        and tokens[-1].upper() not in _AMBIGUOUS_STATES
    ):
        tokens = tokens[:-1]
        s = " ".join(tokens)

    # Collapse whitespace
    s = re.sub(r"\s+", " ", s).strip()

    # Title-case word by word (str.title() mangles apostrophes: "O'S" -> "O'S")
    words = []
    for w in s.split(" "):
        lower = w.lower()
        if lower in _CASE_EXCEPTIONS and words:
            words.append(lower)
        elif w:
            words.append(w[0].upper() + w[1:].lower())
    return " ".join(words)
