# RTK replay pilot (experiment only)

Measures whether [RTK](https://github.com/rtk-ai/rtk) shortens captured
command output **without losing** failure IDs, skips, timeouts or the real
change in a long diff. It is not an integration: no `rtk init`, no hook, no
shell-wide command rewriting, no `rtk-proxy`, no model, and RTK never runs a
live or untrusted command; it only reads fixtures in `fixtures/`. The runner
sets `RTK_TELEMETRY_DISABLED=1` with a private HOME/XDG and a minimal
environment, but does not network-isolate RTK, so absence of telemetry or other
network access is **not proven**.

## Pin

`rtk.lock.json`: release `v1.2.1` (tag commit `d0f0399a5ff8cd1d81325a033f0b2d8864d665f6`),
asset `rtk-oss-x86_64-unknown-linux-musl.tar.gz` (Apache-2.0 only: filters, no
BUSL engine), asset sha256 `44aa460c…e8b4` (matches the release API digest and
`checksums.txt`), binary sha256 `e2950ef6…94ada0`. The runner refuses any other
binary.

## Fetch and verify (outside the repo)

```bash
d=/path/to/private/rtk && mkdir -p "$d" && cd "$d"
curl -fsSLO https://github.com/rtk-ai/rtk/releases/download/v1.2.1/rtk-oss-x86_64-unknown-linux-musl.tar.gz
echo "44aa460c4827d206d11405c652832c9f7082edccfbc3a9f15a69a851e8a9e8b4  rtk-oss-x86_64-unknown-linux-musl.tar.gz" | sha256sum -c
tar xzf rtk-oss-x86_64-unknown-linux-musl.tar.gz rtk
```

## Run

```bash
python3 -I tools/rtk-pilot/run_pilot.py --rtk "$d/rtk" --out <private-out-dir>
make test-rtk-pilot          # offline tests with stub binaries
```

Exit: `0` every case measured and every marker kept, `1` a marker was lost,
`2` refused (wrong digest, bad lock, bad cases file, mode, fixture, case keys
or id; nothing ran or was written), `3` a case was unmeasured (crash, timeout,
exec failure, empty output), `4` unexpected runner error (never a verdict).
Each case must have exactly `id`, `fixture`, `mode`, `producer_exit`,
`must_keep`; `id` becomes a directory name, so it must match
`[A-Za-z0-9][A-Za-z0-9_-]{0,63}` and be unique. Per case the out dir keeps
`argv.json`, `raw.input`, `rtk.stdout`, `rtk.stderr`, `result.json`.

Producer-exit parity is **not tested**. A replay feeds a captured fixture to
RTK; there is no producer process, so `producer_exit` is a recorded label and
is never compared with RTK's exit (`rtk summary cat` exits with `cat`'s
status). Results carry `producer_exit_parity: "not-tested"`. Checking that
RTK preserves the wrapped command's exit needs a live wrapped run, which this
pilot does not do.

Cases (`cases.json`): failing pytest run, all-skipped suite, a timeout log, a
252-line diff with one real logic change. Modes are a fixed table:
`rtk log` (stdin), `rtk diff -` (stdin), `rtk summary cat <fixture>`.

## Result on the kit dev VM (2026-11-22): no-go

**Official binary: unmeasured.** The pinned v1.2.1 OSS release binary dies with
`SIGILL` (exit 132) on this host's Xeon E5-2680 v2 (no AVX2/BMI2), including
`rtk --version` (gdb: `vpbroadcastq … %ymm3`). All four cases recorded
`status=crashed`, `reduction=null`; runner exit 3.

**Local source build: measured, fails the gate.** A local baseline built from
the v1.2.1 tag commit (`d0f0399a…`, `x86_64-unknown-linux-gnu`, binary sha256
`ff6b87a6…2dad30`, no AVX2/BMI2 instructions) runs (`rtk --version` exit 0).
It is not the pinned binary: the run used a scratch lock for that digest, and
`rtk.lock.json` is unchanged. Replay result (`summary.json` in a private
scratch dir), runner exit **1**:

| case | reduction | lost markers | verdict |
|---|---|---|---|
| failing-tests | 66.0% | none | kept |
| all-skipped | 69.4% | `DATABASE_URL not set` | **lossy** |
| timeout | 44.0% | none | kept |
| long-diff | 0.0% | none | kept |

`rtk summary cat` on the all-skipped pytest log reduces it to "0 passed, 3
skipped" and drops the skip reason, which is the line that says why nothing
ran. Median reduction (55%) clears the 25% bar, but the gate also needs no lost
markers, so RTK is **not adopted**.

Not tested and not approved: producer-exit / exit-classification parity (every
case is `producer_exit_parity: "not-tested"`; a replay has no producer process,
see above) and any guard parity with the kit's own wrappers. No `rtk init`,
hook, install or PATH change was made or is approved. Reopening needs a new
release or filter that keeps skip reasons, a live wrapped run for exit parity,
and a separate install/hook review. Adoption gate (from the research note):
≥25% lower median output, no lost markers, unchanged exit classification.
