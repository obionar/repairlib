"""Case- and accent-insensitive text matching shared by list searches."""

import re
import unicodedata

MAX_QUERY = 100
PHONE_SHAPE = re.compile(r"\+?[0-9 ()./\[\]-]+")


def phone_digits(phone: str) -> str:
    return re.sub(r"[^0-9]", "", phone)


def phone_search_pattern(query: str) -> str | None:
    query = query.strip()
    if PHONE_SHAPE.fullmatch(query) and (digits := phone_digits(query)):
        return like_pattern(digits)
    return None


def fold(text: str | None) -> str:
    """Fold for matching: compatibility-decompose, drop accents, casefold (Greek-aware)."""
    if text is None:
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def like_pattern(folded: str) -> str:
    """Contains-pattern for LIKE ... ESCAPE '\\' with wildcards in the query taken literally."""
    escaped = folded.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def normalize_query(raw: str) -> str:
    query = raw.strip()
    if len(query) > MAX_QUERY or not query.isprintable():
        raise ValueError(f"Search text may have at most {MAX_QUERY} printable characters.")
    return fold(query)
