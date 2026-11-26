# Data transfer and recovery

[RepairLib README](../README.md)

| Goal | Workflow |
|---|---|
| Add customers, catalog items or opening stock in batches | [CSV intake](#preview-and-apply-a-customer-csv) |
| Transfer business records and originals into an empty initialized shop | [Portable shop archive](#export-preview-and-import-a-portable-shop-archive) |
| Recover the whole workspace, including credentials | [Offline backup](#take-and-verify-an-offline-workspace-backup) and [restore](#restore-a-full-workspace-backup) |
| Move an existing development workspace to the current schema | [Explicit migration](#upgrade-a-development-workspace) |

Examples use the [local CLI](integrations.md#use-the-cli-on-the-shop-machine) as the built-in `cli` administrator. `repairlib` means `bin/repairlib` from the checkout (or the `repairlib` link). Add `--workspace PATH` for another workspace.

## Export, preview and import a portable shop archive

On POSIX, choose an existing private directory you own and a new output filename.

```sh
repairlib_portable="$(mktemp -d)"
repairlib portable export "$repairlib_portable/shop.zip" --json
repairlib portable preview "$repairlib_portable/shop.zip" --json
```

Export stages the complete archive before saving it, publishes it with mode `0600` and refuses an existing destination. If a retained attachment is missing or changed (including a purge racing the export) the export is refused. Archives hold customer data and original files in plaintext; keep them private.

API (human admin bearer token, the authenticated administrator's shop): `GET /api/v1/portable/export` returns `application/zip` with `Content-Length`; agent tokens cannot export unmasked archives. Send a raw ZIP as `application/octet-stream` to `POST /api/v1/portable/preview`. Preview reports the declared source shop, schema, table counts and retained attachment count and bytes, and checks the archive without writing business state. The source identity is descriptive metadata; SHA-256 gives byte integrity within the archive.

Export writes `repairlib-portable-shop-2`, an uncompressed ZIP for schema 43 with these exact members. Preview and import also read `repairlib-portable-shop-1` (schema 21, see below); the manifest `format` says which one an archive is:

- `manifest.json`: `format`, `schema`, `source_shop` (positive integer `id`, printable `name`), `counts` (rows per table) and `files` (byte `size` and lowercase `sha256` of each other member).
- `records.json`: UTF-8 JSON tables of schema-43 row objects: `shop_settings` (timezone, upload limits, language, address, phone, terms, diagnosis fee), `users`, `customers`, `repairs` (including warranty end date, warranty return link and payment status), `work_entries`, `work_entry_revisions`, `attachments` (including the device-photo role), `attachment_links`, `inventory_items`, `inventory_movements`, `repair_charges`, `repair_discounts`, `repair_fees` (the diagnosis and labour charge links), `repair_part_consumptions` and `audit_events`. Estimates were retired at schema 36 and are not in format 2. IDs and username snapshots describe source relationships and attribution; import gives every record a destination ID and remaps all links, including fee links and warranty returns, so a paid ticket arrives paid with the same fee split and photo.
- `attachments/<id>`: original bytes of each retained attachment, including detached files. Purged attachments keep metadata and history.

Archives omit password hashes, API tokens and labels, sessions, authentication history, retry receipts, internal file storage names, printer configuration and deployment secrets. Shop timezone and upload/preview limits are included. **Format 1** (schema 21, still readable) predates warranty dates and return links, the shop address, phone, terms and default language, payment status, the diagnosis and repair fee links and the device-photo designation. After a format-1 import the charge lines and totals are intact, but paid tickets read unpaid, the fee fields read 0.00 (re-entering the fees adds duplicate charge lines), no attachment is the device photo and warranty and shop contact must be re-entered. Preview lists these omissions as `omitted`, and the CLI prints them for a format-1 preview or import; for a complete move from an old archive use the full backup archive instead. Format 2 omits nothing of these. Test fixtures of both formats live in `tests/fixtures/portable/`.

Limits: 1 GiB per ZIP, 16 MiB of records JSON, 2 MiB of manifest JSON, 50,000 rows and 10,000 attachment records. Preview rejects unsupported members, invalid hashes or sizes, inconsistent counts and invalid records or references. API: 422 invalid input, 413 over-limit upload, 409 unavailable export. CLI: exit 8 for invalid input or over-limit, exit 7 for unavailable export.

### Import into an initialized empty shop

Run on the destination machine. Confirm the destination's settings `revision` with `settings show` first. A standalone workspace has shop ID 1. At revision 1:

```sh
repairlib_destination=/path/to/destination-workspace
repairlib settings show --workspace "$repairlib_destination" --json
repairlib portable import "$repairlib_portable/shop.zip" --workspace "$repairlib_destination" \
  --destination-shop-id 1 --revision 1 --key initial-shop-import --json
```

API: `POST /api/v1/portable/import?destination_shop_id=1&revision=1` with the raw ZIP as `application/octet-stream` and an `Idempotency-Key` header; the destination ID confirms the authenticated shop. Success is 201 with the destination ID, new settings revision, source identity, record counts and retained file count. After a lost response, retry with the same archive bytes, administrator, destination, original revision and key. Importing into an occupied shop returns 409.

The import keeps the destination's name, users, credentials and printer configuration, and copies the source timezone and attachment limits. It allocates record IDs in source-ID order above the destination's current IDs and remaps links. Imported accounts are disabled and have no credentials; a colliding login gets a generated name while historical username snapshots keep the original spelling. Stock reconciliation uses the imported movement and repair audit history. The destination administrator is recorded on the import audit event.

Older transfers could leave work-entry or fee-charge IDs in history pointing to their original shop. When an ID cannot be matched on import, it is kept with a `work_entry_id_unresolved` or `charge_id_unresolved` marker instead of being guessed; the marker stays on later transfers.

Originals are staged and verified before their records become visible. If the process stops before the commit, retry the import; with the app stopped, run `reconcile-files` to remove staging files or unreferenced originals left behind.

## Preview and apply a customer CSV

Download the header, fill in the rows, then preview.

```sh
repairlib csv customers template > customers.csv
repairlib csv customers preview customers.csv --json
```

Keep the columns in this order. Limits count characters after CSV unquoting.

| Column | Required | Maximum characters |
|---|---|---:|
| `name` | Yes | 160 |
| `phone` | Optional | 80 |
| `email` | Optional | 254 |
| `address` | Optional | 500 |
| `notes` | Optional | 1000 |

Use comma-separated UTF-8 (a leading Excel byte order mark and trailing blank lines are accepted) with 1 to 100 data rows and a 256 KiB limit. Double-quote fields containing commas, double quotes or line breaks, and escape a double quote with a second one. Address and notes may span lines. Blank optional cells are allowed. Reported row numbers are the starting lines of CSV records, with the header on line 1.

Review every proposed create and correct all row errors. A match on any nonempty identity field requires correction, including matches against archived customers: names fold case and accents and collapse whitespace, email folds case, phone ignores spaces, dots, dashes and parentheses. This intake only creates customers; it never merges with or updates existing ones.

Apply the unchanged file with the `preview_id` and `sha256` from the clean preview (set `preview_id` and `csv_sha256` in your shell from that result):

```sh
repairlib csv customers apply customers.csv \
  --preview-id "$preview_id" --sha256 "$csv_sha256" --key customer-import-001 --json
```

A clean preview replaces that administrator's previous customer CSV preview. The server stores its review ID, input hash and review context, and at apply time rechecks the exact bytes, the reviewed shop settings revision, the proposed actions and duplicates. A successful apply commits every customer, their attributed history entries and the request receipt together, and the result maps CSV rows to customer IDs and revisions. To recover a lost response, repeat the same file, preview ID, hash and key as the same administrator; the saved result survives restart. Reusing a committed key with different inputs is a conflict.

API: `GET /api/v1/csv/customers/template` (admin read or write token); send the raw file as `text/csv` to `POST /api/v1/csv/customers/preview` (admin write token), which returns 200 with `sha256`, `actions`, `errors` and a `preview_id` when clean; `POST /api/v1/csv/customers/apply` with the same raw file, `preview_id` and `sha256` query parameters and an `Idempotency-Key` header returns 201 with `sha256` and `rows`. Changed input or a stale review returns 409. Fix row errors, preview again, and use a new key for the new intent.

## Preview and apply catalog and opening-stock CSVs

Two more kinds share the customer CSV's file rules, limits (UTF-8, at most 100 data rows, 256 KiB), review, apply, retry and conflict behavior. Replace `catalog` with `opening-stock` for the second:

```sh
repairlib csv catalog template > catalog.csv
repairlib csv catalog preview catalog.csv --json
repairlib csv catalog apply catalog.csv \
  --preview-id "$preview_id" --sha256 "$csv_sha256" --key catalog-import-001 --json
```

`catalog` creates new inventory items. Columns in order: `sku` (required, 80), `description` (required, 200), `category` (80), `manufacturer` (120), `part_number` (120), `package` (80), `specifications` (1000, may span lines), `bin` (80), `unit` (required, 16), `low_stock_milli`, `purchase_cost_cents`, `selling_price_cents` (required), `opening_quantity_milli`. Numbers are plain unsigned whole numbers without signs, decimals or leading zeros; quantities are milli-units (1500 is 1.5 units) and money is euro cents. A blank `low_stock_milli` is 0 and a blank purchase cost is unknown. A nonblank opening quantity must be 1 to 1,000,000,000 and records an `Opening stock` receipt. A SKU repeated in the file or already used in your shop (including archived items) requires correction. Existing items are never updated or merged.

`opening-stock` records the first balance of existing items. Columns: `sku` (required, 80), `quantity_milli` (required, 1 to 1,000,000,000), `reason` (optional, 200 printable characters; blank means `Opening stock`). The SKU must belong to an active item in your shop with no stock movements; use receipts or adjustments for items that already have a ledger. A repeated SKU, an unknown or archived item, or a zero, negative, blank or fractional quantity requires correction. Preview actions show each item's ID and reviewed revision.

Each applied opening quantity appends one attributed `receive` movement and an `inventory.opening_stock` history entry. Apply rechecks SKUs, duplicates, item state and reviewed revisions, and commits every item, movement, cached on-hand change, history entry and the request receipt together, or nothing. An item changed since preview returns 409 `stale_revision`. Result rows map CSV rows to item IDs and new revisions.

API routes: `GET /api/v1/csv/catalog/template`, `POST /api/v1/csv/catalog/preview`, `POST /api/v1/csv/catalog/apply`, and the same paths under `/api/v1/csv/opening-stock/`, with the customer routes' authentication, parameters and responses.

## Take and verify an offline workspace backup

Stop every app process for the disposable workspace. Set `repairlib_workspace` to its existing path and choose a private recovery directory:

```sh
repairlib_recovery="$(mktemp -d)"
repairlib backup create --workspace "$repairlib_workspace" --output "$repairlib_recovery/repairlib.zip"
repairlib backup verify "$repairlib_recovery/repairlib.zip"
```

The archive holds a standalone SQLite snapshot with workspace settings and authentication records, plus the original bytes of every retained attachment, including detached files. Purged attachments keep their metadata in the database; purged bytes, unreferenced files and upload staging are excluded. The ZIP is unencrypted and sensitive. Its JSON manifest records the format, database schema, member sizes and SHA-256 hashes. Verification checks paths, membership, hashes, SQLite integrity, foreign keys, schema objects and migration history, and compares attachment bytes with the snapshot's metadata.

Limits: 10,000 retained files (uploads past this are refused, and `doctor` warns from 8,000), 2 GiB per attachment, 64 GiB database, 256 GiB total payload. Allow space in the recovery directory for the temporary SQLite file used during creation and verification.

Database-only copy while stopped, and of a running app with `--online`:

```sh
repairlib backup snapshot --workspace "$repairlib_workspace" --output "$repairlib_recovery/repairlib.sqlite3"
repairlib backup verify "$repairlib_recovery/repairlib.sqlite3"
repairlib backup snapshot --online --workspace "$repairlib_workspace" \
  --output "$repairlib_recovery/repairlib-online.sqlite3"
```

`--online` takes the shared maintenance lock, so it runs beside a working app but is refused while an exclusive operation such as an update holds the lock. It reads the running database through a read-only SQLite connection and copies it with SQLite's backup API: committed transactions still in the WAL are included, uncommitted writes are not. If a writer keeps the database busy for 30 seconds the snapshot is refused and no output file is left. The result is verified and published like a stopped snapshot. It holds the database only, not attachment originals; use `backup create` while stopped for a full archive.

Scheduling and copying backups off the machine is up to your own scripts: call `backup snapshot --online` (or `backup create` while stopped) and copy the output plus `state/files/`.

The stopped snapshot and the archive hold the exclusive maintenance lock through publication; the online snapshot holds the shared lock. All verify the staged backup and publish it with owner-only POSIX permissions under a new name in an existing owner-only directory outside the workspace; the destination filesystem must support hard links. The database-only snapshot is unencrypted, sensitive and holds attachment metadata but not the files. Keep it owner-writable for verification. Both verification paths check supported prior schemas in place and leave the backup unchanged.

The destination filesystem must also support syncing directories. If a directory sync fails after
publication, the command reports failure but leaves the output in place. Verify it and make
another backup on a working filesystem before relying on it.

## Restore a full workspace backup

Choose a new workspace path inside the private recovery directory:

```sh
repairlib_restored="$repairlib_recovery/restored-workspace"
repairlib backup restore "$repairlib_recovery/repairlib.zip" --workspace "$repairlib_restored"
repairlib status --workspace "$repairlib_restored"
```

Restore verifies the full archive while staging the database and retained originals next to the destination; the verified workspace and its maintenance lock become visible together with owner-only permissions. The destination must be absent, its parent must exist, be owner-only and have a path without symlinks. Existing destinations are refused, including empty directories and running workspaces. Restore needs Linux, procfs and filesystem support for atomic no-replace directory rename.

If interrupted, check the destination before retrying. An interruption after publication leaves the
complete workspace in place. If the final parent directory sync fails, the command reports failure
but does not delete the published workspace; verify it before use. Abrupt termination can leave a
private `.restore-*` staging directory beside the destination; once the restore process has stopped,
the owner can remove it. Treat staged copies as sensitive.

The restored database keeps its archived schema, records, settings, password hashes, sessions and API tokens. A current-schema restore enables SQLite WAL before publication and is ready to start; an older schema needs explicit migration. Restore accepts full workspace backups only; portable shop archives use the preview and import operations above.

## Upgrade a development workspace

Stop every app process for the disposable workspace. Create and verify a full backup and rehearse restoring it into a new disposable destination with the commands above. Then upgrade the existing workspace, optionally requiring a verified backup that matches its contents:

```sh
repairlib migrate --workspace "$repairlib_workspace"
repairlib migrate --workspace "$repairlib_workspace" --backup "$repairlib_recovery/repairlib.zip"
```

`migrate` explicitly upgrades any supported older schema to the current one (`status` shows both) and enables SQLite WAL; fresh setup creates the current schema and startup never migrates. With `--backup`, RepairLib verifies the backup under the maintenance lock before any write and checks that its schema and committed database contents match the stopped workspace, including changes still in the WAL. A missing, corrupt, stale or unrelated backup is refused with the database unchanged. Take a fresh backup if anything was written after the last one. Keep it outside the workspace and use a full archive if the workspace has retained attachments.

For a recovery rehearsal, run `migrate` against `"$repairlib_restored"` and rerun `status` and `doctor` there. Check representative records and download retained originals before using the restored workspace. If migration is interrupted, keep the verified backup, stop workspace processes and rerun `migrate` so SQLite can resolve any rollback journal. The rehearsal covers process interruption on Linux; power-loss recovery has not been verified.
