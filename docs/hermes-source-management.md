# Hermes source management

Agent-Ops-Kit (`/opt/repos/agent-ops-kit`) is the canonical source for the
custom Hermes setup: profiles, SOUL/instruction templates, skills, Kanban
workflow, execution/security config, dev and staging infrastructure, runtime
patches with their metadata, install/upgrade/rollback scripts, and
compatibility tests. The installed runtime (`~/.hermes/hermes-agent`) runs
Hermes; it is not a workspace.

## Rules

1. **The installed runtime is read-only.** Inspect it for diagnostics only.
   That covers every path to it: the shell, direct file tools, symlinks,
   `$HERMES_HOME`, bind mounts. Never edit, test, reset, stash, clean, commit
   or switch branches there to "restore" a state.
2. **Classify before you change anything.**
   - Config, prompt, skill, or something a supported extension point (plugin,
     hook, skill, config key, MCP) can do: change it in the kit only.
   - Native Hermes defect: write a narrow patch in a separate isolated checkout
     pinned to an exact upstream revision (see below).
   - Infrastructure or privileged execution: add reviewed source and
     provisioning artifacts to the kit. Installing them needs separate
     authorization.
3. **Unrelated dirty files are never touched.** Don't overwrite them or fold
   them into a patch.
4. **Review and deployment are separate.** A reviewed patch is not an approved
   install. Nothing auto-applies patches or auto-restarts the gateway. Live
   restarts follow the existing operator approval procedure
   (`make hermes-restart`, from the operator's terminal).

## Native patch workflow

1. Record the **pinned upstream base**: the full 40-char commit of upstream
   Hermes (a tag or `main` commit), plus the affected component.
2. Create the isolated checkout outside `~/.hermes`, e.g.
   `git worktree add` from a separate clone at that commit (never from the
   installed checkout), or a fresh `git clone` + `git checkout <sha>`.
3. Apply the kit's existing ordered patches there with the existing manager
   (no second installer):
   ```bash
   scripts/hermes-runtime-patch.sh status --src /path/to/isolated
   scripts/hermes-runtime-patch.sh apply  --src /path/to/isolated
   ```
   Every operation runs at the canonical git toplevel of `--src` (a
   subdirectory is accepted and resolved; git apply run in a subdirectory
   would silently skip paths outside it and report the patch `applied`).
   Every git call runs with `GIT_OPTIONAL_LOCKS=0` and `core.fsmonitor=false`,
   and caller `GIT_*` variables (`GIT_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE`,
   ...) are dropped, so `status`/`check` don't refresh the installed index.
   `apply` refuses:
   - a missing `--src`;
   - when this user's home can't be resolved from the passwd database
     (`getent passwd $(id -u)`): installed identity is unverified, so apply
     is disabled entirely, with no fallback to `$HOME`;
   - a checkout whose path, dev/inode (bind mount of the root or an
     ancestor), git dir or git common dir matches, is inside, or contains any
     installed location: `<passwd home>/.hermes/hermes-agent`,
     `$HERMES_REAL_HOME/.hermes/hermes-agent`, `$HOME/.hermes/hermes-agent`,
     `$HERMES_HOME/hermes-agent`, the profile root of
     `$HERMES_HOME=<root>/profiles/<name>`, and every `HERMES_HOME` in the kit
     `.env` and in `ENV_FILE`. A profile session (`HOME=$HERMES_HOME/home`)
     therefore no longer hides the installed runtime, and setting `ENV_FILE`
     no longer hides the kit's own `.env` (both are read). A relative
     `ENV_FILE` is read under the kit root (where the installers resolve it)
     and under the caller's cwd;
   - an explicit `ENV_FILE` that exists in none of those places, any config
     file that is not a readable regular file, or one containing a NUL byte
     (bash `read` and `env_file_get`'s grep could disagree on it);
   - a config file that assigns `HERMES_HOME` in any form other than a
     literal absolute path (unquoted, `'...'` or `"..."` without `$`/backquote/
     backslash or the other quote, optional `export `, nothing after the
     value: a trailing comment or blank is refused because `env_file_get`,
     which the installers use, keeps it as part of the path): `$VAR`,
     `${...}`, `~`, command substitution, relative paths, `declare`,
     `${HERMES_HOME:=...}` are never evaluated. `status`/`check` refuse too
     without `--src` (the default runtime is unknown) and warn with it;
   - a linked worktree, `gitdir:` alias or `core.worktree` that shares or
     points into the installed git metadata or tree (installed git dirs are
     read from its `.git` files; no git command runs there);
   - any filesystem mounted below the checkout (`/proc/self/mountinfo`), or
     an unreadable mount table.
   There is no opt-out. `status`/`check` stay read-only and default to the
   first existing installed location in the order above (HERMES_HOME first).
   Tests: `make test-hermes-runtime-patch` (fixture `getent`, homes and
   checkouts only).
4. Develop the fix as a commit on top, with regression tests. Export it with
   `git format-patch` into `hermes/runtime-patches/` (next number, no
   trailers) and describe it in `hermes/runtime-patches/README.md`.
5. Get independent code and security review. Record the reviewer, the verdict
   and the patch SHA-256.
6. Re-verify on a fresh isolated checkout at the pinned base: the full ordered
   set applies cleanly, the runtime's tests pass, and `git status` shows only
   the files the patches name.
7. Prepare the install and rollback plan (below) and ask for authorization.

### Native patch candidates (separate from the legacy set)

`hermes/native-patches/<name>/` holds single-patch candidates pinned to an
official upstream base: one `git format-patch` file of the reviewed commit plus
a `hermes-native-patch/v1` manifest (base and base tree, patch SHA-256, the
reviewed commit and the tree base + patch must yield, `enabled: false`,
`auto_apply: false`). They are not in the legacy manifest or series and are
never combined with 0001–0004. The kit ships only an offline, read-only verifier:
`make hermes-native-patch-verify` (`scripts/hermes-native-patch.py verify`) checks the
manifest gates, the exact patch SHA-256 and the patch shape, and takes no checkout, runs no
git and writes nothing. Nothing applies, builds, installs, enables or restarts. Reproducing
the tree is reviewer-only, in a brand-new clone outside every Hermes home (README recipe);
any build, deployment or activation needs separate operator authorization.
Current: `aux-model-floor`
(`hermes/native-patches/aux-model-floor/README.md`).

### Pinned upstream vs patch ancestry

These are different facts. Keep both.

- **Pinned upstream base**: the upstream commit the patch set is applied to.
  For the legacy 0001–0004 set it is `9ebe577798eaab1b3e95b5c44c588c9bb5e43799`,
  which is a **local, customized, unpushed** commit of the old live checkout
  (upstream ancestor `205645ee`), not an upstream commit; the manifest pins it
  as provenance only. The legacy set is archived and unreviewed, none of it
  applies to the official runtime 71736c9f, and it is not an install
  prerequisite (`hermes/runtime-patches/README.md`).
- **Patch ancestry**: the private branch commits each patch was exported from
  (for example 0001–0003 = `9ebe5777..4f845b43ec`, 0004 = `git diff
  4f845b43ec..61cbead82d`). Ancestry shows provenance. It is not evidence that a
  patch was reviewed, and a matching hash doesn't make it reviewed either.

## Deployment proposal (not implemented, needs approval)

- Build each candidate as an **immutable release directory**, e.g.
  `~/.hermes/releases/<upstream-sha>+<patchset-sha256>/`, from a clean
  checkout at the pinned base plus the reviewed ordered patches. Make it
  read-only after the tests pass.
- Select the active release with a **version pointer** (one symlink or pointer
  file that the launcher resolves). You activate a release by switching the
  pointer atomically, and only after authorization, then restart through the
  approved procedure.
- **Rollback** means switching the pointer back to the previous release
  directory and restarting through the same approval. Old releases are kept
  until someone explicitly prunes them.
- The current dirty `~/.hermes/hermes-agent` stays exactly as it is until the
  operator decides how to preserve and reconcile it. Do not migrate it
  automatically.

## Authority and risk

- **Prompts don't control the OS.** These rules steer agents but enforce
  nothing. Any process running as the same UID as the gateway (agents, shells,
  editors, delegates) can write the installed runtime, the release directories
  and the pointer. The manager's refusal only stops honest mistakes made
  through the manager.
- **Docker** group membership or socket access is root-equivalent. A container
  that mounts `~/.hermes` can bypass every file check.
- Real enforcement would need a separate owner UID for release directories and
  the pointer (agents get read-only access), deployment through a narrow
  privileged helper, and no Docker socket for agent UIDs. Every one of these
  changes ownership, permissions, groups or services, so it needs its own
  approved **impact plan** listing affected processes, restart windows,
  rollback, and how the operator's own workflows keep working. Nothing in this
  repository applies them.

## Known gaps in `scripts/hermes-runtime-patch.sh`

The manager does not yet do these. It needs them before it can be used to
build a deployable release:

- **Manifest has no reviewed entries and no full base.**
  `hermes/runtime-patches/manifest.json` (v1) pins the ordered patch list and
  per-patch SHA-256; `apply`/`check` refuse a missing or invalid manifest, a
  base that is not a full id equal to HEAD, a missing, extra, reordered or
  tampered patch, and any patch not `reviewed` with verdict, reviewer,
  reference, date and a `review.sha256` equal to the patch hash (see
  `hermes/runtime-patches/README.md`); duplicate keys, NaN/Infinity, deep
  nesting and lone surrogates refuse the manifest. 0001–0004 are
  all `unreviewed` (archived legacy; base pinned as provenance only), so
  `apply` refuses the real set. Add review entries only from real review
  records, never from hashes alone. The manifest is a file
  any same-UID process can edit, and `--manifest FILE` substitutes any file
  the caller chooses: every field, including `review.sha256`, is
  self-asserted. It stops honest mistakes, it is not a signature.
  Replace refs are ignored for every git read and the HEAD-dependent reads use
  the captured full id with HEAD rechecked before and after the write, but the
  index, worktree and refs are read at different moments: a concurrent
  same-UID writer can still race the snapshot (detected only where a recheck
  sees it). Exact-base gating means `apply` needs a fresh checkout at the
  base (HEAD == base); a checkout with committed patches is reported by
  `status` but `apply` refuses it.
- **Apply is checked, not transactional.** `apply` classifies every patch
  before writing anything and checks that `--json` can encode its paths and
  names (a non-UTF-8 `--src`, patch name or target exits 2 before any write).
  The report is rendered and written to stdout after the write phase, so a failed stdout (full disk, closed pipe) exits 4 with a best-effort
  stderr note. Any nonzero exit or missing/partial report from `apply` means
  the outcome is unknown to the caller: run `status` before retrying.
  Any state other than `applied`/`applicable` refuses the set, and `check`
  fails in exactly those cases. The remaining patches are replayed with
  `git apply` in a scratch copy of their targets and must produce the
  simulated bytes; the targets are re-read and must be unchanged; one
  `git apply` (with `core.autocrlf=false`, `core.eol=lf`, whitespace fixing
  and `apply.ignoreWhitespace` off) writes the checkout, and the written
  targets are compared with the simulation. `git apply` writes files one by
  one, so an I/O error, a full disk or a concurrent writer can still leave the
  checkout partly updated: once the real write has begun, any failure or an
  unverified result is reported with outcome `unknown` (those patches' state
  `unknown`, `applied_now` null, `write_attempted` true), never rolled back. A later run then reports the patches whose changes are fully
  present as `applied` and the rest `applicable` (it resumes) or, if the
  worktree matches no state of the chain, `dirty` (inspect by hand).
- **Classification is a simulation with fixed rules, not a proof of intent.**
  The set is peeled back from HEAD (each patch, last first, whose change HEAD
  carries) and replayed forward on the targets' blobs. A hunk is placed at its
  stated line when the file is the patch's exact base (the `index` line's
  abbreviated blob id), otherwise only where its context occurs exactly once
  in the current state and at most once in the peeled base; anything else
  (repeated block, duplicate patch, overlapping hunk, change already present
  in lines another patch of the set writes) is `conflict`/`ambiguous`, never
  shifted. Unique-context placement on a different base is still an inference
  (the same one `git apply` makes, minus nearest-match). Each target's HEAD and
  worktree must equal exactly one state of the chain (bytes and, with
  `core.filemode`, the exec bit); changes the set explains are `applied`
  (uncommitted ones say so), a staged change, local edit, worktree revert of a
  committed patch or partly present patch is `dirty`, a HEAD that is not a
  chain state (e.g. a later patch committed without an earlier one) is
  `conflict`, and patches sharing a file with a refused patch are `blocked`.
  Behaviour change: a target carrying applied patches *plus* other local edits
  used to read `applied`; it now reads `dirty`. Binary, copy, symlink and
  submodule patches are `unsafe` (unsupported). Synthetic fixtures cover these
  rules; whether the real 0001–0004 apply to a given base is only established
  by running `status` against that base.
- **Base-revision check is exact, the base itself is still unrecorded.**
  `apply`/`check` refuse unless the manifest's `base` is a full commit id equal
  to the checkout's HEAD (see the manifest gap above). Until a reviewed base is
  recorded in `hermes/runtime-patches/manifest.json` (`base` is null today),
  every real `apply` is refused.
- **Pathname checks, not isolation.** The refusals are checks by a same-UID
  process against paths and dev/inode numbers. They race with concurrent
  renames, symlink swaps and mounts between the check and `git apply`
  (TOCTOU). A bind alias of a *descendant* of the installed runtime that is
  not a mount below the checkout (e.g. a mount made after the check, or one
  in another mount namespace) is not detected; neither is a mock `getent` on
  `PATH`, a hostile same-UID process, or root. The bind-mount and mount-table
  refusals are not exercised by the fixture suite (no unprivileged mounts on
  this host). Nothing here is OS isolation; see "Authority and risk".
- **Target checks are read-side, not a sandbox.** Each patch's targets (both
  sides of renames, deletions, creations, quoted/unicode names) come from a
  header parser cross-checked against `git apply --numstat -z`; a patch whose
  targets can't be fully enumerated (copies, submodules, unparseable, unsafe or
  non-UTF-8 names, no `python3`) is `unsafe`: never checked, never applied, and
  `check`/`apply` fail. Targets that are assume-unchanged or skip-worktree (a
  local edit there is invisible to `diff-files`) or unmerged are `unsafe` too.
  A patch that applies both forward and in reverse (hunks inside repeated
  lines) is `ambiguous`: never reported as applied, and `check` and `apply`
  fail. State is read with plumbing limited to the targets (`ls-tree`,
  `ls-files -s`, `cat-file --batch`, plain reads of the target files, so
  untracked and ignored files count) instead of `git status`, and a target
  with a `filter`, `ident`, `working-tree-encoding` or non-LF `eol` attribute
  (or a `text` attribute plus CR bytes) makes the patch `unsafe` before
  anything compares it, so no repo-configured clean/smudge/process filter runs
  and git's conversions are the identity. status/check still read the repository's
  config, attributes, index and the target files; that's what "read-only"
  means here, and it's verified only on fixture repos (byte-identical `.git`
  with unchanged mtimes), never by probing the installed runtime. With a split
  index, git refreshes the mtime of `.git/sharedindex.*` on every index read
  (even with `core.splitIndex=false`), so status/check refuse (exit 3) a
  checkout with any `sharedindex.*` file instead of claiming read-only; `apply`
  to an isolated split-index checkout is allowed. Other git write-on-read paths
  are not ruled out beyond what the fixtures cover. A same-UID process can change
  attributes, config or files between these checks and `git apply` (TOCTOU). Dirtiness
  compares bytes, so a stat-only change no longer counts. `--src` must be run by its own git
  dir: a `core.worktree` or gitfile that makes the toplevel resolve to another
  repository is refused for every command. `--json` output is now properly
  escaped and `dirty_targets` is an array of the targets the chain does not
  explain, sorted bytewise whatever the locale; a patch's `state` is one of
  `applied`, `applicable`, `conflict`, `ambiguous`, `blocked`, `dirty`,
  `unsafe`. Text output escapes every non-printable byte of paths, names and
  reasons (no terminal escape sequences), identically in every locale.
- **Process ownership.** The manager doesn't detect another writer in the
  target checkout. Check for one (`ps`, `lsof`, the Kanban run that owns the
  card) before applying.
