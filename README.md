# RepairLib

RepairLib is a single-workshop application for tracking customers, repairs, work
entries, parts, labor, payment status, attachments, and inventory. Staff use the browser
UI. Remote integrations use the authenticated JSON API; the CLI and stdio MCP bridge work on the local workspace. The app
also provides CSV intake, portable shop archives, and offline backup and restore.
Use disposable workspaces while evaluating the development build.

## Get started

1. Install the source prerequisites below, or follow the [Linux installation guide](docs/operations.md).
2. [Initialize a disposable workspace](docs/operations.md#initialize-a-workspace-in-the-browser)
   and sign in. To start with three fictional customers, [seed an isolated demo](docs/operations.md#initialize-an-isolated-synthetic-demo).
3. Use the [workshop guide](docs/workshop.md#daily-workflow) to run a repair from intake through pickup.

## Guides

| Task | Guide |
|---|---|
| Install on Alpine/OpenRC or Linux/systemd, set up a workspace, rehearse updates | [Installation and operations](docs/operations.md) |
| Run the workshop, manage staff and stock, print or attach files | [Workshop workflows](docs/workshop.md) |
| Use the local CLI, set passwords, discover the versioned API or connect a stdio MCP host | [CLI, API and MCP](docs/integrations.md) |
| Import CSVs, transfer shop records, back up, restore or migrate | [Data transfer and recovery](docs/data.md) |
| Change templates, styles or page structure | [Web frontend map](docs/frontend.md) |
| Operate RepairLib safely as an agent | [RepairLib operations for agents](docs/repairlib-operations.md) |
| Report a bug or contribute a change | [Contributing](CONTRIBUTING.md) |

## Requirements

RepairLib runs on the stock `python3` and the distribution's own Python packages.
That is the tested setup: no `pip`, virtual environment or `uv`, Python 3.13 or
newer, on Debian 13 (trixie) and Alpine 3.24.

```sh
# Debian 13
sudo apt install python3 python3-fastapi python3-jinja2 python3-uvicorn \
  python3-pydantic python3-httpx python3-argon2 python3-python-multipart git
# Alpine 3.24
doas apk add python3 py3-fastapi py3-pydantic py3-jinja2 uvicorn py3-httpx \
  py3-argon2-cffi py3-python-multipart git
```

If your distribution does not ship these packages, or you prefer isolation, the
same checkout runs from a virtual environment. Install the dependency list, not
the project, and keep `PYTHONPATH=src`:

```sh
python3 -m venv .venv
.venv/bin/pip install fastapi jinja2 uvicorn pydantic httpx argon2-cffi python-multipart
PYTHONPATH=src .venv/bin/python -m repairlib --help
```

`scripts/install.py` remains stock-only and refuses any interpreter that is not
the distribution's `/usr/bin/python3`.

Run source commands from the checkout root with
`PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m repairlib ...`.
The MCP bridge is part of RepairLib itself (standard library only).

For a Linux install outside the source checkout, see
[Linux install](docs/operations.md#linux-install).

## Quick start

With the packages above installed, one clone, one target and one service:

```sh
git clone https://github.com/obionar/repairlib.git
cd repairlib
bin/repairlib setup --shop "My Shop" --username owner
bin/repairlib service install
bin/repairlib service start
```

`bin/repairlib` runs this checkout with the workspace in `./data` and the
listener on port 8000, so no paths or environment variables are needed. `setup`
reads the first admin password from the terminal and takes the timezone from
this machine unless `--timezone` is given. Open <http://127.0.0.1:8000>.
Upgrade later with `git pull` and `bin/repairlib service restart`.

The [installer](docs/operations.md#linux-install) remains available for a
versioned installation with atomic staged updates and rollback.

## Portable checkout

The simplest deployment has no installer at all: clone the repository anywhere
the operator owns, keep the workspace and its backups in a separate directory,
and register the service from that checkout.

```sh
git clone https://github.com/obionar/repairlib.git "$HOME/repairlib"
cd "$HOME/repairlib"
bin/repairlib setup --shop "My Shop" --username owner
bin/repairlib service install
bin/repairlib service start
```

Update with `git pull` in the checkout and `service restart`; nothing else moves.
To move to another machine, copy the whole checkout and its `data/` and
`data-backups/` directories (owned by the service user, mode 0700), or clone
again and copy those two directories, then install the service there.

The workspace can be the checkout's own `data/` directory, which is already
Git-ignored, so one folder carries both code and records. A workspace inside the
checkout must be ignored: a branch switch or `git checkout` would otherwise
overwrite shop records, and `git clean -xdf` still deletes ignored files, so keep
a backup before cleaning. The installer's target is separate from its workspace
for the same reason.

## Check

From the project root:

```sh
python3 scripts/check.py
```

The standard-library runner discovers tests under `tests/` and exits nonzero if
any fail. Test workspaces are disposable. The runner disables bytecode writes.
Use `python3 scripts/check.py --fast` for the local smoke suite; run the full
command before committing. On a multicore machine, `python3 scripts/check.py
--jobs 6` runs the full suite with six module workers (about two minutes on six cores); the default stays serial.
With Git and Podman, `python3 scripts/check.py --distro debian` (or `alpine`)
checks committed `HEAD` in a disposable clone using that distribution's stock
packages. It does not mount the original checkout or test uncommitted changes.
Use the host check for edits, or commit them on a local verification branch to
check that exact revision in the container.

## Development server

For an uninitialized development listener, choose a fresh path outside the
source tree:

```sh
repairlib_workspace="$(mktemp -d)/workspace"
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m repairlib dev \
  --workspace "$repairlib_workspace"
```

The server binds `127.0.0.1:8000` by default. Check the listener and workspace readiness:

```sh
curl http://127.0.0.1:8000/health
# {"status":"ok","state":"uninitialized","ready":false,"workspace_exists":false}
```

The health response reports `ready: true` after an initialized workspace has
started in sign-in mode.

## Project map

- `src/repairlib/app.py` — side-effect-free FastAPI factory and health route.
- `src/repairlib/config.py` — explicit/default workspace path resolution.
- `src/repairlib/doctor.py` — read-only diagnostics.
- `src/repairlib/__main__.py` — local lifecycle and CLI command entry point.
- `src/repairlib/adapters/web/` — shared base template, setup, sign-in, dashboard, and business forms.
- `src/repairlib/adapters/api/` — versioned business, identity, archive, and CSV endpoints.
- `src/repairlib/adapters/mcp.py` — stdio MCP tools over the local workspace, as the `mcp` user.
- `src/repairlib/client/` — local workspace operations shared by the CLI and MCP bridge.
- `src/repairlib/domain/` — shared workflow, access, billing, stock, and file operations.
- `src/repairlib/lifecycle/` — workspace locking, offline recovery, Linux service management, and update worker.
- `src/repairlib/storage/` — SQLite schema, compatibility checks, and database snapshots.
- `scripts/check.py` — the single check entry point.
- `tests/` — workflow, adapter, recovery, installation, and architecture checks.

Importing `repairlib` or building the app never creates or migrates a database.

## License

RepairLib is licensed under the [MIT License](LICENSE).
