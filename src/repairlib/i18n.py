"""External UI catalogs (src/repairlib/locales/*.json), language negotiation and English fallback.

Keys are the English source text; placeholders use ``{name}`` and are filled after lookup.
"""

from functools import cache
import json
from pathlib import Path
import re

LOCALES = Path(__file__).parent / "locales"
DEFAULT = "en"
COOKIE = "repairlib_lang"


def load() -> dict[str, dict]:
    catalogs = {}
    for path in sorted(LOCALES.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("language") != path.stem or not isinstance(data.get("messages"), dict):
            raise ValueError(f"Malformed catalog {path.name}")
        catalogs[path.stem] = data
    if DEFAULT not in catalogs:
        raise ValueError("The English catalog is required")
    return catalogs


CATALOGS = load()
LANGUAGES = {code: CATALOGS[code]["name"]  # default language first, then the rest by code
             for code in sorted(CATALOGS, key=lambda code: (code != DEFAULT, code))}


def negotiate(chosen: str, accept_language: str, shop_default: str = "") -> str:
    if chosen in LANGUAGES:
        return chosen
    ranked = []
    for index, part in enumerate(accept_language.split(",")[:20]):
        tag, _, params = part.strip().partition(";")
        weight = 1.0
        if params.strip().startswith("q="):
            try:
                weight = float(params.strip()[2:])
            except ValueError:
                continue
        primary = tag.strip().lower().split("-")[0]
        if primary in LANGUAGES and weight > 0:
            ranked.append((-weight, index, primary))
    if ranked:
        return min(ranked)[2]
    return shop_default if shop_default in LANGUAGES else DEFAULT


def datetime_format(language: str, *, date_only=False) -> str:
    catalog = CATALOGS.get(language, CATALOGS[DEFAULT])
    return catalog["date_format" if date_only else "datetime_format"]


@cache
def patterns(language: str) -> tuple:
    """Catalog keys with `{name}` placeholders, as regexes for already-formatted messages."""
    found = []
    for key in CATALOGS.get(language, CATALOGS[DEFAULT])["messages"]:
        pieces = re.split(r"\{(\w+)\}", key)
        if len(pieces) > 1:
            body = "".join(f"(?P<{piece}>.+?)" if index % 2 else re.escape(piece)
                           for index, piece in enumerate(pieces))
            found.append((re.compile(body, re.S), key))
    return tuple(found)


def translator(language: str):
    messages = CATALOGS.get(language, CATALOGS[DEFAULT])["messages"]

    def gettext(text, /, **values):
        if not isinstance(text, str):
            return text
        translated = messages.get(text) or text
        if translated is text and not values and language != DEFAULT:
            for pattern, key in patterns(language):
                match = pattern.fullmatch(text)
                if match and messages.get(key):
                    # Substituted parts (a nested refusal, a capability) translate too.
                    return messages[key].format(**{name: gettext(part) for name, part in match.groupdict().items()})
        return translated.format(**values) if values else translated

    return gettext
