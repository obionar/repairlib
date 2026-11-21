"""`repairlib links` and the installer's home links: one command, one skill, never a real file."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from repairlib.lifecycle.links import APP_ROOT, LinkError, default_executable, install_links

ROOT = Path(__file__).resolve().parents[1]


class InstallLinksTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.home = self.root / "home"
        self.executable = self.root / "bin" / "repairlib"
        self.executable.parent.mkdir(parents=True)
        self.executable.write_text("#!/bin/sh\nexit 0\n")
        self.executable.chmod(0o755)
        self.skills = self.root / "skills" / "repairlib"
        self.skills.mkdir(parents=True)
        (self.skills / "SKILL.md").write_text("---\nname: repairlib\n---\n")

    def test_links_the_command_and_the_skill(self):
        links = install_links(self.executable, self.skills, self.home)
        command = self.home / ".local/bin/repairlib"
        skill = self.home / ".agents/skills/repairlib"
        self.assertEqual(links["command"]["status"], "created")
        self.assertEqual(links["skill"]["status"], "created")
        self.assertEqual(command.resolve(), self.executable.resolve())
        self.assertEqual(skill.resolve(), self.skills.resolve())
        self.assertEqual((skill / "SKILL.md").read_text(), "---\nname: repairlib\n---\n")

    def test_a_second_run_changes_nothing(self):
        install_links(self.executable, self.skills, self.home)
        again = install_links(self.executable, self.skills, self.home)
        self.assertEqual({link["status"] for link in again.values()}, {"unchanged"})

    def test_a_redirected_symlink_is_replaced(self):
        command = self.home / ".local/bin/repairlib"
        command.parent.mkdir(parents=True)
        command.symlink_to(self.root / "elsewhere")
        links = install_links(self.executable, self.skills, self.home)
        self.assertEqual(links["command"]["status"], "created")
        self.assertEqual(os.readlink(command), str(self.executable.resolve()))

    def test_a_real_file_is_never_replaced(self):
        command = self.home / ".local/bin/repairlib"
        command.parent.mkdir(parents=True)
        command.write_text("#!/bin/sh\necho hand written\n")
        links = install_links(self.executable, self.skills, self.home)
        self.assertEqual(links["command"]["status"], "blocked")
        self.assertEqual(command.read_text(), "#!/bin/sh\necho hand written\n")
        self.assertEqual(links["skill"]["status"], "created")

    def test_a_missing_skill_or_launcher_is_refused(self):
        with self.assertRaises(LinkError):
            install_links(self.executable, self.root / "empty", self.home)
        with self.assertRaises(LinkError):
            install_links(self.root / "missing", self.skills, self.home)
        self.assertFalse((self.home / ".local/bin/repairlib").exists())

    def test_the_default_launcher_is_the_checkout_or_the_stable_launcher(self):
        self.assertEqual(default_executable(ROOT), ROOT / "bin" / "repairlib")
        staged = ROOT / "generation" / "source"
        self.assertEqual(default_executable(staged), ROOT / "repairlib")
        self.assertTrue((APP_ROOT / "skills" / "repairlib" / "SKILL.md").is_file())


class LinksCommandTests(unittest.TestCase):
    def run_command(self, *arguments):
        return subprocess.run(
            [sys.executable, "-m", "repairlib", "links", *arguments],
            capture_output=True, text=True, timeout=60, cwd=ROOT,
            env={**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1",
                 "HOME": str(self.home)},
        )

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.home = self.root / "home"
        self.home.mkdir()

    def test_reports_both_links_and_exits_zero(self):
        done = self.run_command("--executable", str(ROOT / "bin" / "repairlib"))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(".local/bin/repairlib", done.stdout)
        self.assertIn(".agents/skills/repairlib", done.stdout)
        self.assertEqual((self.home / ".local/bin/repairlib").resolve(), (ROOT / "bin/repairlib").resolve())
        self.assertEqual((self.home / ".agents/skills/repairlib").resolve(),
                         (ROOT / "skills/repairlib").resolve())

    def test_a_blocking_file_exits_one_with_a_repair_hint(self):
        command = self.home / ".local/bin/repairlib"
        command.parent.mkdir(parents=True)
        command.write_text("hand written\n")
        done = self.run_command("--executable", str(ROOT / "bin" / "repairlib"))
        self.assertEqual(done.returncode, 1)
        self.assertIn("is not a symlink", done.stderr)
        self.assertEqual(command.read_text(), "hand written\n")


if __name__ == "__main__":
    unittest.main()
