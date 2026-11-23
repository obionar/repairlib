# CLI, API and MCP

[RepairLib README](../README.md)

[Customer privacy for agents](#customer-privacy-for-ai-agents) · [Local CLI](#use-the-cli-on-the-shop-machine) · [HTTP API](#http-api) · [MCP host setup and tool reference](#connect-an-mcp-host-over-stdio)

Task-specific CLI and API examples are in [Workshop workflows](workshop.md) and [Data transfer and recovery](data.md).

## Customer privacy for AI agents

RepairLib sends an AI agent only the customer data a request needs, so less personal data reaches a
cloud model. Lists, repair briefs, the dashboard, customer history and exports show a customer as `C7`
(the customer ID, also shown in the web customer list and record) with phone, email and address as
`[masked]`. A request about a person gets the details:

- A customer search by name (`customers list --query john`, MCP `list_customers` with `query`, API
  `GET /api/v1/customers?q=john`) returns the matching names; contacts stay masked. "All repairs from
  John" is that search, then `repairs list --customer ID`.
- One customer's record (`customers show 7`, MCP `get_customer`) is shown whole: "who is C7?" or
  "what is John's phone?". Agent API tokens get the name and keep contacts masked.

Agent routes are the MCP bridge, API tokens of kind `agent`, and the CLI when `REPAIRLIB_AGENT=1` is
set in the agent's environment (put it in the agent host's configuration, next to
`REPAIRLIB_WORKSPACE`). People (the web UI, `user` API tokens and the CLI without the variable) see
everything. Work-log text and customer notes are shown as written. This reduces what reaches a model in
normal use; it is not a barrier against someone who can read the workspace folder.

## Use the CLI on the shop machine

The CLI and MCP bridge run on the machine that holds the workspace and work on it directly. They need no token or profile: anyone who can open the workspace folder can already read its database, so shell access is the credential. Every change is still recorded in the audit history, attributed to the built-in user `cli` (or `mcp`) with the same source name.

```sh
repairlib dashboard --workspace "$repairlib_workspace"
repairlib repairs list --workspace "$repairlib_workspace" --json
```

Without `--workspace` the commands use the default state directory, the same one `setup` and `dev` use. Both built-in users act as shop administrators, cannot sign in to the web app, are hidden from user administration, and their names cannot be used for new accounts. Each is created on first use and that creation is audited.

`repairlib passwd --username NAME` sets any password, even while the app runs ([users](workshop.md#manage-users-and-recover-an-administrator-password)). Exit codes: [reading a refusal](repairlib-operations.md#reading-a-refusal).

## HTTP API

The HTTP API is for programs on other machines. Sign in to the web app, open **API tokens** in Account, and create a named token with your current password. Read-only tokens are available to every role; admins and technicians can also choose read and write, within their role's permissions. Revoke a token from Account with your current password.

`GET /api/v1/identity` (bearer authentication) returns shop, user, role, scope and token name; the schema is at `/api/v1/openapi.json`.

`GET /api/v1/repairs/{id}/activity` accepts a read- or write-scoped token and returns same-shop repair activity newest first by event ID: operation, UTC timestamp, recorded actor username, source, and affected entity type and ID. Raw changes, costs and token labels are never returned, even to administrators. `limit` defaults to 25 (1–100); pass the returned `next_before` as `before` for the next older page (`null` means none). `repairs activity ID` and the MCP `repair_activity` tool return the same page.

## Connect an MCP host over stdio

The MCP bridge works on the local workspace as the built-in `mcp` administrator, like the [CLI](#use-the-cli-on-the-shop-machine), and makes no HTTP calls. It acts with full shop access (including purchase costs), so connect only hosts you trust. Because its caller is an AI agent, every result follows [customer privacy for agents](#customer-privacy-for-ai-agents).

Configure a local stdio server in your MCP host, substituting the absolute paths to the interpreter, the checkout's `src` directory and the workspace:

```json
{
  "mcpServers": {
    "repairlib": {
      "command": "/usr/bin/python3",
      "args": ["-m", "repairlib", "mcp", "--workspace", "/path/to/workspace"],
      "env": {"PYTHONPATH": "/path/to/repairlib/src", "PYTHONDONTWRITEBYTECODE": "1"}
    }
  }
}
```

For a private installation use the absolute installed launcher as `command`, start `args` with `mcp` and omit `env`. Omit `--workspace` and its value for the default state directory. The host exchanges MCP messages with the bridge on stdin and stdout.

Use `list_customers` or `list_repairs` to find record IDs, then the detail and history tools to read revisions, charges and attachment metadata. The bridge also exposes dashboard queues and counts, inventory, stock movements and stock reconciliation. `read_attachment_content` returns verified original bytes as base64 for files up to 64 KiB and refuses larger files (use `repairs download`).

Paginated tools take an optional `limit` (1–100) and a `before` cursor; pass the returned `next_before` for the next page, and a null cursor marks the last page. Results are under `structuredContent.result`. A failed call sets `isError` and returns `structuredContent.error` with a code and message, plus the equivalent HTTP status when there is one.

| Workflow | Mutation tools |
|---|---|
| Customers | `create_customer`, `edit_customer`, `archive_customer`, `restore_customer` |
| Repairs | `create_repair`, `edit_repair`, `change_repair_stage`, `mark_repair_picked_up`, `undo_repair_pickup`, `mark_repair_paid`, `mark_repair_unpaid`, `archive_repair`, `restore_repair` |
| Work entries | `create_work_entry`, `correct_work_entry`, `delete_work_entry`, `restore_work_entry` |
| Inventory catalog | `create_inventory_item`, `edit_inventory_item`, `archive_inventory_item`, `restore_inventory_item` |
| Stock movements | `receive_stock`, `adjust_stock`, `reverse_stock_movement` |
| Repair catalog parts | `consume_part`, `return_part`, `correct_intake_mistake` |
| Repair charges and discount | `create_charge`, `correct_charge`, `delete_charge`, `restore_charge`, `set_discount` |
| Repair attachments | `upload_attachment`, `edit_attachment_caption`, `link_attachment`, `unlink_attachment`, `detach_attachment`, `restore_attachment`, `purge_attachment` |

Every mutation needs a caller-supplied `key` of 1–128 ASCII letters, digits, underscores or hyphens. Changes to an existing record also need its reviewed `revision`. Repair edits replace all intake fields, including the customer and technician links: supply every field and use null for an unassigned link. Work-entry correction, deletion and restoration need a reason and keep previous revisions, and reopening a completed repair needs a reason.

Catalog edits replace all catalog fields (use null to clear the purchase cost). Stock quantities are integer milli-units and prices integer EUR cents. Read the repair and inventory item before `consume_part`, then supply their revisions and separate consumed and billed quantities. `part_return_status` gives the reviewed revisions and the quantity still returnable; `return_part` puts stock back and lowers the billed quantity by the same amount at the charge price (removing a line billed to zero). `correct_intake_mistake` also needs the reviewed charge revision, returns all remaining consumption and hides the charge.

`create_charge` adds a free-text part or labor charge (integer milli-units and EUR cents). `correct_charge` replaces all charge fields at the revision from `get_charge`: supply all fields and a reason, with null for an unset unit cost and empty text for unset notes or unit. `delete_charge` and `restore_charge` take a reason and the reviewed charge revision. These tools change billing only.

`set_discount` takes the `discount_revision` from `charge_summary` as `revision` (0 before the first discount) and a `value` in EUR cents for `amount`, a whole percentage for `percent`, or 0 for `none`.

`upload_attachment` takes `repair_id`, a printable `filename` of up to 255 characters, `content_base64` and `key`; an optional `entry_id` links the file to a visible work entry of the same repair, and `role: "device"` sets an image as the device photo. Encode 1–65,536 original bytes (64 KiB) as standard padded base64, with no whitespace or data-URL prefix and at most 87,384 characters. The bytes are stored through the same upload path as `repairs attach`, and the tool returns attachment metadata including `size_bytes` and `sha256`. Use `repairs attach` for larger files; the shop's configured upload limit still applies.

The other attachment tools take `repair_id`, `attachment_id`, the reviewed `revision` from `get_attachment` and `key`. Pass `caption` as empty text to clear it; link and unlink take an `entry_id`; detach keeps the original bytes and restore reattaches the file. Purge needs an administrator and at least 30 days detached, permanently deletes the bytes and keeps metadata and history.

Save the key and arguments before calling a mutation. If the outcome is uncertain (an unexpected `server_error` says so), send the identical call with the original key, arguments and revision: the stored result is returned without applying the change again. A `stale_revision` error means the record changed, so review it first; a `conflict` can mean the key already belongs to a different request. Allocate a new key only for a newly reviewed intent.
