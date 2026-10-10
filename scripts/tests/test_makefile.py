"""Makefile: grouped `make help` output and the hermes-config-apply target.

Offline: `make help` only parses the Makefile and `make -n` prints recipes
without running them, so nothing under HERMES_HOME is touched.
"""
import re
import shutil
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ANSI = re.compile(r"\x1b\[[0-9;]*m")
SECTIONS = ("Setup and Docker stack", "Proxies and auth", "Hermes", "Health and routing",
            "Specialist routing", "Knowledge", "Windows VM", "Tests (offline)")


def make(*args):
    r = subprocess.run(["make", "--no-print-directory", *args], cwd=REPO,
                       capture_output=True, text=True, timeout=30)
    return r.returncode, ANSI.sub("", r.stdout)


def documented_targets():
    names = []
    for line in (REPO / "Makefile").read_text().splitlines():
        m = re.match(r"^([a-zA-Z_-]+(?: [a-zA-Z_-]+)*):.*##", line)
        if m:
            names.extend(m.group(1).split())
    return names


@unittest.skipUnless(shutil.which("make"), "make not installed")
class HelpTest(unittest.TestCase):
    def setUp(self):
        code, self.out = make("help")
        self.assertEqual(code, 0)

    def test_help_is_the_default_goal(self):
        self.assertEqual(make()[1], self.out)

    def test_help_has_every_section_in_order(self):
        positions = [self.out.find(f"\n{s}\n") for s in SECTIONS]
        self.assertNotIn(-1, positions, self.out)
        self.assertEqual(positions, sorted(positions))

    def test_every_documented_target_is_listed_once(self):
        listed = re.findall(r"^  (\S+(?: \S+)*?)\s{2,}\S", self.out, re.M)
        listed = [n for entry in listed for n in entry.split()]
        for target in documented_targets():
            self.assertEqual(listed.count(target), 1, target)

    def test_targets_sit_under_their_section(self):
        def section_of(target):
            current = None
            for line in self.out.splitlines():
                if line in SECTIONS:
                    current = line
                elif line.startswith(f"  {target} "):
                    return current
        self.assertEqual(section_of("hermes-config-apply"), "Hermes")
        self.assertEqual(section_of("hermes-restart"), "Hermes")
        self.assertEqual(section_of("specialist-enable"), "Specialist routing")
        self.assertEqual(section_of("test-router"), "Tests (offline)")
        self.assertEqual(section_of("up"), "Setup and Docker stack")

    def test_config_apply_help_says_no_restart(self):
        line = next(l for l in self.out.splitlines() if l.startswith("  hermes-config-apply "))
        self.assertIn("HERMES_SKIP_GATEWAY=1", line)
        self.assertIn("--force", line)
        self.assertIn("no gateway restart", line)


@unittest.skipUnless(shutil.which("make"), "make not installed")
class ConfigApplyTest(unittest.TestCase):
    def test_recipe_is_exactly_the_skip_gateway_forced_install(self):
        code, out = make("-n", "hermes-config-apply")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "HERMES_SKIP_GATEWAY=1 bash scripts/hermes-install.sh --force")

    def test_recipe_never_restarts_the_gateway(self):
        out = make("-n", "hermes-config-apply")[1]
        self.assertNotIn("restart", out)
        self.assertNotIn("hermes gateway", out)

    def test_existing_hermes_targets_are_unchanged(self):
        self.assertEqual(make("-n", "hermes-install")[1].strip(), "bash scripts/hermes-install.sh")
        self.assertEqual(make("-n", "hermes-restart")[1].strip(), "hermes gateway restart")


if __name__ == "__main__":
    unittest.main()
