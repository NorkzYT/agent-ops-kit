#!/usr/bin/env python3
"""model-floor-migrate — move existing kit installs onto the GPT model floor.

Every kit-managed GPT call uses version 6.1 or newer, family Terra or higher
(scripts/lib/model_floor.py). Installs made before the floor can still carry
below-floor ids (``gpt-5.6-luna``, ``gpt-5.6-sol``, ``gpt-6-astra`` ...) in the
kit ``.env``, the Hermes home and every profile created by
``hermes profile create --clone``. The router and the installer refuse those
values; this tool plans and applies the update, and ``--check`` is the
fail-closed preflight ``make hermes-restart`` and the installer run.

    model-floor-migrate.py [--env-file F] [--hermes-home H] [--profile N ...]
                           [--no-home | --profiles-only] [--json] # plan (dry-run)
    model-floor-migrate.py ... --check                            # preflight: exit 0 only when clean
    model-floor-migrate.py ... --apply --approve <plan-digest>    # write

Scope: known settings only, parsed structurally (PyYAML node graph for
``config.yaml``, a strict dotenv reader for ``.env``). Nothing else is
rewritten, byte-for-byte:

* single model values -> ``gpt-6.1-terra`` (the Terra default; never Sol or
  Astra, no automatic upgrade): ``model.default``/``model.model``,
  ``auxiliary.<task>.model``, ``delegation.model``, the legacy
  ``compression.summary_model`` and ``smart_model_routing.cheap_model.model``,
  model-router ``specialist_classifier_model``; ``.env`` HERMES_MODEL,
  HONCHO_MODEL, AUX_MODEL, AUX_COMPRESSION_MODEL,
  MODEL_ROUTER_SPECIALIST_CLASSIFIER_MODEL, KNOWLEDGE_CLASSIFY_MODEL.
* fallbacks: a below-floor GPT entry is removed, never replaced
  (``fallback_providers``, legacy ``fallback_model``,
  ``auxiliary.<task>.fallback_chain``, ``delegation.fallback_providers``; an
  emptied list becomes ``[]``, an emptied single ``fallback_model`` ``{}``);
  ``.env`` AUX_COMPRESSION_FALLBACK_MODEL becomes ``none`` and
  AUX_FALLBACK_MODEL empty.
* lists: below-floor ids are dropped from model-router
  ``available_gpt_models`` and ``.env`` MODEL_ROUTER_GPT_MODELS.

Unresolved (reported, never rewritten, and never "clean"): an id that names a
GPT model but is not a strict family id (refused: ``gpt-6-1-terra``,
``GPT-6.1-Terra``, ``gpt-4o``, ``azure-gpt-4o``); a below-floor single value
behind provider wrappers (refused: ``openai/gpt-5.6-sol``,
refused: ``openrouter/openai/gpt-5.6-sol``, refused: ``openai:gpt-5.6-sol``;
the replacement depends on what that provider serves); a below-floor id at
any other setting; value syntax this reader does not model (block scalars, ``$`` expansion, unbalanced quotes, unparseable
YAML); a structural edit that does not read back as exactly the intended data;
and, with ``--env-file``, a below-floor HONCHO_MODEL exported in the shell
(docker compose prefers the shell over ``.env``). Plan and ``--check`` exit 5
while anything is unresolved and ``--apply`` refuses. Provider-prefixed ids
at or above the floor (``openai/gpt-6.1-terra``) and non-GPT ids (Claude,
local ``gpt4all``/``gpt-oss``) are kept literally.

Safety: the plan is the default and reads only. ``--apply`` needs the plan
digest printed by the dry-run on the same invocation line (approval is never
read from the environment or a file, and a changed plan refuses). Each changed
file is re-read without following symlinks and must still hash to the planned
bytes, is backed up beside itself (``<file>.bak.model-floor.<UTC>``, same
mode, O_EXCL|O_NOFOLLOW, never overwritten), replaced atomically through a
fresh exclusive temp file and read back (the re-plan of the written file must
be empty). Symlinked files, hidden or non-canonical profile directories
(``.trash``, ``name.deleted``, ``name~``) and missing profiles are skipped and
reported. Output carries file paths, key names and model ids only: never
another value from ``.env`` or ``config.yaml``. Running it again after an apply
plans nothing (idempotent). Rollback: ``mv <file>.bak.model-floor.<UTC>
<file>`` for each written file (printed after an apply), then
``make hermes-restart``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import model_floor as mf  # noqa: E402

try:
    import yaml
except ImportError:  # refused below: no regex fallback for config.yaml
    yaml = None

TERRA = mf.DEFAULT_MODEL
ENV_SINGLE = ("HERMES_MODEL", "HONCHO_MODEL", "AUX_MODEL", "AUX_COMPRESSION_MODEL",
              "MODEL_ROUTER_SPECIALIST_CLASSIFIER_MODEL", "KNOWLEDGE_CLASSIFY_MODEL")
ENV_FALLBACK = {"AUX_COMPRESSION_FALLBACK_MODEL": "none", "AUX_FALLBACK_MODEL": ""}
ENV_LIST = ("MODEL_ROUTER_GPT_MODELS",)
# docker-compose.yml interpolates these; the shell environment beats .env there.
COMPOSE_KEYS = ("HONCHO_MODEL",)
ROUTER = ("plugins", "entries", "model-router", "settings")
YAML_SINGLE = (("model", "default"), ("model", "model"), ("auxiliary", "*", "model"),
               ("delegation", "model"), ("compression", "summary_model"),
               ("smart_model_routing", "cheap_model", "model"), ROUTER + ("specialist_classifier_model",))
YAML_ENTRIES = (("fallback_providers",), ("fallback_model",), ("auxiliary", "*", "fallback_chain"),
                ("delegation", "fallback_providers"))
YAML_IDS = (ROUTER + ("available_gpt_models",),)
PROFILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_GPTISH = re.compile(r"gpt-[0-9]|\bluna\b", re.IGNORECASE)


@dataclass
class Change:
    file: str
    key: str
    old: str
    new: str
    action: str  # replace | remove-entry | filter


@dataclass
class Unresolved:
    file: str
    key: str
    model: str
    reason: str


@dataclass
class Skip:
    target: str
    reason: str


def _shown_id(value: str) -> str:
    """A model id for output; anything that does not look like one is withheld."""
    return value if mf.floor_status(value) != "other" and len(value) <= 80 else "(value withheld)"


# ------------------------------------------------------------------ .env ----
_ENV_LINE = re.compile(r"^(\s*(?:export\s+)?)([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
_UNQUOTED_OK = re.compile(r"[^\s'\"`$\\#][^\s'\"`$\\]*")


def parse_env_value(raw: str) -> Optional[Tuple[str, str, str]]:
    """``(value, quote, suffix)`` for the dotenv subset shell ``source``,
    docker compose and env_file_get agree on, else ``None`` (unclear):
    ``KEY=`` / ``KEY=v`` / ``KEY="v"`` / ``KEY='v'``, each optionally followed by
    whitespace and a ``#`` comment (kept as ``suffix``)."""
    if raw == "":
        return "", "", ""
    if raw[0] in "'\"":
        q = raw[0]
        end = raw.find(q, 1)
        if end < 0:
            return None
        inner, rest = raw[1:end], raw[end + 1:]
        if q == '"' and re.search(r"[\\$`]", inner):
            return None
        if rest and not re.fullmatch(r"\s+#.*|\s*", rest):
            return None
        return inner, q, rest
    m = re.match(r"(.*?)(\s+#.*)?$", raw)
    value, suffix = m.group(1), m.group(2) or ""
    if value and not _UNQUOTED_OK.fullmatch(value):
        return None
    return value, "", suffix


def plan_env(text: str, label: str) -> Tuple[List[Change], List[Unresolved], str]:
    changes: List[Change] = []
    unresolved: List[Unresolved] = []
    out = []
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        eol = line[len(body):]
        m = _ENV_LINE.match(body)
        if not m or m.group(2) not in ENV_SINGLE + ENV_LIST + tuple(ENV_FALLBACK):
            out.append(line)
            continue
        prefix, key, raw = m.groups()
        parsed = parse_env_value(raw)
        if parsed is None:
            if _GPTISH.search(raw):
                unresolved.append(Unresolved(label, key, "", "value syntax not understood (quotes/expansion)"))
            out.append(line)
            continue
        value, q, suffix = parsed
        new: Optional[str] = None
        action = ""
        if key in ENV_LIST:
            items = mf.split_list(value)
            unknown = [i for i in items if mf.floor_status(i) == "unknown"]
            bad = [i for i in items if mf.floor_status(i) == "below"]
            for i in unknown:
                unresolved.append(Unresolved(label, key, _shown_id(i), "not a recognised GPT model id"))
            if bad and not unknown:
                new, action = ",".join(i for i in items if i not in bad), "filter"
                value = ",".join(bad)
        else:
            status = mf.floor_status(value)
            if status == "unknown":
                unresolved.append(Unresolved(label, key, _shown_id(value), "not a recognised GPT model id"))
            elif status == "below" and key in ENV_FALLBACK:
                new, action = ENV_FALLBACK[key], "remove-entry"
            elif status == "below" and mf.provider_split(value)[0]:
                unresolved.append(Unresolved(label, key, value, "provider-prefixed id: choose an eligible "
                                                                "id that provider serves"))
            elif status == "below":
                new, action = TERRA, "replace"
        if new is None:
            out.append(line)
            continue
        changes.append(Change(label, key, value, new, action))
        out.append(f"{prefix}{key}={q}{new}{q}{suffix}{eol}")
    return changes, unresolved, "".join(out)


def env_file_get(path: Path, key: str) -> str:
    """The value scripts/lib.sh ``env_file_get`` returns: the LAST assignment."""
    found = ""
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            m = _ENV_LINE.match(line.rstrip("\r"))
            if m and m.group(2) == key:
                parsed = parse_env_value(m.group(3))
                found = parsed[0] if parsed else m.group(3).strip("'\"")
    except (OSError, UnicodeDecodeError):
        pass
    return found


# ----------------------------------------------------------- config.yaml ----
def _match(path: Tuple[Any, ...], patterns: Sequence[Tuple[str, ...]]) -> bool:
    return any(len(path) == len(p) and all(a == "*" or a == b for a, b in zip(p, path)) for p in patterns)


def _end(node: Any) -> int:
    """Index one past the node's last character (block collections end at
    their last descendant, not at the next token)."""
    if isinstance(node, yaml.ScalarNode) or getattr(node, "flow_style", False):
        return node.end_mark.index
    if isinstance(node, yaml.MappingNode):
        kids = [n for kv in node.value for n in kv]
    else:
        kids = list(node.value)
    return max([_end(k) for k in kids] or [node.end_mark.index])


class _YamlPlan:
    def __init__(self, text: str, label: str):
        self.text, self.label = text, label
        self.changes: List[Change] = []
        self.unresolved: List[Unresolved] = []
        self.edits: List[Tuple[int, int, str]] = []
        self.handled: set = set()
        self.seen: set = set()

    # text helpers
    def line_start(self, i: int) -> int:
        return self.text.rfind("\n", 0, i) + 1

    def line_after(self, i: int) -> int:
        j = self.text.find("\n", i)
        return len(self.text) if j < 0 else j + 1

    def unresolve(self, path, model: str, reason: str) -> None:
        self.unresolved.append(Unresolved(self.label, ".".join(map(str, path)), _shown_id(model), reason))

    # edit primitives
    def replace_scalar(self, node, new: str, path) -> bool:
        if node.style not in (None, "", "'", '"') or node.start_mark.line != node.end_mark.line:
            self.unresolve(path, node.value, "scalar style not editable (block/multi-line)")
            return False
        q = node.style or ""
        self.edits.append((node.start_mark.index, node.end_mark.index, f"{q}{new}{q}"))
        return True

    def empty_value(self, key, value, literal: str, path) -> bool:
        if getattr(value, "flow_style", False) or isinstance(value, yaml.ScalarNode):
            self.edits.append((value.start_mark.index, _end(value), literal))
            return True
        tail = self.text[key.end_mark.index:self.line_after(key.end_mark.index)]
        m = re.match(r"(\s*:)", tail)
        if not m or not re.fullmatch(r"\s*:\s*(#[^\n]*)?\n?", tail):
            self.unresolve(path, "", "layout not editable")
            return False
        colon = key.end_mark.index + len(m.group(1))
        self.edits.append((colon, colon, f" {literal}"))
        self.edits.append((self.line_after(key.end_mark.index), self.line_after(_end(value) - 1), ""))
        return True

    def drop_items(self, key, seq, drop: List[int], path) -> bool:
        items = seq.value
        if len(drop) == len(items):
            return self.empty_value(key, seq, "[]", path)
        if seq.flow_style:
            spans = [self.text[it.start_mark.index:_end(it)] for it in items]
            between = self.text[seq.start_mark.index:seq.end_mark.index]
            if "#" in "".join(between.replace(s, "") for s in spans) or "\n" in between:
                self.unresolve(path, "", "flow list layout not editable (comments/multi-line)")
                return False
            kept = [s for i, s in enumerate(spans) if i not in drop]
            self.edits.append((seq.start_mark.index, seq.end_mark.index, "[" + ", ".join(kept) + "]"))
            return True
        for i in drop:
            it = items[i]
            ls = self.line_start(it.start_mark.index)
            if not re.fullmatch(r"[ \t]*-[ \t]+", self.text[ls:it.start_mark.index]):
                self.unresolve(path, "", "list item layout not editable")
                return False
            self.edits.append((ls, self.line_after(_end(it) - 1), ""))
        return True

    # policy
    def single(self, path, node) -> None:
        if not isinstance(node, yaml.ScalarNode):
            return  # a mapping/list here is left to the generic scan
        self.handled.add(id(node))
        status = mf.floor_status(node.value)
        if status == "unknown":
            self.unresolve(path, node.value, "not a recognised GPT model id")
        elif status == "below" and mf.provider_split(node.value)[0]:
            self.unresolve(path, node.value, "provider-prefixed id: choose an eligible id that provider serves")
        elif status == "below" and self.replace_scalar(node, TERRA, path):
            self.changes.append(Change(self.label, ".".join(path), node.value, TERRA, "replace"))

    def _entry_model(self, item) -> Optional[Any]:
        if isinstance(item, yaml.ScalarNode):
            return item
        if isinstance(item, yaml.MappingNode):
            for k, v in item.value:
                if isinstance(k, yaml.ScalarNode) and k.value == "model" and isinstance(v, yaml.ScalarNode):
                    return v
        return None

    def entries(self, path, key, node) -> None:
        if isinstance(node, yaml.MappingNode):  # legacy single fallback_model dict
            model = self._entry_model(node)
            if model is None:
                return
            self.handled.add(id(model))
            status = mf.floor_status(model.value)
            if status == "unknown":
                self.unresolve(path, model.value, "not a recognised GPT model id")
            elif status == "below" and self.empty_value(key, node, "{}", path):
                self.changes.append(Change(self.label, ".".join(path), model.value, "", "remove-entry"))
            return
        if not isinstance(node, yaml.SequenceNode):
            return
        drop, removed, blocked = [], [], False
        for i, item in enumerate(node.value):
            model = self._entry_model(item)
            if model is None:
                continue
            self.handled.add(id(model))
            status = mf.floor_status(model.value)
            if status == "unknown":
                self.unresolve(path, model.value, "not a recognised GPT model id")
                blocked = True
            elif status == "below":
                drop.append(i)
                removed.append(model.value)
        if drop and not blocked and self.drop_items(key, node, drop, path):
            for m in removed:
                self.changes.append(Change(self.label, ".".join(path), m, "", "remove-entry"))

    def ids(self, path, key, node) -> None:
        if not isinstance(node, yaml.SequenceNode):
            if isinstance(node, yaml.ScalarNode) and mf.below_floor(node.value):
                self.handled.add(id(node))
                self.unresolve(path, node.value, "expected a list of model ids")
            return
        drop, removed, blocked = [], [], False
        for i, item in enumerate(node.value):
            if not isinstance(item, yaml.ScalarNode):
                continue
            self.handled.add(id(item))
            status = mf.floor_status(item.value)
            if status == "unknown":
                self.unresolve(path, item.value, "not a recognised GPT model id")
                blocked = True
            elif status == "below":
                drop.append(i)
                removed.append(item.value)
        if drop and not blocked and self.drop_items(key, node, drop, path):
            self.changes.append(Change(self.label, ".".join(path), ",".join(removed), "", "filter"))

    def walk(self, node, path=()) -> None:
        if id(node) in self.seen:  # an alias: the anchored node was already visited
            if _GPTISH.search(yaml.serialize(node)):
                self.unresolve(path, "", "YAML alias/anchor on a model setting is not editable")
            return
        self.seen.add(id(node))
        if isinstance(node, yaml.MappingNode):
            for k, v in node.value:
                if not isinstance(k, yaml.ScalarNode):
                    continue
                p = path + (k.value,)
                if _match(p, YAML_SINGLE):
                    self.single(p, v)
                elif _match(p, YAML_ENTRIES):
                    self.entries(p, k, v)
                elif _match(p, YAML_IDS):
                    self.ids(p, k, v)
                self.walk(v, p)
        elif isinstance(node, yaml.SequenceNode):
            for i, v in enumerate(node.value):
                self.walk(v, path + (i,))
        elif isinstance(node, yaml.ScalarNode) and id(node) not in self.handled:
            if node.tag == "tag:yaml.org,2002:str" and mf.below_floor(node.value):
                self.unresolve(path, node.value, "GPT id below the floor at a setting this tool does not "
                                                 "manage: edit it by hand")


def _expected(data: Any, changes: List[Change]) -> Any:
    """The intended data, computed on the parsed objects (independent of the
    text edits): the readback of the edited file must equal it."""
    def get(path):
        cur = data
        for p in path:
            cur = cur[p]
        return cur

    def setp(path, value):
        get(path[:-1])[path[-1]] = value

    def model_of(e):
        return e.get("model") if isinstance(e, dict) else e

    for c in changes:
        path = tuple(c.key.split("."))
        if c.action == "replace":
            setp(path, c.new)
        elif c.action == "remove-entry":
            cur = get(path)
            if isinstance(cur, dict):
                setp(path, {})
            else:
                drop = [i for i, e in enumerate(cur) if model_of(e) == c.old]
                if drop:
                    del cur[drop[0]]
        elif c.action == "filter":
            bad = set(c.old.split(","))
            setp(path, [x for x in get(path) if x not in bad])
    return data


def plan_yaml(text: str, label: str) -> Tuple[List[Change], List[Unresolved], str]:
    if yaml is None:
        return [], [Unresolved(label, "(file)", "", "PyYAML not available: cannot parse config safely")], text
    try:
        docs = list(yaml.compose_all(text, Loader=yaml.SafeLoader))
        data = yaml.safe_load(text) if len(docs) == 1 else None
    except yaml.YAMLError:
        return [], [Unresolved(label, "(file)", "", "not parseable as YAML")], text
    if not docs or docs[0] is None:
        return [], [], text
    if len(docs) > 1:
        return [], [Unresolved(label, "(file)", "", "multi-document YAML")], text
    plan = _YamlPlan(text, label)
    plan.walk(docs[0])
    if not plan.changes:
        return [], plan.unresolved, text
    edits = sorted(plan.edits)
    for (a0, a1, _), (b0, _b1, _x) in zip(edits, edits[1:]):
        if b0 < a1:
            plan.unresolved.append(Unresolved(label, "(file)", "", "overlapping edits"))
            return [], plan.unresolved, text
    out, pos = [], 0
    for a, b, rep in edits:
        out.append(text[pos:a] + rep)
        pos = b
    new = "".join(out) + text[pos:]
    try:
        ok = yaml.safe_load(new) == _expected(data, plan.changes)
    except (yaml.YAMLError, KeyError, IndexError, TypeError):
        ok = False
    if not ok:
        plan.unresolved.append(Unresolved(label, "(file)", "", "edit did not read back as the intended data"))
        return [], plan.unresolved, text
    return plan.changes, plan.unresolved, new


# --------------------------------------------------------------- targets ----
def resolve_home(args) -> Tuple[Path, List[str]]:
    """The same Hermes home the installer writes: --hermes-home, else
    HERMES_HOME from the kit .env (last assignment), else ~/.hermes. Without
    --env-file the runtime's $HERMES_HOME counts. A differing process
    $HERMES_HOME is reported, never silently preferred."""
    notes: List[str] = []
    proc = os.environ.get("HERMES_HOME", "").strip()
    if args.hermes_home:
        home = args.hermes_home
    elif args.env_file:
        home = env_file_get(Path(args.env_file), "HERMES_HOME") or str(Path.home() / ".hermes")
    else:
        home = proc or str(Path.home() / ".hermes")
    home_p = Path(home).expanduser()
    if proc and Path(proc).expanduser() != home_p:
        notes.append(f"process HERMES_HOME={proc} differs from the planned home {home_p}; "
                     f"plan it too with --hermes-home {proc}")
    return home_p, notes


def discover(args) -> Tuple[List[Tuple[Path, str, str]], List[Skip], List[str]]:
    targets: List[Tuple[Path, str, str]] = []
    skips: List[Skip] = []
    if args.env_file:
        targets.append((Path(args.env_file), "env", "env-file"))
    if args.no_home:
        return targets, skips, []
    home, notes = resolve_home(args)
    if not home.is_dir():
        skips.append(Skip(str(home), "hermes home not found"))
        return targets, skips, notes
    if not args.profiles_only:
        targets += [(home / "config.yaml", "yaml", "home"), (home / ".env", "env", "home")]
    pdir = home / "profiles"
    if args.profile:
        names = list(dict.fromkeys(args.profile))
    else:
        names = sorted(p.name for p in pdir.iterdir()) if pdir.is_dir() and not pdir.is_symlink() else []
    for name in names:
        where = pdir / name
        if not PROFILE_NAME.fullmatch(name):
            skips.append(Skip(f"profiles/{name}", "hidden or non-canonical profile directory"))
        elif where.is_symlink():
            skips.append(Skip(f"profiles/{name}", "symlinked profile directory"))
        elif not where.is_dir():
            skips.append(Skip(f"profiles/{name}", "profile not found"))
        else:
            targets += [(where / "config.yaml", "yaml", f"profile:{name}"),
                        (where / ".env", "env", f"profile:{name}")]
    return targets, skips, notes


def _read_nofollow(path: Path) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
    with os.fdopen(fd, "rb") as fh:
        if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
            raise OSError("not a regular file")
        return fh.read()


def plan_file(path: Path, kind: str, label: str):
    shown = f"{label}:{path.name}"
    if path.is_symlink():
        return [], [], None, None, Skip(shown, "symlink (not followed)")
    if not path.exists():
        return [], [], None, None, None
    try:
        raw = _read_nofollow(path)
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return [], [], None, None, Skip(shown, "unreadable or not a regular file")
    changes, unresolved, new = (plan_env if kind == "env" else plan_yaml)(text, shown)
    return changes, unresolved, (new if changes else None), hashlib.sha256(raw).hexdigest(), None


def digest(plans) -> str:
    h = hashlib.sha256()
    for path, changes, sha in plans:
        h.update(str(path.resolve()).encode() + b"\0" + sha.encode())
        for c in changes:
            h.update(json.dumps(asdict(c), sort_keys=True).encode())
    return h.hexdigest()[:16]


def write_atomic(path: Path, text: str, planned_sha: str) -> Path:
    """Back up and replace ``path`` without following a symlink at the file,
    its backup or its temp name; refuses when the file changed since the plan."""
    if path.is_symlink() or hashlib.sha256(_read_nofollow(path)).hexdigest() != planned_sha:
        raise RuntimeError("file changed since the plan")
    mode = stat.S_IMODE(os.lstat(path).st_mode)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    n = 0
    while True:
        backup = path.with_name(f"{path.name}.bak.model-floor.{stamp}" + (f".{n}" if n else ""))
        try:
            fd = os.open(backup, flags, mode)
            break
        except FileExistsError:
            n += 1
    with os.fdopen(fd, "wb") as fh:
        fh.write(_read_nofollow(path))
        fh.flush()
        os.fsync(fh.fileno())
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.model-floor.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            os.fchmod(fh.fileno(), mode)
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        if hashlib.sha256(_read_nofollow(path)).hexdigest() != planned_sha:
            raise RuntimeError("file changed during the write")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    dfd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)
    return backup


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--env-file", help="kit .env to plan (not touched unless given)")
    ap.add_argument("--hermes-home", help="Hermes home (default: HERMES_HOME from --env-file like the "
                                          "installer, else ~/.hermes; without --env-file $HERMES_HOME)")
    ap.add_argument("--profile", action="append", help="only these profiles (repeatable; default: all)")
    ap.add_argument("--no-home", action="store_true", help="plan --env-file only")
    ap.add_argument("--profiles-only", action="store_true",
                    help="skip the Hermes home's own config.yaml/.env (profiles only)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="write the plan (needs --approve)")
    mode.add_argument("--check", action="store_true",
                      help="preflight: exit 0 only when nothing is left to change or resolve")
    ap.add_argument("--approve", default="", help="plan digest from the dry-run of this same plan")
    ap.add_argument("--json", action="store_true", help="machine-readable plan/result")
    args = ap.parse_args(argv)

    targets, skips, notes = discover(args)
    plans = []
    unresolved: List[Unresolved] = []
    if args.check:  # the preflight cannot vouch for what it did not read
        unresolved += [Unresolved(s.target, "(file)", "", f"not verified: {s.reason}") for s in skips
                       if s.reason != "hidden or non-canonical profile directory"]
    for path, kind, label in targets:
        changes, unres, new, sha, skip = plan_file(path, kind, label)
        if skip:
            skips.append(skip)
            if args.check and skip.reason != "hidden or non-canonical profile directory":
                unresolved.append(Unresolved(skip.target, "(file)", "", f"not verified: {skip.reason}"))
        unresolved += unres
        if changes:
            plans.append((path, changes, new, kind, sha, label))
    if args.env_file:
        for key in COMPOSE_KEYS:
            value = os.environ.get(key, "")
            if mf.below_floor(value):
                unresolved.append(Unresolved("process-environment", key, _shown_id(value),
                                             "exported in this shell; docker compose prefers it over .env: "
                                             f"unset {key}"))
    plan_digest = digest((p, c, sha) for p, c, _, _, sha, _ in plans)
    changes = [c for _, cs, _, _, _, _ in plans for c in cs]
    report: Dict[str, Any] = {
        "digest": plan_digest if plans else "", "changes": [asdict(c) for c in changes],
        "unresolved": [asdict(u) for u in unresolved], "skipped": [asdict(s) for s in skips],
        "notes": notes, "applied": [],
        "mode": "apply" if args.apply else "check" if args.check else "plan"}

    def emit(status: int) -> int:
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True))
            return status
        for s in skips:
            print(f"skip    {s.target}: {s.reason}")
        for n in notes:
            print(f"note    {n}")
        for c in changes:
            arrow = f"{c.old} -> {c.new}" if c.new else f"{c.old} -> (removed)"
            print(f"{c.action:<12} {c.file} {c.key}: {arrow}")
        for u in unresolved:
            print(f"unresolved   {u.file} {u.key}{': ' + u.model if u.model else ''} ({u.reason})")
        for a in report["applied"]:
            print(f"wrote   {a['file']} (backup {a['backup']}, readback ok)")
        if report["applied"]:
            print("rollback: for each file above, mv <file>.bak.model-floor.<stamp> <file>; then make hermes-restart")
        if unresolved:
            print(f"model floor: NOT clean — {len(unresolved)} setting(s) need a manual fix (above)"
                  + ("; nothing was written" if args.apply else ""))
        elif not plans:
            print("model floor: nothing to change")
        elif args.check:
            print("model floor: NOT clean — run the plan, then --apply --approve <digest>")
        elif not args.apply:
            print(f"plan digest: {plan_digest}\napply with: --apply --approve {plan_digest} (same arguments)")
        return status

    if unresolved:
        return emit(5)
    if args.check:
        return emit(5 if plans else 0)
    if not args.apply or (not plans and not args.approve):
        return emit(0)
    if args.approve != plan_digest:
        report["error"] = "approval digest does not match the current plan; re-run the dry-run"
        print(report["error"], file=sys.stderr)
        return emit(3)
    for path, file_changes, new, kind, sha, _label in plans:
        try:
            backup = write_atomic(path, new, sha)
        except (OSError, RuntimeError) as exc:
            report["error"] = f"refused to write {file_changes[0].file}: {exc}"
            print(report["error"], file=sys.stderr)
            return emit(3)
        text = _read_nofollow(path).decode("utf-8")
        again, unres, _ = (plan_env if kind == "env" else plan_yaml)(text, "readback")
        if again or unres or text != new:
            report["error"] = f"readback failed for {file_changes[0].file}; backup kept at {backup.name}"
            print(report["error"], file=sys.stderr)
            return emit(4)
        report["applied"].append({"file": file_changes[0].file, "backup": backup.name})
    return emit(0)


if __name__ == "__main__":
    sys.exit(main())
