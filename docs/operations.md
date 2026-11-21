# Installation and operations

[RepairLib README](../README.md)

[Workspace setup](#initialize-a-workspace-in-the-browser) · [Alpine/OpenRC](#alpine-system-packages-raspberry-pi) · [Linux install](#linux-install) · [Services](#linux-service-lifecycle) · [Diagnostics](#workspace-status-and-diagnostics)

For backup or migration commands, see [Data transfer and recovery](data.md).

## Initialize a workspace in the browser

From the source checkout with the [source prerequisites](../README.md#requirements)
installed, create a disposable workspace and start the loopback setup listener:

```sh
repairlib_workspace="$(mktemp -d)"
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m repairlib dev \
  --workspace "$repairlib_workspace" --browser-setup
```

Open the one-time link printed to the terminal within 15 minutes. Complete the
form with a shop name, IANA timezone, and first admin credentials. After the
workspace is created, stop the server with Ctrl+C and restart it without
`--browser-setup`:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m repairlib dev \
  --workspace "$repairlib_workspace"
```

Open http://127.0.0.1:8000 and sign in with the new credentials. If the setup
link or browser setup session expires before completion, restart with
`--browser-setup` to issue a fresh link.

## Initialize a workspace from the terminal

With the [source prerequisites](../README.md#requirements) installed, choose a disposable workspace from the project root:

```sh
repairlib_workspace="$(mktemp -d)"
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m repairlib setup \
  --workspace "$repairlib_workspace" --shop "Demo Shop" --timezone Europe/Nicosia \
  --username owner
```

Setup reads the first admin password from a hidden terminal prompt or one line on
stdin. It publishes the SQLite database at `state/repairlib.sqlite3` inside the
workspace. If setup was interrupted before publication, run the same command again.
After setup, restart any development server for that workspace and sign in at
http://127.0.0.1:8000.

## Initialize an isolated synthetic demo

To create a new workspace containing three clearly marked synthetic customers,
use a fresh path outside the source tree:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m repairlib demo \
  --workspace /tmp/repairlib-synthetic-demo --username owner
```

The command reads the demo admin password securely, records a maintenance audit
event, and refuses an existing workspace so sample data cannot be mixed with
business records. Start the development server with the same workspace to sign in.

To reseed an untouched demo, stop every app process for that workspace and run:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m repairlib demo reset --confirm \
  --workspace /tmp/repairlib-synthetic-demo
```

Reset requires a current, single-shop workspace with the original demo seed
event and the three unmodified synthetic customers. Additional business records
or attachment storage content prevent reset. Reset preserves accounts, passwords,
sessions, API tokens, shop settings, and prior audit history. It reseeds the
customers and records one maintenance event in the same transaction.

### Add records to an existing test instance

Sign in as an administrator or technician. Create a clearly labeled fictional
customer under **Customers**, then create a linked ticket under **Repairs**.
Leave one ticket in an open stage, and move another to **Repaired** without
recording pickup. Check the dashboard's **Open work** and **Awaiting pickup**
queues and the customer's open repair count. Existing test instances accept
these changes through the usual browser forms or the
[local CLI](integrations.md#use-the-cli-on-the-shop-machine).

## Alpine system packages (Raspberry Pi)

Alpine 3.24/aarch64 with OpenRC is the primary deployment target. Install the
runtime dependencies from that release's official `main` and `community`
repositories:

```sh
doas apk add python3 py3-fastapi py3-jinja2 uvicorn py3-pydantic \
  py3-httpx py3-argon2-cffi py3-python-multipart tzdata
```

`tzdata` supplies the IANA timezone files used for shop settings and timestamp
display. If the configured zone cannot be loaded, RepairLib logs a warning when
rendering timestamps and shows their original stored values.

Keep an exact source revision in a dedicated directory outside the workspace.
Use the system interpreter directly; no `pip`, uv or virtual environment is
needed. Verify that the selected source starts under the system interpreter before registration:

```sh
cd /absolute/path/to/repairlib-source
/usr/bin/python3 -I -B -c 'import sys, runpy; sys.path.insert(0, sys.argv.pop(1)); runpy.run_module("repairlib", run_name="__main__")' \
  "$PWD/src" --help
```

Use that same command prefix (through `"$PWD/src"`) in place of the private
launcher for setup and the [OpenRC service commands](#openrc-system-service-on-alpine).
Use `doas` for system administration, not to run RepairLib itself. The web app,
HTTP API, CLI and MCP bridge need only the packages listed in the README.

### Rehearse a same-schema OpenRC source update

Use a disposable OpenRC installation and workspace. The stock-package path
uses the same system Python and a new source directory for each exact Git commit.
Keep the old source, and use the same workspace and port at every cutover. This
procedure does not migrate the workspace. If the candidate reports anything
other than a current, initialized schema on the restored copy, stop.

Set `old_src` and `new_src` to the absolute `src` directories of the registered
and staged revisions, `workspace` to the disposable workspace, and `port` to
its registered loopback port. Run each step only after the previous one succeeds:

```sh
rl() {
  /usr/bin/python3 -I -B -c 'import sys, runpy; sys.path.insert(0, sys.argv.pop(1)); runpy.run_module("repairlib", run_name="__main__")' "$@"
}
svc() {
  rl "$1" service "$2" --openrc-system --workspace "$workspace" --port "$port"
}
```

1. Verify `svc "$old_src" status`, then run `svc "$old_src" stop`. Keep all
   other workspace writers stopped through the switch. Prevent clients from
   reaching the service until you have accepted the new runtime: this manual
   sequence does not gate writes when the candidate starts.
2. Use a fresh private recovery directory outside the workspace:

   ```sh
   recovery=$(mktemp -d)
   rl "$old_src" backup create --workspace "$workspace" --output "$recovery/backup.zip"
   rl "$new_src" backup verify "$recovery/backup.zip"
   rl "$new_src" backup restore "$recovery/backup.zip" --workspace "$recovery/restored"
   rl "$new_src" doctor --workspace "$recovery/restored"
   ```

   Run `rl "$new_src" service foreground --workspace "$recovery/restored" --port "$port"`
   while OpenRC is stopped. Check `/health` and read representative records and
   original attachment bytes. Stop this foreground process before continuing.
   Do not send test prints from the rehearsal.
3. Run `svc "$old_src" remove`, `svc "$new_src" install`, then `svc "$new_src" start`.
   Confirm `svc "$new_src" status` reports `running`, the new source, and the
   same workspace and port. Check `/health` and verify the live data before
   allowing normal use again. Keep the old source and verified archive. On
   the next update, use the currently registered source as `old_src` and take
   a **fresh** backup containing writes made under that revision.
4. If registration or startup fails, stop and inspect the current service
   state. If the failed candidate is still registered, remove it using
   `svc "$new_src" remove`; never overwrite a mismatched script. If the
   service is absent, the workspace is unchanged and compatible, and no new
   writes could have landed, use `svc "$old_src" install` and
   `svc "$old_src" start`. Check readiness and retained data. If writes may
   have landed, preserve the current workspace for reconciliation; do not
   restore the older snapshot over it.

This procedure was rehearsed with an isolated OpenRC stand-in and real loopback
listeners. It is not an automated OpenRC updater. The private-install updater
below does not manage stock-package deployments.

## Linux install

From a local Git checkout containing the installer, use an unprivileged Linux account with Git. Choose an absolute, unused target outside the checkout; its parent must exist, belong to you and be protected from other writers.

The installer uses only the distribution's `/usr/bin/python3` (3.13 or newer) and its Python packages, on `x86_64` or `aarch64` (Debian 13 and Alpine 3.24 included). It downloads nothing and creates no virtual environment. Install the packages listed in the [README](../README.md#requirements) first; the installer names any missing module and refuses to continue. Run it with that same `/usr/bin/python3`. Verification limits: [service lifecycle](#linux-service-lifecycle).

With a virtual environment instead, run the checkout directly: create the venv as the [README](../README.md#requirements) shows, install the dependency list there, and use `PYTHONPATH=src .venv/bin/python -m repairlib`. The installer below is stock-only and refuses a venv interpreter.

This installs into a disposable directory with a separate workspace:

```sh
repairlib_root="$(mktemp -d)"
repairlib_target="$repairlib_root/install"
repairlib_workspace="$repairlib_root/workspace"
python3 -I -B scripts/install.py --commit HEAD --target "$repairlib_target"
repairlib="$repairlib_target/repairlib"
```

### Install from a GitHub clone

Pick a released commit or `origin/main`, keep the clone as the source of truth, and let the installer copy that commit into its own target:

```sh
git clone https://github.com/obionar/repairlib.git repairlib-source
git -C repairlib-source checkout --detach <commit>
python3 -I -B repairlib-source/scripts/install.py --commit <commit> \
  --target "$HOME/.local/opt/repairlib"
"$HOME/.local/opt/repairlib/repairlib" setup --workspace "$HOME/.repairlib" \
  --shop "My Shop" --timezone Europe/Nicosia --username owner
```

### Upgrade that installation from GitHub

Stop the listener first. Fetch in the clone, then stage and activate the newer commit; staging refuses equal, older or non-descendant commits:

```sh
git -C repairlib-source fetch origin
git -C repairlib-source checkout --detach origin/main
repairlib_head="$(git -C repairlib-source rev-parse HEAD)"
python3 -I -B repairlib-source/scripts/install.py --stage-update --commit "$repairlib_head" \
  --target "$HOME/.local/opt/repairlib" --workspace "$HOME/.repairlib" \
  --candidate "$HOME/.local/opt/repairlib-next"
python3 -I -B repairlib-source/scripts/install.py --activate-update \
  --target "$HOME/.local/opt/repairlib" --candidate "$HOME/.local/opt/repairlib-next"
"$HOME/.local/opt/repairlib/repairlib" status --json
"$HOME/.local/opt/repairlib/repairlib" doctor
```

Activation leaves writes closed. Start the selected listener with the stable launcher (no `--workspace`), then reopen writes from the checkout:

```sh
"$HOME/.local/opt/repairlib/repairlib" service foreground --port 8000 &
python3 -I -B repairlib-source/scripts/install.py --reopen-update \
  --target "$HOME/.local/opt/repairlib" --candidate "$HOME/.local/opt/repairlib-next" --port 8000
"$HOME/.local/opt/repairlib/repairlib" status --json
```

On an OpenRC host, remove the registered service with `repairlib service remove --openrc-system` before staging; the installer refuses to stage while `/etc/init.d/repairlib` exists.

`--commit` takes a local Git revision (default `HEAD`); run the copy of `scripts/install.py` from that commit. The installer archives the commit into the target, checks that it starts on the installed packages, and records the exact commit and interpreter in `install.json`. The launcher runs `/usr/bin/python3` against the staged source; upgrade distribution packages through the distribution's package manager and rehearse an update before relying on it. Keep the target at its original path and workspace files and backups outside it. After an interruption that leaves a partial target, use a fresh path.

Initialize the workspace, then run in the foreground or register with the platform service manager (systemd user or OpenRC, below):

```sh
"$repairlib" setup --workspace "$repairlib_workspace" \
  --shop "Demo Shop" --timezone Europe/Nicosia --username owner
"$repairlib" service foreground --workspace "$repairlib_workspace"
```

Check `http://127.0.0.1:8000/health`; Ctrl+C stops the foreground process. To register a service, stop it and follow the [systemd](#linux-service-lifecycle) or [OpenRC](#openrc-system-service-on-alpine) steps with the same launcher and workspace.

## Maintenance

- **Logs.** The service logs through the system logger (`logger` on OpenRC, the user journal with
  systemd), so rotation and retention follow the host configuration. Nothing is written inside the
  workspace.
- **Backups.** `repairlib backup snapshot --workspace … --output …` takes a database-only snapshot
  while stopped, or with `--online` while the app runs; `repairlib backup verify <archive>` proves
  a snapshot restores. The scheduled backup keeps `<workspace>-backups` fresh; `doctor` reports
  whether the last verified backup is stale.
- **Disk.** The app reserves 1 GiB of free space and reports it in `status`; `cleanup` removes
  stale uploads, expired sessions and old request receipts while stopped.
- **Health.** `repairlib doctor --workspace …` is the single read-only check: schema, SQLite
  integrity and foreign keys, stock ledger, orphan entries, retained attachments and backup age.
  Run it after every update and before a backup is trusted.
- **Restore drill.** Copy the archive away from the host, verify it, and restore into a disposable
  workspace once per release so a real recovery is not the first attempt.

## Update a checkout or an installation

A source checkout updates with `bin/repairlib update`: it refuses a dirty tree, checks out the
tracked branch with `--ff-only`, then restarts the registered service (systemd user unit or
OpenRC script) and reports the old and new commits. `bin/repairlib update --check` prints both
commits without changing anything.

An installation created by `scripts/install.py` keeps immutable generations and updates with the
staged recipe below; never `git pull` inside an installation.

## Check a private installation for update blockers

From the local source checkout, set `repairlib_target` to the existing private
installation and `repairlib_next` to the desired local Git revision, then run:

```sh
python3 -I -B scripts/install.py --check-update --target "$repairlib_target" --commit "$repairlib_next"
```

The JSON reports the original installed commit, any selected candidate, its
on-disk phase, the requested commit, and a blocker (exit 1) or `null` (exit 0).
It does not query the running listener, fetch, write files, or start services.
A clear result is not a staging guarantee: workspace locks, backup, disk space,
migration compatibility and health are checked during the rehearsal below.
An open foreground selection can stage another descendant on the same target.
Managed selections still block repeated staging. This command is not an OpenRC updater.

## Rehearse a Linux update on a disposable installation

Use an existing private install and its initialized synthetic workspace, with writers stopped. The checkout's `HEAD` must be a strict descendant of the installed commit and its installer must match. Use a fresh candidate path beside the install and workspace above. The installer refuses staging, activation, reopening and recovery while `/etc/init.d/repairlib` exists; the managed handoff below uses a systemd user unit.

```sh
repairlib_next="$(git rev-parse --verify HEAD)"
repairlib_candidate="$repairlib_root/candidate"
python3 -I -B scripts/install.py --stage-update --commit "$repairlib_next" \
  --target "$repairlib_target" --workspace "$repairlib_workspace" \
  --candidate "$repairlib_candidate"
"$repairlib_candidate/repairlib" doctor --workspace "$repairlib_candidate/workspace"
```

Staging resolves one commit and refuses the installed commit, non-descendants and incompatible migration histories. It locks the installation and takes the workspace maintenance lock, creates a verified archive, rehearses restoring it into a temporary workspace, then restores separately as the candidate workspace under the candidate's own private runtime. Explicit migration, doctor and private HTTP health checks run against the candidate, plus an authenticated synthetic customer write and readback in a temporary workspace.

A successful `stage.json` records both commits, schema versions, fingerprints and completed checks. Keep it with `backup.zip` and the candidate at their original paths. An incomplete directory may hold a verified backup without `stage.json`: keep the backup and use a fresh candidate path. A stage from an older installer must be staged again.

With the installation still stopped, rehearse activation from the same checkout and candidate. The installer reacquires both locks, checks generations and workspaces against the staged record, verifies the retained backup, atomically selects the candidate's runtime and workspace, and runs private health checks before and after the switch.

```sh
python3 -I -B scripts/install.py --activate-update \
  --target "$repairlib_target" --candidate "$repairlib_candidate"
"$repairlib" status --json
"$repairlib" doctor
```

`install.json` still identifies the original installation; `current.json` selects the candidate and records its update phase. The stable launcher's `status --json` adds `update_phase` and `writes_open`; `doctor` reports both and needs the listener stopped for its integrity and attachment checks. The staged launcher stays available for diagnostics.

For the listener handoff, keep the original listener stopped, choose an unused loopback port and run the stable launcher in one terminal:

```sh
"$repairlib" service foreground --port 8000
```

It initially serves only `GET /health`; everything else, including reads, gets HTTP 503. In another terminal verify the listener and reopen writes:

```sh
python3 -I -B scripts/install.py --reopen-update \
  --target "$repairlib_target" --candidate "$repairlib_candidate" --port 8000
"$repairlib" status --json
```

The installer checks the staged workspace fingerprints, probes the listener and atomically records `open`; the same listener then serves the normal authenticated app. If the probe fails or the worker exits before publishing `open`, requests stay closed and the command can be retried while the listener runs.

For recovery, stop the candidate listener and use the matching installer:

```sh
python3 -I -B scripts/install.py --recover-update \
  --target "$repairlib_target" --candidate "$repairlib_candidate"
```

Before `open`, recovery can return `original` if both workspaces still match their staged fingerprints. After `open`, it verifies the stopped candidate in place (SQLite integrity, retained attachment bytes, private app health) and a success returns `open`. If checks fail, retain both workspaces and the archive for reconciliation before choosing a workspace. The original runtime, workspace and backup remain recovery artifacts, and recovery never copies old files over the candidate.

For another **foreground** update after writes have opened, stop the selected
listener and use its candidate workspace as `--workspace` for the next stage.
Keep the same installation `--target` and choose a fresh `--candidate` path.
Keep the earlier candidate directory and the new candidate's
`previous-selection.json` with its other recovery artifacts. Before the new
selection opens for writes, recovery reselects the preceding open generation.
After writes open, recovery verifies the new selection in place. Repeated managed
and OpenRC updates are outside this procedure.

The rehearsal used a loopback foreground listener under an isolated supervisor, interrupted the worker on each side of the write boundary and read back committed synthetic records and original attachment bytes after restart.

### Hand off an existing Linux user unit

For a disposable installation with a registered `repairlib.service`, use the
same user-manager configuration and fixed port that you used to register the
unit. Stop writers for staging. Activation stops the matching original listener
if it is running and checks that the staged workspace is still current. It
changes the unit to invoke the stable launcher, reloads the manager, selects
the candidate and starts it under the same unit. The installer verifies
supervisor readiness and loopback health. The candidate serves health only
until you reopen writes.

```sh
repairlib_port=8000
python3 -I -B scripts/install.py --activate-update --managed --port "$repairlib_port" \
  --target "$repairlib_target" --candidate "$repairlib_candidate"
"$repairlib" status --json
python3 -I -B scripts/install.py --reopen-update --managed --port "$repairlib_port" \
  --target "$repairlib_target" --candidate "$repairlib_candidate"
```

If a candidate start fails after selection reaches `ready`, retry with the
same managed reopen command. It starts the candidate if needed and verifies
that the process reported by the manager serves the selected workspace before
publishing `open`. You can also recover using the same managed port:

```sh
python3 -I -B scripts/install.py --recover-update --managed --port "$repairlib_port" \
  --target "$repairlib_target" --candidate "$repairlib_candidate"
```

Managed recovery handles stopping and starting the unit. Before `open`, it
can restart the original after an interrupted activation or after verifying
and clearing a closed selection. After `open`, it verifies the stopped
candidate and restarts it in place. Failed data checks require reconciliation
with both workspaces retained. After adoption, use the user manager for
routine unit control, for example `systemctl --user restart repairlib.service`.
The stable launcher follows the recorded selection on each managed start.

The managed handoff was rehearsed with an isolated supervisor and real
loopback child processes. Native systemd user-service lifecycle verification
has passed; native managed handoff and power-loss validation remain open.

## Linux service lifecycle

On a systemd host, the service commands register one `repairlib.service` per
Linux user with a systemd user manager. The initial unit uses the absolute Python
executable and source directory from the invocation. Keep those paths stable and
use the same workspace and port for every command. Use the installed launcher in
place of `python3 -m repairlib` when registering a private installation. An
installer managed handoff adopts the stable launcher for subsequent unit starts.

From the project root, as an unprivileged user, try the commands with a new
disposable workspace:

```sh
export PYTHONPATH="$PWD/src" PYTHONDONTWRITEBYTECODE=1
repairlib_workspace="$(mktemp -d)/workspace"

python3 -m repairlib service install --workspace "$repairlib_workspace"
python3 -m repairlib setup --workspace "$repairlib_workspace" \
  --shop "Demo Shop" --timezone Europe/Nicosia --username owner
python3 -m repairlib service start --workspace "$repairlib_workspace"
python3 -m repairlib service status --workspace "$repairlib_workspace"
```

Installation registers the unit in `$XDG_CONFIG_HOME/systemd/user` (default
`~/.config/systemd/user`). Start is explicit. Use `--port` on every service
command to choose a port other than 8000. Status reports JSON with the observed
supervisor state, process ID, configured loopback URL, workspace schema and
journal command. `running` requires systemd to report a running process, that
process to own the configured loopback listener, and `/health` to report
readiness. A running process that fails either readiness check reports `unready`.

Use `service restart` or `service stop` with the same options. While stopped,
run the existing `doctor` and `backup create` commands against the workspace.
Ordinary service startup requires a current schema. Follow the backup and
[explicit migration procedure](data.md#upgrade-a-development-workspace) for an older workspace.

```sh
python3 -m repairlib service remove --workspace "$repairlib_workspace"
```

Removal stops the managed unit, removes its unit file and reloads the user
manager. The workspace, its backups and the private service control lock remain
on disk. An unmanaged unit or a loaded override causes a refusal.

### OpenRC system service on Alpine

Alpine hosts with OpenRC can register one root-supervised `repairlib` service
that runs the application as the invoking user. This explicit mode keeps the app
unprivileged while allowing boot activation. It needs a working passwordless
`doas` or `sudo` configuration for the trusted administrator doing the
registration. Do not grant passwordless access to generic `install`, `ln`,
or `rm` commands to a restricted shop account: they can modify system files.
RepairLib probes non-interactively and never stores credentials.

Use the command prefix in [Alpine system packages](#alpine-system-packages-raspberry-pi)
or the absolute launcher from [Linux install](#linux-install),
and the initialized workspace.

```sh
"$repairlib" service install --openrc-system \
  --workspace "$repairlib_workspace" --port 8000
"$repairlib" service start --openrc-system \
  --workspace "$repairlib_workspace" --port 8000
"$repairlib" service status --openrc-system \
  --workspace "$repairlib_workspace" --port 8000
```

`install` registers `/etc/init.d/repairlib` in the OpenRC `default` runlevel,
maintains a private service control lock and refuses an existing foreign or
modified script. `running` requires a matching supervised process that owns the
loopback listener and a fresh, ready `/health` response.
Use the same flag and options for stop, remove, status and restart. Removal stops
the service, removes its runlevel entry and deletes only the exact matching
script; workspace data remains. Status/log readback is `rc-service repairlib
status` and syslog (`grep repairlib /var/log/messages` on Alpine). Boot activation
is configured by the `default` runlevel but is not proven until a reboot is
tested.

If neither service manager is available, run the initialized workspace directly
in the terminal. Use Ctrl+C to stop it.

```sh
python3 -m repairlib service foreground --workspace "$repairlib_workspace"
```

Linux service verification includes isolated supervisor tests, offline systemd unit
verification, native systemd and Alpine/OpenRC install/start/restart/status/stop/
removal rehearsals, and real foreground smoke tests. Native managed handoff,
reboot persistence and power-loss validation remain open.

## Laptop deployment on postmarketOS / OpenRC

Verified on this machine: postmarketOS v26.06 (Alpine-based), aarch64, with OpenRC
and no systemd. The OpenRC system service is selected automatically; it runs as
the invoking user on loopback port 8000, and the workspace lives in the
checkout's `data/` directory.

Prerequisites: the stock Python 3 and the distribution packages. They are already
installed on this machine; from scratch:

```sh
sudo apk add python3 py3-fastapi py3-jinja2 uvicorn py3-pydantic \
  py3-httpx py3-argon2-cffi py3-python-multipart tzdata
```

From the checkout root (`/home/ian/Dev/repairlib`):

```sh
# 1. Initialize the workspace and the first admin. Setup reads the admin
#    password from a hidden prompt (or one line of piped stdin) and takes the
#    timezone from this machine unless --timezone is given.
bin/repairlib setup --shop "My Shop" --username owner

# 2. Register the root-supervised OpenRC service: writes /etc/init.d/repairlib
#    and adds `repairlib` to the default runlevel. This step does not start it.
bin/repairlib service install --workspace /home/ian/Dev/repairlib/data

# 3. Explicit start: runs `sudo rc-service repairlib start` and waits until
#    the app is ready.
bin/repairlib service start --workspace /home/ian/Dev/repairlib/data
```

Verify the deployment:

```sh
sudo rc-service repairlib status
#  * status: started
curl http://127.0.0.1:8000/health
# {"status":"ok","state":"initialized","ready":true,"workspace_exists":true}
rc-update show default | grep repairlib
#             repairlib | default
```

The browser app is served at http://127.0.0.1:8000; `GET /` redirects to the
`/login` page. Without the supervised service, run in the foreground:

```sh
bin/repairlib service foreground --workspace /home/ian/Dev/repairlib/data
```

Notes:

- `service install` only registers the service; `service start` is the explicit
  start (separate commands; `service status` reports `started` when healthy).
- Service logs go through syslog (`logger -t repairlib`); check the system log
  for `repairlib`, for example `grep repairlib /var/log/messages` where the
  distribution keeps that file.

## Refresh a test instance running from the checkout
Refresh the browser after a CSS or template-only edit. After Python changes,
restart the checkout-backed server before reviewing the UI, especially when a
template expects new fields from its handler. A running process can pick up an
edited template while it still has the previous Python handler loaded.

For a test instance running as the systemd user unit, run this on the host.
Replace the example port with the port assigned to the instance.

```sh
systemctl --user restart repairlib.service
curl --fail http://127.0.0.1:8000/health
```

Once health reports `ready: true`, refresh the browser and open the signed-in
dashboard. The `dev` command currently needs a manual restart after Python edits.

## Workspace status and diagnostics

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m repairlib status \
  --workspace /tmp/repairlib-demo --json
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m repairlib doctor \
  --workspace /tmp/repairlib-demo
```

`status` reports the workspace path, schema version and expected schema version,
readiness, and the local shop's customer and repair totals, including archived
records. Counts are unavailable when the workspace cannot be read on the current
schema. The demo flag reports the presence of the seed event. Readiness describes
the workspace on disk. Use `/health` to check the running app. Both diagnostics
leave the schema and business data untouched.

Stop all app processes and other workspace maintenance before running `doctor`.
It takes the existing exclusive maintenance lock and checks SQLite integrity and
foreign keys using a private temporary copy of the database and any WAL. Allow
space for that copy in `TMPDIR` (default `/tmp`) outside the workspace.

Doctor compares retained attachments, including detached ones, with their recorded
sizes and SHA-256 hashes. It reports affected attachment IDs and counts orphans,
upload staging, setup staging and `.restore-*` entries in the workspace parent.
The restore staging count covers the whole parent directory. A rollback journal
is reported and database checks are left unchecked until explicit migration.
Doctor also checks inventory across the workspace. It reports cached stock
mismatches, inconsistent movement balances, broken consumption and return links,
and changed charge snapshots. Doctor leaves files, database records and audit
history untouched.

Exit status 0 means the checks passed, or the workspace is missing or uninitialized
with no staging found. Exit status 1 means a problem, pending recovery state, an
upgrade requirement, or an initialized workspace whose checks could not complete.
Use `reconcile-files` separately when you intend to remove unreferenced upload bytes.

## Before an agent writes

Run the read-only check first when an automated agent is about to change a workspace:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m repairlib profile check \
  --workspace /tmp/repairlib-demo
```

It prints, in one place, the workspace path, the schema version against the
expected one, whether a backup was verified in the last 24 hours, the built-in
`cli` identity and its role, and the configured server. It writes nothing, so
the built-in actor appears only after its first real write. Exit status 0 means
writing is possible. A non-zero status names each blocker on stderr: a workspace
that needs `migrate` or `setup`, a person account using the `cli` name, or an
unreachable server.

Set `REPAIRLIB_BASE_URL` (for example `http://127.0.0.1:8000`) when an agent
should also confirm a running server's `/health`; otherwise the server line says
`not configured`.

Three rules hold for every agent session:

- **One writer per tree.** Never run a second agent, CLI command or migration
  against a workspace another writer is changing.
- **No real data.** Use a disposable synthetic workspace outside the source
  tree; never point a test or an agent at a shop's records.
- **No push.** Commit locally inside the authorized scope; do not push, publish
  or deploy.

## Clean up stale uploads

New attachment uploads are refused if writing them would leave less than 1 GiB
free on the workspace database filesystem. If an upload reports insufficient
storage, free space on that filesystem before retrying.

Stop every app process for the workspace, then run:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m repairlib cleanup \
  --workspace /tmp/repairlib-demo
```

Cleanup takes the exclusive maintenance lock and removes app-named upload parts
at least one hour old from `state/files/.staging`, expired browser sessions, and
request receipts older than 90 days from the replay tables. It preserves fresh
parts, links, unrecognized files, the database, original attachments,
configuration, backups, and interrupted setup state. RepairLib has no persistent
workspace cache.
