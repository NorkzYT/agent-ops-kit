"""hermes/skills/anatomy: the vendored Anatomy skill stays self-contained, licensed and offline.

The installer copies each hermes/skills/<name>/ directory as-is (scripts/hermes-install.sh,
section 6), so everything SKILL.md and its references point at must ship inside the skill.
The Node checks copy the skill to a temp dir (as the installer does), build examples there,
read the generated pages for the full MIT license, and run every Chrome launcher against a fake
Chrome that records its argv. They skip when Node 20.10+ is missing, unless
ANATOMY_REQUIRE_NODE=1 (CI sets it, so the checks can't silently skip). The checks that import
kit/iso-kit.ts need Node 22.18+/23.6+ (built-in type stripping) and skip with that reason on an
older Node; ANATOMY_REQUIRE_NODE=1 turns that skip into a failure too. The workspace tests run
the SKILL.md workspace block itself against a project with existing LICENSE, kit/ and scripts/. The printed-sheet check
needs a real Chrome (CHROME_PATH or /usr/bin/google-chrome); ANATOMY_REQUIRE_CHROME=1 makes it required.
ANATOMY_SKILL_DIR points the suite at another copy of the skill (used for mutation checks).
"""

from __future__ import annotations

import json
import shlex
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SKILL = Path(os.environ.get("ANATOMY_SKILL_DIR") or REPO / "hermes" / "skills" / "anatomy").resolve()
UPSTREAM_REV = "1482e8469b0a9e0d8cc18ea2740a01423d29ae80"
CODE_SUFFIXES = {".mjs", ".js", ".ts", ".tsx"}
DESC_LIMIT = 60  # Hermes SKILL_PROMPT_DESC_LIMIT: the skill index width
MAX_SKILL_CHARS = 100_000  # Hermes MAX_SKILL_CONTENT_CHARS
MAX_FILE_BYTES = 1_048_576  # Hermes MAX_SKILL_FILE_BYTES
IMPORT_RE = re.compile(r"""(?:\bfrom\s*|\bimport\s*\(\s*|\bimport\s+)["'](\.{1,2}/[^"']+)["']""")
PATH_REF_RE = re.compile(r"`((?:kit|scripts|references|examples)/[A-Za-z0-9_./-]+)")
URL_RE = re.compile(r"https?://[^\s\"'`)<>]+")
# Loopback, XML namespaces, and the upstream repo named (never fetched) in the license headers.
ALLOWED_URL = re.compile(r"https?://(127\.0\.0\.1|localhost)[:/$]|https?://www\.w3\.org/|https://github\.com/wheresryan22/anatomy$")


def _frontmatter(text: str) -> dict[str, str]:
    m = re.match(r"---\n(.*?)\n---\n", text, re.S)
    if not m:
        raise AssertionError("SKILL.md has no frontmatter")
    out = {}
    for line in m.group(1).splitlines():
        km = re.match(r"([a-z_]+):\s*(.*)$", line)
        if km:
            out[km.group(1)] = km.group(2).strip().strip("'\"")
    return out


def _node() -> str:
    """Node 20.10+ or skip; with ANATOMY_REQUIRE_NODE=1 a missing or old Node fails instead."""
    required = os.environ.get("ANATOMY_REQUIRE_NODE") == "1"
    node = shutil.which("node")
    ver = subprocess.run([node, "--version"], capture_output=True, text=True).stdout.strip().lstrip("v") if node else ""
    if not node or tuple(int(x) for x in ver.split(".")[:2]) < (20, 10):
        msg = f"node 20.10+ needed (found {ver or 'none'})"
        if required:
            raise AssertionError(msg + " and ANATOMY_REQUIRE_NODE=1")
        raise unittest.SkipTest(msg)
    return node


def _node_ts() -> str:
    """A Node that runs .ts with built-in type stripping (22.18+ or 23.6+), for the React/TS kit.
    Skips (with the reason) on older Node; ANATOMY_REQUIRE_NODE=1 makes it a failure, so CI
    (Node 24) can't skip it."""
    node = _node()
    ver = subprocess.run([node, "--version"], capture_output=True, text=True).stdout.strip().lstrip("v")
    major, minor = (int(x) for x in ver.split(".")[:2])
    if (major, minor) >= (23, 6) or (major == 22 and minor >= 18):
        return node
    msg = f"React/TS kit checks need Node 22.18+/23.6+ type stripping (found {ver}); framework-free checks still ran"
    if os.environ.get("ANATOMY_REQUIRE_NODE") == "1":
        raise AssertionError(msg + " and ANATOMY_REQUIRE_NODE=1")
    raise unittest.SkipTest(msg)


def _chrome() -> str:
    for c in (os.environ.get("CHROME_PATH"), "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable",
              "/usr/bin/chromium", "/usr/bin/chromium-browser"):
        if c and Path(c).is_file():
            return c
    if os.environ.get("ANATOMY_REQUIRE_CHROME") == "1":
        raise AssertionError("Chrome not found and ANATOMY_REQUIRE_CHROME=1")
    raise unittest.SkipTest("Chrome not found")


def _workspace(tmp: str) -> Path:
    """The workspace SKILL.md tells the agent to make: kit, scripts and LICENSE copied from an
    installed copy of the skill (copied the way the installer copies it)."""
    installed = Path(tmp) / "skills" / "agent-ops-kit" / "anatomy"
    shutil.copytree(SKILL, installed)
    work = Path(tmp) / "work"
    work.mkdir()
    for name in WORKSPACE_COPY:
        src = installed / name
        (shutil.copytree if src.is_dir() else shutil.copy2)(src, work / name)
    return work


def _workspace_block() -> str:
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    sec = text[text.index("## Workspace\n"):]
    m = re.search(r"```bash\n(.*?)```", sec, re.S)
    if not m:
        raise AssertionError("SKILL.md workspace section has no bash block")
    return m.group(1)


def _tree(root: Path) -> dict[str, tuple]:
    """Relative path -> (kind, bytes or link target), without following symlinks."""
    out = {}
    for p in sorted(root.rglob("*")):
        rel = str(p.relative_to(root))
        if p.is_symlink():
            out[rel] = ("link", os.readlink(p))
        elif p.is_dir():
            out[rel] = ("dir",)
        else:
            out[rel] = ("file", p.read_bytes())
    return out


def _license_text() -> str:
    return (SKILL / "LICENSE").read_text(encoding="utf-8")


def _files(suffixes=None):
    for p in sorted(SKILL.rglob("*")):
        if p.is_file() and (suffixes is None or p.suffix in suffixes):
            yield p


WORKSPACE_COPY = ("kit", "scripts", "LICENSE")
LAUNCHERS = ("capture.mjs", "drive.mjs", "turn-check.mjs", "print.mjs")
FAKE_CHROME = """#!/bin/sh
printf '%s\\n' "$@" >> "$ANATOMY_ARGV_LOG"
printf -- '---\\n' >> "$ANATOMY_ARGV_LOG"
exit 3
"""


class AnatomySkillTest(unittest.TestCase):
    def test_skill_md_fits_hermes_index(self):
        text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        fm = _frontmatter(text)
        self.assertEqual(fm.get("name"), "anatomy")
        desc = fm.get("description", "")
        self.assertTrue(desc.endswith("."), desc)
        self.assertLessEqual(len(desc), DESC_LIMIT, desc)
        self.assertLessEqual(len(text), MAX_SKILL_CHARS)
        self.assertNotRegex(text, r"/home/[a-z0-9_-]+/")
        # Not every explanation is a figure: the skill must say when not to use it.
        self.assertIn("When not to use", text)

    def test_relative_imports_resolve_inside_skill(self):
        missing = []
        for f in _files(CODE_SUFFIXES):
            for spec in IMPORT_RE.findall(f.read_text(encoding="utf-8")):
                target = (f.parent / spec).resolve()
                cands = [target] + [target.with_name(target.name + s) for s in (".ts", ".tsx", ".mjs", ".js")]
                if not any(c.is_file() for c in cands) or SKILL.resolve() not in target.parents:
                    missing.append(f"{f.relative_to(SKILL)} -> {spec}")
        self.assertEqual(missing, [])

    def test_documented_paths_ship(self):
        missing = []
        # upstream-notice.md names the files left out on purpose; every other doc must resolve.
        docs = [SKILL / "SKILL.md", *sorted(p for p in (SKILL / "references").glob("*.md")
                                            if p.name != "upstream-notice.md")]
        for d in docs:
            for ref in PATH_REF_RE.findall(d.read_text(encoding="utf-8")):
                ref = ref.rstrip(".,")
                if "<" in ref or "*" in ref:
                    continue
                p = SKILL / ref
                if not (p.exists() or p.with_suffix(p.suffix + ".mjs").exists()):
                    missing.append(f"{d.relative_to(SKILL)}: {ref}")
        self.assertEqual(missing, [])

    def test_license_and_notice(self):
        lic = (SKILL / "LICENSE").read_text(encoding="utf-8")
        self.assertTrue(lic.startswith("MIT License\n\nCopyright (c) 2026 Ryan\n"))
        self.assertIn("THE SOFTWARE IS PROVIDED \"AS IS\"", lic)
        notice = (SKILL / "references" / "upstream-notice.md").read_text(encoding="utf-8")
        self.assertIn("https://github.com/wheresryan22/anatomy", notice)
        self.assertIn(UPSTREAM_REV, notice)
        self.assertIn("MIT", _frontmatter((SKILL / "SKILL.md").read_text(encoding="utf-8")).get("license", ""))

    def test_no_outbound_or_oversized_files(self):
        names = {p.name for p in _files()}
        for banned in ("publish.mjs", "session.mjs", "fingerprint.mjs", "publish.md", "openai.yaml"):
            self.assertNotIn(banned, names)
        self.assertFalse([p for p in _files() if p.name.endswith(".test.mjs")])
        self.assertFalse([p for p in _files({".html", ".pdf", ".png", ".webp"})])
        self.assertFalse([p for p in _files() if p.stat().st_size > MAX_FILE_BYTES])
        bad = []
        for f in _files(CODE_SUFFIXES):
            for url in URL_RE.findall(f.read_text(encoding="utf-8")):
                if not ALLOWED_URL.match(url):
                    bad.append(f"{f.relative_to(SKILL)}: {url}")
        self.assertEqual(bad, [])

    def test_every_chrome_launch_is_offline_static(self):
        launchers = sorted(f.name for f in _files({".mjs"}) if "--headless" in f.read_text(encoding="utf-8"))
        self.assertEqual(launchers, sorted(LAUNCHERS))  # a new launcher must join the runtime argv test
        flags = (SKILL / "scripts" / "chrome-offline.mjs").read_text(encoding="utf-8")
        for flag in ("--proxy-server=http://127.0.0.1:9", "--disable-background-networking",
                     "--disable-component-update", "--no-pings"):
            self.assertIn(flag, flags)

    def _fake_chrome_argv(self, node, work, cmd, tmp):
        log = Path(tmp) / "argv.log"
        log.write_text("")
        fake = Path(tmp) / "fake-chrome"
        fake.write_text(FAKE_CHROME)
        fake.chmod(0o755)
        env = {**os.environ, "CHROME_PATH": str(fake), "ANATOMY_ARGV_LOG": str(log)}
        r = subprocess.run([node, *cmd], cwd=work, env=env, capture_output=True, text=True, timeout=60)
        launches = [chunk.strip("\n").split("\n") for chunk in log.read_text().split("---\n") if chunk.strip()]
        return r, launches

    def test_every_chrome_launch_spawns_offline_flags(self):
        """Run each launcher against a fake Chrome and read the argv it really spawned."""
        node = _node()
        with tempfile.TemporaryDirectory() as tmp:
            work = _workspace(tmp)
            flags = json.loads(subprocess.run(
                [node, "--input-type=module", "-e",
                 'import { OFFLINE_CHROME_FLAGS as f } from "./scripts/chrome-offline.mjs"; console.log(JSON.stringify(f));'],
                cwd=work, capture_output=True, text=True, check=True).stdout)
            self.assertIn("--proxy-server=http://127.0.0.1:9", flags)
            (work / "page.html").write_text("<!doctype html><title>t</title>")
            (work / "page.pdf.html").write_text("<!doctype html><title>t</title>")
            runs = {
                "capture.mjs": ["scripts/capture.mjs", "page.html", "--out", "shot.png"],
                "drive.mjs": ["scripts/drive.mjs", "page.html", "[]"],
                "turn-check.mjs": ["scripts/turn-check.mjs", "page.html"],
                "print.mjs": ["--input-type=module", "-e",
                              'import { printPdf, } from "./scripts/print.mjs"; import { PAPER } from "./scripts/print-sheet.mjs";'
                              ' try { printPdf("page.pdf.html", "out.pdf", { png: "out.png", paper: PAPER.a4 }); } catch {}'],
            }
            self.assertEqual(sorted(runs), sorted(LAUNCHERS))
            for name, cmd in runs.items():
                r, launches = self._fake_chrome_argv(node, work, cmd, tmp)
                self.assertTrue(launches, f"{name} never launched Chrome: {r.stderr}")
                for argv in launches:
                    i = argv.index("--headless=new")
                    self.assertEqual(argv[i + 1:i + 1 + len(flags)], flags, f"{name}: {argv}")

    def test_launchers_refuse_untrusted_targets(self):
        """Only self-built pages: a local file or loopback URL. Remote pages never reach Chrome."""
        node = _node()
        with tempfile.TemporaryDirectory() as tmp:
            work = _workspace(tmp)
            for target in ("https://example.com/", "http://169.254.169.254/", "http://localhost.example.com/"):
                for cmd in (["scripts/capture.mjs", target], ["scripts/drive.mjs", target, "[]"],
                            ["scripts/turn-check.mjs", target]):
                    r, launches = self._fake_chrome_argv(node, work, cmd, tmp)
                    self.assertNotEqual(r.returncode, 0, cmd)
                    self.assertEqual(launches, [], cmd)
                    self.assertIn("refusing", r.stderr, cmd)
            r = subprocess.run([node, "--input-type=module", "-e",
                                'import { trustedTarget as t } from "./scripts/chrome-offline.mjs";'
                                ' console.log(JSON.stringify(["a.html", "http://localhost:3000/x", "http://127.0.0.1:8/", "http://[::1]/"].map(t)));'],
                               cwd=work, capture_output=True, text=True, check=True)
            self.assertEqual(len(json.loads(r.stdout)), 4)

    def test_installer_copies_it_ungated(self):
        man = json.loads((REPO / "hermes/capabilities/activation/manifest.json").read_text(encoding="utf-8"))
        self.assertNotIn("anatomy", man["skills"])

    def test_source_kit_files_start_with_full_license(self):
        """kit/iso.css and the React/TS kit are copied into user projects as files: each starts with
        the full license. Whether a bundler keeps the comment depends on the user's toolchain, so
        the skill says to ship LICENSE with any bundled distribution instead of relying on it."""
        banner = "/*! @license Anatomy kit (https://github.com/wheresryan22/anatomy)\n" + _license_text() + "*/\n"
        for rel in ("kit/iso-kit.ts", "kit/react/live.tsx", "kit/react/draw.tsx", "kit/iso.css"):
            self.assertTrue((SKILL / rel).read_text(encoding="utf-8").startswith(banner), rel)
        for rel in ("SKILL.md", "references/react.md"):
            self.assertNotRegex((SKILL / rel).read_text(encoding="utf-8"), r"(?i)keep[s]? (it |them )?in bundles|by default", rel)

    def test_license_constant_and_iso_css_match(self):
        lic = _license_text()
        node = _node()
        r = subprocess.run([node, "--input-type=module", "-e",
                            'import { ANATOMY_LICENSE, ISO_CSS } from "./kit/iso-kit.mjs";'
                            ' process.stdout.write(JSON.stringify([ANATOMY_LICENSE, ISO_CSS]));'],
                           cwd=SKILL, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        got_lic, iso_css = json.loads(r.stdout)
        self.assertEqual(got_lic, lic)
        css = (SKILL / "kit/iso.css").read_text(encoding="utf-8")
        banner = "/*! @license Anatomy kit (https://github.com/wheresryan22/anatomy)\n" + lic + "*/\n"
        self.assertEqual(css[len(banner):].strip(), iso_css.strip())  # same styles after the header

    def test_ts_kit_license_constant(self):
        """iso-kit.ts runs only on a Node that strips types natively (22.18+/23.6+); the
        framework-free path needs 20.10+ and never imports it."""
        node = _node_ts()
        r = subprocess.run([node, "--input-type=module", "-e",
                            'import { ANATOMY_LICENSE } from "./kit/iso-kit.ts"; process.stdout.write(ANATOMY_LICENSE);'],
                           cwd=SKILL, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, _license_text())

    def test_skill_workspace_copy_includes_license(self):
        cp = re.search(r'^  cp -R ("\$SKILL_DIR/[^\n]*)"\$w/"$', _workspace_block(), re.M)
        self.assertTrue(cp, "SKILL.md workspace block has no cp -R line")
        copied = re.findall(r'"\$SKILL_DIR/([A-Za-z]+)"', cp.group(1))
        self.assertEqual(sorted(copied), sorted(WORKSPACE_COPY))
        self.assertIn("LICENSE", (SKILL / "references" / "react.md").read_text(encoding="utf-8"))

    def _run_workspace_block(self, tmp, parent, slug="token-bucket"):
        """Run the SKILL.md workspace block verbatim, with only its three `=...` inputs filled."""
        installed = Path(tmp) / "skills" / "agent-ops-kit" / "anatomy"
        if not installed.exists():
            shutil.copytree(SKILL, installed)
        script = _workspace_block()
        for var, val in (("SKILL_DIR", installed), ("PARENT", parent), ("SLUG", slug)):
            script, n = re.subn(rf"^{var}=\.\.\.[^\n]*$", lambda _m: f"{var}={shlex.quote(str(val))}", script, flags=re.M)
            self.assertEqual(n, 1, var)
        return subprocess.run(["bash", "-c", script], cwd=tmp, capture_output=True, text=True, timeout=300)

    def test_workspace_block_never_touches_existing_project_files(self):
        """The documented commands, run in a project that already has LICENSE, kit/ and scripts/,
        make a new dedicated subdirectory and leave every existing file byte-identical."""
        _node()
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp) / "user project"
            (proj / "kit").mkdir(parents=True)
            (proj / "scripts").mkdir()
            canaries = {proj / "LICENSE": "user GPL license canary\n",
                        proj / "kit" / "iso-kit.mjs": "// user kit canary\n",
                        proj / "scripts" / "capture.mjs": "// user script canary\n",
                        proj / "kit" / "mine.txt": "keep\n"}
            for f, body in canaries.items():
                f.write_text(body)
            before = _tree(proj)
            r = self._run_workspace_block(tmp, proj)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            work = Path(re.search(r"^workspace: (.+)$", r.stdout, re.M).group(1))
            self.assertEqual(work.parent, proj)
            self.assertTrue(work.name.startswith("anatomy-token-bucket."), work)
            self.assertEqual((work / "LICENSE").read_text(), _license_text())
            for name in ("dial-indicator.html", "dial-indicator-light.html"):
                self.assertTrue((work / "figures" / "token-bucket" / name).is_file(), name)
            after = {k: v for k, v in _tree(proj).items() if not k.startswith(work.name)}
            self.assertEqual(after, before)
            # A rerun makes another new directory; the first one is not reused or merged into.
            first = _tree(work)
            r2 = self._run_workspace_block(tmp, proj)
            self.assertEqual(r2.returncode, 0, r2.stderr)
            work2 = Path(re.search(r"^workspace: (.+)$", r2.stdout, re.M).group(1))
            self.assertNotEqual(work2, work)
            self.assertEqual(_tree(work), first)
            self.assertEqual({k: v for k, v in _tree(proj).items()
                              if not k.startswith((work.name, work2.name))}, before)

    def test_workspace_block_refuses_unsafe_parents(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "real"
            real.mkdir()
            (real / "LICENSE").write_text("canary\n")
            link = Path(tmp) / "link"
            link.symlink_to(real)
            before = _tree(Path(tmp))
            for parent, slug in ((link, "x"), (Path(tmp) / "missing" / "deeper", "x"),
                                 (real, "../escape"), (real, ""), (real, "Bad Slug")):
                r = self._run_workspace_block(tmp, parent, slug)
                self.assertNotEqual(r.returncode, 0, (parent, slug))
                self.assertIn("refusing", r.stderr, (parent, slug))
                self.assertNotIn("workspace:", r.stdout)
                self.assertEqual({k: v for k, v in _tree(Path(tmp)).items() if not k.startswith("skills")},
                                 {k: v for k, v in before.items() if not k.startswith("skills")}, (parent, slug))

    def test_built_pages_carry_full_license(self):
        """Build examples in a SKILL.md-shaped workspace and read the HTML they actually wrote."""
        node = _node()
        lic = _license_text()
        with tempfile.TemporaryDirectory() as tmp:
            work = _workspace(tmp)
            # Framework-free pages via pageHtml + kitScript; the other helpers are read below.
            builds = {"dial-indicator": [[], ["--light"]], "push-button": [[]]}
            for name, themes in builds.items():
                shutil.copytree(SKILL / "examples" / name, work / "figures" / name)
                for theme in themes:
                    r = subprocess.run([node, f"figures/{name}/build.mjs", *theme], cwd=work,
                                       capture_output=True, text=True, timeout=300)
                    self.assertEqual(r.returncode, 0, r.stderr)
            pages = sorted((work / "figures").rglob("*.html"))
            self.assertGreaterEqual(len(pages), 3, pages)
            for page in pages:
                html = page.read_text(encoding="utf-8")
                self.assertTrue(html.startswith("<!doctype html><!--\n" + lic + "-->"), page.name)
                self.assertIn("<svg", html)
                self.assertNotRegex(html, r"<(script|link|img)[^>]+(src|href)=\"https?://")
            dial = (work / "figures/dial-indicator/dial-indicator.html").read_text(encoding="utf-8")
            self.assertIn("/*! @license Anatomy kit (https://github.com/wheresryan22/anatomy)\n" + lic + "*/", dial)
            self.assertTrue((work / "figures/dial-indicator/dial-indicator-light.html").is_file())
            # Every inline-kit helper, a lone standalone SVG and the React/TS pageHtml.
            probe = r"""
import * as inl from "./scripts/inline-kit.mjs";
import * as k from "./kit/iso-kit.mjs";
const out = {};
for (const f of ["kitScript", "glScript", "turnScript", "fixedScript", "glSharedScript"]) out[f] = inl[f]();
out.svg = k.figureSvg({ width: 10, height: 10, label: "x", body: "", standalone: "dark" });
out.svgInline = k.figureSvg({ width: 10, height: 10, label: "x", body: "" });
console.log(JSON.stringify(out));
"""
            r = subprocess.run([node, "--input-type=module", "-e", probe], cwd=work, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            out = json.loads(r.stdout)
            banner = "/*! @license Anatomy kit (https://github.com/wheresryan22/anatomy)\n" + lic + "*/\n"
            for f in ("kitScript", "glScript", "turnScript", "fixedScript", "glSharedScript"):
                self.assertTrue(out[f].startswith(banner), f)
                self.assertEqual(out[f].count("@license"), 1, f)
            comment = "<!--\n" + lic + "-->"
            self.assertIn(comment, out["svg"])
            self.assertNotIn(comment, out["svgInline"])  # inside a page, pageHtml already carries it

    def test_ts_kit_pages_carry_full_license(self):
        node = _node_ts()
        comment = "<!--\n" + _license_text() + "-->"
        probe = ('import * as kt from "./kit/iso-kit.ts"; console.log(JSON.stringify({'
                 ' page: kt.pageHtml({ title: "t", body: "" }),'
                 ' svg: kt.figureSvg({ width: 10, height: 10, label: "x", body: "", standalone: "light" }) }));')
        r = subprocess.run([node, "--input-type=module", "-e", probe], cwd=SKILL, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertTrue(out["page"].startswith("<!doctype html>" + comment))
        svg = out["svg"]
        self.assertTrue(svg.startswith("<svg"), svg[:40])
        self.assertIn(">" + comment + "<style>", svg)  # first child of the root <svg>, before its styles

    def test_printed_sheet_carries_full_license(self):
        """A real print (headless Chrome, offline flags): the sheet HTML embeds the kit CSS, so it
        must start with the license too."""
        node, chrome = _node(), _chrome()
        lic = _license_text()
        with tempfile.TemporaryDirectory() as tmp:
            work = _workspace(tmp)
            shutil.copytree(SKILL / "examples" / "push-button", work / "figures" / "push-button")
            env = {**os.environ, "CHROME_PATH": chrome}
            for cmd in (["figures/push-button/build.mjs", "--light"], ["scripts/print.mjs", "figures/push-button/sheet.mjs"]):
                r = subprocess.run([node, *cmd], cwd=work, env=env, capture_output=True, text=True, timeout=300)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            sheet = (work / "figures/push-button/push-button-poster-a4.html").read_text(encoding="utf-8")
            self.assertTrue(sheet.startswith("<!doctype html><!--\n" + lic + "-->"))
            self.assertTrue((work / "figures/push-button/push-button-poster-a4.pdf").stat().st_size > 0)

if __name__ == "__main__":
    unittest.main()
