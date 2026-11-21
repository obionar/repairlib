"""Validate first-run workspace settings and initialize the local owner."""

from __future__ import annotations

from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from repairlib import i18n
from repairlib.domain.passwords import hasher
from repairlib.lifecycle.workspace import create_workspace
from repairlib.storage.users import LOCAL_ACTORS


def initialize_workspace(
    workspace: Path, *, shop: str, timezone: str, username: str, password: str,
    demo: bool = False, language: str = "",
) -> None:
    shop = shop.strip()
    username = username.strip()
    if not shop or len(shop) > 160:
        raise ValueError("Shop name must be between 1 and 160 characters.")
    if not username or len(username) > 80:
        raise ValueError("Username must be between 1 and 80 characters.")
    if username in LOCAL_ACTORS:
        raise ValueError("That username is reserved for the local CLI and MCP tools.")
    if not password or len(password) > 1024:
        raise ValueError("Enter a password of at most 1024 characters.")
    try:
        ZoneInfo(timezone)
    except (ValueError, ZoneInfoNotFoundError) as error:
        raise ValueError("Enter a valid IANA timezone.") from error
    if language and language not in i18n.LANGUAGES:
        raise ValueError(f"Unknown language {language!r}. Available: {', '.join(i18n.LANGUAGES)}.")
    password_hash = hasher().hash(password)
    create_workspace(workspace, shop, timezone, username, password_hash, demo=demo,
                     language=language)


def initialize_demo_workspace(workspace: Path, *, username: str, password: str) -> None:
    initialize_workspace(workspace, shop="Synthetic Repair Shop", timezone="UTC",
                         username=username, password=password, demo=True)
