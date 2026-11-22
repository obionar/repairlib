# Deployment

RepairLib runs one workshop from one Linux machine. Pick one of three variants. All of them
use the same checkout and the same commands afterwards.

| Variant | Use it when | Tested on |
|---|---|---|
| [System packages](#1-system-packages) | Your distribution packages Python 3.13+ and the libraries. Fewest moving parts; security updates come with the system. | Debian 13, Arch, Alpine 3.24 |
| [.venv with pip](#2-venv-with-pip) | The packaged libraries are missing or too old, or you want the app isolated from system Python. | Debian 13, Arch, Alpine 3.24 |
| [Podman container](#3-podman-container) | The host Python is too old (Debian 12, Ubuntu 24.04), or you want the app sealed off from the host. | Arch host (rootless, Quadlet) |

Each variant has a host check that lists everything missing in one go and prints the commands to
fix it for your distribution, ready to copy and paste. The check is read-only and runs on any
Python 3, even one too old for RepairLib:

```sh
python3 scripts/preflight.py                # system packages
python3 scripts/preflight.py --mode venv    # .venv with pip
python3 scripts/preflight.py --mode podman  # Podman container
```

It checks the CPU architecture, Python 3.13 or newer, each library and its minimum version (from
[`requirements.txt`](../requirements.txt)), the timezone database, `git`, whether systemd keeps
user services running after logout, and whether the port is free (`--port N`, default 8000). It
exits with 0 when the host is ready. `bin/repairlib` and `scripts/install.py` point to it when
something is missing.

## First steps on a fresh machine

Use an ordinary user account, not root. Install `git` and `python3`, clone the repository and run
the check:

```sh
# Debian 13 / Ubuntu
sudo apt update && sudo apt install -y git python3
# Arch
sudo pacman -Syu --needed git python
# Alpine 3.24 (doas or sudo)
doas apk add git python3

git clone https://github.com/obionar/repairlib.git "$HOME/repairlib"
cd "$HOME/repairlib"
python3 scripts/preflight.py
```

Run the commands it prints, then run the check again until it says `Ready`.

## 1. System packages

The default and the most tested setup: the stock `/usr/bin/python3` and the distribution's own
Python packages, with no pip. If you install by hand instead of running the check:

```sh
# Debian 13 (trixie)
sudo apt install -y python3 python3-fastapi python3-jinja2 python3-uvicorn python3-pydantic \
  python3-httpx python3-argon2 python3-python-multipart tzdata git
# Arch
sudo pacman -Syu --needed python python-fastapi python-jinja uvicorn python-pydantic \
  python-httpx python-argon2-cffi python-python-multipart tzdata git
# Alpine 3.24
doas apk add python3 py3-fastapi py3-jinja2 uvicorn py3-pydantic py3-httpx \
  py3-argon2-cffi py3-python-multipart tzdata git
```

Alpine's base image ships without `tzdata`; without it, setup refuses every timezone.

Debian 12 and Ubuntu 24.04 ship Python older than 3.13. Ubuntu packages are not tested. On those
systems, use the [.venv variant](#2-venv-with-pip) with a newer Python, or the
[container](#3-podman-container). The check says which applies.

Then [start the workshop](#start-the-workshop).

## 2. .venv with pip

Needs only Python 3.13+ with its `venv` module. pip downloads the libraries into `.venv/` inside
the checkout, and nothing else on the system changes.

```sh
# Debian 13: venv is a separate package
sudo apt install -y python3 python3-venv tzdata git
# Arch
sudo pacman -Syu --needed python tzdata git
# Alpine 3.24
doas apk add python3 tzdata git

cd "$HOME/repairlib"
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
python3 scripts/preflight.py --mode venv
```

`bin/repairlib` switches to `.venv/bin/python` whenever a checkout `.venv` exists, so you never
activate it. `service install` records the `.venv` interpreter in the unit. Set
`REPAIRLIB_SYSTEM_PYTHON=1` to force the system Python for one command.

**Ubuntu 22.04/24.04:** take Python 3.13 from the deadsnakes PPA, then create the venv with it:

```sh
sudo apt install -y software-properties-common
sudo add-apt-repository -y ppa:deadsnakes/ppa
sudo apt update && sudo apt install -y python3.13 python3.13-venv tzdata git
python3.13 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

**Updating libraries:** `.venv/bin/pip install --upgrade -r requirements.txt`, then
`bin/repairlib service restart`. After a system Python upgrade (for example 3.13 to 3.14), delete
`.venv` and create it again. A venv is tied to the Python it was made with.

**Why not pipx:** pipx installs a packaged app into its own hidden venv. RepairLib runs from its
checkout. Its templates, migrations and `bin/repairlib update` live there, and the workspace sits
next to them in `data/`. A checkout `.venv` gives the same isolation and keeps `update` working.

The [versioned installer](operations.md#linux-install) (`scripts/install.py`) supports only
system packages and refuses a venv interpreter. With a venv, run the checkout as shown here.

## Start the workshop

For both variants above, from the checkout:

```sh
bin/repairlib setup --shop "My Shop" --username owner   # asks for the admin password
bin/repairlib service install && bin/repairlib service start
```

Open <http://127.0.0.1:8000> and sign in as `owner`.

- **systemd (Debian, Arch):** the service is a user unit. Logs: `journalctl --user -u repairlib`.
  Turn on lingering so it keeps running after you log out and starts at boot:
  `sudo loginctl enable-linger "$USER"`.
- **OpenRC (Alpine):** use `bin/repairlib service install --openrc-system` (root-supervised,
  runs as your user). See [OpenRC system service](operations.md#openrc-system-service-on-alpine).
- **Different port:** add `--port 8010` to `setup`, `service install` and `preflight.py`.
- **Command from anywhere:** `bin/repairlib links` puts `repairlib` in `~/.local/bin`.

## 3. Podman container

The image is Debian 13 with the distribution's Python packages and this checkout's `src/`. Shop
records stay on the host in a directory mounted at `/data`. The container runs rootless as your
own user (`--userns=keep-id`), so the files on the host belong to you and backups need no `sudo`.

### Install Podman

```sh
# Debian 13 / Ubuntu
sudo apt update && sudo apt install -y podman git
# Arch
sudo pacman -Syu --needed podman git
# Alpine 3.24: rootless Podman needs the cgroups service
doas apk add podman git
doas rc-update add cgroups && doas rc-service cgroups start
```

Rootless containers need subordinate user IDs. Most installers add them. If
`python3 scripts/preflight.py --mode podman` reports them missing:

```sh
sudo usermod --add-subuids 100000-165535 --add-subgids 100000-165535 "$USER"
podman system migrate
```

### Build and set up

```sh
cd "$HOME/repairlib"
podman build -t localhost/repairlib .
mkdir -p "$HOME/repairlib-data" "$HOME/repairlib-backups"
chmod 700 "$HOME/repairlib-data" "$HOME/repairlib-backups"
podman run --rm -it --userns=keep-id -v "$HOME/repairlib-data:/data:Z" \
  localhost/repairlib setup --workspace /data --shop "My Shop" --username owner --timezone Europe/Athens
```

Pass `--timezone` explicitly. The container cannot read the host's timezone.

### Run

RepairLib listens on `127.0.0.1` only. Host networking makes that the host's loopback:

```sh
podman run -d --name repairlib --network host --userns=keep-id \
  -v "$HOME/repairlib-data:/data:Z" localhost/repairlib
curl -s http://127.0.0.1:8000/health
```

Use a different port with `... localhost/repairlib service foreground --workspace /data --port 8010`.

Run CLI commands inside the running container:

```sh
podman exec -it repairlib python3 /app/bin/repairlib status
podman exec -it repairlib python3 /app/bin/repairlib repairs list
```

### Start at boot (systemd hosts: Quadlet, Podman 4.4+)

Stop the manual container first (`podman rm -f repairlib`), then save this as
`~/.config/containers/systemd/repairlib.container`:

```ini
[Container]
Image=localhost/repairlib
ContainerName=repairlib
Network=host
UserNS=keep-id
Volume=%h/repairlib-data:/data:Z

[Service]
Restart=on-failure

[Install]
WantedBy=default.target
```

```sh
systemctl --user daemon-reload
systemctl --user start repairlib.service
sudo loginctl enable-linger "$USER"
journalctl --user -u repairlib
```

Alpine has no Quadlet. Add `--restart unless-stopped` to `podman run` and start the container
from `/etc/local.d/repairlib.start` (`su -l youruser -c 'podman start repairlib'`, then
`doas rc-update add local`). This Alpine path has not been tested.

### Back up and update

The backup directory must be private (`chmod 700`, as above):

```sh
podman run --rm --userns=keep-id -v "$HOME/repairlib-data:/data:Z" \
  -v "$HOME/repairlib-backups:/backups:Z" localhost/repairlib \
  backup create --workspace /data --output "/backups/repairlib-$(date +%F).zip"
```

To update, take a backup, then:

```sh
cd "$HOME/repairlib" && git pull --ff-only
podman build -t localhost/repairlib .
systemctl --user restart repairlib.service   # or: podman rm -f repairlib, then the run command
```

## Tips

- **One machine, one workspace.** Keep `data/` (or `repairlib-data/`) on local disk, not a network
  share. SQLite needs real file locking.
- **Back up off the machine.** `bin/repairlib backup create --output <path outside data/>` and
  copy the archive elsewhere. Rehearse a restore before you depend on it:
  [data and recovery](data.md).
- **Other devices** (a phone at the bench): the app listens only on loopback. Reaching it from
  the LAN needs a reverse proxy or SSH tunnel you configure yourself. This is not covered here.
- **Checks after any change:** `bin/repairlib doctor` and `bin/repairlib status`.
- **Raspberry Pi and other ARM boards:** `aarch64` works with any variant. Use a 64-bit OS.
