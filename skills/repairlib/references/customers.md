# Customers

All commands take `--workspace`; mutations take `--key` and (except create) a reviewed
`--revision`. `--json` prints the record or page. `show`, `create`, `edit`, `archive`,
`restore` and `anonymize` may print duplicate and contact warnings on stderr.

| Command | Notes |
|---|---|
| `customers list [--query TEXT] [--include-archived] [--limit 25] [--before ID]` | newest first; search is case- and accent-insensitive over name, phone and email. It does not transliterate: search `Ελένη` in Greek, not `eleni`. Phone search matches digits only. |
| `customers create --key K --name NAME [--phone P] [--email E] [--address A] [--notes N]` | prints `Created customer ID.`; `--json` returns the record with `revision`. |
| `customers show ID [--json]` | record fields and `Revision N`; archived rows are labelled. |
| `customers edit ID --revision R --key K [--name N] [--phone P] [--email E] [--address A] [--notes N]` | only supplied fields change; empty text clears a field. |
| `customers archive ID --revision R --key K` | archive; existing repairs keep their links. |
| `customers restore ID --revision R --key K` | restore an archived customer. |
| `customers anonymize ID --revision R --key K` | irreversible: erase personal data, keep the skeleton and history. Confirm before calling; there is no undo. |
| `customers export ID` | print one customer's record, repairs and charges as JSON (no `--json` flag). |
| `customers history ID [--limit 25] [--before ID] [--json]` | attributed changes, newest first: timestamp, operation, actor, source, changes. |

```sh
repairlib customers create --workspace "$WS" --key customer-1 \
  --name "Synthetic Customer" --phone "+357 555 0100" --json
repairlib customers edit 1 --workspace "$WS" --revision 1 --key customer-2 \
  --phone "+357 555 0101"
repairlib customers list --workspace "$WS" --query "customer" --json
```

Creating a customer may report `Possible duplicate customer N.` — review it, but do not
merge records by hand. Anonymization and export are privacy-sensitive: use synthetic data
in tests and never copy a real export into a repository, note or support bundle.
