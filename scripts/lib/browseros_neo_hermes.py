"""Opt-in Hermes registration for the BrowserOS neo pilot (docs/browseros-neo.md).

    browseros_neo_hermes.py enable  SNIPPET_YAML   apply the entry with native `hermes config set`
    browseros_neo_hermes.py disable                set mcp_servers.browseros_neo.enabled false
    browseros_neo_hermes.py plan    SNIPPET_YAML   print the commands (what hermes-commands prints)

Called by `scripts/browseros-neo.sh hermes-enable --approve-activation` (after the wrapper
verified the Compose-owned MCP gate under its lifecycle lock) and `hermes-disable`.
Writes only through the `hermes` CLI (BROWSEROS_NEO_HERMES, default `hermes`), so Hermes
validates and writes each key and unrelated config is untouched.

- Target: `hermes config path` names the config file once (HERMES_HOME, else the sticky
  active profile, else ~/.hermes); every later call runs with HERMES_HOME pinned to its folder.
- State is read from that file itself (read-only) and cross-checked with `hermes config get`:
  a missing file or a missing key is "absent"; an unreadable file, invalid YAML or a
  non-mapping is an error (exit 5), never "not configured". Hermes' own `config get` reports
  "not set" for an unparseable file, so it is never trusted alone.
- One exclusive lock per canonical Hermes home (`<realpath of the home>/browseros-neo-hermes.lock`,
  so worktrees, symlinked homes and profile aliases share it) is held from the first read to
  the last write/read-back/rollback. It waits BROWSEROS_NEO_HERMES_LOCK_WAIT seconds (default
  120), then exits 6 with nothing changed. It does not stop `hermes config set`, `hermes config
  edit` or a manual edit run outside these commands.
- enable writes `enabled false` first, every other key next, `enabled true` last, with two
  verification points from the file and Hermes' view: after the first write it proves
  `enabled: false` before staging any key, and before `enabled true` it proves the entry is
  still false and holds every reference value (exclusions, trust, url ...), so a CLI write that
  exits 0 without persisting never gets the entry enabled. An existing
  entry is reused only when each key it has equals the reference value; any other value is an
  operator override and is refused (exit 3), never overwritten. An entry that already matches
  is a no-op. On any failure it sets `enabled false` again, checks the file, and only when there
  was no entry before AND the entry still holds nothing but reference values, unsets it.
- disable exits 0 only for valid absence or after reading `enabled: false` back from the file.
- Output names keys, never values; child output is shortened and redacted.
"""
import fcntl
import json
import os
import re
import shlex
import subprocess
import sys
import time

NAME = "browseros_neo"
BASE = f"mcp_servers.{NAME}"
HERMES = os.environ.get("BROWSEROS_NEO_HERMES") or "hermes"
TIMEOUT = 90
LOCK_NAME = "browseros-neo-hermes.lock"
ENV = dict(os.environ)


class Unsafe(Exception):
    """The Hermes config state could not be established; nothing may be claimed about it."""


def log(msg):
    print(f"browseros-neo: {msg}", file=sys.stderr, flush=True)


_SECRETISH = [
    (re.compile(r"(?i)\b(bearer|basic)\s+\S+"), r"\1 <redacted>"),
    (re.compile(r"(?i)((?:api[_-]?key|token|secret|password|passwd|authorization|cookie|credential)s?\W{0,3}[:=]\s*)\S+"),
     r"\1<redacted>"),
    (re.compile(r"://[^/@\s]+@"), "://<redacted>@"),
    (re.compile(r"\b[A-Za-z0-9_\-]*[0-9][A-Za-z0-9_\-]*[A-Za-z][A-Za-z0-9_\-]{20,}\b|\b[A-Za-z0-9+/=_\-]{32,}\b"), "<redacted>"),
]


def redact(text, limit=200):
    """First non-empty line of a child's output, with secret-shaped values replaced."""
    line = next((l.strip() for l in (text or "").splitlines() if l.strip()), "")
    for pat, rep in _SECRETISH:
        line = pat.sub(rep, line)
    return line[:limit] or "(no output)"


def reference(path):
    import yaml
    with open(path) as f:
        doc = yaml.safe_load(f)
    servers = doc["mcp_servers"]
    if list(servers) != [NAME]:
        raise SystemExit(f"reference entry must define exactly mcp_servers.{NAME}")
    cfg = servers[NAME]
    if cfg.get("enabled") is not True or (cfg.get("tools") or {}).get("exclude") is None:
        raise SystemExit("reference entry must be enabled with tool exclusions")
    return cfg


def flat(prefix, v):
    if isinstance(v, dict):
        for k, x in v.items():
            yield from flat(f"{prefix}.{k}", x)
    else:
        yield prefix, v


def text(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, list):
        return "[" + ",".join(map(str, v)) + "]"
    return str(v)


def plan(cfg):
    steps = [[HERMES, "config", "set", f"{BASE}.enabled", "false"]]
    for key, val in flat(BASE, {k: v for k, v in cfg.items() if k != "enabled"}):
        steps.append([HERMES, "config", "set", key, text(val)])
    steps.append([HERMES, "config", "set", f"{BASE}.enabled", "true"])
    return steps


def run(argv):
    try:
        return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=TIMEOUT, env=ENV)
    except (OSError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(argv, 127, "", type(e).__name__)


def resolve_config_path():
    """The config file Hermes itself resolves; pins HERMES_HOME to its folder for every later call."""
    p = run([HERMES, "config", "path"])
    lines = [l.strip() for l in p.stdout.splitlines() if l.strip()]
    path = lines[-1] if lines else ""
    if p.returncode != 0 or not os.path.isabs(path) or os.path.basename(path) != "config.yaml":
        raise Unsafe(f"`hermes config path` did not name a config file (rc {p.returncode}: {redact(p.stderr or p.stdout)})")
    ENV["HERMES_HOME"] = os.path.dirname(path)
    return path


def read_file_entry(path):
    """mcp_servers.browseros_neo from the file itself: dict, or None only for valid absence."""
    import yaml
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        return None
    except OSError as e:
        raise Unsafe(f"cannot read {path}: {e.strerror or type(e).__name__}")
    try:
        doc = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        where = f" (line {mark.line + 1})" if mark is not None else ""
        raise Unsafe(f"{path} is not valid YAML{where}; Hermes runs on defaults and `hermes config get` "
                     "would say \"not set\". Fix it (`hermes config edit`, then `hermes config check`)")
    if doc is None:
        return None
    if not isinstance(doc, dict):
        raise Unsafe(f"{path}: the top level is not a mapping")
    if "mcp_servers" not in doc or doc["mcp_servers"] is None:
        return None
    servers = doc["mcp_servers"]
    if not isinstance(servers, dict):
        raise Unsafe(f"{path}: mcp_servers is not a mapping")
    if NAME not in servers:
        return None
    if not isinstance(servers[NAME], dict):
        raise Unsafe(f"{path}: {BASE} is not a mapping")
    return servers[NAME]


def cli_entry():
    """The entry as `hermes config get` resolves it: dict or None; anything else raises."""
    p = run([HERMES, "config", "get", "--json", BASE])
    if p.returncode != 0:
        if "config key not set" in (p.stdout + p.stderr).lower():
            return None
        raise Unsafe(f"`hermes config get {BASE}` failed (rc {p.returncode}: {redact(p.stderr or p.stdout)})")
    try:
        v = json.loads(p.stdout)
    except ValueError:
        raise Unsafe(f"`hermes config get {BASE}` did not print JSON")
    if not isinstance(v, dict):
        raise Unsafe(f"Hermes resolves {BASE} to a non-mapping")
    return v


def state(path):
    """The entry, read from the file and confirmed by Hermes' resolved view (presence and `enabled`)."""
    f = read_file_entry(path)
    c = cli_entry()
    if (f is None) != (c is None) or (f is not None and f.get("enabled") != c.get("enabled")):
        raise Unsafe(f"{path} and `hermes config get` disagree about {BASE}; not touching it")
    return f


class Lock:
    """Exclusive lock for one canonical Hermes home, waited on for a bounded time."""

    def __init__(self, path):
        home = os.path.realpath(os.path.dirname(os.path.realpath(path)))
        self.path = os.path.join(home, LOCK_NAME)
        try:
            self.wait = float(os.environ.get("BROWSEROS_NEO_HERMES_LOCK_WAIT") or 120)
        except ValueError:
            self.wait = 120.0
        self.fd = None

    def __enter__(self):
        try:
            self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        except OSError as e:
            raise Unsafe(f"cannot open the Hermes config lock {self.path}: {e.strerror}")
        deadline, said = time.monotonic() + self.wait, False
        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    os.close(self.fd)
                    raise LockTimeout(f"the Hermes config lock {self.path} is still held by another "
                                      f"hermes-enable/hermes-disable after {self.wait:g} s; nothing changed")
                if not said:
                    log(f"waiting for the Hermes config lock {self.path} (another hermes-enable/disable)")
                    said = True
                time.sleep(0.1)

    def __exit__(self, *exc):
        os.close(self.fd)


class LockTimeout(Exception):
    pass


def conflicts(have, want, prefix=BASE):
    """Keys in `have` whose value differs from `want` (enabled ignored). Missing keys are fine."""
    out = []
    for k, v in have.items():
        key = f"{prefix}.{k}"
        if prefix == BASE and k == "enabled":
            continue
        if k not in want:
            out.append(key)
        elif isinstance(v, dict) and isinstance(want[k], dict):
            out += conflicts(v, want[k], key)
        elif v != want[k]:
            out.append(key)
    return out


def missing(have, want):
    have_keys = dict(flat(BASE, have))
    return [k for k in dict(flat(BASE, want)) if k not in have_keys]


def matches(have, want):
    return have is not None and have.get("enabled") is True and not conflicts(have, want) and not missing(have, want)


def set_disabled(path):
    """Write enabled false, then prove it from the file and Hermes' view. True only when proven."""
    p = run([HERMES, "config", "set", f"{BASE}.enabled", "false"])
    if p.returncode != 0:
        log(f"`hermes config set {BASE}.enabled false` failed (rc {p.returncode}: {redact(p.stderr or p.stdout)})")
    try:
        e = state(path)
    except Unsafe as err:
        log(f"cannot read back {BASE}: {err}")
        return False
    return e is not None and e.get("enabled") is False


def check_disabled(path):
    """Verification point 1, after the first write: nothing is staged unless `enabled: false` persisted."""
    e = state(path)
    if e is None or e.get("enabled") is not False:
        raise RuntimeError(f"`enabled false` was not read back for {BASE}; no key staged")


def check_staged(path, cfg):
    """Verification point 2, before `enabled true`: still disabled and every reference value persisted."""
    e = state(path)
    if e is None or e.get("enabled") is not False:
        raise RuntimeError(f"{BASE} is no longer `enabled: false` before enable; `enabled true` not written")
    diff = conflicts(e, cfg) + missing(e, cfg)
    if diff:
        raise RuntimeError(f"staged {BASE} does not match the reference ({', '.join(diff)}); "
                           "`enabled true` not written")


def port_hint(keys):
    if f"{BASE}.url" in keys:
        return (" The registered url differs from this Neo's MCP port: either run Neo on the registered "
                "port (BROWSEROS_NEO_MCP_PORT), or run `browseros-neo.sh hermes-disable`, review the entry with "
                f"`hermes config get {BASE}`, then `hermes config unset {BASE}` (only if nothing else of yours is "
                "in it) and run hermes-enable again.")
    return ""


def enable(cfg, path):
    before = state(path)
    if before is not None:
        bad = conflicts(before, cfg)
        if bad:
            log(f"refused: existing {BASE} has operator values that differ from the reference "
                f"({', '.join(bad)}); nothing changed. Review `browseros-neo.sh hermes-snippet` and "
                f"edit it with `hermes config set`, or unset the entry first.{port_hint(bad)}")
            return 3
        if matches(before, cfg):
            log(f"{BASE} already matches the reference and is enabled; nothing changed")
            return 0
    applied = []
    steps = plan(cfg)
    try:
        for i, argv in enumerate(steps):
            if i == len(steps) - 1:
                check_staged(path, cfg)
            p = run(argv)
            if p.returncode != 0:
                raise RuntimeError(f"`{shlex.join(['hermes'] + argv[1:3])} ...` failed "
                                   f"(rc {p.returncode}: {redact(p.stderr or p.stdout)})")
            applied.append(argv)
            if i == 0:
                check_disabled(path)
        after = state(path)
        if resolve_config_path() != path:
            raise RuntimeError("`hermes config path` changed during the run")
        if not matches(after, cfg):
            diff = (conflicts(after, cfg) + missing(after, cfg)) if after else [BASE]
            if after is not None and after.get("enabled") is not True:
                diff.append(f"{BASE}.enabled")
            raise RuntimeError(f"read-back of {BASE} does not match the reference ({', '.join(diff)})")
    except Exception as e:  # fail closed (Unsafe included), then roll back only what this run can own
        log(f"enable failed: {e}")
        if not set_disabled(path):
            log(f"ROLLBACK FAILED: {BASE} is NOT proven disabled. Run `hermes config set {BASE}.enabled false` "
                f"and check it with `hermes config get {BASE}` now")
            return 4
        if before is None:
            try:
                now = read_file_entry(path)
            except Unsafe as err:
                log(f"rolled back to disabled; entry not removed: {err}")
                return 1
            if now is not None and not conflicts(now, cfg):
                p = run([HERMES, "config", "unset", BASE])
                try:
                    gone = p.returncode == 0 and state(path) is None
                except Unsafe:
                    gone = False
                log("rolled back: entry removed" if gone else "entry left disabled (unset not confirmed)")
            else:
                log(f"rolled back to disabled; entry not removed: it now holds values this run did not write "
                    f"({', '.join(conflicts(now, cfg)) if now else 'unknown'}), another writer changed it")
        else:
            log("rolled back to disabled; keys this run already wrote stay (each equals the reference)")
        return 1
    log(f"{BASE} enabled ({len(applied)} native config set commands, exclusions before enable). "
        "A running gateway connects it on its next MCP config reconcile; no restart. "
        "Revoke: browseros-neo.sh hermes-disable")
    return 0


def disable(path):
    e = state(path)
    if e is None:
        log(f"{BASE} is not configured in {path}; nothing changed")
        return 0
    if e.get("enabled") is False:
        log(f"{BASE} is already disabled in {path}; nothing changed")
        return 0
    if not set_disabled(path):
        log(f"{BASE} is NOT disabled: `enabled: false` was not read back from {path}. "
            f"Run `hermes config set {BASE}.enabled false` and check `hermes config get {BASE}`")
        return 1
    log(f"{BASE} disabled in {path} (entry kept; a running gateway disconnects it on its next reconcile)")
    return 0


def main(argv):
    if len(argv) == 3 and argv[1] == "plan":
        for a in plan(reference(argv[2])):
            print(shlex.join(["hermes"] + a[1:]))
        return 0
    if (len(argv) == 2 and argv[1] == "disable") or (len(argv) == 3 and argv[1] == "enable"):
        cfg = reference(argv[2]) if argv[1] == "enable" else None
        try:
            path = resolve_config_path()
            log(f"Hermes config: {path} (from `hermes config path`; set HERMES_HOME to choose a profile)")
            with Lock(path):
                return enable(cfg, path) if cfg is not None else disable(path)
        except LockTimeout as e:
            log(str(e))
            return 6
        except Unsafe as e:
            if cfg is None:
                log(f"disable refused: {BASE} is NOT proven disabled; nothing was written. {e}")
            else:
                log(f"enable refused; nothing was written: {e}")
            return 5
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
