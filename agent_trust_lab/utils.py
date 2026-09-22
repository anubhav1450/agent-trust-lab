"""Small shared helpers."""
import re

_AMOUNT_RE = re.compile(r"refund\D{0,20}?(\d+(?:\.\d+)?)", re.IGNORECASE)


def extract_refund_amount(text: str):
    m = _AMOUNT_RE.search(text)
    return float(m.group(1)) if m else None
