# Native patch candidate: auxiliary GPT model floor (disabled, source only)

> **Status: candidate, not activated.** `manifest.json` has `enabled: false` and
> `auto_apply: false`. Nothing in the kit applies, installs or enables this patch, renders
> its config key, or restarts anything. It is separate from the archived
> `hermes/runtime-patches/` set (0001–0004): different base, not in that manifest or
> series, never applied together with it, and `make hermes-runtime-patch-*` never sees it.

## What it is

One `git format-patch` file of the reviewed native commit
`57a36ef6a0720c30a80573cd40e8274f8cc81c36` (tree `3075a8f13ae6fb983be150abfd0cb31a8773768d`)
on the official upstream base `6b21d0564de0492e2ebd0d8f63fca86c03b5c892`
(tree `31134041c2353045891fd394da206bac911e5002`, `https://github.com/NousResearch/hermes-agent`).
The commit's own author, date and message are kept; only the git version signature is
omitted (`--no-signature`). SHA-256 of the patch:
`283bcb722a82934ba0ec769239d6be284f2a8cc867ab93efb214486d1fb6b935`.

It adds `agent/aux_model_floor.py` and an opt-in `auxiliary.gpt_model_floor` key
(native default `false`). When a profile sets it to `true`, auxiliary calls and their
fallback chain, the iteration-limit summary, plugin side questions and the
background-review/curator forks refuse, on the request actually handed to the provider,
any GPT id below 6.1 Terra/Sol/Astra, any non-strict GPT id and any attempt with no
model — a refused attempt is never retried or re-sent. Same grammar as
`scripts/lib/model_floor.py`. Contract: `tests/agent/test_aux_model_floor.py` in the patch.

## What it does not change (caveats kept)

- **Live runtime and kit config.** The installed runtime was observed on 2026-11-24 at
  the same upstream commit and tree as the base, but it is still never a patch, build or
  test target, and nothing in the kit applies the patch anywhere. `hermes/config.yaml.tmpl`
  does **not** set `auxiliary.gpt_model_floor`: on an unpatched Hermes the key would be
  silently ignored and the config would falsely look covered. The enforced control for
  auxiliary paths stays the restart preflight (`model-floor-migrate.py --check`,
  docs/model-routing.md, "GPT model floor"). Setting the key is only meaningful after a
  separately authorized deployment of a Hermes built from base + this patch.
- **Bypasses.** The main agent loop is not guarded by this patch (the model-router
  plugin's execution seam is). Traffic that never enters Hermes' auxiliary dispatch —
  the Claude proxy (`claude-max-proxy`) and its plugin, and standalone clients
  (`knowledge-classify`, `knowledge-benchmark`, the Windows worker) — bypasses it and
  keeps its own checks.
- **Mac validation lane.** Unchanged: Mac cards still need `HERMES_MAC_REPO_ROOT`
  (docs/mobile-validation.md).
- **Trust.** The manifest is self-asserted JSON. The review record is not shipped with
  the kit; a hash match proves byte identity, not that a review happened. Packaging is
  not deployment approval (`review.deployment_approved: false`).

## Offline verification (the only kit command)

```bash
make hermes-native-patch-verify      # = python3 scripts/hermes-native-patch.py verify
```

Reads only this directory. The directory is opened once with `O_NOFOLLOW`, which refuses a
symlink only as the final path component (symlinked parent directories of `--dir` are still
followed); `manifest.json` and the patch are opened relative to that descriptor with
`O_NOFOLLOW`, must be regular files, and are read once. Checks: manifest gates
(`enabled`/`auto_apply`/`deployment_approved` all `false`, `name` a plain slug equal to the
directory name, full ids, tree and parent consistency), a single `*.patch` file whose SHA-256
equals the manifest, and the patch shape (header names the reviewed commit, one commit, no CR,
no signature, no binary hunks).
It takes no checkout, runs no git, writes no files or git objects and touches no Hermes home.
It does **not** re-derive base + patch = `result_tree`; that is the recorded verification below.
Exit codes: 0 verified, 1 failed, 2 usage. There is no `apply` or `test` command.

## Reproducing the tree (reviewer-only, optional)

Only in a brand-new, empty directory created for this, outside `~/.hermes` (including its
cache/scratch), `$HERMES_HOME` and every installed runtime, with no shared objects or
alternates. It fetches the base without a checkout and applies only to a temporary index, so
no worktree is touched.

First set the parent directory. The value below is a placeholder, not a usable path: replace
it with an existing directory that meets the rule above.

```bash
NATIVE_VERIFY_PARENT=/path/outside/every/hermes/home
```

Then run the recipe from the kit root. It is one subshell, so a failed step stops only the
subshell and never exits your interactive shell. The verifier runs first and writes the bytes
it hashed into the new private (`mktemp -d`, mode 0700) directory; that copy is re-hashed
against the pinned SHA-256 before anything touches the network, so a failed verifier or a
short copy stops the recipe with no fetch. `env -i` drops inherited `GIT_*` settings.

```bash
(
  set -eu
  W=$(mktemp -d "$NATIVE_VERIFY_PARENT/native-verify.XXXXXX")
  python3 scripts/hermes-native-patch.py verify --emit-patch > "$W/verified.patch"
  printf '%s  %s\n' 283bcb722a82934ba0ec769239d6be284f2a8cc867ab93efb214486d1fb6b935 "$W/verified.patch" | sha256sum -c -
  env -i PATH="$PATH" HOME="$W" GIT_CONFIG_NOSYSTEM=1 sh -euc '
    git init -q --bare "$1/base.git" && cd "$1/base.git"
    git fetch -q --depth 1 https://github.com/NousResearch/hermes-agent.git 6b21d0564de0492e2ebd0d8f63fca86c03b5c892
    test "$(git rev-parse FETCH_HEAD^{tree})" = 31134041c2353045891fd394da206bac911e5002
    export GIT_INDEX_FILE="$1/index"
    git read-tree FETCH_HEAD
    git apply --cached "$1/verified.patch"
    test "$(git write-tree)" = 3075a8f13ae6fb983be150abfd0cb31a8773768d
    echo "tree reproduced: 3075a8f13ae6fb983be150abfd0cb31a8773768d"
  ' sh "$W"
)
```

The subshell exits non-zero at the first failed step (unset or placeholder parent, verifier
failure, hash mismatch, fetch, base-tree or result-tree mismatch). `$W` is left in place for
inspection; remove it yourself afterwards.

This only proves the tree. Building, testing or deploying a patched Hermes, setting
`auxiliary.gpt_model_floor: true` in a profile and restarting are separate steps that need
their own operator authorization; the installed runtime is never the place to do them.

## Regenerating

Only from a newly reviewed commit on a pinned upstream base:

```bash
git -C <isolated-checkout> format-patch -1 --no-signature --stdout <reviewed-commit> > <file>.patch
```

then update `sha256`, `base`, `base_tree`, `result_tree` and `reviewed_source`, run
`make hermes-native-patch-verify`, and reproduce the tree as above. A new hash needs a new review.

## Verification record (2026-11-24)

- Independent shallow https fetch of upstream `6b21d056` (no objects borrowed from the source
  checkout): base tree `31134041` confirmed; base + patch on a temporary index = tree
  `3075a8f1` = reviewed tree.
- Focused tests, `tests/agent/test_aux_model_floor.py`, in a second independent fetch outside
  every Hermes home: **209 passed** (pytest via the source checkout's
  `uv sync --frozen --group dev --group test` venv; imports confirmed to resolve to the
  independent checkout; upstream conftest isolates `HERMES_HOME`). The same file from a fetch
  under `~/.hermes/cache/scratch` gave 115 failed / 94 passed, every failure upstream
  conftest refusing I/O under the real Hermes home (environment, not the patch).
- Broader upstream tests at the exact source commit `57a36ef6`, run per file through the
  supported per-file runner: 155 files selected; **154 files passed (1859 tests)** and one,
  `test_run_agent`, hit the 300 s per-file timeout. That file was then run
  alone at the same exact commit: **277 passed**. A separate, earlier grouped run (6 chunks) had
  5 chunks pass and 1 fail, that failure caused by a fixture home path in the test
  environment.
- Those runs used `apply`/`test` helper subcommands that have since been removed from the kit;
  the kit now ships only the offline verifier above.
- Not done: the full upstream suite (only the 155-file selection above), any run against the
  live runtime or a real provider, macOS.
