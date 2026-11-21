# Inventory and stock

Quantities are decimal units in arguments (up to three places) and integer milli-units in
JSON. Prices are decimal EUR in arguments and integer cents in JSON. Every mutation takes
`--key` and a reviewed `--revision`.

| Command | Notes |
|---|---|
| `inventory list [--query TEXT] [--category C] [--low-stock] [--include-archived] [--limit 25] [--before ID]` | newest first; search covers SKU, description, manufacturer and part number; `--low-stock` lists active items at or below their threshold. |
| `inventory create --key K --sku SKU --description TEXT [--unit U] [--category C] [--manufacturer M] [--part-number P] [--package PKG] [--specifications S] [--bin B] [--low-stock Q] [--purchase-cost EUR] [--selling-price EUR]` | `--unit` defaults to `pcs`; SKU must be unique. |
| `inventory show ID [--json]` | revision, full details, on-hand, threshold, prices. |
| `inventory edit ID --revision R --key K [fields…] [--clear-purchase-cost]` | supplied fields replace values; the unit is locked once the item has stock movements (create a new item instead). |
| `inventory archive ID --revision R --key K` / `inventory restore ID --revision R --key K` | keep or restore the SKU and stock ledger. |
| `inventory receive ID --revision R --key K --quantity Q [--reason TEXT]` | positive receipt; records a movement and increments on-hand. |
| `inventory adjust ID --revision R --key K --quantity DELTA --reason TEXT` | signed adjustment, e.g. `-1.5`; never below zero. |
| `inventory reverse ID MOVEMENT_ID --revision R --key K --reason TEXT` | compensating entry for a receipt or adjustment. Use `repairs return-part` for repair consumption. |
| `inventory movements ID [--limit 25] [--before ID]` | ledger, newest first: id, kind, quantity, on-hand after, actor, timestamp, reason. |
| `inventory reconcile [--json]` | verify cached on-hand against the ledger and repair links; exit 1 on any mismatch or ledger issue. |

```sh
repairlib inventory create --workspace "$WS" --key item-1 --sku JACK-K1 \
  --description "Power jack K1" --unit pcs --selling-price 12.00 \
  --purchase-cost 4.00 --low-stock 2 --json
repairlib inventory receive 1 --workspace "$WS" --key receive-1 --revision 1 --quantity 5
repairlib inventory show 1 --workspace "$WS"        # revision is now 2
repairlib repairs consume-part 1 1 --workspace "$WS" --key part-1 \
  --repair-revision 1 --item-revision 2 --consumed-quantity 1 --billed-quantity 1
```

Consumption is one atomic transaction: stock moves and the repair charge are written
together, with both reviewed revisions. A partial failure writes neither. Use
`repairs part-return-status` before returning or correcting a consumed part.

Never stocktake by editing the database: use `receive`, `adjust`, `reverse` (stock value)
and `repairs return-part` / `intake-mistake` (repair consumption).
