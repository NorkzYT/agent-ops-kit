# Runtime patches to the installed Hermes

> **Status (2026-11-04): archived legacy, not an install prerequisite.** The installed runtime is
> the official Hermes `71736c9f32597bbca9d89cede5aa136e55946b2f`. None of 0001–0004 applies to it
> (0001 fails `git apply --check` on 6 files and reverses a documented upstream invariant: worker
> shells stay fenced and workers write the board through native `kanban_*` tools; 0002 is native
> upstream; 0003 is partly upstreamed; 0004's tool is a rejected design and its trigger, Tirith,
> was removed upstream in 911da42e9e). Source: the scratch compatibility report
> `~/.hermes/cache/scratch/runtime-compat-20261104/REPORT.md` §1–2, §6. The base `9ebe5777` =
> `9ebe577798eaab1b3e95b5c44c588c9bb5e43799` is a **local, customized, unpushed** commit of the old
> live checkout (on upstream `205645ee`), not an upstream commit; `manifest.json` pins it as
> provenance only. Every patch stays `unreviewed`: the reproduction proof (base + 0001..0004 = tree
> of `61cbead82d`) is not a review, and nothing here is approved. Do not apply, regenerate or rebase
> these patches. The kit's workflow no longer depends on them: `workflow.py` reads the board and
> prints native ops (docs/discord-kanban-workflow.md, "Board transport"). The text below describes
> the legacy set as it was built.

The kit configures Hermes through documented keys, skills and prompts. Four
behaviours it depends on live in the Hermes runtime itself and are not
configurable; they are carried here as git patches against the installed
checkout (`~/.hermes/hermes-agent`, Hermes v0.21.2, base commit `9ebe5777`; 0001–0003 are the
three commits of the clean repair branch `feat/kanban-supervised-terminal-clean` (9ebe5777..4f845b43ec), 0004 is the exact
`git diff 4f845b43ec..61cbead82d` of `feat/kanban-autonomous-reviewed` (commits d15a70ddc8, f04d4adc80, 61cbead82d), which carries
the same three before them, so 0004 repeats no hunk of 0001–0003 and base + 0001..0004 reproduces the tree of 61cbead82d;
no trailers) so a reinstall or upgrade can re-apply them and
`make hermes-runtime-patch-status` can tell whether the live runtime still has them.

| Patch | What it changes | Why the kit needs it |
|-------|-----------------|----------------------|
| `0001-feat-kanban-hand-the-worker-s-own-terminal-its-task-.patch` | The dispatcher-owned worker's own terminal keeps its task identity (`HERMES_KANBAN_TASK`, `HERMES_KANBAN_RUN_ID`, `HERMES_KANBAN_SUPERVISED_TERMINAL`, worker pid; never the claim lock). The CLI lets that terminal comment/attach/heartbeat/notify-subscribe its own card, create cards, and link cards when both endpoints are its own or created by it (plus one shape the workflow needs: a card it created as a new parent of a card its own card already gates, the report card). The grant (task, mandatory run id, worker pid, short expiry) is re-verified inside every DB write transaction: task `running` under that pid and run, run row alive, grant unexpired; it never covers board metadata. Delegate children, cron runs and every other spawn surface stay fenced; lifecycle transitions stay in-process tool calls. Kanban identity never persists in the bash session snapshot. | `workflow.py` runs in the worker's terminal and must know its card (`$HERMES_KANBAN_TASK`) and record `code-sha`, evidence and round cards on it. Without this every `hermes kanban comment/create/link` from a worker is refused and the worker mistakes its own pid for a competing run (incident `t_12595b96`, run 369). |
| `0002-fix-approvals-honour-permanent-command_allowlist-pat.patch` | Headless runs (`hermes chat -q`, cron, unattended platforms) honour the operator's permanent `command_allowlist` pattern keys ("Always" answers). Session-only approvals, the hardline floor and the tirith scan are unchanged. | Kanban workers are `-q` sessions; the live config already holds `script execution via -e/-c flag`, yet workers were denied `python3 -c` (incident runs 367–368). |
| `0003-fix-kanban-stop-guard-counts-only-successful-termina.patch` | The worker turn-end guard counts a terminal `kanban_*` call only when its result arrived and is not an error (`{"error"}`, `{"ok": false}`, `{"success": false}`, `Error:` text; a call with no result is pending, not terminal), includes `kanban_request_review`/`kanban_request_changes`, and tells the worker to continue (collect a running delegate child, retry transient failures) instead of blocking for unfinished work; a card that already has its review/validation child is finished with `kanban_complete`, never a duplicate review request. | A refused `kanban_complete` used to end the turn as a protocol violation; the old nudge pushed workers to block on incomplete work. |

| `0004-feat-kanban-typed-dev_test_run-tool-scanner-recovery.patch` (`git diff 4f845b43ec..61cbead82d`; runtime commits d15a70ddc8 + f04d4adc80 + 61cbead82d) | New `dev_test_run` tool (kanban toolset, dispatcher-owned workers only): typed arguments, root-owned registry + runner re-verified (owner, mode, symlink, pinned SHA-256) on every call, the fixed invocation scanned by the normal guards, empty environment; the runner binds to the live claim and repository lease and sandboxes the tests. **Fails closed on isolation at every surface**: the registry must carry per-profile `isolation.mode`; `same-uid-landlock` is a rejected design and `credential-broker-netns` is a recognised *proposed* design string (config validity only, not approved, not implemented). Readiness needs `backend_attested()` to pass and no backend exists, so regardless of what a root-owned registry declares the tool is hidden, refuses to run (naming the missing implementation) and emits no `RECOVERY:` directive; `kanban_block` is never refused on its account. Once a real broker probe attests a backend (future work; a mode string, flag or environment variable never does), an unattended rejection carries `RECOVERY:` and `kanban_block` refuses until it was tried; recovery state is per (task, run) and cleared only by an actual runner start or a recorded precondition refusal. The additive guards read the trusted registry leniently, so an older registry without `isolation.mode` keeps blocking credential reads. Additive guards: registered credential file / test container reads blocked in every mode (and in file tools); non-loopback database targets (URL host, `?host=`, multi-host, `hostaddr=`, `PGHOST`, `PGHOSTADDR`, `PGSERVICE*`) and URL passwords refused in unattended deny mode (text guard, not a boundary). | Workers are `-q` sessions; Tirith warns (`raw_ip_url`, `schemeless_to_sink`) on the approved `PGPASSFILE=… *_DATABASE_URL=postgresql://…@127.0.0.1:55443/… go test` command and the deny mode blocked the card (incident `t_12595b96`, run 370). See `docs/dev-test-runner.md`. |

## Commands

```bash
make hermes-runtime-patch-status      # applied | applicable | conflict | ambiguous | blocked | dirty | unsafe
make hermes-runtime-patch-apply SRC=/path/to/isolated-checkout   # never the installed runtime (refused)
scripts/hermes-runtime-patch.sh check --src /path/to/hermes-agent   # exit 0 iff every patch is applied or applicable (= apply would not refuse)
```

`apply` needs an explicit isolated `--src` and refuses the installed runtime
by path, dev/inode, shared git metadata (linked worktrees) and every home it
can be reached from, including profile sessions; it is disabled when the passwd
home can't be resolved (docs/hermes-source-management.md, which also lists the
residual limits: pathname checks only, apply atomic only up to `git apply`).

**Manifest gate.** `manifest.json` (schema `hermes-runtime-patches/v1`, or `--manifest FILE`)
pins the full upstream `base` commit, the ordered patch file names, each patch's SHA-256 and
its `review` (`status`: `unreviewed` | `reviewed` | `rejected`; `reviewed` also needs
`verdict: "approved"`, `reviewer`, `reference`, `date` and `sha256` equal to the patch's hash,
which binds the record to those bytes). The manifest is parsed strictly: duplicate keys,
`NaN`/`Infinity`, nesting deeper than 32 and strings that are not valid Unicode refuse the set
(status still exits 0, `NOT VERIFIED`). `apply` refuses the whole set (exit 1,
nothing written) unless the manifest exists, `base` is a full id equal to the checkout's HEAD,
the patch files equal the listed ones in order, every hash matches and every patch is
`reviewed`; `check` fails in the same cases. `status` exits 0 and prints `manifest: verified`
or `NOT VERIFIED` with each reason, plus `review=<status>` per patch (`--json`: `manifest`,
`patches[].review`). Today's manifest pins the hashes of 0001–0004, all `unreviewed`, and the
legacy local base `9ebe577798eaab1b3e95b5c44c588c9bb5e43799` as provenance only (`status:
archived-legacy`): `apply` refuses because nothing is reviewed, and against the official runtime
because HEAD is not that base. A hash match is not a review; never mark a
patch reviewed without the reviewer's record. Regenerating a patch changes its hash, so its
review must be redone. Every field is self-asserted: the manifest (and any file passed with
`--manifest`, which replaces the default entirely) is plain JSON any same-UID process can
write; `review.sha256` is an equality check, not a signature or proof that a review happened.
Git reads ignore replace refs (`GIT_NO_REPLACE_OBJECTS=1`), trees and blobs are read by the
captured full HEAD id, and HEAD is rechecked after classification and before and after the
write (a move exits 2; for `apply` it refuses before the write, or after the real write began
marks the patches `unknown`); the index and worktree are still
read separately, so this cannot exclude a hostile same-UID process racing them.
Deployment is a separate authorized step. Classification is a simulation, not `git apply --check`:
the set is peeled back from HEAD (committed patches) and replayed in order on the targets' HEAD
blobs; a hunk lands at its stated line on its exact base (`index` blob id), elsewhere only where
its context occurs exactly once (now and in the base), so a repeated block, a duplicate patch
or an overlapping hunk is `conflict`/`ambiguous`, never shifted. Each target's worktree must
equal one state of that chain: changes the set explains (an earlier or interrupted apply) are
`applied` and the rest `applicable`, so a partly applied isolated checkout resumes; a staged
change, a local edit or a partly present patch is `dirty`, and patches after a refused one on
the same file are `blocked`. A patch whose targets or hunks can't be parsed exactly (copy,
binary, symlink/submodule modes), or that touches a filtered, ident, working-tree-encoding,
non-LF `eol`, CR-carrying `text`, submodule, assume-unchanged or skip-worktree path, is
`unsafe`. `check` fails exactly when `apply` would refuse; any refused patch refuses the whole
set, `--json` encodability of paths and names is checked before any write (the report is
rendered and written after the write phase: a nonzero exit or missing report from `apply`
means an unknown outcome, run `status` before retrying; exit 4 = the report could not be
written), the remaining patches are
replayed with `git apply` in a scratch copy and must match the simulation, and the written
targets are compared afterwards (a mismatch is reported, not rolled back: not a transaction).
`apply` reports `outcome: not_attempted` (refused or no-op, nothing written), `verified`, or
`unknown` (the real write began, then failed or was not verified: patch state `unknown`,
`applied now: unknown`; `--json` adds `outcome`, `write_attempted`, `applied_now` = null).
An unrelated dirty file never blocks it. status/check
refuse a split-index checkout (any index read rewrites `.git/sharedindex.*` mtimes). Nothing
is committed, reset or restarted. After `hermes update`, run `status`: an
upstream that already contains a change reports it `applied`; a `conflict` means
the patch must be rebased by hand on the new source (the tests under
`tests/tools/test_kanban_supervised_terminal.py`, `tests/agent/test_kanban_stop.py`,
`tests/tools/test_single_query_approval_mode.py` and `tests/tools/test_dev_test_tool.py` in the
patch set are the contract to keep green). 0004 exposes `dev_test_run` only once a root-owned runner and registry exist *and* the
runtime attests an implemented isolation backend; none is implemented, the installer refuses
(`REJECTED` for the rejected design, `NOT IMPLEMENTED` for the proposed one), so the tool stays
hidden and no recovery directive is emitted.

Regenerate after changing the runtime branch:

```bash
git -C ~/.hermes/hermes-agent format-patch -3 --no-signature --zero-commit --no-numbered -o "$PWD/hermes/runtime-patches" <base>..<third>
# 0004 is one combined patch: a format-patch header followed by `git diff --stat` and the EXACT
# `git diff <third> HEAD` (today 4f845b43ec..61cbead82d), so status can classify it on its own.
# Verify before committing it: in a temporary index (GIT_INDEX_FILE=<tmp> git read-tree <base>;
# git apply --cached 0001..0004; git write-tree) the tree must equal `git rev-parse HEAD^{tree}`,
# and `git apply --reverse --check 0004` must pass at HEAD.
# commit messages carry no trailers (the kit's commit-msg hook rejects Co-Authored-By; the runtime commits follow the same rule)
```

Tests: `make test-hermes-runtime-patch`.
