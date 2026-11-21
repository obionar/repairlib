# CLI contract

## Global flags

| Flag | Meaning |
|---|---|
| `--workspace PATH` | workspace directory (per command; also `REPAIRLIB_WORKSPACE`) |
| `--json` | one machine-readable object on stdout; errors print an error object |
| `--dry-run` | run a covered mutation in a rolled-back transaction and print the audit events |
| `--version` | commit identification of the running source |
| `--help` | authoritative per-command flags and defaults |

`--dry-run` covers only `customers create`, `repairs create`, `repairs stage`,
`repairs add-charge` and `repairs pickup`; anything else exits 2 with
`dry_run_unsupported`. It prints `{"dry_run": true, "events": [...]}`.

## Exit codes

| Code | Meaning | Typical cause |
|---|---|---|
| 0 | success | change recorded, receipt stored |
| 1 | local error | unreadable/occupied path, a `profile check` blocker |
| 2 | usage | unknown subcommand or flag; nothing was written |
| 3 | unavailable | workspace not initialized, wrong `--workspace`, busy database |
| 4 | unauthenticated | HTTP API only: missing/expired bearer token |
| 5 | forbidden | role or token cannot perform the action (HTTP) |
| 6 | not found | the record does not exist in this workspace |
| 7 | conflict | stale revision, key reuse, archived/deleted record, insufficient stock, print receipt |
| 8 | invalid fields | bad decimal/date, missing `--reason`, invalid form field |

## JSON output

Success prints the command's object (shape differs per command; read `--json` once and
keep the field names). Failure prints:

```json
{"error": {"code": "stale_revision", "message": "The record changed after you reviewed it. Read it again and retry with the new revision.", "fields": {}}}
```

`code` is stable; `message` is for humans. CLI-side argument rejections report
`client_error` at exit 8; API-shaped validation reports `validation` with `fields`.

## Idempotency keys

Every mutation needs `--key` (MCP: `key`): 1–128 ASCII letters, digits, `_` or `-`.
`--key auto` mints a fresh key so scripts need not invent one.

- Same key + identical payload = stored result replayed, nothing written. Safe after a
  lost response or a timeout.
- Same key + different payload = exit 7 `conflict` ("This key already belongs to a
  different request"). Mint a new key only for a newly reviewed intent.
- Store the key and payload before calling; a retry must be byte-identical.

## Revisions

Records carry a `revision` that increments on every write. Read it immediately before a
mutation and pass it (`--revision`, or the specific flag the command documents). A stale
revision exits 7 `stale_revision`: read again and retry with the new revision and a new
key. Never retry the old payload blindly.

Separate cursors exist where a repair holds independent state:

- `repairs discount --revision` = the **discount** revision from `repairs charges --json`
  (`discount_revision`, `0` if never set), not the repair revision.
- `repairs consume-part`, `return-part`, `intake-mistake` and `print-label` take two or
  three revisions (repair, item, charge, printer) — read the corresponding `show` /
  `label-options` / `part-return-status` first.

## Money and quantities

- Arguments take decimal EUR (`12`, `12.50`) with up to two places; JSON stores integer
  cents (`1250`).
- Quantities take decimal units (`0.75`) with up to three places; JSON stores integer
  milli-units (`750`). `--quantity -1.5` removes stock in `inventory adjust`; receipts
  take positives only.
- Discounts: `--kind amount --value 5` (EUR), `--kind percent --value 10` (whole percent),
  `--kind none`; the value is `0` for none. Percent is applied to the charges subtotal.
- Dates are `YYYY-MM-DD` (warranty); timestamps are UTC ISO-8601.

## Pagination

List commands take `--limit` (default 25, max 100) and `--before CURSOR`. The response
returns `next_before`; pass it as `--before` for the next older page; `null` means the
last page. Human output prints `Next page: --before N` on stderr when another page exists.

## `catalog` and `mcp`

`repairlib catalog` lists every runnable CLI command path and every MCP tool with its
arguments (add `--json` for one JSON object). Use it instead of guessing.

`repairlib mcp --workspace "$WS"` serves the same operations as a stdio MCP server, as
the built-in `mcp` administrator. It makes no HTTP calls and exposes the full shop,
including purchase costs; connect only trusted hosts. MCP mutation tools require `key`
and `revision` exactly like the CLI. The bridges truncate nothing, but
`read_attachment_content` refuses files over 64 KiB — use `repairs download`.

The CLI and MCP are the only local principals: they need no token, are recorded as `cli`
and `mcp` in the audit history, cannot sign in to the web app and are not shop users.

## Global environment

| Variable | Effect |
|---|---|
| `REPAIRLIB_WORKSPACE` | default workspace when `--workspace` is absent |
| `REPAIRLIB_BASE_URL` | the HTTP base URL an agent was pointed at (informational) |
| `TZ` | local zone used by `setup` when `--timezone` is absent |
