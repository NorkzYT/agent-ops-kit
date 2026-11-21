"""Opt-in Hermes registration for the BrowserOS neo pilot (docs/browseros-neo.md).

    browseros_neo_hermes.py enable  SNIPPET_YAML   apply the entry with native `hermes config set`
    browseros_neo_hermes.py disable                set mcp_servers.browseros_neo.enabled false
    browseros_neo_hermes.py plan    SNIPPET_YAML   print the commands (what hermes-commands prints)

Called by `scripts/browseros-neo.sh hermes-enable --approve-activation` / `hermes-disable`.
Only ever runs the `hermes` CLI (BROWSEROS_NEO_HERMES, default `hermes`), so Hermes validates and
writes each key and unrelated config is untouched; it never opens config.yaml itself.

- enable writes `enabled false` first, every other key next, `enabled true` last.
- An existing entry is reused only when each key it already has equals the reference value
  (a partly applied earlier run); any other value is an operator override and is refused,
  never overwritten. An entry that already matches is a no-op (idempotent).
- On any failure it sets `enabled false` again (fail closed) and, when there was no entry
  before, unsets the entry it created (rollback). It then exits non-zero.
- No secrets: the entry has no headers or tokens; nothing is read from the environment
  except BROWSEROS_NEO_HERMES.
"""
import json
import os
import shlex
import subprocess
import sys

NAME = "browseros_neo"
BASE = f"mcp_servers.{NAME}"
HERMES = os.environ.get("BROWSEROS_NEO_HERMES") or "hermes"
TIMEOUT = 90


def log(msg):
    print(f"browseros-neo: {msg}", file=sys.stderr, flush=True)


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
    return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=TIMEOUT)


def current():
    """The entry as Hermes resolves it: dict, or None when absent. Raises on anything else."""
    p = run([HERMES, "config", "get", "--json", BASE])
    if p.returncode != 0:
        if "not set" in (p.stdout + p.stderr).lower():
            return None
        raise RuntimeError(f"`hermes config get {BASE}` failed: {(p.stderr or p.stdout).strip()[:300]}")
    v = json.loads(p.stdout)
    if not isinstance(v, dict):
        raise RuntimeError(f"{BASE} is not a mapping: {v!r}")
    return v


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


def matches(have, want):
    return have is not None and have.get("enabled") is True and not conflicts(have, want) \
        and all(k in dict(flat("", have)) for k in dict(flat("", want)))


def disable():
    p = run([HERMES, "config", "set", f"{BASE}.enabled", "false"])
    return p.returncode == 0


def enable(cfg):
    before = current()
    if before is not None:
        bad = conflicts(before, cfg)
        if bad:
            log(f"refused: existing {BASE} has operator values that differ from the reference "
                f"({', '.join(bad)}); nothing changed. Review `browseros-neo.sh hermes-snippet` and "
                "edit it with `hermes config set`, or unset the entry first.")
            return 3
        if matches(before, cfg):
            log(f"{BASE} already matches the reference and is enabled; nothing changed")
            return 0
    applied = []
    try:
        for argv in plan(cfg):
            p = run(argv)
            if p.returncode != 0:
                raise RuntimeError(f"`{shlex.join(argv[1:])}` failed: {(p.stderr or p.stdout).strip()[:300]}")
            applied.append(argv)
        after = current()
        if not matches(after, cfg):
            raise RuntimeError(f"read-back of {BASE} does not match the reference: {after!r}")
    except Exception as e:  # fail closed, then roll back what this run created
        log(f"enable failed: {e}")
        if not disable():
            log(f"ROLLBACK FAILED: run `hermes config set {BASE}.enabled false` now")
            return 4
        if before is None:
            p = run([HERMES, "config", "unset", BASE])
            log("rolled back: entry removed" if p.returncode == 0
                else f"entry left disabled (unset failed: {(p.stderr or p.stdout).strip()[:200]})")
        else:
            log("rolled back to disabled; keys this run already wrote stay (each equals the reference)")
        return 1
    log(f"{BASE} enabled ({len(applied)} native config set commands, exclusions before enable). "
        "A running gateway connects it on its next MCP config reconcile; no restart. "
        "Revoke: browseros-neo.sh hermes-disable")
    return 0


def main(argv):
    if len(argv) >= 2 and argv[1] == "disable":
        if current() is None:
            log(f"{BASE} is not configured; nothing changed")
            return 0
        if not disable():
            log(f"disable failed: run `hermes config set {BASE}.enabled false`")
            return 1
        log(f"{BASE} disabled (entry kept; a running gateway disconnects it on its next reconcile)")
        return 0
    if len(argv) == 3 and argv[1] in ("enable", "plan"):
        cfg = reference(argv[2])
        if argv[1] == "plan":
            for a in plan(cfg):
                print(shlex.join(["hermes"] + a[1:]))
            return 0
        return enable(cfg)
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
