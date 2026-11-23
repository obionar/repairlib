# RepairLib operations for agents

[RepairLib README](../README.md)

This is the one file an agent needs before operating RepairLib: the pre-write check, the
repair workflow as ordered commands, how to read a refusal, and what never to touch.

`repairlib` is the launcher (`bin/repairlib`, linked by `repairlib links`). Every `--help`
page is authoritative; this page is a summary.

## Before you write

Run the read-only check first and read every blocker and field it prints:

```sh
repairlib profile check --workspace "$repairlib_workspace"
```

It prints workspace path, state, schema version (with expected), backup verification
today, the local identity (`cli`), role and configured server. Exit 1 means at least one
blocker; do not write until it exits 0.

Use a separate synthetic demo workspace for each agent. Stop any running server
before resetting the demo. Never open a shop's real state directory, import real
CSVs, or copy a production database into the checkout or a test.

Also always pass `--workspace` explicitly. Without it the CLI silently uses the default
state directory, which on a real machine is the shop's live workspace.

### Get a workspace to test against

Create a throwaway synthetic workspace outside the source tree. `demo` reads the first
admin password from a hidden prompt or one line on stdin, and refuses to reuse an
existing workspace:

```sh
repairlib_workspace="$(mktemp -d)/workspace"
repairlib demo --workspace "$repairlib_workspace" --username owner
repairlib status --workspace "$repairlib_workspace"
```

Stop any server for that workspace, then reseed the synthetic customers in place (accounts
and credentials are kept):

```sh
repairlib demo reset --workspace "$repairlib_workspace" --confirm
```

Delete the temporary directory when you are done; never point `--workspace` back at a
real shop.

## The workflow

Run the steps in order against the same workspace. Every mutation needs `--key`: reuse the
same key and the same payload to replay the stored result, or pass `--key auto` to mint a
fresh one. When supported, `--json` prints machine-readable results and structured operation errors.
Set the `*_id` variables from the records the CLI returns; seeded demos already contain customers.

1. Read the revision before any change; `repairs show`, `customers show` and
   `inventory show` print it. Set `repair_revision` or `item_revision` from the corresponding
   read and refresh it before the next mutation.

2. Create the customer and keep the returned `id` as `customer_id`:

   ```sh
   repairlib customers create --workspace "$repairlib_workspace" \
     --key customer-1 --name "Synthetic Customer" --phone "+357 555 0100" --json
   ```

3. Create the repair for that customer and keep its `id` as `repair_id`:

   ```sh
   repairlib repairs create --workspace "$repairlib_workspace" \
     --key repair-1 --customer "$customer_id" --device-type Laptop --brand Acme --model K1 \
     --reported-issue "Does not power on" --json
   ```

4. Add a free-text charge (never changes stock):

   ```sh
   repairlib repairs add-charge "$repair_id" --workspace "$repairlib_workspace" \
     --kind labor --description "Replace power jack" --quantity 1 --unit-price 45.00 \
     --key charge-1 --json
   ```

   Or consume a catalog part, which deducts stock and adds the charge together. Create and
   receive the item first, setting `item_id` from its receipt. Re-read both revisions
   before `consume-part`:

   ```sh
   repairlib inventory create --workspace "$repairlib_workspace" \
     --key item-1 --sku JACK-K1 --description "Power jack K1" --unit pcs \
     --selling-price 12.00 --purchase-cost 4.00 --low-stock 2 --json
   repairlib inventory receive "$item_id" --workspace "$repairlib_workspace" \
     --revision "$item_revision" --quantity 5 --key receive-1 --json
   # Refresh repair_revision and item_revision with repairs show and inventory show.
   repairlib repairs consume-part "$repair_id" "$item_id" --workspace "$repairlib_workspace" \
     --repair-revision "$repair_revision" --item-revision "$item_revision" --consumed-quantity 1 --billed-quantity 1 \
     --key part-1 --json
   ```

   Read the totals back in integer cents; totals record work value, not payments:

   ```sh
   repairlib repairs charges "$repair_id" --workspace "$repairlib_workspace" --include-consumption --json
   ```

5. Record pickup with the current repair revision (pickup is independent of stage; add
   `--undo` to clear it):

   ```sh
   repairlib repairs pickup "$repair_id" --workspace "$repairlib_workspace" \
     --revision "$repair_revision" --key pickup-1 --json
   ```

6. Print the handover. There is no CLI command for the customer handover summary: it is the
   browser page `/repairs/ID/print` (open `http://127.0.0.1:8000/repairs/1/print` and use
   the browser's Print command). The CLI prints the physical P15 repair label, which needs
   the repair and printer revisions:

   ```sh
   repairlib repairs label-options 1 --workspace "$repairlib_workspace" --json
   repairlib repairs print-label 1 --workspace "$repairlib_workspace" \
     --revision 1 --printer-revision 1 --key label-1 --json
   ```

For covered mutations, the global `--dry-run` reports what would be written and rolls the
change back. Put it before the command.

```sh
repairlib --dry-run repairs add-charge "$repair_id" --workspace "$repairlib_workspace" \
  --kind part --description "Dry run" --quantity 1 --unit-price 1.00 --key dry-1
```

## Reading a refusal

Exit codes are part of the CLI contract:

| Code | Meaning | Typical cause |
|---|---|---|
| 0 | success | change recorded, receipt stored |
| 1 | local error | unreadable or occupied path, a `profile check` blocker |
| 2 | usage | unknown flag, missing argument or rejected parser value; the command did not run |
| 3 | unavailable | workspace not initialized, wrong `--workspace`, or busy database |
| 4 | unauthenticated | no or expired API token (HTTP API only) |
| 5 | forbidden | role or token cannot perform this action |
| 6 | not found | the record ID does not exist in this workspace |
| 7 | conflict | stale revision, key reused with a different payload, archived or deleted record, insufficient stock, printer receipt not acknowledged |
| 8 | invalid fields | bad decimal, bad date, missing `--reason`, invalid form field |

With `--json`, a structured operation error looks like this on stdout:

```json
{"error": {"code": "stale_revision", "message": "The record changed after you reviewed it. Read it again and retry with the new revision.", "fields": {}}}
```

Without `--json` the same message is printed as `prefix: message` on stderr. `code` is the
stable identifier to branch on; `message` is for humans. Operation-level validation can
report `client_error` at exit 8 or `validation` for rejected fields. Do not assume every
failure produces JSON: parser errors, including invalid inventory quantities, can print
usage to stderr and exit 2.

Common refusals and what to do:

- `stale_revision` (7): someone or something changed the record after your read. Run
  `repairs show`, `customers show` or `inventory show` again, then retry with the new
  `--revision` and a new `--key`. Do not retry the old payload blindly.
- Key replayed with a different payload (7, `conflict`, "This key already belongs to a
  different request"): the key is bound to one request. Use the same key only for the
  identical payload; otherwise mint a new key with `--key auto`.
- Same key, same payload: not an error. It returns the stored result and writes nothing, so
  a lost response is safe to retry.
- Archived record (7, `archived`): restore it first (`customers restore`,
  `inventory restore`, `repairs archive --undo`) and then retry. Archived repairs stay
  readable, and pickup can still be recorded or cleared.
- Deleted work entry or charge (7, `deleted`): restore it with `restore-entry` or
  `restore-charge` (both need `--reason`) before editing.
- `busy` (3, "The workspace is busy. Retry with the same key."): another process holds the
  database. Stop the competing writer; do not retry in a loop.
- Print receipt not acknowledged (7): the label may already have reached paper. Check the
  printer before reusing the key; the same key and revisions return the stored receipt
  without printing again.
- `unavailable` (3): the workspace is not initialized or you pointed at the wrong path.
  Re-check `repairlib profile check --workspace …`, then `repairlib status`.
- Viewer role (`forbidden`, 5): viewer tokens can read but never write. The CLI and MCP run
  as the local `cli` and `mcp` administrators, so this refusal comes from the HTTP API or a
  read-only token; use a write-scoped admin or technician token.
- Field errors (8): fix the argument and retry with a new key — quantities allow up to three
  decimals, EUR prices two, and corrections and removals require `--reason`.

## What not to do

- **No direct SQL.** Do not open `state/repairlib.sqlite3` with `sqlite3`, a Python script or
  a GUI, and do not edit files under the workspace by hand. Every write must go through the
  CLI, the API or the browser so revisions, receipts and audit history stay correct.
- **No database or backup surgery.** Backups, restores and migrations belong to
  `repairlib backup`, `repairlib migrate` and the [data guide](data.md).
- **No service control.** Do not run `repairlib service install|start|stop|restart|remove`,
  `systemctl`, `rc-service` or `rc-update`. Starting and stopping the shop's service is the
  operator's decision, not an agent's.
- **No real customer data.** Synthetic records only, in a throwaway workspace. Do not test
  against a live workspace, and do not attach real documents, photos or exports.
- **RepairLib only.** RepairFlow commands, paths, skills and documentation do not describe
  this application and must not be applied here.
