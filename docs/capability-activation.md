# Capability activation proposal (Milestone E: proposed, not applied)

What would change in the default profile's Hermes home if the twelve
source-implemented capability skills were activated, how it was rehearsed, and
how it is undone. **Nothing here has been applied to a live home.** The tool
that copies files refuses every target except a disposable rehearsal home.

## The exact selection

`hermes/capabilities/activation/manifest.json` lists every file: source path,
destination relative to `HERMES_HOME`, SHA-256, size and mode (`0644` for all;
no executable bit, no symlink). Regenerate and compare:

```bash
python3 scripts/capability_activation.py check      # exit 1 on drift
python3 scripts/capability_activation.py manifest > hermes/capabilities/activation/manifest.json
```

| Destination | Contents |
|------|------|
| `skills/agent-ops-kit/<skill>/` | The 12 skill directories: `problem-definition`, `pm-product-discovery`, `pm-lean-prd`, `pm-competitor-research`, `gstack-office-hours`, `gstack-spec`, `gstack-plan-eng-review`, `gstack-plan-design-review`, `gstack-design-consultation`, `gstack-design-review`, `gstack-review`, `gstack-qa` (SKILL.md, method maps, MIT notices) |
| `agent-ops-kit/capability-root/` | The source root the skills call as `$KIT`: `scripts/capability_*.py`, `capability-plan.py`, `capability-select.py`, `config-get.py`, the registry, lock, routing recipes, generated routing catalog, all schemas, company/project contracts and synthetic examples, `LICENSE`, and byte copies of every file a non-planned capability's implementation digest covers (so `defined` computes `yes` there) |

The capability root sits outside `skills/`, so Hermes never scans its SKILL.md
copies as skills. Excluded on purpose: evaluation fixtures and tests, the
model-router plugin (it is a plugin, installed by `make hermes-install` or `make
specialist-install`, not part of this activation), every raw upstream file (a test proves no bundled file's hash equals a
locked upstream file), any RO-PNA text, gstack `bin/`, hooks, setup, preamble,
update, telemetry, browser and ship programs.

`make hermes-install` does not copy these 12 skills. Before writing anything,
`scripts/hermes-install.sh` (every mode except `--render-only`) fails closed:
`hermes/capabilities/` must be present, the manifest must exist, be strict JSON
(no duplicate keys) and its `skills` list must equal the registry's
source-implemented skill names, each present under `hermes/skills/`. Otherwise
it stops with "capability activation gate" and writes nothing. A deleted or
dangling `hermes/capabilities/` is refused the same way, so removing it cannot
switch the gate off. Gated skills are skipped with a logged "activation-gated"
line.

Plugins, including the model-router, install as they did before the capability
work: each `hermes/plugins/<name>/` is copied over `$HERMES_HOME/plugins/<name>/`
(existing files overwritten, no backup, files absent from the source left in
place), before the gateway step. On a fresh home the router is therefore on disk
before the gateway is installed and restarted. The router's capability seam is
inert by default: the rendered `config.yaml` sets neither
`specialist_capability_root` nor `specialist_capability_catalog_sha256`, and no
script writes them.

Running `make hermes-install` against a live home is a router rollout: it
replaces the installed model-router with this checkout's (seam included) and,
when `DISCORD_BOT_TOKEN` is set and a systemd user session exists (and it is not
run inside Hermes), runs `hermes gateway install` and `hermes gateway restart`.
That needs its own operator approval, like any router rollout. Neither the seam
nor this installer change has been approved for, or deployed to, a live home.

`scripts/test_hermes_install.sh` (fake `HOME`, stub `hermes`, `systemctl`,
`loginctl` and `curl`; no network or real service) asserts: the skip and that
other kit skills still install; on a fresh home the router installs
byte-identical to source and is present when the stubbed gateway install and
restart run, with `hermes doctor` after; an existing router is overwritten as
before (stale extra file kept, no backup); the two opt-in settings are absent by
default; and, in a private copy of the kit's git-tracked files, refusal with
nothing written for a missing, malformed, duplicate-key, skills-less, empty,
incomplete, over-broad or non-list manifest and for a deleted or dangling
`hermes/capabilities/`.

### Source-root contract

Each capability skill ends with a "Source root" section: `$KIT` is the reviewed
kit checkout or `$HERMES_HOME/agent-ops-kit/capability-root`. Every script finds
the registry, schemas and examples relative to its own location, so the default
CLI paths work from either. `capability-select.py` loads the model-router plugin
from its own tree when present, else `$HERMES_HOME/plugins/model-router`; the
installed plugin has no capability seam until the D plugin change is installed,
and the CLI then exits 2 with that reason.

### Automatic routing dependency (model-router plugin change)

Natural-language selection without the CLI needs the source model-router change
(`specialists.py`, `specialist_hook.py`, `router_config.py`) installed, plus two
settings. The live `pre_llm_call` hook (`SpecialistHook.on_pre_llm_call` →
`route()`) then reads the copied
`agent-ops-kit/capability-root/hermes/capabilities/routing-catalog.json`,
checks it against the pinned SHA-256 and against the hashes it records for the
copied `registry.json` and `routing-recipes.json`, and offers its IDs to the
SAME classifier call that already picks profiles (ai mode; no keyword
fallback, no second call, no scheduler). Selected IDs and their skills are
appended to the routing note as pending coordinator input; the coordinator
still decides, plans (`capability-plan.py`) and runs the skill. Without the
settings the hook is the profile-only router, unchanged. Unpinned, absent,
symlinked, malformed or drifted: no capability is offered, profile routing
continues. Until this plugin change is reviewed, installed and the gateway
restarted, there is no automatic capability routing, only the coordinator CLI.

```yaml
plugins:
  entries:
    model-router:
      settings:
        specialist_capability_root: <HERMES_HOME>/agent-ops-kit/capability-root   # absolute path
        specialist_capability_catalog_sha256: <sha256 printed by capability_selection.py check>
```

Rollback: delete the two keys (or set the root to `''`) and restart; or
reinstall the previous plugin files from the `specialist-install` backup.

### Prerequisite

`jsonschema>=4.10.3,<5` (`scripts/requirements-capabilities.txt`). Example
values from one reference Ubuntu 24.04 host, per interpreter:
`/usr/bin/python3` 3.12.3 with jsonschema 4.10.3 (apt `python3-jsonschema`
4.10.3-2ubuntu1); the Hermes venv Python 3.12.3 with 4.26.0. These are not
claims about your host or interpreter (other environments ship other system
versions); the manifest records them as examples, not requirements. To verify
on your host run, per interpreter,
`<python> -I -c 'import sys, jsonschema; print(sys.version.split()[0], jsonschema.__version__)'`. Nothing is installed: `preflight` and `apply` check the actual
interpreter the skills will use (`--python`) on the actual host and refuse when
jsonschema is missing or outside the range, or when that interpreter's version
does not satisfy the manifest's `python` prerequisite (`>=3.10`). Both versions
must be final releases (`X[.Y[.Z]]`); a pre-release, dev or local version (for
example `3.10.0rc1`, `4.10.3rc1`) is refused rather than approximated. The probe
runs the interpreter with `-I` and a 60 s timeout (a timeout is a refusal). Which interpreter the Hermes
terminal resolves as `python3` must be confirmed at activation.

### Configuration impact

None for the skills: no `config.yaml`, plugin enablement, cron, service or
profile change. Hermes lists the new skills in sessions started after the copy;
no restart is performed or needed for them. Automatic routing is a separate step
with its own approval: install the model-router change, add the two settings
above, restart the gateway (plugins and their settings load at startup).

## Compatibility with the installed Hermes (read-only)

`scripts/capability_compat.py --hermes-agent DIR --skills-root DIR
--installed-plugin DIR` parses the installed Hermes source as text (`ast`
literals; nothing imported or run) and checks the skills against its hard rules
(name pattern and length, description ≤ 1024, SKILL.md ≤ 100 000 chars,
supporting files ≤ 1 MiB) and linter rules (60-char prompt-index description,
name = directory, version/author/license, tags, a "When to use" section,
forbidden files, dangling references). Against Hermes `71736c9f`: hard rules
passed; the first run found 48 advisory findings (all 12 descriptions over the
60-char index width, missing author/license, no "When to use" section). Those
were fixed in source; the second run reports no findings, no name collision
with installed skills, and that the installed model-router differs from this
source only in `specialists.py` and `specialist_hook.py` (no capability seam).

## Rehearsal (disposable home only)

```bash
python3 scripts/capability_activation.py bundle --out <new-dir>/bundle   # prints manifest_sha256
# A new private 0700 directory with marker .capability-rehearsal-home {purpose, nonce}
python3 scripts/capability_activation.py preflight --target <home> --bundle <bundle> --confirm-rehearsal <nonce> --python python3
python3 scripts/capability_activation.py apply --target <home> --bundle <bundle> --confirm-rehearsal <nonce> \
    --manifest-sha256 <sha> --journal <new-file-outside-home> --python python3
python3 scripts/capability_activation.py verify --target <home> --bundle <bundle>
python3 scripts/capability_activation.py rollback --target <home> --bundle <bundle> --confirm-rehearsal <nonce> --journal <journal> --out <new-file>
```

The target guard refuses: no `--target` default; a symlink; anything not owned by
the caller with mode 0700; a missing marker or a nonce mismatch; any Hermes state
(`config.yaml`, `.env`, `auth.json`, `state.db`, `hermes-agent`, `sessions`,
`memories`, `profiles`, `kanban`, `SOUL.md`); the account's real `~/.hermes`,
`$HERMES_HOME`, anything containing them, and anything inside them except their
`cache/scratch` area.

`apply` verifies every bundled file and the approved manifest digest, holds
every manifest destination to the same rule `rollback` applies to journals (see
below: normalized segments, at most 200 characters, under one of the two
roots), so no bundle can apply something its journal could not roll back, and
requires the manifest's `skills` list to equal the skill directories actually
bundled under `skills/agent-ops-kit/` (the same-name collision check uses that
list). It records
each destination's preimage (`absent` or `identical`), and refuses before
writing on any differing file, symlinked parent (of the target too), same-named
skill elsewhere under `skills/`, or failed prerequisite. It then re-reads the
bundle and refuses unless the manifest still has the approved digest and the
same file list, and re-reads and re-hashes each bundled file at the moment it is
written. The journal must be a new file outside the target with no symlink in
its directory path; updates go through a fresh `O_EXCL|O_NOFOLLOW` temp file and
an atomic rename. Missing parent directories are created one component at a
time from the target: each existing component is opened with `O_NOFOLLOW` (a
symlink or non-directory refuses) before anything is created beneath it, and
each new directory is made relative to its checked parent. Files are written
without clobbering (temp file plus `link()`). A repeated apply writes nothing.

On any exception raised inside the process mid-apply, including
`KeyboardInterrupt` (Ctrl-C/SIGINT), it removes what it created, records
`failed-reverted`, and re-raises (a `KeyboardInterrupt` is re-raised as
itself). Each created directory is recorded as soon as `mkdir` returns. It
records `failed-partial` instead when something it created has changed, when a
removal hit an OS error (`revert.errors`), or when the interrupted step's
ownership is not known: an interrupt inside `link()` or `mkdir()`, or a temp file
that could not be removed after its destination was linked. Those paths are
listed in `revert.cleanup_unknown` and left in place. This is in-process state,
not a transaction. That is not crash
recovery. A hard kill (SIGKILL), power loss or host crash is not reverted
in-process, and neither is a second interrupt that lands during the revert
itself: the journal is written at the start (`in-progress`, no entries) and
completed only at the end, so it is left `in-progress` and does not list the
files already written, and `rollback` refuses an `in-progress` journal.
Recovery is manual and never a blind cleanup: a rehearsal home is disposable,
so discard it and make a new one; do not delete files from any other directory
on the strength of a manifest match. A live mode would need per-file
checkpoints before this could be claimed.

`rollback` validates the journal before any side effect: your own
single-link, non-symlinked regular file of at most 1 MiB outside the target,
strict JSON (no duplicate keys or NaN), exactly the journal keys and types,
status `applied`, this target, every destination a normalized relative path
(no `.`, `..`, empty or absolute segment) under `skills/agent-ops-kit/` or
`agent-ops-kit/capability-root/`, no duplicate entries or directories, SHA-256
digests and mode `0644` for created files, and `created_dirs` limited to parents
of files that journal created. The journal must name the `--bundle` manifest
digest, and every entry must be a file of that approved manifest (created entries
with its SHA-256). A journal is consumed: a successful rollback rewrites it to
`rolled-back`, a rollback that removed some files and then hit an OS error
rewrites it to `rollback-partial` and writes a `partial` report listing what was
removed and the errors (also when a directory component changes after the file
removals); neither status can be rolled back again. The report path must be new, outside the target
and not the journal; it is created exclusively before anything is removed. All
paths are opened component by component from the target with `O_NOFOLLOW`, so a
symlinked component refuses the rollback instead of being followed. It removes
only files the journal created that still have the applied bytes and mode; if
any changed it removes nothing. It also removes created directories that are
empty again.

`verify` is read-only. It refuses a target with a symlink in its path and
reports a file as mismatched when any parent component below the target is a
symlink (the apply parent check), not only when its bytes or mode differ.

Limits: this is a rehearsal tool, not a sandbox. Another process running as the
same user can still race the checks (for example swap a directory between
`apply`'s checks and its writes); the guarantees above hold against stale
(consumed) and malformed journals and against existing symlinks, not against a
concurrent same-UID writer. There is no trusted receipt binding a journal to an
apply: a same-UID forged journal can still name approved-bundle files whose
current bytes match the bundle.

Tests (`scripts/tests/test_capability_activation.py`) cover all of this,
including a caught mid-apply error, a bundle swapped after preflight, bundled
bytes changed during apply, forged and unsafe journals and report paths
(refused with `os.unlink`/`os.rmdir` replaced by inert recording mocks), a
symlinked component at rollback, a symlinked intermediate directory planted
after preflight (nothing created outside the target), a symlink planted at the
exact journal-temp name (`os.urandom` made predictable; it is neither followed
nor replaced), destinations rollback would reject, a `skills` list that differs
from the bundled directories, Python and pre-release version refusal, `verify`
through a symlinked parent, a `KeyboardInterrupt` mid-apply, changed-file
refusal and a byte/mode/absence snapshot equal to the preimage after rollback. They also run the installed
root's default CLI paths (`capability_registry.py validate|list`,
`capability-plan.py`, `capability_workflow.py validate|render` on a source
fixture, `capability-select.py` without and with a staged plugin) from the
disposable home.

## Activation gates (all open)

1. Independent review of the source change (the restricted-review boundary is
   not accepted; no unrestricted host reviewer has been used).
2. The existing secure-execution repair gates that apply to this checkout
   (for example, a canonical checkout reserved by another card).
3. Commit and push to the feature branch only after that review passes.
4. An explicit operator approval bound to the manifest SHA-256 and the target
   home, plus the interpreter that will run `$KIT/scripts`.
5. A live apply mode does not exist. After approval, either a reviewed change
   adds one (same preimage, collision, journal and rollback rules, the approved
   manifest digest as a required argument) or the operator copies the manifest
   files with those checks by hand. No restart is part of it.
6. Required for automatic natural-language routing, separate: the
   model-router change installed (`make specialist-install`, or `make
   hermes-install`, which also copies it, without a backup), the two
   `specialist_capability_*` settings bound to the approved catalog SHA-256, and
   a gateway restart, with its own review and approval. Without it, selection is
   the coordinator CLI only.

## Rollback of a live activation (proposal)

Before applying, record each destination's preimage (expected: all absent). To
undo, remove only manifest files whose installed bytes and mode still equal the
manifest; if any changed, stop and ask. Then remove the empty
`skills/agent-ops-kit/<skill>/` and `agent-ops-kit/capability-root/`
directories. Re-run `capability_compat.py` against the skill roots to confirm
no capability skill remains. No other file is touched.

## Not claimed

No approval or deployment of the model-router capability seam, no live
installation, restart, runtime acceptance, executed workflow on real
company data, automatic routing in a real Hermes session, natural-language
selection by the live classifier (its key is
not available in this environment) or company isolation.
