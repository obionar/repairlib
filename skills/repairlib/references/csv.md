# CSV intake

Three kinds: `customers`, `catalog` (items with optional opening stock) and
`opening-stock` (stock for existing items). Each kind has `template`, `preview` and
`apply`. Limits: 256 KiB, 100 rows, a regular file, UTF-8.

| Command | Notes |
|---|---|
| `csv KIND template` | write the CSV header to stdout; redirect to a file to start a batch. |
| `csv KIND preview FILE [--json]` | validate every row; prints the input SHA-256, a preview ID when clean, proposed rows and per-field errors. Exit 7 when a preview has blocking errors (no preview ID). |
| `csv KIND apply FILE --preview-id ID --sha256 SHA --key K [--json]` | apply the exact bytes that were previewed; a changed file or a reused key with other content is refused. |

```sh
repairlib csv customers template --workspace "$WS" > /tmp/customers.csv
# edit /tmp/customers.csv, then:
repairlib csv customers preview /tmp/customers.csv --workspace "$WS" --json
repairlib csv customers apply /tmp/customers.csv --workspace "$WS" \
  --preview-id "$PREVIEW_ID" --sha256 "$SHA256" --key csv-1 --json
```

Exact templates (numbers are plain unsigned integers, never decimals):

- customers: `name,phone,email,address,notes` (`name` required).
- catalog: `sku,description,category,manufacturer,part_number,package,specifications,bin,unit,low_stock_milli,purchase_cost_cents,selling_price_cents,opening_quantity_milli` — `sku`, `description` and `unit` required; quantities are milli-units (`1500` = 1.5) and money is cents (`1200` = 12.00 EUR); a blank threshold means 0, a blank purchase cost means unknown; opening stock records a receipt.
- opening-stock: `sku,quantity_milli,reason` for active items that have no stock movements yet.

Preview first on a disposable workspace; never import a real customer list into a test.
Applying is one transactional batch: a row error refuses the whole batch.
