# Operations

[RepairLib README](../README.md) · [Deployment](deployment.md) · [Data and recovery](data.md)

Commands run from the checkout root. `bin/repairlib` uses the `data/` workspace unless
`--workspace` or `REPAIRLIB_WORKSPACE` says otherwise.

## Initialize a workspace in the browser

```sh
repairlib_workspace="$(mktemp -d)"
bin/repairlib dev --workspace "$repairlib_workspace" --browser-setup
```

Open the one-time link it prints within 15 minutes and enter the shop name, IANA timezone and
first admin. Then stop the server (Ctrl+C), start it again without `--browser-setup`, and sign
in at <http://127.0.0.1:8000>. An expired link needs a restart with `--browser-setup`.

## Initialize a workspace from the terminal

```sh
bin/repairlib setup --workspace "$repairlib_workspace" --shop "Demo Shop" \
  --timezone Europe/Nicosia --username owner
```

The admin password comes from a hidden prompt or one line on stdin. The database is
`state/repairlib.sqlite3` inside the workspace. If setup was interrupted, run it again.

## Initialize an isolated synthetic demo

Three fictional customers, in a new workspace only (it refuses an existing one):

```sh
bin/repairlib demo --workspace /tmp/repairlib-synthetic-demo --username owner
```

`demo reset --confirm` reseeds an untouched demo while every app process is stopped. Accounts,
settings and audit history stay.

A full workshop for testing (20 customers, 50 repairs at every stage, a parts catalog):

```sh
demo="$(mktemp -d)/workspace"
printf 'demo\n' | bin/repairlib setup --workspace "$demo" \
  --shop "The Mythical Fix-It Emporium" --username demo --timezone Europe/Nicosia
python3 scripts/seed_demo.py --workspace "$demo" --dataset scripts/demo-mythical.json
bin/repairlib dev --workspace "$demo" --port 8010
```

Sign in as `demo`/`demo` (also `technician`/`demo-technician-passphrase`). These passwords are
public: throwaway demos only. Without `--dataset` the seeder uses the smaller
`scripts/demo-seed.json`. It refuses a workspace with records or a running server. Container
version: [Podman demo](deployment.md#try-the-demo-in-podman).

## Linux service lifecycle

One service per Linux user: a systemd user unit, or on Alpine an OpenRC system service.
Use the same workspace, port and flags (`--port`, `--lan`, `--openrc-system`) on every command.

```sh
bin/repairlib service install      # register only
bin/repairlib service start
bin/repairlib service status       # JSON; "running" means process, listener and /health all ready
bin/repairlib service restart      # also: stop, remove
bin/repairlib service foreground   # no service manager; Ctrl+C stops it
```

- systemd: the unit lives in `~/.config/systemd/user`; logs with `journalctl --user -u repairlib`.
- Startup needs a current schema. For an older workspace, follow
  [explicit migration](data.md#upgrade-a-development-workspace).
- `remove` stops the service and deletes only its own unit or script. Workspace data stays.
- After Python edits on a test instance, restart it before checking the UI.

### OpenRC system service on Alpine

```sh
bin/repairlib service install --openrc-system --port 8000
bin/repairlib service start --openrc-system --port 8000
bin/repairlib service status --openrc-system --port 8000
```

It writes `/etc/init.d/repairlib`, adds it to the `default` runlevel and runs the app as your
user. Registration needs passwordless `doas` or `sudo`; RepairLib never prompts for or stores
credentials. Do not give a restricted shop account passwordless `install`, `ln` or `rm`.
Logs go to syslog: `grep repairlib /var/log/messages`.

## Update a checkout

Run as the service owner, not root:

```sh
bin/repairlib update --check   # compare with the branch on origin
bin/repairlib update           # update checkout, workspace and registered service
```

Needs a regular Git checkout, exactly one RepairLib service pointing at it, and a compatible
workspace. It refuses dirty or diverged checkouts and does nothing without an incoming
fast-forward. With one, it:

1. stops the service and runs `doctor` under the exclusive maintenance lock;
2. writes and verifies a full archive in `.repairlib-recovery-*` beside the checkout, restores it
   to a throwaway workspace and tests migration and `doctor` there;
3. fast-forwards to that exact commit, migrates the live workspace if needed, runs `doctor`;
4. restarts the service if it was running and checks `/health`.

Plan downtime and disk space for the archive, a restored copy and a temporary checkout. Run no
other Git, SQLite or service commands meanwhile. After success it keeps only this archive and the previous one.

### Recover an interrupted checkout update

A failed update leaves `.git/repairlib-update-pending.json` (old and new commit, workspace,
backend, stage, archive path). It blocks further updates and managed startup.

1. Stop all writers. Compare `git rev-parse HEAD` and `git status` with the record.
2. Verify the archive: `bin/repairlib backup verify /path/to/backup.zip`. If you need it,
   [restore into a new workspace](data.md#restore-a-full-workspace-backup). Nothing restores over
   an existing workspace.
3. Run `bin/repairlib doctor` with the code you intend to run. If it asks only for migration,
   find the cause first, then migrate with the verified archive. Never run old code on a
   migrated workspace.
4. When HEAD and workspace are good, move the record into the recovery directory and start the
   service with the workspace, port and backend from the record.

## Linux install

`scripts/install.py` makes a versioned installation outside the checkout, with staged updates
and recovery. It uses only `/usr/bin/python3` 3.13+ and distribution packages (it refuses a
venv), downloads nothing, and runs the [host check](deployment.md) first.

```sh
repairlib_source="$HOME/repairlib-source"
repairlib_target="$HOME/.local/opt/repairlib"
git clone https://github.com/obionar/repairlib.git "$repairlib_source"
python3 -I -B "$repairlib_source/scripts/install.py" --commit HEAD --target "$repairlib_target"
"$repairlib_target/repairlib" setup --shop "My Shop" --username owner
"$repairlib_target/repairlib" service foreground --port 8000
```

The workspace is `~/.local/state/repairlib`. The installer also links `~/.local/bin/repairlib`
and the agent skill (`--no-link` skips that). Keep the target at its path; never `git pull`
inside it. After an interrupted install, use a fresh target path.

### Upgrade an installation

Stop the listener. On OpenRC, `service remove --openrc-system` first: the installer refuses to
stage while `/etc/init.d/repairlib` exists.

```sh
git -C "$repairlib_source" fetch origin
git -C "$repairlib_source" checkout --detach origin/main
repairlib_head="$(git -C "$repairlib_source" rev-parse HEAD)"
python3 -I -B "$repairlib_source/scripts/install.py" --check-update --target "$repairlib_target" --commit "$repairlib_head"
python3 -I -B "$repairlib_source/scripts/install.py" --stage-update --commit "$repairlib_head" \
  --target "$repairlib_target" --workspace "$HOME/.local/state/repairlib" \
  --candidate "$repairlib_target-next"
python3 -I -B "$repairlib_source/scripts/install.py" --activate-update \
  --target "$repairlib_target" --candidate "$repairlib_target-next"
"$repairlib_target/repairlib" service foreground --port 8000 &
python3 -I -B "$repairlib_source/scripts/install.py" --reopen-update \
  --target "$repairlib_target" --candidate "$repairlib_target-next" --port 8000
"$repairlib_target/repairlib" status --json
```

- `--check-update` is read-only: a blocker exits 1.
- Staging refuses equal, older or non-descendant commits. It backs up, rehearses a restore,
  migrates and health-checks the candidate. Keep `stage.json`, `backup.zip` and the candidate
  where they are.
- After activation the listener serves only `/health` (everything else 503) until
  `--reopen-update` opens writes.
- With a systemd user unit, add `--managed --port <port>` to `--activate-update`,
  `--reopen-update` and `--recover-update`; the installer then controls the unit.
- Recovery, with the listener stopped:
  `python3 -I -B scripts/install.py --recover-update --target "$repairlib_target" --candidate "$repairlib_target-next"`.
  Before writes open it can return to the original; after, it verifies the candidate in place.
  It never copies old files over the candidate.

## Workspace status and diagnostics

```sh
bin/repairlib status --json     # path, schema, readiness, record counts
bin/repairlib doctor            # app stopped: schema, SQLite integrity and FKs, stock ledger, files
bin/repairlib cleanup           # app stopped: old upload parts, expired sessions, receipts > 90 days
bin/repairlib profile check     # read-only pre-write check for agents
```

- `doctor` copies the database to `TMPDIR` for its checks and changes nothing. Exit 0 means
  passed (or missing/uninitialized with no staging left); 1 means a problem or pending recovery.
- `status` reads the disk; use `/health` for the running app.
- Uploads are refused below 1 GiB free on the workspace disk.
- `reconcile-files` removes unreferenced upload bytes; run it only when you mean to.
- Logs go to the system logger; nothing is written inside the workspace.
