#!/usr/bin/env python3
"""Development-test mode of a workflow request: which path a code card's database tests take.

Settings (non-secret). The native place is Hermes' own config.yaml, written with the supported
`hermes config set` (which keeps a backup); two environment names are accepted as aliases:

    config.yaml (`$HERMES_HOME/config.yaml`)              environment alias
    agent_ops_kit.dev_test.require_dev_test_run  true|false   AOK_REQUIRE_DEV_TEST_RUN
    agent_ops_kit.dev_test.backend               compose|dev_test_run   AOK_TEST_BACKEND

Each source resolves on its own by this table (unset or empty: not set):

    REQUIRE   BACKEND        mode
    -         -              ""            default: the card text is byte-identical to before
    true      - / dev_test_run   dev_test_run  only the dev_test_run tool; no Compose path
    false     compose        compose       never dev_test_run; the project's Compose dev services
    -         compose        compose
    -         dev_test_run   dev_test_run
    true      compose        error (conflict)
    false     - / dev_test_run   error (false needs a backend that still produces test evidence)
    anything else            error (fail closed: the command exits 3, the card blocks)

A profile home (`<root>/profiles/<name>`, how Hermes runs a routed profile worker) reads its own
config.yaml and the root home's: the operator sets the pair once in the root config.yaml and the
coordinator and every profile worker see it (a routed worker does not inherit the launch .env, so
the environment aliases alone would not reach it). Two config files that select different modes
are an error.

Precedence is fail-closed, not an override: when config and environment both select a mode they
must agree, else error; one source alone decides; neither is the default. Only the exact two
environment names count; a differently-cased variant (`aok_test_backend`) is an error rather
than silently ignored. An unreadable `agent_ops_kit` section of config.yaml is an error.

Intake resolves the mode once and writes it on the request's cards as the `Dev-test-mode:`
header line (omitted for the default, so existing and default cards are unchanged). The card
header is the only durable authority from then on: no comment or record changes it. An existing
default card moves to an explicit mode only by the operator's own supported
`hermes kanban edit <id> --body` of the exact bodies `workflow.py dev-test-mode body-plan` prints
(read-only; request and identity preserved, old/new sha256). A worker whose setting names a mode
refuses a card without that mode (no fallback to the default).

What the workflow script enforces (runtime, in its own process): the resolution above; the
header value; the worker-setting/card conflict; `completion evidence` and every later
read apply the same db-tests record validation (`db_record_problem`); `completion handoff`
requires a passing db-tests record of the card's backend when the criteria select the db-tests
check; `completion block-check` refuses "no dev_test_run" as a stop on a compose card.
The script itself never runs dev_test_run, docker or tests.

What it does not enforce: which tools the agent calls, and whether a recorded command ran. A
record is the agent's report bound to the source state; its log digest shows a file of that
content existed when recorded, not that the command produced it. Nothing here is OS isolation.
Stdlib only.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Mapping, Optional

ENV_REQUIRE = "AOK_REQUIRE_DEV_TEST_RUN"
ENV_BACKEND = "AOK_TEST_BACKEND"
ENV_NAMES = (ENV_REQUIRE, ENV_BACKEND)
CONFIG_SECTION = ("agent_ops_kit", "dev_test")
CONFIG_REQUIRE = "require_dev_test_run"
CONFIG_BACKEND = "backend"
CONFIG_KEYS = {CONFIG_REQUIRE: f"{'.'.join(CONFIG_SECTION)}.{CONFIG_REQUIRE}",
               CONFIG_BACKEND: f"{'.'.join(CONFIG_SECTION)}.{CONFIG_BACKEND}"}
HEADER_LABEL = "Dev-test-mode"
HEADER_KEY = HEADER_LABEL.lower()
DEFAULT = ""
DEV_TEST_RUN = "dev_test_run"
COMPOSE = "compose"
MODES = (DEV_TEST_RUN, COMPOSE)
CLI_CHOICES = ("default", *MODES)
DB_CHECK = "db-tests"  # the completion check whose records must match an explicit mode
DEV_TEST_RUN_RE = re.compile(r"dev[_-]?test[_-]?run", re.I)  # block-check reason classification only
HEX64 = re.compile(r"^[0-9a-f]{64}$")
COUNT_KEYS = ("passed", "failed", "skipped")


class ModeError(ValueError):
    """An invalid or conflicting dev-test setting: fail closed."""


# ----------------------------------------------------------- resolution ---

def _pair(req: str, backend: str, names: tuple) -> str:
    """The mode of one source's (require, backend) pair; *names* label the two settings."""
    n_req, n_backend = names
    if req and req.lower() not in ("true", "false"):
        raise ModeError(f"{n_req}={req!r} is not true or false")
    if backend and backend not in MODES:
        raise ModeError(f"{n_backend}={backend!r} is not one of {', '.join(MODES)}")
    req = req.lower()
    if req == "true":
        if backend == COMPOSE:
            raise ModeError(f"{n_req}=true conflicts with {n_backend}=compose")
        return DEV_TEST_RUN
    if req == "false":
        if backend != COMPOSE:
            raise ModeError(f"{n_req}=false needs {n_backend}=compose (the backend that still runs the "
                            f"tests); got {backend or 'unset'}")
        return COMPOSE
    return backend


def from_env(env: Optional[Mapping[str, str]] = None) -> str:
    """The mode the two environment aliases select ("" when neither is set); ModeError otherwise."""
    env = os.environ if env is None else env
    near = sorted(k for k in env if k.upper() in ENV_NAMES and k not in ENV_NAMES)
    if near:
        raise ModeError(f"environment name(s) {', '.join(near)}: only {ENV_REQUIRE} and {ENV_BACKEND} (exact case) "
                        "are dev-test settings")
    return _pair((env.get(ENV_REQUIRE) or "").strip(), (env.get(ENV_BACKEND) or "").strip(), ENV_NAMES)


def config_path(env: Optional[Mapping[str, str]] = None) -> Path:
    env = os.environ if env is None else env
    home = (env.get("HERMES_HOME") or "").strip() or str(Path.home() / ".hermes")
    return Path(home).expanduser() / "config.yaml"


def config_paths(env: Optional[Mapping[str, str]] = None) -> list:
    """The config.yaml files this process reads: its own home's, and for a profile home
    (`<root>/profiles/<name>`, how Hermes runs a routed profile worker) also the root home's, so
    one `hermes config set` in the root reaches the coordinator and every profile worker. A
    routed worker does not inherit the launch profile's .env names (Hermes'
    strip_launch_profile_env), so the environment aliases alone would not reach it."""
    own = config_path(env)
    home = own.parent
    if home.parent.name == "profiles":
        return [own, home.parent.parent / "config.yaml"]
    return [own]


_LINE = re.compile(r"^( *)([A-Za-z0-9_.-]+|'[^']*'|\"[^\"]*\")[ \t]*:(?:[ \t]+(.*?))?[ \t]*$")


def _scalar(raw: str, where: str) -> str:
    """A plain or quoted YAML scalar as text; anything structured is refused."""
    text = raw.strip()
    if " #" in text or "\t#" in text:
        text = re.split(r"[ \t]#", text, 1)[0].rstrip()
    if not text or text in ("~", "null", "Null", "NULL"):
        return ""
    if text[0] in "'\"":
        if len(text) < 2 or text[-1] != text[0]:
            raise ModeError(f"{where}: unterminated quoted value")
        return text[1:-1]
    if text[0] in "[{&*!|>@`%":
        raise ModeError(f"{where}: {text!r} is not a plain scalar")
    return text


def read_config(path: Path) -> dict:
    """{require, backend} from the `agent_ops_kit.dev_test` block of a Hermes config.yaml ("" when
    absent). Only block mappings with scalar leaves are read; anything else in that block, or a
    duplicate or unknown key there, is a ModeError (fail closed). A missing file is unset."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {CONFIG_REQUIRE: "", CONFIG_BACKEND: ""}
    except OSError as exc:
        raise ModeError(f"{path}: unreadable ({exc})") from exc
    out, seen = {CONFIG_REQUIRE: "", CONFIG_BACKEND: ""}, set()
    stack = []  # [(indent, key)] of the enclosing mappings
    for no, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#") or line.strip() in ("---", "..."):
            continue
        indent = len(line) - len(line.lstrip(" "))
        while stack and stack[-1][0] >= indent:
            stack.pop()
        path_keys = [k for _, k in stack]
        inside = path_keys[:1] == [CONFIG_SECTION[0]]
        m = _LINE.match(line)
        if not m:
            if inside or line.lstrip().startswith(CONFIG_SECTION[0]):
                raise ModeError(f"{path}:{no}: the {CONFIG_SECTION[0]} block is not a plain block mapping")
            continue  # not ours: Hermes reads the rest
        key = m.group(2).strip("'\"")
        value = (m.group(3) or "").strip()
        value = "" if value.startswith("#") else value
        here = [*path_keys, key]
        where = f"{path}:{no} {'.'.join(here)}"
        if path_keys == list(CONFIG_SECTION):
            if key not in out:
                raise ModeError(f"{where}: unknown key (allowed: {', '.join(CONFIG_KEYS.values())})")
            if key in seen:
                raise ModeError(f"{where}: duplicate key")
            seen.add(key)
            out[key] = _scalar(value, where) if value else ""
            stack.append((indent, key))  # a nested line under a leaf fails below
            continue
        if path_keys[:len(CONFIG_SECTION)] == list(CONFIG_SECTION):
            raise ModeError(f"{where}: {'.'.join(CONFIG_SECTION)} leaves must be scalars")
        if list(CONFIG_SECTION)[:len(here)] == here and value:
            raise ModeError(f"{where}: must be a block mapping, not {value!r}")
        if not value:
            stack.append((indent, key))
    return out


def from_config(path: Optional[Path] = None) -> str:
    cfg = read_config(config_path() if path is None else path)
    return _pair(cfg[CONFIG_REQUIRE].strip(), cfg[CONFIG_BACKEND].strip(),
                 (CONFIG_KEYS[CONFIG_REQUIRE], CONFIG_KEYS[CONFIG_BACKEND]))


def from_configs(paths) -> str:
    """The mode the config files select together: each resolves on its own; explicit modes must
    agree (ModeError naming both files), else the one explicit mode, else ""."""
    chosen = {}
    for path in paths:
        try:
            mode = from_config(path)
        except ModeError as exc:
            raise ModeError(f"{path}: {exc}") from exc
        if mode:
            chosen[str(path)] = mode
    if len(set(chosen.values())) > 1:
        raise ModeError("config files disagree: " + ", ".join(f"{p} selects {m}" for p, m in chosen.items())
                        + ": make them agree or unset one")
    return next(iter(chosen.values()), "")


def local_setting(env: Optional[Mapping[str, str]] = None, config: Optional[Path] = None) -> dict:
    """{mode, source} this process's settings select: config.yaml and the environment aliases.
    Both explicit and different: ModeError. *config* None: `$HERMES_HOME/config.yaml` of *env*."""
    env = os.environ if env is None else env
    env_mode = from_env(env)
    cfg_mode = from_configs(config_paths(env) if config is None else [config])
    if env_mode and cfg_mode and env_mode != cfg_mode:
        raise ModeError(f"config.yaml {'.'.join(CONFIG_SECTION)} selects {cfg_mode}, the environment "
                        f"({ENV_REQUIRE}/{ENV_BACKEND}) selects {env_mode}: make them agree or unset one")
    mode = cfg_mode or env_mode
    source = ("config+environment" if env_mode and cfg_mode else "config" if cfg_mode
              else "environment" if env_mode else "default")
    return {"mode": mode, "source": source}


def resolve_intake(cli: Optional[str], env: Optional[Mapping[str, str]] = None,
                   config: Optional[Path] = None) -> dict:
    """{mode, source} for a new request: --dev-test-mode, else the settings. An explicit CLI
    value that differs from an explicit setting is a conflict."""
    local = local_setting(env, config)
    if cli is None or cli == "":
        return local
    if cli not in CLI_CHOICES:
        raise ModeError(f"--dev-test-mode {cli!r} is not one of {', '.join(CLI_CHOICES)}")
    mode = DEFAULT if cli == "default" else cli
    if local["mode"] and local["mode"] != mode:
        raise ModeError(f"--dev-test-mode {cli} conflicts with the {local['source']} setting {local['mode']}")
    return {"mode": mode, "source": "cli"}


def card_mode(header: Mapping[str, str]) -> str:
    """The mode a card header records ("" when absent); ModeError for an unknown value."""
    value = (header.get(HEADER_KEY) or "").strip()
    if value and value not in MODES:
        raise ModeError(f"card header {HEADER_LABEL}: {value!r} is not one of {', '.join(MODES)}")
    return value


def check_worker(mode: str, env: Optional[Mapping[str, str]] = None, config: Optional[Path] = None) -> dict:
    """The card's mode against this worker's settings. The card wins over an unset setting; an
    explicit setting that differs (including a card without a mode) is refused: fail closed."""
    local = local_setting(env, config)
    out = {"mode": mode or "default", "card": mode or "default", "environment": local["mode"] or "unset",
           "setting_source": local["source"]}
    if mode and local["mode"] and local["mode"] != mode:
        raise ModeError(f"this card records {HEADER_LABEL}: {mode}; this worker's {local['source']} setting selects "
                        f"{local['mode']}: fix the worker's configuration, the card is not changed")
    if local["mode"] and not mode:
        raise ModeError(f"this card has no {HEADER_LABEL} (the default); this worker's {local['source']} setting "
                        f"selects {local['mode']}: the default is not assumed. The operator either applies "
                        f"`workflow.py dev-test-mode body-plan <code card> --mode {local['mode']}` with "
                        "`hermes kanban edit --body`, or unsets the setting for this worker")
    return out


# ------------------------------------------------------------- evidence ---

def _int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def db_record_problem(mode: str, ev) -> Optional[str]:
    """Why a db-tests evidence record does not count on a card in *mode* (None: it counts). The
    same rule at ingestion (`completion evidence`) and at every read, so a record written by an
    older script, under another mode or by hand is judged exactly like a new one. Never raises."""
    if not isinstance(ev, dict):
        return "not a record"
    if ev.get("backend") != mode:
        return f"backend {ev.get('backend')!r}, the card's mode is {mode}"
    if ev.get("result") not in ("pass", "fail"):
        return f"result {ev.get('result')!r} is not pass or fail"
    if not isinstance(ev.get("command"), str) or not ev["command"].strip():
        return "no command"
    if not _int(ev.get("exit_code")):
        return f"exit code {ev.get('exit_code')!r} is not an integer"
    counts = ev.get("counts")
    if not isinstance(counts, dict) or set(counts) - set(COUNT_KEYS):
        return f"counts {counts!r} are not {{passed, failed, skipped}}"
    if any(not _int(v) or v < 0 for v in counts.values()):
        return f"counts {counts!r} are not non-negative integers"
    if ev["result"] == "fail":
        return None
    log = ev.get("log")
    if ev["exit_code"] != 0:
        return "a pass needs exit code 0"
    if counts.get("passed", 0) < 1 or counts.get("failed", 0) != 0:
        return "a pass needs passed >= 1 and no failed test (a run that executed no test is not evidence)"
    if not (isinstance(log, dict) and set(log) == {"sha256", "bytes"} and isinstance(log["sha256"], str)
            and HEX64.match(log["sha256"]) and _int(log["bytes"]) and log["bytes"] > 0):
        return "a pass needs the digest of a non-empty --log file"
    return None


def check_backend(mode: str, check: str, backend: str) -> None:
    """Refuse a --backend that contradicts the card (any check), or a db-tests record without one."""
    if backend and backend not in MODES:
        raise ModeError(f"--backend {backend!r} is not one of {', '.join(MODES)}")
    if not mode:
        return
    if backend and backend != mode:
        raise ModeError(f"--backend {backend} on a {HEADER_LABEL}: {mode} card: only {mode} is this card's path")
    if check == DB_CHECK and not backend:
        raise ModeError(f"{DB_CHECK} evidence on a {mode} card needs --backend {mode}")


def check_db_record(mode: str, rec: dict) -> None:
    """Ingestion: the record about to be written must pass the read-time rule."""
    if mode and rec.get("check") == DB_CHECK:
        problem = db_record_problem(mode, rec)
        if problem:
            raise ModeError(f"{DB_CHECK} evidence refused: {problem}")


def accepts(mode: str, ev) -> bool:
    """Whether a recorded db-tests evidence counts for a card in *mode* (re-checked at read)."""
    return db_record_problem(mode, ev) is None


# ---------------------------------------------------------------- body ---

def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ CLI ---

def config_plan(require: str, backend: str, env: Optional[Mapping[str, str]] = None) -> dict:
    """The exact backed-up `hermes config set` writes for a pair; validated, never executed."""
    mode = _pair(require.strip(), backend.strip(), (CONFIG_KEYS[CONFIG_REQUIRE], CONFIG_KEYS[CONFIG_BACKEND]))
    path = config_paths(env)[-1]  # the root home's config.yaml: shared by every profile worker
    prefix = [] if path == config_path(env) else ["env", f"HERMES_HOME={path.parent}"]
    cmds = [["cp", "-p", str(path), f"{path}.bak.dev-test-mode"]]
    for key, value in ((CONFIG_REQUIRE, require.strip()), (CONFIG_BACKEND, backend.strip())):
        cmds.append([*prefix, "hermes", "config", "set", CONFIG_KEYS[key], value] if value
                    else [*prefix, "hermes", "config", "unset", CONFIG_KEYS[key]])
    cmds.append([sys.executable, str(Path(__file__).resolve()), "check"])
    return {"mode": mode or "default", "config": str(path), "executed": False,
            "commands": [" ".join(shlex.quote(a) for a in c) for c in cmds],
            "notes": ["proposal only: run it after the operator approves; it touches only these two keys",
                      "running workers read config.yaml on each command; cards keep their recorded mode",
                      "`make hermes-install --force` re-renders config.yaml from the kit template and drops these "
                      "keys (back to the default) unless they are set again"]}


def report(env: Optional[Mapping[str, str]] = None) -> dict:
    """What `check` prints: the effective mode and its source, the config files read with only
    their two dev_test keys, and which alias names are set (names only: no environment value,
    no other variable). ModeError when the settings are invalid or disagree."""
    env = os.environ if env is None else env
    local = local_setting(env)
    files = []
    for path in config_paths(env):
        cfg = read_config(path)
        files.append({"path": str(path), "exists": path.is_file(),
                      "settings": {CONFIG_KEYS[k]: v.strip() for k, v in cfg.items() if v.strip()}})
    return {"mode": local["mode"] or "default", "source": local["source"], "config": files,
            "environment_names": [n for n in ENV_NAMES if (env.get(n) or "").strip()]}


def main(argv=None) -> int:
    """`check`: print the mode this process's settings select and where from (exit 0), or the
    error (exit 3).
    `config-plan --require R --backend B`: print the backed-up config writes for that pair."""
    argv = sys.argv[1:] if argv is None else argv
    try:
        if argv == ["check"]:
            print(json.dumps(report()))
            return 0
        if argv[:1] == ["config-plan"] and len(argv) == 5 and argv[1] == "--require" and argv[3] == "--backend":
            print(json.dumps(config_plan(argv[2], argv[4]), indent=2))
            return 0
    except ModeError as exc:
        print(f"dev-test mode: {exc}", file=sys.stderr)
        return 3
    print("usage: dev_test_mode.py check | config-plan --require <true|false|''> --backend <compose|dev_test_run|''>",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
