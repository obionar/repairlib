# RepairLib

A repair-shop app for one workshop: customers, repairs, work log, parts and labour, payment
status, attachments and stock. Staff use the browser. Integrations use the JSON API; the CLI
and the stdio MCP bridge work on the local workspace.

## Requirements

Linux (`x86_64` or `aarch64`) with Python 3.13+. Check the host first; it prints copy-paste
fixes for anything missing:

```sh
python3 scripts/preflight.py                # or --mode venv, --mode podman
```

System packages, a `.venv` or a Podman container: see the [deployment guide](docs/deployment.md).

## Quick start

```sh
git clone https://github.com/obionar/repairlib.git "$HOME/repairlib"
cd "$HOME/repairlib"
bin/repairlib setup --shop "My Shop" --username owner
bin/repairlib service install && bin/repairlib service start
```

Open <http://127.0.0.1:8000> and sign in as `owner`. The workspace is `./data`
(`REPAIRLIB_WORKSPACE` overrides it). Keep it Git-ignored and backed up: `git clean -xdf`
deletes it. `bin/repairlib links` puts `repairlib` on your `PATH` and links the agent skill.
Update with `bin/repairlib update`. Staff devices on a closed LAN: add `--lan`
([deployment tips](docs/deployment.md#tips)).

Try it first: [disposable workspace](docs/operations.md#initialize-a-workspace-in-the-browser)
or [synthetic demo](docs/operations.md#initialize-an-isolated-synthetic-demo).

## Guides

| Task | Guide |
|---|---|
| Prepare a host, daily runbook | [Deployment](docs/deployment.md) |
| Services, updates, versioned install, diagnostics | [Operations](docs/operations.md) |
| Daily workshop, staff, stock, printing, files | [Workshop](docs/workshop.md) |
| CLI, API, MCP, customer privacy for AI agents | [Integrations](docs/integrations.md) |
| CSV import, transfer, backup, restore, migrate | [Data and recovery](docs/data.md) |
| Templates and styles | [Frontend map](docs/frontend.md) |
| Operating RepairLib as an agent | [Agent operations](docs/repairlib-operations.md) |
| Contributing | [CONTRIBUTING](CONTRIBUTING.md) |

## Check

```sh
python3 scripts/check.py           # full suite, one module per CPU; run before committing
python3 scripts/check.py --fast    # smoke suite
python3 scripts/check.py --module test_search.py   # one test module
python3 scripts/check.py --full    # adds the slow installer tests
python3 scripts/check.py --distro debian   # committed HEAD in a clean container (or alpine)
```

Tests use throwaway workspaces. Importing `repairlib` or building the app never creates or
migrates a database.

## Layout

`src/repairlib/`: `adapters/` (web, api, mcp) and `cli/` call `domain/` (rules, transactions,
audit), which calls `storage/` (the only SQL). `client/` runs domain operations locally for the
CLI and MCP; `lifecycle/` handles locking, backup, services and updates.

## License

[MIT](LICENSE).
