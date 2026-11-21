"""Exact EUR cents and milli-unit quantities: string parsing, half-up rounding, no floats."""

CURRENCY = "EUR"
MAX_CENTS = 10_000_000_000         # 100 million EUR per unit price or amount
MAX_MILLI = 1_000_000_000          # one million units
MAX_TOTAL = (1 << 63) - 1          # SQLite INTEGER
DISCOUNT_KINDS = ("none", "amount", "percent")


def _decimal(value: str, places: int) -> int:
    """Parse a non-negative ASCII decimal with at most `places` fraction digits."""
    text = value.strip()
    whole, dot, fraction = text.partition(".")
    if (not whole or not whole.isascii() or not whole.isdigit()
            or (dot and (not fraction or not fraction.isascii() or not fraction.isdigit()))
            or len(fraction) > places or len(whole) > 15):
        raise ValueError("Enter a plain non-negative number"
                         f" with at most {places} decimal places.")
    return int(whole) * 10 ** places + int(fraction.ljust(places, "0") or "0")


def parse_cents(value: str) -> int:
    cents = _decimal(value, 2)
    if cents > MAX_CENTS:
        raise ValueError("Amount is out of range.")
    return cents


def parse_milli(value: str) -> int:
    milli = _decimal(value, 3)
    if not 1 <= milli <= MAX_MILLI:
        raise ValueError("Quantity must be greater than zero and at most 1000000.")
    return milli


def parse_threshold(value: str) -> int:
    milli = _decimal(value, 3)
    if milli > MAX_MILLI:
        raise ValueError("Quantity must be at most 1000000.")
    return milli


def parse_percent(value: str) -> int:
    text = value.strip()
    if not text or not text.isascii() or not text.isdigit() or len(text) > 3 or int(text) > 100:
        raise ValueError("Enter a whole percentage from 0 to 100.")
    return int(text)


def line_total(quantity_milli: int, unit_price_cents: int) -> int:
    return (quantity_milli * unit_price_cents + 500) // 1000


def discount(kind: str, value: int, subtotal: int) -> int:
    if kind == "none":
        if value != 0:
            raise ValueError("A discount value needs a discount type.")
        return 0
    if kind == "amount":
        if not 0 <= value <= MAX_CENTS:
            raise ValueError("Discount amount is out of range.")
        if value > subtotal:
            raise ValueError("The discount cannot exceed the subtotal.")
        return value
    if kind == "percent":
        if not 0 <= value <= 100:
            raise ValueError("Discount percentage must be from 0 to 100.")
        return (subtotal * value + 50) // 100
    raise ValueError("Choose none, amount or percent.")


def format_cents(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}{cents // 100}.{cents % 100:02d}"


def format_milli(milli: int) -> str:
    sign = "-" if milli < 0 else ""
    whole, fraction = divmod(abs(milli), 1000)
    return f"{sign}{whole}.{fraction:03d}".rstrip("0").rstrip(".") if fraction else f"{sign}{whole}"
