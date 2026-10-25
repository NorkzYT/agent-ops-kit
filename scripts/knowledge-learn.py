#!/usr/bin/env python3
"""knowledge-learn — resumable, headless ``/learn`` for book-backed profiles.

Raw knowledge (books synced into ``~/.hermes/profiles/<p>/knowledge``) is not
the same as a learned skill. This runner turns the raw corpus into the
on-demand skill ``<p>-knowledge`` one source file at a time:

  * one bounded Hermes turn per knowledge file (a "unit"), so a timeout loses
    at most one book's in-flight work and the next run continues from there;
  * the prompt is the official ``/learn`` prompt, built by Hermes'
    ``agent.learn_prompt.build_learn_prompt`` and passed with ``--query-file``
    (``chat -q '/learn ...'`` sends the text literally; slash commands are not
    expanded in single-query mode);
  * the run uses the profile (``hermes -p <p>``) and only the ``file``,
    ``skills`` and ``todo`` toolsets — no terminal, no web, no delegation;
  * a unit counts as learned only when Hermes exited 0, the skill changed during
    the run, and the skill references that source; the state lives in
    ``<profile>/knowledge-learn/state.json`` keyed by the file's sha256, so
    unchanged books are never re-learned and changed ones are;
  * every unit gets its own log under ``<profile>/knowledge-learn/logs/``;
  * a per-profile lock stops two runners from editing one skill;
  * books are read-only input: the runner never writes under ``knowledge/``
    and stops the profile if a source changes while it is being learned;
  * a ``knowledge/refs/<name>.ref.md`` file is a canonical reference
    (scripts/knowledge_plan.py): the unit's text is the shared copy under
    ``$KNOWLEDGE_CANONICAL/sources/<source_id>/`` and it is learned only when
    the reference validates (path inside the canonical root, regular file,
    sha256 matches); an invalid reference is reported and never learned.

Usage:
  knowledge-learn.py --status [PROFILE ...|--all] [--json]
  knowledge-learn.py PROFILE ... | --all [--dry-run] [--timeout S] [--max-turns N]
                     [--limit N] [--jobs N] [--force] [--retry]
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import errno
import fcntl
import hashlib
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import knowledge_plan as kp  # noqa: E402  (canonical reference files)

SEEDS = REPO / "hermes" / "profiles"
KNOWLEDGE_EXT = (".md", ".txt")
STATE_DIR = "knowledge-learn"
SKILL_CATEGORY = "knowledge"
TOOLSETS = "file,skills,todo"
DEFAULT_TIMEOUT = 1800       # seconds per unit (hard kill)
MIN_TIMEOUT = 60
DEFAULT_MAX_TURNS = 200
DEFAULT_MAX_ATTEMPTS = 3     # failed attempts before a unit is blocked (--retry resets)
WRAP_UP = 120                # --run-budget ends WRAP_UP s (or 25%) before the hard kill, so the agent saves first
KILL_GRACE = 15
PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
# Hermes-internal directory names that can show up as stray profiles (e.g. a
# ``hermes -p skills`` run). Never book-backed domains, so ``--all`` skips them;
# naming one explicitly still works.
INTERNAL_PROFILES = frozenset({"skills", SKILL_CATEGORY})

_print_lock = threading.Lock()
_live: set = set()            # running Hermes processes (killed on Ctrl-C/SIGTERM)
_stopping = threading.Event()


def say(msg: str, stream=None) -> None:
    with _print_lock:
        print(msg, file=stream or sys.stdout, flush=True)


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
def hermes_root() -> Path:
    """Root Hermes home. A profile's HERMES_HOME (``.../profiles/<p>``) maps to its root."""
    home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes").expanduser()
    if home.parent.name == "profiles":
        return home.parent.parent
    return home


def profiles_root() -> Path:
    return hermes_root() / "profiles"


def skill_name(profile: str) -> str:
    return f"{profile}-knowledge"


def book_backed_profiles() -> List[str]:
    """Repo profiles with a book list, plus installed profiles with raw knowledge.

    Hermes-internal names (INTERNAL_PROFILES) are not domain profiles and are skipped.
    """
    names = set()
    if SEEDS.is_dir():
        for d in SEEDS.iterdir():
            if d.is_dir() and not d.name.startswith("_") and (d / "knowledge-sources.txt").is_file():
                names.add(d.name)
    root = profiles_root()
    if root.is_dir():
        for d in root.iterdir():
            if (d.is_dir() and PROFILE_RE.match(d.name) and d.name not in INTERNAL_PROFILES
                    and knowledge_files(d / "knowledge")):
                names.add(d.name)
    return sorted(names)


def knowledge_files(root: Path) -> List[Path]:
    if not root.is_dir():
        return []
    out = []
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for f in sorted(files):
            if f.lower().endswith(KNOWLEDGE_EXT) and not f.startswith("."):
                out.append(Path(dirpath) / f)
    return out


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def slugify(text: str, width: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (s[:width].rstrip("-")) or "source"


def unit_stem(rel: str) -> str:
    name = Path(rel).name
    return name[:-len(kp.REF_SUFFIX)] if name.endswith(kp.REF_SUFFIX) else Path(rel).stem


def unit_slugs(rels: List[str]) -> Dict[str, str]:
    """Stable, unique reference-file prefix per source (file stem; hash on collision)."""
    base = {r: slugify(unit_stem(r)) for r in rels}
    counts: Dict[str, int] = {}
    for s in base.values():
        counts[s] = counts.get(s, 0) + 1
    return {r: (s if counts[s] == 1 else f"{s[:33]}-{hashlib.sha256(r.encode()).hexdigest()[:6]}")
            for r, s in base.items()}


def find_skill(home: Path, profile: str) -> List[Path]:
    """Every ``skills/**/<profile>-knowledge/SKILL.md`` (hidden dirs, e.g. backups, skipped)."""
    skills = home / "skills"
    if not skills.is_dir():
        return []
    want = skill_name(profile)
    hits = []
    for md in skills.rglob("SKILL.md"):
        rel = md.relative_to(skills).parts
        if md.parent.name == want and not any(p.startswith(".") for p in rel):
            hits.append(md.parent)
    return sorted(hits)


def skill_files(skill_dir: Path) -> List[Path]:
    return [Path(d) / f for d, _, fs in os.walk(skill_dir) for f in fs]


def newest_mtime(skill_dir: Optional[Path]) -> float:
    if skill_dir is None:
        return 0.0
    m = 0.0
    for f in skill_files(skill_dir):
        try:
            m = max(m, f.stat().st_mtime)
        except OSError:
            pass
    return m


def source_refs(skill_dir: Optional[Path], slug: str) -> int:
    if skill_dir is None or not (skill_dir / "references").is_dir():
        return 0
    return sum(1 for f in (skill_dir / "references").iterdir()
               if f.is_file() and f.name.startswith(slug + "-") and f.suffix == ".md")


def skill_mentions(skill_dir: Optional[Path], *needles: str) -> bool:
    if skill_dir is None:
        return False
    try:
        text = (skill_dir / "SKILL.md").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return any(n and n in text for n in needles)


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #
class Profile:
    def __init__(self, name: str):
        self.name = name
        self.home = profiles_root() / name
        self.knowledge = self.home / "knowledge"
        self.dir = self.home / STATE_DIR
        self.state_path = self.dir / "state.json"
        self.logs = self.dir / "logs"
        self.prompts = self.dir / "prompts"

    @property
    def installed(self) -> bool:
        return self.home.is_dir()

    def load_state(self) -> dict:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("units"), dict):
                return data
        except (OSError, ValueError):
            pass
        return {"version": 1, "profile": self.name, "skill": skill_name(self.name), "units": {}}

    def save_state(self, state: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, self.state_path)

    def skill(self) -> Optional[Path]:
        hits = find_skill(self.home, self.name)
        return hits[0] if hits else None

    def locked(self) -> bool:
        lock = self.dir / ".lock"
        if not lock.exists():
            return False
        with lock.open("a") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True
            fcntl.flock(fh, fcntl.LOCK_UN)
        return False


def units(p: Profile) -> List[dict]:
    """Every knowledge file with its hash and learned/pending/blocked classification."""
    state = p.load_state()
    files = knowledge_files(p.knowledge)
    rels = [str(f.relative_to(p.knowledge)) for f in files]
    slugs = unit_slugs(rels)
    skill = p.skill()
    out = []
    for f, rel in zip(files, rels):
        rec = state["units"].get(rel, {})
        path, kind, problem, ref = f, "file", "", {}
        if kp.is_ref(f):
            kind = "ref"
            try:
                ref = kp.parse_ref(f)
                resolved, problem = kp.resolve_ref(ref)
            except ValueError as exc:
                resolved, problem = None, str(exc)
            path = resolved or f
        digest = sha256(path)
        if kind == "ref" and problem:
            cls = "invalid-ref"
        elif rec.get("status") == "learned" and rec.get("sha256") == digest and skill is not None:
            cls = "learned"
        elif rec.get("status") == "learned" and rec.get("sha256") != digest:
            cls = "changed"
        elif rec.get("blocked") and rec.get("sha256") == digest:
            cls = "blocked"
        else:
            cls = "pending"
        out.append({"rel": rel, "path": path, "sha256": digest, "size": path.stat().st_size,
                    "slug": slugs[rel], "class": cls, "record": rec, "kind": kind,
                    "source": ref.get("source", ""), "problem": problem,
                    "refs": source_refs(skill, slugs[rel])})
    return out


def readiness(name: str) -> dict:
    """Raw knowledge vs. learned skill, from stat, hashes and state (never book text)."""
    p = Profile(name)
    r = {"profile": name, "skill": skill_name(name), "installed": p.installed,
         "raw_files": 0, "raw_bytes": 0, "learned": 0, "pending": 0, "changed": 0,
         "blocked": 0, "invalid_ref": 0, "references": 0, "skill_path": "", "running": False, "notes": []}
    if not p.installed:
        r["status"] = "not-installed"
        r["notes"].append(f"make hermes-profile NAME={name}")
        return r
    us = units(p)
    skill = p.skill()
    r["raw_files"] = len(us)
    r["raw_bytes"] = sum(u["size"] for u in us)
    for u in us:
        key = u["class"].replace("-", "_")
        r[key] = r.get(key, 0) + 1
        if u["kind"] == "ref":
            r["references"] += 1
    if skill is not None:
        r["skill_path"] = str(skill.relative_to(p.home))
    if len(find_skill(p.home, name)) > 1:
        r["notes"].append("several <profile>-knowledge skills; the first is used")
    r["running"] = p.locked()
    todo = r["pending"] + r["changed"]
    if not us:
        r["status"] = "no-knowledge"
    elif skill is None and r["learned"] == 0:
        r["status"] = "needs-learn"
        if any(u["record"].get("status") == "learned" for u in us):
            r["notes"].append("state says learned but the skill is missing")
    elif todo or r["blocked"] or r["invalid_ref"]:
        r["status"] = "partial"
    else:
        r["status"] = "ready"
    if r["invalid_ref"]:
        r["notes"].append(f"{r['invalid_ref']} canonical reference(s) do not validate (never learned): "
                          + "; ".join(f"{u['rel']}: {u['problem']}" for u in us if u["class"] == "invalid-ref")[:300])
    if r["blocked"]:
        r["notes"].append(f"{r['blocked']} blocked after {DEFAULT_MAX_ATTEMPTS} failed attempts (RETRY=1)")
    if r["changed"]:
        r["notes"].append(f"{r['changed']} changed since learned")
    return r


def print_status(rows: List[dict]) -> None:
    fmt = "{:<26} {:<13} {:>9} {:>8} {:>8} {:>8}  {}"
    say(fmt.format("PROFILE", "STATUS", "RAW", "LEARNED", "TODO", "BLOCKED", "SKILL / NOTES"))
    for r in rows:
        raw = f"{r['raw_files']}f/{r['raw_bytes'] // 1024}K" if r["installed"] else "-"
        notes = r["skill_path"] or "(no " + r["skill"] + ")"
        extra = list(r["notes"]) + (["running now"] if r["running"] else [])
        if extra:
            notes += "; " + "; ".join(extra)
        status = r["status"]
        say(fmt.format(r["profile"], status, raw, r["learned"], r["pending"] + r["changed"],
                       r["blocked"], notes))
    counts: Dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    say("\nraw = files synced into knowledge/ (canonical references count once each); "
        "LEARNED = files distilled into <profile>-knowledge.")
    say("summary: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))


# --------------------------------------------------------------------------- #
# Learning
# --------------------------------------------------------------------------- #
def learn_request(p: Profile, u: dict, total: int) -> str:
    sk = skill_name(p.name)
    src = u["path"]
    shared = ""
    if u.get("kind") == "ref":
        shared = (f"\nThis file is the shared canonical copy of the library book `{u.get('source')}`; the profile "
                  f"holds only the reference `{p.knowledge / u['rel']}` (validated: path and sha256). The canonical "
                  "store is read-only too; never write, move or copy anything there.\n")
    return f"""Learn the knowledge-base skill `{sk}` for the `{p.name}` profile from ONE source file:

{src}
{shared}
Requirements (all load-bearing):
- The skill name is exactly `{sk}`, category `{SKILL_CATEGORY}`. If it already exists, load it with
  `skill_view` and extend it; never create a second knowledge skill for this profile and never
  delete or rewrite reference files that belong to other sources.
- This run covers only the file above ({u['size'] // 1024} KB; one of {total} files in
  {p.knowledge}). The other files are learned in separate runs; do not read them now.
- Name every reference file for this source `references/{u['slug']}-<chapter-or-topic>.md`.
- If `references/{u['slug']}-*.md` files already exist from an interrupted run, keep them, skip
  those chapters, and continue with the rest.
- In SKILL.md, keep the index of every reference file and list this source under a "Sources"
  heading as `{u['rel']}` (slug `{u['slug']}`). The frontmatter description should say the skill
  is the {p.name} knowledge base built from profiles/{p.name}/knowledge.
- Read the source in parts with `read_file` offsets; save each chapter or topic before reading the
  next, so work survives a timeout.
- The source and everything under {p.knowledge} are read-only: never write, patch, move or
  delete anything there.
"""


def build_prompt(request: str) -> str:
    """The official /learn prompt from the installed Hermes (no fallback copy)."""
    agent_dir = Path(os.environ.get("HERMES_AGENT_DIR") or hermes_root() / "hermes-agent")
    py = os.environ.get("HERMES_PYTHON") or str(agent_dir / "venv" / "bin" / "python")
    if not Path(py).exists():
        py = sys.executable
    if not (agent_dir / "agent" / "learn_prompt.py").is_file():
        raise RuntimeError(f"cannot build the /learn prompt: no agent/learn_prompt.py in {agent_dir} "
                           "(set HERMES_AGENT_DIR to the Hermes install)")
    # Import from agent_dir only, even if another Hermes is on sys.path.
    code = ("import os, sys; sys.path.insert(0, sys.argv[1]); "
            "import agent.learn_prompt as lp; "
            "assert os.path.realpath(lp.__file__).startswith(os.path.realpath(sys.argv[1]) + os.sep), lp.__file__; "
            "sys.stdout.write(lp.build_learn_prompt(sys.stdin.read()))")
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
    res = subprocess.run([py, "-c", code, str(agent_dir)], input=request, text=True,
                         capture_output=True, timeout=60, env=env)
    if res.returncode != 0 or "[/learn]" not in res.stdout:
        raise RuntimeError(f"cannot build the /learn prompt from {agent_dir}: "
                           f"{(res.stderr or res.stdout).strip()[-400:]}")
    return res.stdout


def hermes_cmd(profile: str, prompt_file: Path, timeout: int, max_turns: int) -> List[str]:
    budget = max(timeout * 3 // 4, timeout - WRAP_UP)
    return [os.environ.get("HERMES_BIN", "hermes"), "-p", profile, "chat",
            "--query-file", str(prompt_file), "--oneshot", "-Q",
            "--max-turns", str(max_turns), "--run-budget", str(budget),
            "-t", TOOLSETS, "--source", "tool"]


def run_bounded(cmd: List[str], log, timeout: int) -> Tuple[Optional[int], bool]:
    """Run ``cmd`` in its own process group, output to ``log``; hard-kill at ``timeout``."""
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                            start_new_session=True, cwd=str(Path.home()))
    _live.add(proc)
    try:
        return proc.wait(timeout=timeout), False
    except subprocess.TimeoutExpired:
        kill_group(proc)
        return proc.returncode, True
    except BaseException:  # Ctrl-C/SIGTERM: the child is in its own session, so take it down too
        kill_group(proc)
        raise
    finally:
        _live.discard(proc)


def kill_group(proc: subprocess.Popen) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            break
        try:
            proc.wait(timeout=KILL_GRACE)
            break
        except subprocess.TimeoutExpired:
            continue


class ProfileLock:
    def __init__(self, p: Profile):
        self.path = p.dir / ".lock"
        self.fh = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = self.path.open("a")
        try:
            fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            self.fh.close()
            self.fh = None
            if e.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                return None
            raise
        return self

    def __exit__(self, *exc):
        if self.fh:
            fcntl.flock(self.fh, fcntl.LOCK_UN)
            self.fh.close()


def learn_profile(name: str, args) -> dict:
    p = Profile(name)
    tag = f"[{name}]"
    result = {"profile": name, "learned": 0, "failed": 0, "skipped": 0, "outcome": "ok"}
    if not p.installed:
        say(f"{tag} not installed; run: make hermes-profile NAME={name}")
        result["outcome"] = "not-installed"
        return result
    us = units(p)
    if not us:
        say(f"{tag} no raw knowledge in {p.knowledge}; run: make knowledge-refresh NAME={name}")
        result["outcome"] = "no-knowledge"
        return result
    for u in us:
        if u["class"] == "invalid-ref":
            say(f"{tag} invalid reference {u['rel']}: {u['problem']} (not learned)", sys.stderr)
    todo = [u for u in us if u["class"] in ("pending", "changed")
            or (args.force and u["class"] == "learned")
            or (args.retry and u["class"] == "blocked")]
    skipped = [u for u in us if u not in todo]
    result["skipped"] = len(skipped)
    for u in skipped:
        say(f"{tag} skip {u['class']:<7} {u['rel']}")
    if args.limit:
        todo = todo[:args.limit]
    if not todo:
        say(f"{tag} nothing to learn ({len(us)} files, skill {skill_name(name)})")
        return result

    if args.dry_run:
        try:  # side-effect free: proves the official /learn prompt builds
            size = len(build_prompt(learn_request(p, todo[0], len(us))))
            say(f"{tag} /learn prompt builds ({size} chars)")
        except (RuntimeError, OSError, subprocess.SubprocessError) as e:
            say(f"{tag} {e}", sys.stderr)
            result["outcome"] = "prompt-error"
            return result
        for u in todo:
            cmd = hermes_cmd(name, p.prompts / f"{u['slug']}.txt", args.timeout, args.max_turns)
            say(f"{tag} would learn {u['rel']} ({u['size'] // 1024} KB, refs so far {u['refs']})")
            say(f"{tag}   {shlex.join(cmd)}")
        result["outcome"] = "dry-run"
        return result

    with ProfileLock(p) as lock:
        if lock is None:
            say(f"{tag} another knowledge-learn run holds {p.dir / '.lock'}; skipped")
            result["outcome"] = "locked"
            return result
        if not args.force:  # another run may have finished files before we got the lock
            done = {x["rel"] for x in units(p) if x["class"] == "learned"}
            todo = [u for u in todo if u["rel"] not in done]
        p.logs.mkdir(parents=True, exist_ok=True)
        p.prompts.mkdir(parents=True, exist_ok=True)
        for i, u in enumerate(todo, 1):
            if _stopping.is_set():
                result["outcome"] = "interrupted"
                break
            outcome = learn_unit(p, u, len(us), args, f"{tag} [{i}/{len(todo)}]")
            if outcome == "interrupted":
                result["outcome"] = "interrupted"
                break
            if outcome == "learned":
                result["learned"] += 1
            else:
                result["failed"] += 1
            if outcome == "source-modified":
                result["outcome"] = "source-modified"
                say(f"{tag} a source file changed during the run; stopping this profile")
                break
            if outcome == "prompt-error":
                result["outcome"] = "prompt-error"
                break
    if result["failed"] and result["outcome"] == "ok":
        result["outcome"] = "incomplete"
    if any(u["class"] == "invalid-ref" for u in us) and result["outcome"] == "ok":
        result["outcome"] = "invalid-ref"
    return result


def learn_unit(p: Profile, u: dict, total: int, args, tag: str) -> str:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = p.logs / f"{stamp}-{u['slug']}.log"
    prompt_path = p.prompts / f"{u['slug']}.txt"
    state = p.load_state()
    rec = dict(state["units"].get(u["rel"], {}))
    if rec.get("sha256") != u["sha256"]:
        rec = {"attempts": 0}
    rec.update({"sha256": u["sha256"], "size": u["size"], "slug": u["slug"],
                "last_run": now_iso(), "log": str(log_path)})
    if args.retry or args.force:
        rec["attempts"] = 0
        rec.pop("blocked", None)

    try:
        prompt_path.write_text(build_prompt(learn_request(p, u, total)), encoding="utf-8")
    except (RuntimeError, OSError, subprocess.SubprocessError) as e:
        say(f"{tag} {e}", sys.stderr)
        return "prompt-error"

    cmd = hermes_cmd(p.name, prompt_path, args.timeout, args.max_turns)
    say(f"{tag} learning {u['rel']} (timeout {args.timeout}s) -> {log_path}")
    started = time.time()
    with log_path.open("w", encoding="utf-8") as log:
        log.write(f"# {now_iso()} {p.name} {u['rel']}\n# sha256 {u['sha256']}\n# {shlex.join(cmd)}\n\n")
        log.flush()
        try:
            rc, timed_out = run_bounded(cmd, log, args.timeout)
        except OSError as e:
            rc, timed_out = None, False
            log.write(f"\n# cannot start hermes: {e}\n")
        elapsed = int(time.time() - started)
        if _stopping.is_set():
            log.write("\n# result interrupted (runner stopped; file stays pending)\n")
            say(f"{tag} interrupted {u['rel']}; it stays pending")
            return "interrupted"

        skill = p.skill()
        touched = newest_mtime(skill) >= started - 1
        refs = source_refs(skill, u["slug"])
        listed = skill_mentions(skill, u["slug"], u["rel"])
        src = u["path"]
        intact = src.is_file() and sha256(src) == u["sha256"]
        if not intact:
            status = "source-modified"
        elif timed_out:
            status = "timeout"
        elif rc != 0:
            status = "failed"
        elif skill is None:
            status = "no-skill"
        elif not touched or not (refs or listed):
            status = "unverified"
        else:
            status = "learned"
        log.write(f"\n# result {status} rc={rc} timed_out={timed_out} elapsed={elapsed}s "
                  f"skill={skill or '-'} refs_for_source={refs} listed={listed}\n")

    progressed = status == "timeout" and refs > rec.get("refs", 0)
    rec.update({"status": status, "rc": rc, "elapsed": elapsed, "refs": refs})
    if status == "learned":
        rec.update({"learned_at": now_iso(), "attempts": 0})
        rec.pop("blocked", None)
    elif progressed:  # a big book saved more chapters before the timeout: resume, not a failure
        rec["attempts"] = 0
    else:
        rec["attempts"] = rec.get("attempts", 0) + 1
        if rec["attempts"] >= args.max_attempts:
            rec["blocked"] = True
    state = p.load_state()
    state["units"][u["rel"]] = rec
    state["skill"] = skill_name(p.name)
    state["updated"] = now_iso()
    p.save_state(state)
    with (p.dir / "runs.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"at": now_iso(), "unit": u["rel"], "status": status, "rc": rc,
                             "elapsed": elapsed, "refs": refs, "log": str(log_path)}) + "\n")
    note = "" if status == "learned" else f" (attempt {rec['attempts']}/{args.max_attempts}; see log)"
    say(f"{tag} {status} {u['rel']} in {elapsed}s, {refs} reference file(s){note}")
    return status


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("profiles", nargs="*", help="profile names (or 'all')")
    ap.add_argument("--all", action="store_true", help="every book-backed profile")
    ap.add_argument("--status", action="store_true", help="read-only readiness report")
    ap.add_argument("--json", action="store_true", help="--status as JSON")
    ap.add_argument("--dry-run", action="store_true", help="show the plan; write nothing, run nothing")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="hard per-file timeout (s)")
    ap.add_argument("--max-turns", type=int, default=DEFAULT_MAX_TURNS)
    ap.add_argument("--max-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS)
    ap.add_argument("--limit", type=int, default=0, help="files per profile this run (0 = all)")
    ap.add_argument("--jobs", type=int, default=1, help="profiles learned in parallel")
    ap.add_argument("--force", action="store_true", help="re-learn files already learned")
    ap.add_argument("--retry", action="store_true", help="retry blocked files")
    args = ap.parse_args(argv)

    names = list(args.profiles)
    if args.all or names == ["all"]:
        names = book_backed_profiles()
    elif not names and args.status:
        names = book_backed_profiles()
    if not names:
        ap.error("name a profile, or use --all")
    bad = [n for n in names if not PROFILE_RE.match(n)]
    if bad:
        ap.error(f"invalid profile name(s): {', '.join(bad)}")
    if args.timeout < MIN_TIMEOUT or args.jobs < 1 or args.limit < 0 or args.max_attempts < 1:
        ap.error(f"--timeout >= {MIN_TIMEOUT}, --jobs >= 1, --limit >= 0, --max-attempts >= 1")

    if args.status:
        rows = [readiness(n) for n in names]
        if args.json:
            print(json.dumps(rows, indent=2))
        else:
            print_status(rows)
        return 0

    def on_term(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, on_term)
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs)
    try:
        futures = [pool.submit(learn_profile, n, args) for n in names]
        while not all(f.done() for f in futures):
            time.sleep(0.2)
        results = [f.result() for f in futures]
    except KeyboardInterrupt:
        _stopping.set()
        for proc in list(_live):
            kill_group(proc)
        pool.shutdown(wait=True, cancel_futures=True)
        say("\nstopped; unfinished files stay pending. Rerun the same command to resume.", sys.stderr)
        return 130
    pool.shutdown(wait=True)
    say("")
    if not args.dry_run:
        print_status([readiness(n) for n in names])
    bad_outcomes = {"incomplete", "source-modified", "prompt-error", "locked", "interrupted", "invalid-ref"}
    return 1 if any(r["outcome"] in bad_outcomes for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
