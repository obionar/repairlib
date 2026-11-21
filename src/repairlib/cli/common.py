"""Shared CLI helpers: workspace option and safe display of stored text."""

import getpass
import json
import sys
import warnings

from repairlib import money
from repairlib.client.local import ClientError

WORKSPACE_HELP = "workspace directory (defaults to the standard RepairLib state directory)"


def parse_cents_option(value: str, flag: str) -> int:
    """Identify a bad money option without disguising it as a workspace failure."""
    try:
        return money.parse_cents(value)
    except ValueError as error:
        raise ClientError(f"{flag}: {error}", 8) from None


def add_workspace(command) -> None:
    command.add_argument("--workspace", default=None, help=WORKSPACE_HELP)


def add_revision(command, *flags, dest="revision", required=True, help=None) -> None:
    """Add a revision argument; the primary revision also accepts --revision."""
    command.add_argument(*(flags or ("--revision",)), dest=dest, type=int,
                         required=required, help=help)


def display_name(username: str) -> str:
    return username if username.isprintable() else json.dumps(username, ensure_ascii=True)


def read_password(prompt: str) -> str:
    """Read a password from a hidden prompt, or one line of piped stdin; never from argv."""
    if sys.stdin.isatty():
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            return getpass.getpass(prompt)
    return sys.stdin.readline(1026).removesuffix("\n")
