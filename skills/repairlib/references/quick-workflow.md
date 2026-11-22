# Fast synthetic workflow

Run this only for a demo. It creates a fresh workspace, uses returned IDs, and reads the
repair only once after billing. Set `DEMO_PASSWORD` to a disposable password before running
the example; the password goes to `demo` on stdin. Requires `repairlib` and `jq` on `PATH`.

```sh
set -eu
: "${DEMO_PASSWORD:?Set a disposable demo password}"
root=$(mktemp -d)
WS="$root/workspace"
rl() { repairlib "$@" --workspace "$WS"; }
field() { printf '%s\n' "$1" | jq -er "$2"; }

printf '%s\n' "$DEMO_PASSWORD" | rl demo --username owner >/dev/null
rl profile check >/dev/null
customer=$(rl customers create --name "Synthetic Customer" --key customer-1 --json)
customer_id=$(field "$customer" .id)
record=$(rl repairs create --customer "$customer_id" --device-type Laptop \
  --reported-issue "Does not power on" --key repair-1 --json)
id=$(field "$record" .id)

change() {
  revision=$(field "$record" .revision) || return
  rl repairs "$@" --revision "$revision" --json
}

# Fees returns a charges summary, so get the current repair revision once.
change fees "$id" --diagnosis 20 --repair 45 --key fees-1 >/dev/null
record=$(rl repairs show "$id" --json)
record=$(change stage "$id" --stage in_progress --key stage-1)
record=$(change stage "$id" --stage repaired --key stage-2)
record=$(change payment "$id" --key paid-1)
record=$(change pickup "$id" --key pickup-1)
printf '%s\n' "$record"
```

Use the same key and payload to retry an uncertain call. If a response may be a replay,
re-read before chaining its revision. If someone else changed the repair, stop on
`stale_revision`, review the latest record, and use a new key for a newly reviewed write.

- Stock-linked parts require both the item and repair revisions; see [Inventory](inventory.md).
- For bulk intake, use [CSV preview and apply](csv.md) against the same reviewed file.
- For a series of scripted operations, prefer an already configured [MCP session](cli-contract.md#mcp-and-catalog).
