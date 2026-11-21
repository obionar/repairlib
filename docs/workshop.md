# Workshop workflows

[RepairLib README](../README.md)

[Dashboard](#dashboard) · [Repair intake and entries](#manage-repairs-and-work-entries) · [Attachments](#attach-files-to-repairs) · [Printing](#print-repair-summaries-and-labels) · [Inventory](#manage-the-inventory-catalog) · [Users and settings](#manage-users-and-recover-an-administrator-password)

CLI examples run on the shop machine against the default workspace; add `--workspace PATH` for another. See the [local CLI](integrations.md#use-the-cli-on-the-shop-machine). They write `repairlib …`; from a source checkout define it once:

```sh
repairlib() { PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m repairlib "$@"; }
```

## Conventions for writes

- Every write needs a unique `--key` (API: `Idempotency-Key` header). After an uncertain result, retry with the same key, payload and revision to get the original result; use a new key for a new action.
- Edits take the reviewed `--revision` (API: `revision`). Read the record first (`show ID --json`). A stale revision, a key reused with a different payload, an archived record, insufficient stock or a no-op state change returns HTTP 409 and CLI exit 7 with no write. Review the current record before a new edit.
- Other CLI exits: 5 permission, 6 missing record, 8 invalid fields. For optional fields an empty string clears the value.
- Roles: admins and technicians write; viewers read. Unit costs are hidden from viewers everywhere (web, API, CLI).
- The CLI uses the same HTTP API contracts. Lists page with `limit` and `before` (CLI `--limit`, `--before`; keep filters when following `next_before`).

## Daily workflow

1. Create or find the customer, then record the device and reported issue in **Repairs**.
2. Open the job from the dashboard or Repairs register. Review its repair status and assignment.
3. Update the repair status, add or correct work entries, record charges or stocked parts, and link files as needed.
4. Open **Awaiting pickup** for completed work. Print the summary or label, record pickup, and check the job leaves the queue.

## Dashboard

Shows open work, repairs awaiting pickup and recent customers. Open work lists up to eight unarchived repairs in workflow order, oldest first within each stage. Awaiting pickup lists up to eight unarchived repairs with a final outcome and no pickup, oldest update first. Both show the full shop count and link to the filtered register (**View open repairs**, **View repairs awaiting pickup**). Recent customers shows the five newest unarchived. **Account** opens profile tools and sign-out. Low stock is described under [inventory](#manage-the-inventory-catalog).

## Manage customers

**View all customers** on the dashboard, then select a customer for details, repairs (including archived) and history; the two lists page independently. Admins and technicians create, edit, archive and restore; the CLI needs a write-scoped token for changes over HTTP.

```sh
repairlib customers create --key demo-create-1 --name "Demo Customer" --json
repairlib customers list --limit 25 --json
repairlib customers edit 1 --revision 1 --key demo-edit-1 --phone "+357 555" --json
repairlib customers archive 1 --revision 2 --key demo-archive-1 --json
repairlib customers restore 1 --revision 3 --key demo-restore-1 --json
repairlib customers history 1 --json
```

- The list shows active customers newest first; **Include archived customers** + **Search** (CLI `--include-archived`, API `include_archived=true`) adds archived ones. Open repair count covers nonterminal, unarchived repairs. `customers list --query TEXT` searches.
- Archived customers stay readable and editable, vanish from the new-repair picker, and keep their repair links. Read `archived_at` for status.
- API: `POST/GET /api/v1/customers`, `PATCH /api/v1/customers/{id}` (`revision` + changed fields), `POST …/{id}/archive` with `revision` and `enabled` (true archives, false restores), `GET …/{id}/history` (newest first: creation, edits, archives, restores, with actor username, UTC time, source, token label and changes).

## Manage repairs and work entries

**Repairs** in the navigation. **New repair** starts with a customer picker (search name, phone or email; **Continue without a customer**; **New customer** returns to intake; or start from a customer's **Repairs** panel). After saving, the record offers **Print label**. When editing, use **Search for a different customer** before typing unsaved changes. An existing technician assignment stays selected even if inactive; choose **Unassigned** to remove it, and new assignments need an active technician of this shop. At CLI intake omit `--customer`/`--technician` when unassigned.

```sh
repairlib repairs create --key repair-create-1 --customer 1 --technician 2 \
  --device-type Laptop --reported-issue "Does not power on" --json
repairlib repairs add-entry 1 --kind work --body "Inspected the power circuit" --key entry-create-1 --json
repairlib repairs show 1            # plain summary; --json gives record and revision
repairlib repairs list --queue pickup --stage repaired --json
```

- **Search** in Repairs and Customers ignores case and accents (`οθονη` finds `ΟΘΌΝΗ`). Queues combine with stage, pickup, archive and technician filters; paging keeps filters and shows the full count. API filters: `q`, `stage`, `queue` (`open`/`pickup`), `picked_up`, `archived`, `technician` (ID, `me`, `unassigned`), `customer_id`.
- The header links to the customer's repairs and other open repairs. **Activity** at the bottom (**Older activity** / **Newest activity**) shows recorded actions on the repair, entries, charges and files with actor and time, without raw change details; CLI `repairs activity ID`. `dashboard` prints queue counts.
- `repairs show` summarises device, customer, technician, issue, stage, pickup and archive status and charge totals (not payments). `repairs edit` takes only desired changes plus the revision; `--clear-customer`/`--clear-technician` remove links. `repairs entries ID` marks deleted entries.
- Stage: `repairs stage ID --revision N --stage STAGE --key KEY`; moving from `repaired` or `no_repair` back to `in_progress` also needs `--reason`.
- Pickup is independent of stage: `repairs pickup ID --revision N --key KEY` records server time and signed-in user (no collector name or notes); `repairs archive ID …` archives; `--undo` reverses either. Activity shows who recorded or cleared it. Archived repairs stay readable; restore before editing intake or stage (pickup can still change).
- Payment is a yes/no status on a finished repair (`repaired` or `no_repair`), not an amount:
  `repairs payment ID --revision N --key KEY` marks it paid and `--undo` marks it not paid.
  `repairs show --json` reports `payment_paid` (`null` until first recorded). The API route is
  `POST /api/v1/repairs/ID/payment` with `{"revision": N, "enabled": true}`.
- Entries: `edit-entry`, `delete-entry`/`restore-entry` (soft), `entry-history`; corrections and visibility changes need a reason.

## Use a catalog part on a repair

On an unarchived repair, **Parts and labor → Use catalog part**. Search by SKU, description, manufacturer or part number (search survives paging and part review). Enter quantity taken from stock, quantity billed and billed unit price (default: catalog selling price; zero supplies it free). The stock deduction and charge are recorded together, keeping description, unit, cost and billed price from the time of use. Viewers see quantities, price and totals, not unit cost.

```sh
repairlib repairs consume-part 1 2 --repair-revision 1 --item-revision 4 \
  --consumed-quantity 2 --billed-quantity 1.5 --unit-price 19.99 --key use-part-1 --json
repairlib repairs charges 1 --include-consumption --json
repairlib inventory movements 2 --json
```

API: `POST /api/v1/repairs/{repair_id}/consume-part` with integer `item_id`, `repair_revision`, `item_revision`, `consumed_quantity_milli`, `billed_quantity_milli` and optional `unit_price_cents`. It returns 201 with the charge (including `inventory_item_id`, `movement_id`, `consumed_quantity_milli`). `?include_consumption=true` on charge GETs adds the movement link and consumed quantity.

### Correct billing or return a catalog part

Open the catalog charge from **Parts and labor**. Editing billed quantity, price or notes, or removing/restoring the charge, changes billing only; the original description, unit, cost and consumed quantity stay attached. CLI: `edit-charge`, `delete-charge`, `restore-charge`; an archived item can still have billing corrected on an active repair.

- **Review physical return** shows the remaining quantity; **Return physical part** returns all or part against that charge's original consumption (not above what remains) and leaves billing unchanged. The ledger shows the positive return with the original movement number.
- **Intake mistake: remove charge and return part** removes the charge and returns its remaining quantity in one transaction. Both return actions need a reason and an active item and repair.
- Generic **Reverse** applies only to receipts and adjustments. If stock changed after you opened a movement page, review the latest balance and your drafted reason, then **Review and reverse**; you return to the movement page.

```sh
repairlib repairs part-return-status 1 3 --json
repairlib repairs return-part 1 3 --repair-revision 1 --item-revision 5 --quantity 0.5 \
  --reason "Unused spare" --key return-part-1 --json
repairlib repairs intake-mistake 1 3 --repair-revision 1 --item-revision 6 \
  --charge-revision 1 --reason "Wrong intake" --key intake-error-1 --json
```

API on `/api/v1/repairs/{repair_id}/charges/{charge_id}`: `GET …/return-part` to review; `POST …/return-part` with `repair_revision`, `item_revision`, `quantity_milli`, `reason`; `POST …/intake-mistake` with `repair_revision`, `item_revision`, `charge_revision`, `reason`. Both return 201 with the movement, on-hand stock, charge, exact repair totals and remaining consumption. Billing-only corrections use the charge PATCH and visibility endpoints.

## Record parts, labor and totals

**Parts and labor** lists catalog and free-text charges, parts and labor subtotals, a repair discount and the EUR total. Totals record work value, not payments. Prices have up to two decimals, quantities up to three; each line is rounded half-up to the cent, the subtotal sums rounded lines, percentage discounts are whole points rounded half-up, and a discount never exceeds the subtotal. Corrections and removals need a reason and keep audited before/after values. Free-text charges never change stock.

```sh
repairlib repairs add-charge 1 --kind part --description "Charging port" \
  --quantity 1 --unit-price 19.99 --unit-cost 8 --key charge-1 --json
repairlib repairs discount 1 --revision 0 --kind percent --value 10 --key discount-1 --json
repairlib repairs edit-charge 1 CHARGE --revision N --reason TEXT --quantity 2 --key KEY
```

`delete-charge`/`restore-charge` need a reason. API: `/api/v1/repairs/ID/charges` and `PUT /api/v1/repairs/ID/discount`, integer `*_cents` and `quantity_milli` only.

## Attach files to repairs

**Files** uploads without JavaScript, optionally linked to a visible work entry of the same repair. Bytes go to private workspace storage unchanged with their SHA-256. Only PNG, JPEG, GIF and WebP (detected from bytes, not name) preview inline; everything else downloads. Admins set limits in **Shop settings** or `settings edit --max-upload-mib N --inline-preview-mib N` (defaults 256 and 8 MiB; bounds 1–2048 and 1–64), effective on the next upload. Viewers list and download; archived repairs refuse new files.

- **Detach** hides a file but keeps bytes and can be restored. After 30 days detached, an admin can **Purge bytes permanently** (`repairs purge-attachment ID ATTACHMENT --revision N --key KEY`): only bytes go; record, SHA-256 and history remain and downloads answer 410.
- Admins and technicians can save or clear a caption (up to 1,000 printable characters) and link a file to more visible entries of the same repair or unlink them (also after an entry is soft-deleted). Original file and SHA-256 stay. Metadata of archived repairs and detached or purged files stays readable; restore a detached file before editing it. Each change advances the attachment revision.

```sh
repairlib repairs attach 1 ./board-photo.jpg --key attach-1 --json   # --entry ID, --name NAME
repairlib repairs attachments 1 --json                               # revision and entry_ids
repairlib repairs download 1 1 --output ./board-photo-copy.jpg
repairlib repairs detach 1 1 --revision 1 --key detach-1 --json      # --undo restores
repairlib repairs edit-attachment 1 1 --revision 1 --key caption-1 --caption "Board before cleaning" --json
repairlib repairs link-attachment 1 1 2 --revision 2 --key link-1 --json
repairlib repairs unlink-attachment 1 1 2 --revision 3 --key unlink-1 --json
```

`download` never overwrites a path and refuses bytes whose size or SHA-256 differ from the record.

API: `POST /api/v1/repairs/ID/attachments?filename=NAME` (raw bytes body); `GET …/attachments/ID/content` (`?inline=1` previews an image); `PATCH …/attachments/ATTACHMENT` with `revision` and `caption` (empty clears); `POST …/attachments/ATTACHMENT/links` with `revision`, `entry_id`, `linked` (true adds, false removes). The response includes the sorted `entry_ids` array and the compatibility field `entry_id` (first ID or null).

An interrupted upload can leave unrecorded bytes. With every app process for the workspace stopped, run `repairlib reconcile-files --workspace "$repairlib_workspace"`. It removes only files no live record references (including bytes from an interrupted purge), verifies every recorded SHA-256, writes a maintenance audit event, and exits 2 listing attachment IDs whose bytes are missing or changed, which it preserves for recovery.

## Print repair summaries and labels

The repair page links **Print summary** and **Print label**; use the browser's Print or save as PDF (no JavaScript needed; viewers can too). The summary shows shop, customer and phone, device, issue, repair status, active charges and totals, and up to 200 recent non-deleted entries in chronological order (marked as an excerpt if more exist). It omits unit costs and uses the shop timezone. The 62 × 29 mm label shows repair number, customer, device, serial and intake date.

Direct P15 printing is off by default. An admin opens **Shop settings → Label printer**, chooses **P15** and sends a test label. It is available when the server finds the fixed `p15` executable on its PATH; the operator supplies and configures that CLI (paper size, printer). Admins and technicians then use **Send to P15** on the label page (sticker: repair number and customer name).

```sh
repairlib printer show --json
repairlib printer set p15 --revision 1 --key enable-p15-001      # `none` disables
repairlib printer test --revision 2 --key test-p15-001
repairlib repairs label-options 42 --json
repairlib repairs print-label 42 --revision 1 --printer-revision 2 --key label-42-001
```

API: `GET/PATCH /api/v1/settings/printer`, `POST /api/v1/settings/printer/test` (`revision`), `GET/POST /api/v1/repairs/{id}/label` (`revision` and `printer_revision` from label options).

An accepted attempt returns an `outcome` and message; a non-acknowledged receipt exits 7. After a timeout, driver failure or lost response the label may already be on paper, so check the printer before a new request. Reusing the same key and revisions returns the stored receipt without printing again. Direct printing and replay across a server restart were verified on Alpine 3.24/aarch64 with P15 CLI 0.2.1.

## Manage the inventory catalog

**Inventory** (dashboard or Account) lists SKU, description, bin, low-stock threshold and selling price. Admins and technicians create, edit, archive and restore items and also see the default purchase cost (hidden from viewers in web, API and CLI). SKUs are unique per shop and case-sensitive. Archived items stay readable and keep their SKU; **Include archived items** → open item → **Restore inventory item** advances the revision and preserves SKU, on-hand quantity and history.

Low stock: an active item qualifies when on-hand is at or below its threshold (zero at a zero threshold). The dashboard shows the full count and up to eight newest items; **View low-stock inventory** opens the list with **Low stock only (active items)**. The filter (`GET /api/v1/inventory?low_stock=true`, `inventory list --low-stock`) always excludes archived items. Search matches SKU, description, manufacturer or part number plus optional category (`--query`, `--category`; API `q`, `category`).

```sh
repairlib inventory create --key demo-item-1 --sku RC-1 --description "Synthetic capacitor" \
  --purchase-cost 0.05 --selling-price 0.20 --low-stock 10 --json
repairlib inventory list --low-stock --json            # also --include-archived, --limit, --before
repairlib inventory edit 1 --revision 1 --key demo-item-edit-1 --bin A2 --json
repairlib inventory archive 1 --revision 2 --key demo-item-archive-1 --json
repairlib inventory restore 1 --revision 3 --key demo-item-restore-1 --json
```

CLI and web prices are decimal EUR; the API uses integer `purchase_cost_cents` (or `null`), `selling_price_cents` and `low_stock_milli`. `--clear-purchase-cost` removes the default cost. `inventory edit` sends current values plus your changes. API: `GET/POST /api/v1/inventory`, `GET/PATCH /api/v1/inventory/{id}` (PATCH takes all fields plus `revision`), `POST …/{id}/archive` and `…/restore` with `{"revision": N}`. A duplicate SKU or invalid archive state is also 409 / exit 7.

## Receive and adjust stock

Each item shows on-hand quantity and a ledger of movements. Admins and technicians receive stock (optional note such as a delivery reference), adjust up or down with a required reason, and reverse a receipt or adjustment; viewers read only. A reversal adds a compensating movement referring to the original. A movement reverses once, and a reversal cannot be reversed. Stock never goes below zero, so a receipt whose parts are used cannot be reversed. Every change needs the reviewed item revision and bumps it. The stock unit cannot change once movements exist, and archived items take no stock changes.

```sh
repairlib inventory receive 2 --quantity 10 --reason "Delivery 42" --revision 1 --key demo-receive-1 --json
repairlib inventory adjust 2 --quantity -1.5 --reason "Damaged" --revision 2 --key demo-adjust-1 --json
repairlib inventory reverse 2 2 --reason "Found it" --revision 3 --key demo-reverse-1 --json   # ITEM MOVEMENT
repairlib inventory movements 2
repairlib inventory reconcile
```

Quantities are decimal units (up to three places) on the CLI and web; the API uses signed integer `quantity_milli`. API: `POST /api/v1/inventory/{id}/receive` and `…/adjust` with `quantity_milli`, `revision`, `reason` (reason required for adjust; negative removes stock); `POST …/movements/{movement_id}/reverse` with `reason`, `revision`; each returns 201 `{"item", "movement"}`. `GET …/movements` pages the ledger newest first.

The ledger is authoritative; each item's cached on-hand quantity updates in the same transaction. `inventory reconcile [--json]` and `GET /api/v1/inventory/reconciliation` are read-only checks of the caller's shop, including archived items. They report cache differences in `mismatches` and running-balance, movement-link, consumption-snapshot and return-limit problems in `issues`. The consumption receipt anchors the original charge description, unit and cost; billing edits to quantity, price or visibility stay compatible with a healthy ledger. HTTP 200 means the check completed; the CLI exits 0 when both lists are empty, otherwise 1.

## Manage users and recover an administrator password

Admins open **Users** from Account to create, rename, change roles, reset passwords and disable accounts in their own shop (also `/api/v1/users` and the CLI). Passwords come from a hidden prompt or stdin, never an argument.

```sh
repairlib users create --username technician --role technician --key demo-user-1 --json
repairlib users list --json
repairlib users show ID --json
repairlib users edit ID --revision N --key KEY [--disabled|--active] [--password]
```

Every account change revokes that user's sessions and API tokens. The last active administrator cannot be disabled or demoted, even under concurrent changes.

To set or reset anyone's password from the shop machine at any time:

```sh
repairlib passwd --workspace "$repairlib_workspace" --username owner
```

It reads the password securely, signs the user out everywhere, revokes their API tokens and records a `user.password` audit event by `cli`. It does not reactivate a disabled account.

## Change shop settings

Admins open **Shop settings** for shop name, IANA timezone, default language, the address, phone and terms printed on the handover, and upload and preview limits. **Administration** links Shop settings, Users and Label printer. Changes apply immediately with reviewed revisions and retry keys.

```sh
repairlib settings show --json
repairlib settings edit --revision 1 --key settings-edit-1 --timezone Europe/Nicosia --json
```

`settings edit` also takes `--language`, `--address`, `--phone`, `--terms` (empty clears); plain `settings show` prints all. Resource: `GET/PATCH /api/v1/settings`. Listener, installation and database configuration stay owner-managed, and no restart-required shop settings are exposed.

## Language

The web UI is English and Greek. **Language** is in **Account** when signed in and on the sign-in page when signed out; the choice is saved in a cookie. Without one, the workspace default language applies when the request has neither cookie nor `Accept-Language`; otherwise the browser's language, with English as fallback. Entered text (names, issues, notes) is never translated. UI text lives in `src/repairlib/locales/*.json` keyed by the English text; missing keys fall back to English. API, CLI and MCP output stays English.
