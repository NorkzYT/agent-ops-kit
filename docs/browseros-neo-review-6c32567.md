# BrowserOS neo: residual review findings on 6c32567

Review: restricted tool-less fix-closure review of 6c32567 (P1-handoff and
P2-viewer each `passed=false` on one residual). Evidence:
`~/.hermes/cache/scratch/neo-storagefix-6c32567/` (probe scripts, outputs, rc files).

## R1 — handoff "zero bytes of any type" (P1-handoff residual): confirmed, corrected

Live probe on the pinned image (`neo:0.52.1.2-d32f6ab@sha256:9f6302…2d72e`,
Chromium 155, kept test container `bneo-handoff-live-1795233909-2968009`,
`--network none`, synthetic origin `http://127.0.0.1:8097`), `ls_probe.out`, rc 0:

- `usageBreakdown` lists only `cache_storage`, `file_systems`, `indexeddb`,
  `service_workers`.
- 200 kB localStorage value → usage 0; IndexedDB control → `indexeddb` 10947.
- `DOMStorage.getDOMStorageItems` without a frame on the origin → "Frame not
  found for the given storage id".
- `clearDataForOrigin("all")` removed the planted localStorage value.

Change (`neo-handoff.py`): verification claims narrowed to quota-managed
storage; localStorage/sessionStorage stated as cleared-not-verified in the
docstring, `LIMITS`, wrapper output and docs. New fail-closed check: a
`usageBreakdown` missing a checked type (`QUOTA_TYPES` for `--clear-origin`,
`cache_storage`+`service_workers` for worker origins) fails as
`storage_unchecked`, so an empty breakdown is never read as zero.
Candidate `verify()` on real Chromium (`verify_probe.out`, rc 0): no
`storage_unchecked` (no false failure); localStorage-only passes (the
documented blind spot); IndexedDB control fails with `indexeddb` 10951.

Not done: verifying localStorage would need a frame on the origin with
scripts disabled and requests fulfilled locally (Fetch + DOMStorage). That's
feasible but not built here.

## R2 — host processes bypass mcp-gate (P2-viewer residual): confirmed, NO-GO

`netstat` in the kept container: `0.0.0.0:9010`, `0.0.0.0:9011`,
`0.0.0.0:6081`; 5900/6080/9110/9210 on 127.0.0.1. An earlier live run
(5cbff53) reached 9010 on the bridge address while the gate was paused.
The viewer's Origin check doesn't stop a non-browser process, which can send
any `Origin`.

Change: `mcp-gate.py` no longer claims to be the only path. The docs gain a
"Host-process bypass" section, and the wrapper's `access` and `handoff` text
says that real-account login is NO-GO because of this unresolved engineering
boundary, not because a product decision is pending.

No mitigation was applied. An `internal` network cuts Neo's internet access
and is still host-reachable. Rebinding 9010 isn't a documented image
setting. The viewer bypass would remain either way. A real fix means taking
agent processes off this host's network namespace or adding an
authenticated viewer/MCP path, which is a separate design and deployment.

Note: `mcp-gate.py` sha256 is in the `up` manifest, so an existing
deployment's `resume` will refuse until an approved `down`/`up`. That's
fail-closed and was not exercised here.

## Tests (producer exits)

| Run | rc | Result |
|---|---|---|
| `python3 -I scripts/test_browseros_neo_handoff_unit.py` (candidate) | 0 | 31/0 |
| same against `git show 6c32567:docker/browseros-neo/neo-handoff.py` | 1 | 27/4 (4 new RED) |
| `bash scripts/test_browseros_neo.sh` (mock; `compose config` only) | 0 | 211/0 |
| `ls_probe.py` in kept container | 0 | see R1 |
| `verify_probe.py` (candidate `verify()`) in kept container | 0 | see R1 |

The unit fake now returns Chromium 155's observed breakdown types by default.
No push, no gateway/config writes, no container stopped, removed or
recreated. The untracked `scripts/test_browseros_neo__retain.sh` is unchanged
(sha256 26b4efdb…).
