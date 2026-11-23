# Workspace, services and life cycle

Every command here is run by the operator (or with the operator's explicit authorization).
`--workspace` selects the workspace; the checkout launcher defaults it to `./data`.

## First run and readiness

| Command | Purpose |
|---|---|
| `setup --shop NAME --username NAME [--timezone ZONE] [--language CODE]` | initialize a new workspace and first admin; reads the password from a prompt or one line of stdin (never argv). Refuses a non-empty workspace. |
| `demo [--username owner]` | initialize a new synthetic demo workspace; password from stdin. Refuses to reuse a workspace. |
| `demo reset --confirm` | while stopped, reseed the synthetic customers of a pristine demo; accounts and credentials are preserved. |
| `dev [--host 127.0.0.1] [--port 8000] [--browser-setup]` | run the loopback development server; `--browser-setup` prints a one-time setup link on an interactive terminal. |
| `status [--json]` | workspace state, schema vs expected, disk free, counts, and update phase when launched through the installer. Exit 1 when the workspace is initialized but not ready. |
| `doctor` | read-only diagnosis: schema compatibility, disk, SQLite integrity/foreign keys, retained attachments, staging/orphans. Exit 1 for an unhealthy workspace or pending recovery. |
| `profile check [--json]` | path, state, schema, local identity/role, server. Exit 1 for any blocker. Run it when a write refuses with a workspace or permission error, and before setup or maintenance work. |
| `migrate [--backup ARTIFACT]` | upgrade an initialized workspace explicitly. A verified backup of the current schema may be required with `--backup`; use `backup create` first on real data. |
| `cleanup` | while stopped, remove stale upload staging, expired sessions and old request receipts. |
| `reconcile-files [--json]` | while stopped, remove never-committed uploads and verify attachment bytes; damaged files are reported and exit 2. |
| `support bundle --output FILE` | write a zip of system facts and counts only (no records, costs or tokens). Never overwrites. |
| `dashboard [--json]` | open work, awaiting pickup and low-stock counts, with previews. |
| `links [--executable PATH]` | install `~/.local/bin/repairlib` and `~/.agents/skills/repairlib` as symlinks (see below). |

`links` creates two symlinks and never replaces a real file or directory: `command` →
the launcher, `skill` → this skill directory. It reports `created`, `unchanged` or
`blocked` per link and exits 1 if a blocked path has to be resolved by hand. The private
installer runs it automatically; a checkout runs `bin/repairlib links`.

## Backups and transfer

```sh
XDG_BACKUP_DIR="$HOME/.local/state/repairlib-backups"   # private: owned by you, mode 0700
mkdir -p "$XDG_BACKUP_DIR"; chmod 700 "$XDG_BACKUP_DIR"
repairlib backup snapshot --workspace "$WS" --output "$XDG_BACKUP_DIR/shop.sqlite3"
repairlib backup create   --workspace "$WS" --output "$XDG_BACKUP_DIR/shop.zip"
repairlib backup verify   "$XDG_BACKUP_DIR/shop.zip"
repairlib backup restore  "$XDG_BACKUP_DIR/shop.zip" --workspace "$NEW_WS"
```

| Command | Notes |
|---|---|
| `backup snapshot --output FILE [--online]` | database-only verified snapshot; `--online` copies a running shop through SQLite's backup API. Stopped by default. |
| `backup create --output FILE` | stopped workspace archive including retained attachment files. |
| `backup verify ARTIFACT` | verify an archive or standalone snapshot; prints its kind and schema. |
| `backup restore ARTIFACT --workspace NEW` | restore a verified archive only into a new destination; never overwrites. |

Output directories must be private and owned by you (`0700`); the archive file must not
exist yet.

| Command | Notes |
|---|---|
| `portable export ARCHIVE` | write the current shop to a new private zip; the path must not exist. |
| `portable preview ARCHIVE` | verify an archive and print its source shop, format, schema, attachment count and table counts. |
| `portable import ARCHIVE --destination-shop-id N --revision R --key K` | import into a confirmed empty shop at its reviewed settings revision; writes are transactional and idempotent. |

## Services and updates

| Command | Notes |
|---|---|
| `service install` | register the systemd **user** unit for this checkout, workspace and default port 8000. Does not start it. |
| `service install --openrc-system` | register the root-supervised OpenRC service that runs as this user (unprivileged command; it asks the supervisor). |
| `service start` / `stop` / `restart` | explicit operations; `start` waits for readiness. |
| `service status` | JSON: `service`, `health`, workspace and source; `running`/`ready` when healthy. |
| `service remove` | unregister the unit (`--openrc-system` for OpenRC). |
| `service foreground [--port 8000]` | run in the current terminal; Ctrl+C stops it. Stop a registered service first if it owns the port. |
| `update [--check]` | for a Git checkout only: `--check` prints current and available commit; otherwise refuses a dirty tree, fast-forwards the branch, restarts a registered service and reports the old → new commit. |
| `links` | install the command and skill symlinks (idempotent). |

`service`, `update`, packaging and every state-changing life-cycle operation belong to
the operator. An agent may read `status`, `doctor` and `profile check` freely; it must not
run the others on a shop's instance without explicit instruction.

## `mcp` and `catalog`

- `repairlib mcp --workspace "$WS"` — stdio MCP server as the built-in `mcp` administrator.
  Configure a host with command `~/.local/bin/repairlib` (or `/usr/bin/python3 -m repairlib`)
  and args `mcp --workspace …`. It never calls HTTP and exposes purchase costs.
- `repairlib catalog [--json]` — every CLI command and MCP tool with its arguments. Use it
  as the machine-readable index; `--help` remains authoritative for one command.
