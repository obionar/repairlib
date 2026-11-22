"""scripts/preflight.py: host checks with copy-paste fixes, and the launcher's .venv switch."""

import ast
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("preflight", ROOT / "scripts" / "preflight.py")
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)

DEBIAN_13 = preflight.parse_os_release('ID=debian\nVERSION_ID="13"\nPRETTY_NAME="Debian GNU/Linux 13 (trixie)"')
DEBIAN_12 = preflight.parse_os_release('ID=debian\nVERSION_ID="12"')
ARCH = preflight.parse_os_release("ID=arch\nPRETTY_NAME=\"Arch Linux\"")
ALPINE = preflight.parse_os_release('ID=alpine\nVERSION_ID=3.24.1')
UBUNTU_24 = preflight.parse_os_release('ID=ubuntu\nID_LIKE=debian\nVERSION_ID="24.04"')
ALL_MODULES = {"module:" + name for name in preflight.MODULES}


class PreflightTests(unittest.TestCase):
    def test_requirements_match_the_project_dependencies(self):
        declared = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["dependencies"]
        self.assertEqual([f"{name}>={minimum}" for name, minimum in preflight.requirements()], declared)
        self.assertEqual({name for name, _ in preflight.requirements()}, set(preflight.MODULES))

    def test_the_checker_parses_on_old_pythons(self):
        # It must explain "Python too old" on Debian 12 or Ubuntu 22.04 instead of failing to parse.
        ast.parse((ROOT / "scripts" / "preflight.py").read_text(), feature_version=(3, 8))

    def test_distribution_detection_follows_id_like(self):
        self.assertEqual((DEBIAN_13["family"], DEBIAN_13["version"]), ("debian", "13"))
        self.assertEqual(preflight.parse_os_release("ID=raspbian\nID_LIKE=debian")["family"], "debian")
        self.assertEqual(preflight.parse_os_release("ID=manjaro\nID_LIKE=arch")["family"], "arch")
        self.assertEqual(preflight.parse_os_release("ID=void")["family"], "other")

    def test_system_packages_on_the_three_tested_distributions(self):
        gaps = ALL_MODULES | {"git", "tzdata"}
        debian, = preflight.fix_commands(DEBIAN_13, "system", gaps)
        self.assertTrue(debian.startswith("sudo apt update && sudo apt install -y "))
        for package in ("python3-fastapi", "python3-argon2", "python3-python-multipart", "git", "tzdata"):
            self.assertIn(package, debian.split())
        arch, = preflight.fix_commands(ARCH, "system", gaps)
        self.assertIn("pacman -Syu --needed --noconfirm", arch)
        for package in ("python-fastapi", "python-jinja", "uvicorn", "python-argon2-cffi"):
            self.assertIn(package, arch.split())
        alpine, = preflight.fix_commands(ALPINE, "system", gaps)
        self.assertIn("apk add", alpine)
        for package in ("py3-fastapi", "uvicorn", "py3-argon2-cffi", "tzdata"):
            self.assertIn(package, alpine.split())

    def test_only_the_gaps_are_installed(self):
        command, = preflight.fix_commands(DEBIAN_13, "system", {"module:httpx"})
        self.assertTrue(command.endswith("apt install -y python3-httpx"))
        self.assertEqual(preflight.fix_commands(DEBIAN_13, "system", set()), [])

    def test_an_old_python_points_to_another_variant(self):
        debian_12 = preflight.fix_commands(DEBIAN_12, "system", {"python"})
        self.assertIn("--mode venv", debian_12[-1])
        self.assertIn("upgrade to Debian 13", preflight.fix_commands(DEBIAN_12, "venv", {"python"})[0])
        ubuntu = preflight.fix_commands(UBUNTU_24, "venv", {"python", "git"})
        self.assertIn("ppa:deadsnakes/ppa", ubuntu[0])
        self.assertIn("python3.13-venv git", ubuntu[1])
        self.assertIn("python3.13 -m venv .venv && .venv/bin/pip install -r requirements.txt", ubuntu[2])

    def test_venv_and_podman_fixes(self):
        venv = preflight.fix_commands(ARCH, "venv", {"venv", "git"} | ALL_MODULES)
        self.assertEqual(venv[0], "sudo pacman -Syu --needed --noconfirm python git")
        self.assertIn("python3 -m venv .venv", venv[1])
        podman = preflight.fix_commands(DEBIAN_13, "podman", {"podman", "subuid", "linger"})
        self.assertEqual(podman[0], "sudo apt update && sudo apt install -y podman")
        self.assertIn("usermod --add-subuids", podman[1])
        self.assertEqual(podman[2], 'sudo loginctl enable-linger "$USER"')
        self.assertIn("rc-update add cgroups", preflight.fix_commands(ALPINE, "podman", {"podman"})[0])

    def test_version_comparison_uses_numbers(self):
        self.assertLess(preflight.version_tuple("0.0.9"), preflight.version_tuple("0.0.20"))
        self.assertGreaterEqual(preflight.version_tuple("0.115.11"), preflight.version_tuple("0.115"))


class LauncherTests(unittest.TestCase):
    def checkout(self, base: Path, main: str = "def main(argv):\n    print('ran', argv)\n    return 0\n") -> Path:
        (base / "bin").mkdir()
        (base / "bin" / "repairlib").write_bytes((ROOT / "bin" / "repairlib").read_bytes())
        package = base / "src" / "repairlib"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("")
        (package / "__main__.py").write_text(main)
        return base / "bin" / "repairlib"

    def run_launcher(self, launcher: Path, **env):
        return subprocess.run([sys.executable, str(launcher), "--help"], capture_output=True, text=True,
                              env={**os.environ, "REPAIRLIB_SYSTEM_PYTHON": "", **env}, timeout=30)

    def test_a_checkout_venv_runs_the_launcher(self):
        with tempfile.TemporaryDirectory() as tmp:
            launcher = self.checkout(Path(tmp))
            fake = Path(tmp) / ".venv" / "bin" / "python"
            fake.parent.mkdir(parents=True)
            fake.write_text("#!/bin/sh\necho venv-python \"$@\"\n")
            fake.chmod(0o755)
            done = self.run_launcher(launcher)
            self.assertEqual(done.stdout.split(), ["venv-python", str(launcher), "--help"])
            self.assertIn("ran ['--help']", self.run_launcher(launcher, REPAIRLIB_SYSTEM_PYTHON="1").stdout)

    def test_a_missing_dependency_points_to_the_checker(self):
        with tempfile.TemporaryDirectory() as tmp:
            launcher = self.checkout(Path(tmp), "import fastapi_not_installed_anywhere\n")
            done = self.run_launcher(launcher)
            self.assertNotEqual(done.returncode, 0)
            self.assertIn("'fastapi_not_installed_anywhere' is missing", done.stderr)
            self.assertIn("scripts/preflight.py", done.stderr)


if __name__ == "__main__":
    unittest.main()
