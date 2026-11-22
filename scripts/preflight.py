#!/usr/bin/env python3
"""Check this host for what RepairLib needs and print copy-paste fixes for its distribution.

Read-only: it installs and changes nothing. It runs on any Python 3, including one too old
for RepairLib itself, so keep this file to Python 3.8 syntax and the standard library.

  python3 scripts/preflight.py                # distribution packages (default)
  python3 scripts/preflight.py --mode venv    # a .venv in the checkout, filled with pip
  python3 scripts/preflight.py --mode podman  # a rootless Podman container
"""

import argparse
import json
import os
from pathlib import Path
import platform
import re
import shutil
import socket
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
MINIMUM_PYTHON = (3, 13)
# Distribution name -> import name. Versions come from requirements.txt.
MODULES = {"fastapi": "fastapi", "jinja2": "jinja2", "uvicorn": "uvicorn", "pydantic": "pydantic",
           "httpx": "httpx", "argon2-cffi": "argon2", "python-multipart": "python_multipart"}
DEBIAN_PACKAGES = {"fastapi": "python3-fastapi", "jinja2": "python3-jinja2", "uvicorn": "python3-uvicorn",
                   "pydantic": "python3-pydantic", "httpx": "python3-httpx", "argon2-cffi": "python3-argon2",
                   "python-multipart": "python3-python-multipart"}
ALPINE_PACKAGES = {"fastapi": "py3-fastapi", "jinja2": "py3-jinja2", "uvicorn": "uvicorn",
                   "pydantic": "py3-pydantic", "httpx": "py3-httpx", "argon2-cffi": "py3-argon2-cffi",
                   "python-multipart": "py3-python-multipart"}
ARCH_PACKAGES = {"fastapi": "python-fastapi", "jinja2": "python-jinja", "uvicorn": "uvicorn",
                 "pydantic": "python-pydantic", "httpx": "python-httpx", "argon2-cffi": "python-argon2-cffi",
                 "python-multipart": "python-python-multipart"}
PROBE = """
import importlib.metadata as m, importlib.util as u, json, sys
out = {}
for dist, module in json.loads(sys.argv[1]).items():
    try:
        out[dist] = m.version(dist) if u.find_spec(module) else None
    except Exception:
        out[dist] = None
print(json.dumps(out))
"""


def requirements(root=ROOT):
    """[(distribution, minimum version)] from requirements.txt."""
    found = []
    for line in (root / "requirements.txt").read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            name, _, minimum = line.partition(">=")
            found.append((name.strip(), minimum.strip()))
    return found


def version_tuple(text):
    return tuple(int(part) for part in re.findall(r"\d+", text)[:3])


def parse_os_release(text):
    values = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values[key.strip()] = value.strip().strip('"')
    family = values.get("ID", "")
    like = values.get("ID_LIKE", "").split()
    if family not in ("debian", "ubuntu", "alpine", "fedora", "arch"):
        family = next((name for name in ("ubuntu", "debian", "fedora", "arch") if name in like), "other")
    return {"family": family, "version": values.get("VERSION_ID", ""),
            "name": values.get("PRETTY_NAME", values.get("NAME", "Unknown Linux"))}


def python_version(executable):
    try:
        out = subprocess.run([executable, "-c", "import sys; print('%d.%d.%d' % sys.version_info[:3])"],
                             capture_output=True, text=True, timeout=20, check=True).stdout.strip()
        return out or None
    except (OSError, subprocess.SubprocessError):
        return None


def installed(executable, wanted):
    """{distribution: version or None} as seen by that interpreter."""
    try:
        out = subprocess.run([executable, "-I", "-c", PROBE, json.dumps({d: MODULES[d] for d in wanted})],
                             capture_output=True, text=True, timeout=60, check=True).stdout
        return json.loads(out)
    except (OSError, subprocess.SubprocessError, ValueError):
        return {dist: None for dist in wanted}


def module_rows(executable, needs):
    rows, missing = [], []
    found = installed(executable, [name for name, _ in needs])
    for name, minimum in needs:
        have = found.get(name)
        if have is None:
            rows.append(("MISS", "%s>=%s is not installed" % (name, minimum)))
            missing.append(name)
        elif version_tuple(have) < version_tuple(minimum):
            rows.append(("MISS", "%s %s is older than %s" % (name, have, minimum)))
            missing.append(name)
        else:
            rows.append(("ok", "%s %s" % (name, have)))
    return rows, missing


def admin(distro):
    if distro["family"] == "alpine" and shutil.which("doas") and not shutil.which("sudo"):
        return "doas"
    return "sudo"


def fix_commands(distro, mode, gaps):
    """Copy-paste commands for this distribution. gaps: set of 'python', 'venv', 'git',
    'podman', 'subuid', 'linger' and 'module:<dist>' entries."""
    family, major = distro["family"], (version_tuple(distro["version"]) or (0,))[0]
    su = admin(distro)
    modules = sorted(gap.split(":", 1)[1] for gap in gaps if gap.startswith("module:"))
    tools = [tool for tool in ("git", "tzdata") if tool in gaps]
    commands = []
    old_python = "python" in gaps
    if mode == "podman":
        if "podman" in gaps or tools:
            pkgs = " ".join(["podman"] + tools)
            commands.append({
                "debian": "%s apt update && %s apt install -y %s" % (su, su, pkgs),
                "ubuntu": "%s apt update && %s apt install -y %s" % (su, su, pkgs),
                "fedora": "%s dnf install -y %s" % (su, pkgs),
                "arch": "%s pacman -Syu --needed --noconfirm %s" % (su, pkgs),
                "alpine": "%s apk add %s && %s rc-update add cgroups && %s rc-service cgroups start" % (su, pkgs, su, su),
            }.get(family, "Install Podman 4.4 or newer with your package manager: https://podman.io/docs/installation"))
        if "subuid" in gaps:
            commands.append('%s usermod --add-subuids 100000-165535 --add-subgids 100000-165535 "$USER"' % su)
    elif mode == "venv":
        if old_python or "venv" in gaps or tools:
            if family == "ubuntu" and old_python:
                commands.append("%s apt install -y software-properties-common && %s add-apt-repository -y ppa:deadsnakes/ppa"
                                % (su, su))
                commands.append("%s apt update && %s apt install -y python3.13 python3.13-venv %s"
                                % (su, su, " ".join(tools)))
            elif family == "debian" and old_python:
                commands.append("# Debian %s has no Python 3.13 package: upgrade to Debian 13, or use "
                                "--mode podman" % (distro["version"] or "before 13"))
            elif family in ("debian", "ubuntu"):
                commands.append("%s apt update && %s apt install -y python3 python3-venv %s" % (su, su, " ".join(tools)))
            elif family == "fedora":
                commands.append("%s dnf install -y python3 %s" % (su, " ".join(tools)))
            elif family == "arch":
                commands.append("%s pacman -Syu --needed --noconfirm python %s" % (su, " ".join(tools)))
            elif family == "alpine":
                commands.append("%s apk add python3 %s" % (su, " ".join(tools)))
            else:
                commands.append("# Install Python 3.13 or newer with the venv module, and git")
        if modules or "venv" in gaps or old_python:
            python = "python3.13" if family == "ubuntu" and old_python else "python3"
            commands.append("cd %s && %s -m venv .venv && .venv/bin/pip install -r requirements.txt"
                            % (shell_path(ROOT), python))
    else:  # system packages
        if family == "debian" and (major >= 13 or not old_python):
            pkgs = (["python3"] if old_python else []) + [DEBIAN_PACKAGES[name] for name in modules] + tools
            if pkgs:
                commands.append("%s apt update && %s apt install -y %s" % (su, su, " ".join(pkgs)))
        elif family == "alpine" and not old_python:
            pkgs = [ALPINE_PACKAGES[name] for name in modules] + tools
            if pkgs:
                commands.append("%s apk add %s" % (su, " ".join(pkgs)))
        elif family == "arch" and not old_python:
            pkgs = [ARCH_PACKAGES[name] for name in modules] + tools
            if pkgs:
                commands.append("%s pacman -Syu --needed --noconfirm %s" % (su, " ".join(pkgs)))
        elif family in ("alpine", "arch") and old_python:
            commands.append("%s apk add python3" % su if family == "alpine" else "%s pacman -Syu --needed --noconfirm python" % su)
        elif old_python or modules:
            reason = {"debian": "Debian %s ships an older Python" % distro["version"],
                      "ubuntu": "Ubuntu's packaged versions differ from the tested Debian 13 set",
                      }.get(family, "this distribution is not tested with system packages")
            commands.append("# %s: use the .venv variant instead:" % reason)
            commands.append("python3 %s --mode venv" % shell_path(ROOT / "scripts" / "preflight.py"))
        elif tools:
            commands.append("# Install with your package manager: " + " ".join(tools))
    if "linger" in gaps:
        commands.append('%s loginctl enable-linger "$USER"' % su)
    return commands


def shell_path(path):
    text = str(path)
    home = str(Path.home())
    if text == home or text.startswith(home + "/"):
        return '"$HOME' + text[len(home):] + '"'
    return "'" + text.replace("'", "'\\''") + "'"


def candidate_pythons():
    seen, found = set(), []
    for name in ("python3.14", "python3.13", "python3"):
        path = shutil.which(name)
        if path and os.path.realpath(path) not in seen:
            seen.add(os.path.realpath(path))
            found.append(path)
    return found


def survey(mode, root=ROOT, port=8000):
    """Rows of (status, text) and the set of gaps for this host."""
    rows, gaps = [], set()
    machine = platform.machine()
    rows.append(("ok" if machine in ("x86_64", "aarch64") else "warn",
                 "architecture %s%s" % (machine, "" if machine in ("x86_64", "aarch64") else " (untested)")))
    needs = requirements(root)
    if mode in ("system", "venv"):
        if mode == "system":
            python = "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else None
            version = python_version(python) if python else None
        else:
            venv_python = root / ".venv" / "bin" / "python"
            pythons = [str(venv_python)] if venv_python.exists() else candidate_pythons()
            usable = [(p, python_version(p)) for p in pythons]
            usable = [(p, v) for p, v in usable if v and version_tuple(v) >= MINIMUM_PYTHON] or usable[:1]
            python, version = usable[0] if usable else (None, None)
        if not version or version_tuple(version) < MINIMUM_PYTHON:
            rows.append(("MISS", "Python 3.13 or newer (%s)" % ("found %s at %s" % (version, python) if version
                                                                else "none found")))
            gaps.add("python")
        else:
            rows.append(("ok", "Python %s at %s" % (version, python)))
            if mode == "venv" and not (root / ".venv" / "bin" / "python").exists():
                has_venv = subprocess.run([python, "-c", "import venv, ensurepip"], capture_output=True).returncode == 0
                rows.append(("ok" if has_venv else "MISS", "venv and ensurepip modules for %s" % python))
                if not has_venv:
                    gaps.add("venv")
                rows.append(("MISS", "no .venv in %s yet" % root))
                gaps.update("module:" + name for name, _ in needs)
            else:
                module_lines, missing = module_rows(python, needs)
                rows.extend(module_lines)
                gaps.update("module:" + name for name in missing)
            zone = subprocess.run([python, "-c", "import zoneinfo; zoneinfo.ZoneInfo('Europe/Athens')"],
                                  capture_output=True).returncode == 0
            rows.append(("ok" if zone else "MISS", "timezone database (tzdata)"))
            if not zone:
                gaps.add("tzdata")
    else:
        podman = shutil.which("podman")
        if not podman:
            rows.append(("MISS", "podman"))
            gaps.add("podman")
        else:
            try:
                text = subprocess.run([podman, "--version"], capture_output=True, text=True, timeout=20).stdout
            except (OSError, subprocess.SubprocessError):
                text = ""
            have = version_tuple(text)
            rows.append(("ok" if have >= (4, 4) else "warn", "podman %s%s" % (
                ".".join(map(str, have)) or "unknown", "" if have >= (4, 4) else " (Quadlet autostart needs 4.4+)")))
        user = os.environ.get("USER") or ""
        try:
            entries = Path("/etc/subuid").read_text()
        except OSError:
            entries = ""
        if user and not re.search(r"^%s:" % re.escape(user), entries, re.M):
            rows.append(("MISS", "subordinate UIDs for %s in /etc/subuid (rootless containers)" % user))
            gaps.add("subuid")
    if shutil.which("git"):
        rows.append(("ok", "git"))
    else:
        rows.append(("MISS", "git"))
        gaps.add("git")
    if shutil.which("systemctl") and shutil.which("loginctl"):
        user = os.environ.get("USER") or ""
        try:
            linger = subprocess.run(["loginctl", "show-user", user, "--property=Linger"],
                                    capture_output=True, text=True, timeout=20).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            linger = ""
        if linger == "Linger=no":
            rows.append(("warn", "systemd lingering is off: a user service stops when you log out"))
            gaps.add("linger")
        elif linger:
            rows.append(("ok", "systemd user services keep running after logout"))
    elif shutil.which("rc-service"):
        rows.append(("ok", "OpenRC: see docs/operations.md for the system service"))
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", port))
            rows.append(("ok", "port %d is free on 127.0.0.1" % port))
        except OSError:
            rows.append(("warn", "port %d is in use; pass --port to setup and service commands" % port))
    return rows, gaps


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=("system", "venv", "podman"), default="system",
                        help="deployment variant to check (default: system packages)")
    parser.add_argument("--port", type=int, default=8000, help="listener port to check (default: 8000)")
    args = parser.parse_args(argv)
    if not sys.platform.startswith("linux"):
        print("RepairLib deployment is supported on Linux; this is %s." % sys.platform)
        return 1
    try:
        distro = parse_os_release(Path("/etc/os-release").read_text())
    except OSError:
        distro = parse_os_release("")
    rows, gaps = survey(args.mode, port=args.port)
    print("RepairLib host check (%s) on %s" % ({"system": "distribution packages", "venv": ".venv with pip",
                                                "podman": "Podman container"}[args.mode], distro["name"]))
    print()
    for status, text in rows:
        print("  %-5s %s" % (status, text))
    blocking = any(status == "MISS" for status, _ in rows)
    commands = fix_commands(distro, args.mode, gaps)
    if commands:
        print("\nFix: copy and paste" if blocking else "\nOptional:")
        for command in commands:
            print("  " + command)
    print("\nThen run this check again." if blocking else "\nReady. Next steps: docs/deployment.md")
    return 1 if blocking else 0


if __name__ == "__main__":
    raise SystemExit(main())
