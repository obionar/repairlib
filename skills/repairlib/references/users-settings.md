# Users, settings and the label printer

## Users

The CLI runs as the built-in `cli` administrator; it is not a shop user and cannot sign
in to the web app. Passwords are read from a hidden prompt or one line of stdin, never
from argv, and are never printed.

| Command | Notes |
|---|---|
| `users list [--limit 25] [--before ID]` | id, username, role, active/disabled; `cli`/`mcp` are hidden. |
| `users create --key K --username NAME [--role admin|technician|viewer]` | reads the new password from stdin; role defaults to `technician`. |
| `users show ID [--json]` | role, active state, revision. |
| `users edit ID --revision R --key K [--username N] [--role admin|technician|viewer] [--active|--disabled] [--password]` | `--password` reads a new password; changing it ends that user's sessions. |
| `passwd --username NAME` | set anyone's password from the shell (alias `recover-password`); ends their sessions and revokes their API tokens. Works while the app runs. |

```sh
printf '%s\n' "$NEW_PASSWORD" | repairlib users create --workspace "$WS" \
  --key user-1 --username technician --role technician
repairlib users edit 2 --workspace "$WS" --revision 1 --key user-2 --disabled
repairlib passwd --workspace "$WS" --username owner
```

Use `admin` sparingly; `technician` can run the workshop and `viewer` is read-only (API
tokens only — shop users sign in to the web UI). Never put a password in a shell history,
log, note or tool output.

## Shop settings

| Command | Notes |
|---|---|
| `settings show [--json]` | shop name, timezone, upload and inline-preview limits, language, address, phone, terms, default diagnosis fee, revision. |
| `settings edit --revision R --key K [--name N] [--timezone ZONE] [--language CODE] [--address A] [--phone P] [--terms T] [--diagnosis-fee EUR] [--max-upload-mib N] [--inline-preview-mib N]` | supplied fields replace values; empty text clears address/phone/terms/language. |

The diagnosis fee is the default prefilled in a new Payment window; a saved diagnosis fee
is never overwritten by it. CLI `settings show --json` and `settings edit --json` return it
as integer `diagnosis_fee_cents`. A replay of an older edit can return `null` if the original
receipt did not record the fee; run `settings show --json` for the current fee. Timezones are
IANA names (`Europe/Nicosia`). Server limits
(`--max-upload-mib`, `--inline-preview-mib`) apply to new uploads.

```sh
repairlib settings edit --workspace "$WS" --revision 1 --key settings-1 \
  --name "Synthetic Shop" --language en --diagnosis-fee 20
```

## Printer

| Command | Notes |
|---|---|
| `printer show [--json]` | driver (`p15` or `none`), revision, whether the P15 program is available. |
| `printer set --revision R --key K p15\|none` | driver is positional; `none` disables direct printing. |
| `printer test --revision R --key K` | send one physical test label to P15; exit 7 unless acknowledged. |

Printing and the P15 configuration are physical operations: get explicit authorization,
verify the printer before retrying, and never send test labels to a shop's printer
unasked. The driver must be installed for the same user (on `PATH` or
`/usr/local/bin/p15`). `available: true` only means that the executable was found
on the server's absolute PATH; it does not prove that the printer is connected,
has paper, or is configured for the correct label size. The server process and
interactive shell may have different PATHs and P15 configuration.

For an authorized physical test, use a disposable synthetic workspace, enable P15
using the reviewed printer revision, then read `repairs label-options` and send
one synthetic repair ticket with those exact revisions. Save the request key and
receipt; replaying the identical request reads the receipt without printing.
Never automatically send a fresh key after an uncertain or failed outcome.
Inventory labels are sent from the inventory item's browser detail page when
direct printing is enabled; they contain the SKU and description. The browser
repair preview is not the P15 layout.

The P15 CLI may use a daemon that owns the Bluetooth adapter, so `bluetoothctl`
may not show a controller even when the printer works. Avoid scanning while its
daemon is active. If directly using `p15`, put options such as `-dry-run` before
`--`: everything after `--` is label content and can result in a real print.
