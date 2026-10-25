"""Makefile: grouped `make help` output and the hermes-config-apply target.

Offline: `make help` only parses the Makefile and `make -n` prints recipes
without running them, so nothing under HERMES_HOME is touched. Color is checked
on a pseudo-terminal (pty) and with COLOR/NO_COLOR; captured output is plain.
"""
import os
import pty
import re
import shutil
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ANSI = re.compile(r"\x1b\[[0-9;]*m")
SECTIONS = ("Setup and Docker stack", "Proxies and auth", "Hermes", "Health and routing",
            "Specialist routing", "Knowledge", "Windows VM", "Tests (offline)")


HELP_ENV = {k: v for k, v in os.environ.items()
            if k not in ("COLOR", "NO_COLOR", "HELP_WIDTH", "COLUMNS", "MAKEFLAGS", "MFLAGS")}


def make(*args, raw=False, **env):
    r = subprocess.run(["make", "--no-print-directory", *args], cwd=REPO, env=dict(HELP_ENV, **env),
                       capture_output=True, text=True, timeout=30)
    return r.returncode, r.stdout if raw else ANSI.sub("", r.stdout)


def make_on_tty(*args, **env):
    """`make` with stdout on a pseudo-terminal, as when an operator types it."""
    master, slave = pty.openpty()
    try:
        p = subprocess.Popen(["make", "--no-print-directory", *args], cwd=REPO,
                             env={**HELP_ENV, "TERM": "xterm", **env}, stdout=slave,
                             stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
        os.close(slave)
        chunks = []
        while True:
            try:
                data = os.read(master, 65536)
            except OSError:                       # EIO: the child closed the terminal
                break
            if not data:
                break
            chunks.append(data)
        p.wait(timeout=30)
        return p.returncode, b"".join(chunks).decode("utf-8", "replace").replace("\r\n", "\n")
    finally:
        os.close(master)


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
        listed = re.findall(r"^  (\S+(?: \S+)*?) {2,}\S", self.out, re.M)
        listed = [n for entry in listed for n in entry.split()]
        for target in documented_targets():
            self.assertEqual(listed.count(target), 1, target)

    def section_of(self, target):
        current = None
        for line in self.out.splitlines():
            if line in SECTIONS:
                current = line
            elif line.startswith(f"  {target} "):
                return current

    def test_targets_sit_under_their_section(self):
        section_of = self.section_of
        self.assertEqual(section_of("hermes-config-apply"), "Hermes")
        self.assertEqual(section_of("hermes-restart"), "Hermes")
        self.assertEqual(section_of("specialist-enable"), "Specialist routing")
        self.assertEqual(section_of("test-router"), "Tests (offline)")
        self.assertEqual(section_of("up"), "Setup and Docker stack")

    def column(self):
        """The one column every target description starts at."""
        cols = {m.start(2) - m.start(0) for m in re.finditer(r"^  (\S+(?: \S+)*?) {2,}(\S)", self.out, re.M)}
        self.assertEqual(len(cols), 1, cols)
        return cols.pop()

    def block(self, target):
        """(summary lines, `##|` detail lines) of a target: the summary wraps at the
        description column, details are indented two more spaces."""
        lines = self.out.splitlines()
        i = next(n for n, l in enumerate(lines) if l.startswith(f"  {target} "))
        col = self.column()
        summary, details = [lines[i][col:]], []
        for l in lines[i + 1:]:
            if l.startswith(" " * (col + 2)) and l[col + 2] != " ":
                details.append(l.strip())
            elif l.startswith(" " * col) and l[col] != " " and not details:
                summary.append(l.strip())
            else:
                break
        return summary, details

    def details(self, target):
        return self.block(target)[1]

    def entry(self, target):
        summary, details = self.block(target)
        return " ".join(summary + details)

    def test_descriptions_are_aligned_and_wrapped_to_the_width(self):
        col = self.column()
        self.assertEqual(col, 2 + max(len(t) for t in documented_targets()) + 2)
        for width in ("100", "72"):
            with self.subTest(width=width):
                out = make("help", HELP_WIDTH=width)[1]
                long = [l for l in out.splitlines() if len(l) > int(width) and " " in l.strip()]
                self.assertEqual(long, [])
        self.assertEqual(make("help", HELP_WIDTH="100")[1], self.out)   # 100 columns when not a terminal

    def test_wrapping_keeps_every_word_of_a_summary(self):
        line = next(l for l in (REPO / "Makefile").read_text().splitlines()
                    if l.startswith("knowledge-classify:"))
        self.assertEqual(" ".join(self.block("knowledge-classify")[0]), " ".join(line.split("## ", 1)[1].split()))
        self.assertGreater(len(self.block("knowledge-classify")[0]), 1)

    def test_section_notes_sit_under_the_title(self):
        lines = self.out.splitlines()
        i = lines.index("Knowledge")
        self.assertTrue(lines[i + 1].startswith("  Flow: inventory"), lines[i + 1])
        notes = " ".join(l.strip() for l in lines[i + 1:i + 14])
        self.assertIn("one canonical profile per book", notes)
        for step in ("inventory", "classify", "edit/review the plan", "knowledge-apply PLAN=<plan> APPROVE=1",
                     "knowledge-learn-status"):
            self.assertIn(step, notes.split("Legacy flow")[0], step)

    def test_apply_help_names_the_single_command_and_its_guards(self):
        text = self.entry("knowledge-apply")
        for phrase in ("PLAN=", "APPROVE=1", "DRY_RUN=1", "validate", "canonical", "refs/", "learn",
                       "resume", "never migrated"):
            self.assertIn(phrase, text, phrase)
        self.assertIn("test-knowledge-apply", self.out)
        code, out = make("-n", "knowledge-apply", "PLAN=/tmp/x.plan.json", "APPROVE=1", "DRY_RUN=1")
        self.assertEqual(code, 0, out)
        self.assertIn("knowledge-apply.py", out)
        code, out = make("knowledge-apply")            # no PLAN: usage only, nothing runs
        self.assertEqual(code, 2, out)
        self.assertIn("usage: make knowledge-apply PLAN=", out)

    def test_helper_script_runs_without_make(self):
        r = subprocess.run(["python3", str(REPO / "scripts" / "make-help.py"), str(REPO / "Makefile")],
                           capture_output=True, text=True, timeout=30, env=dict(HELP_ENV, HELP_WIDTH="100"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, self.out)

    def test_every_knowledge_target_has_a_plain_language_explanation(self):
        for target in ("knowledge-inventory", "knowledge-convert", "knowledge-stage",
                       "knowledge-classify", "knowledge-apply", "knowledge-legacy-report",
                       "knowledge-refresh", "knowledge-reconcile",
                       "knowledge-learn", "knowledge-learn-status", "hermes-profile"):
            with self.subTest(target=target):
                self.assertEqual(self.section_of(target), "Knowledge")
                self.assertGreaterEqual(len(self.details(target)), 2, self.out)

    def test_convert_help_says_src_dest_required_and_not_all_of_mnt_books(self):
        text = self.entry("knowledge-convert")
        self.assertIn("SRC and DEST are both required", text)
        self.assertIn("EPUB/PDF files under SRC only", text)
        self.assertIn("does NOT find and convert every book under /mnt/books", text)

    def test_stage_and_refresh_help_say_listed_books_convert_by_default(self):
        self.assertIn("converted to Markdown automatically by default", self.entry("knowledge-stage"))
        self.assertIn("KNOWLEDGE_CONVERT=1", self.entry("knowledge-stage"))
        self.assertIn("automatic EPUB/PDF conversion of listed books", self.entry("knowledge-refresh"))

    def test_config_apply_help_says_no_restart(self):
        text = self.entry("hermes-config-apply")
        self.assertIn("HERMES_SKIP_GATEWAY=1", text)
        self.assertIn("--force", text)
        self.assertIn("no gateway restart", text)

    def test_ownership_policy_is_in_the_knowledge_help(self):
        self.assertIn("one canonical profile per book", self.entry("knowledge-classify"))
        self.assertIn("SECONDARY=", self.entry("knowledge-classify"))
        self.assertIn("SECONDARY=", self.entry("knowledge-reconcile"))


@unittest.skipUnless(shutil.which("make"), "make not installed")
class HelpColorTest(unittest.TestCase):
    def test_captured_output_is_plain(self):
        code, raw = make("help", raw=True)
        self.assertEqual(code, 0)
        self.assertNotIn("\x1b", raw)

    def test_terminal_output_is_colored(self):
        code, out = make_on_tty("help")
        self.assertEqual(code, 0, out)
        self.assertRegex(out, r"\x1b\[[0-9;]*m")
        self.assertIn("\nKnowledge", ANSI.sub("", out))

    def test_color_1_forces_color_without_a_terminal(self):
        for args, env in (((), {"COLOR": "1"}), (("COLOR=1",), {})):
            with self.subTest(args=args, env=env):
                code, raw = make("help", *args, raw=True, **env)
                self.assertEqual(code, 0)
                self.assertRegex(raw, r"\x1b\[[0-9;]*m")
                self.assertEqual(ANSI.sub("", raw), make("help")[1])   # same layout, only colored

    def test_no_color_and_color_0_turn_it_off_on_a_terminal(self):
        for args, env in (((), {"NO_COLOR": "1"}), ((), {"COLOR": "0"}), (("COLOR=0",), {}),
                          ((), {"TERM": "dumb"})):
            with self.subTest(args=args, env=env):
                code, out = make_on_tty("help", *args, **env)
                self.assertEqual(code, 0, out)
                self.assertNotIn("\x1b", out)


@unittest.skipUnless(shutil.which("make"), "make not installed")
class ConfigApplyTest(unittest.TestCase):
    def test_recipe_is_exactly_the_skip_gateway_forced_install(self):
        code, out = make("-n", "hermes-config-apply")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "HERMES_SKIP_GATEWAY=1 bash scripts/hermes-install.sh --force")

    def test_knowledge_recipes_are_unchanged(self):
        self.assertEqual(make("-n", "knowledge-convert", "SRC=s", "DEST=d")[1].splitlines()[-1].strip(),
                         'python3 hermes/skills/document-converter/scripts/doc2md.py --source "s" --dest "d"')
        self.assertEqual(make("-n", "knowledge-stage", "NAME=p")[1].splitlines()[-1],
                         'bash scripts/knowledge-stage.sh "p"')
        self.assertEqual(make("-n", "hermes-profile", "NAME=p")[1].splitlines()[-1],
                         'bash scripts/hermes-profile.sh "p"')

    def test_recipe_never_restarts_the_gateway(self):
        out = make("-n", "hermes-config-apply")[1]
        self.assertNotIn("restart", out)
        self.assertNotIn("hermes gateway", out)

    def test_existing_hermes_targets_are_unchanged(self):
        self.assertEqual(make("-n", "hermes-install")[1].strip(), "bash scripts/hermes-install.sh")
        self.assertEqual(make("-n", "hermes-restart")[1].strip(), "hermes gateway restart")


if __name__ == "__main__":
    unittest.main()
