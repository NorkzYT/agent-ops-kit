# BrowserOS Neo pilot: review triage of aa54759

Base: `feat/browseros-neo-pilot` at `aa54759b1e89ab61367e0778b3796ee0b3314323`.
Inputs: two tool-less independent reviews (`executed_nothing: true`), partitions
P1-handoff (attempt-c311d37b, source sha256 `0d78df3b…de4b`) and P2-viewer
(attempt-744567b5, source sha256 `5feff348…a2df2`), both `passed: false`.
Each finding was confirmed by reading the source and by a failing (RED)
deterministic regression on aa54759 before it was changed. Nothing was run
against a live gateway or container; no deploy, restart, `compose down/stop`
or `--force-recreate`. **Real-account login stays NO-GO.**

## Findings

| # | Finding (review) | Verdict | Fix | Regression (RED on aa54759 → GREEN) |
|---|---|---|---|---|
| P1-1 | Parent-domain / sibling-tossed cookies reach the sign-in origin; no cookie verification (`getUsageAndQuota` has no cookie entry) | **Confirmed (verification gap).** Whether `Storage.clearDataForOrigin` on Chromium 155 already removes `Domain=` parent cookies was not observed. | `cookie_hits()` (host-only on host; `Domain=` on host or parent; partitioned under a site of the host), `delete_cookies()` by exact name/domain/path/partitionKey, verify `Storage.getCookies` from a fresh connection; report `cookies_deleted`/`cookies_left` | unit: parent-domain cookie, partitioned cookie, scoped match (`.ample.com` and sibling host-only not hit), exact delete, delete that does not take effect fails |
| P1-2 | Failed resume leaves a stale `CLOSED`; handoff trusts it | **Confirmed.** aa54759 printed HANDOFF-READY after a failed resume plus a gate exit. | `open_gate` writes `state=OPENING` before starting the gate; `status` reports it as not drained; `handoff` also requires the gate container's `FinishedAt` + budget + 5 s ≤ `closed_at` (and `StartedAt` ≤ `FinishedAt`) | suite: failed resume → `OPENING`, handoff refused (not drained); raw gate start/stop after drain → "CLOSED record is stale"; fresh pause → HANDOFF-READY (positive control) |
| P1-3 | 2 s of silence after `ServiceWorker.enable` read as zero registrations | **Confirmed** (scripted socket: listing at 3 s → `[]`). | Require a `workerRegistrationUpdated` listing within 30 s (else Fail), then 1 s quiet window | unit: late listing found, no listing fails closed, empty listing = zero |
| P1-4 | `collect` timeout mid-frame drops the header / fragments (desync) | **Confirmed** (aa54759: `Fail: CDP connection closed` on a split 126-form header; `JSONDecodeError` on a fragmented message). | `_frame()` consumes only complete frames; fragments kept in `self.partial`; 64 MiB message cap | unit: partial frame across timeouts, fragmented message across timeouts, oversized frame fails |
| P1-5 | Success log overclaims Cache Storage / "timer survived (verified)" | **Confirmed.** | Log and usage header narrowed to the verified scope; states Cache Storage on origins with no listed worker was not cleared or checked; LIMITS updated | suite: old string absent, scope/limit lines present |
| P1-6 | `neo-quit` stdout corrupts the JSON report | **Confirmed** (inherited stdout). | `quit_browser()`: stdin DEVNULL, stdout → stderr | unit: stdout not inherited |
| P1-s | Userinfo / out-of-range ports accepted | **Confirmed** in `parse_args` (wrapper regex already refused userinfo; it accepted :0 and :99999). | Python rejects userinfo and ports outside 1–65535; wrapper bounds port | unit + suite (:0, :99999) |
| P2-1 | 6080 shim fails open if not executable (PATH fallthrough), unchecked | **Confirmed by reasoning** (execvp/shell lookup skip non-executable entries); not executed on the image. | Checker refuses a shim that is not a regular file, not executable by u/g/o, or on a `noexec` fs; manifest records `ws6080_mode`; live probe in `up`/`resume`/`handoff` (`/proc/net/tcp*`: 6080 LISTEN only on 127.0.0.1, nothing on the viewer port; upgrades with no/`null`/loopback Origins must get 403) refuses, stopping nothing | suite: modes 644/754/700 refused, 0755 accepted; probe vs controlled loopback listeners 5/5 (403 ok; 0.0.0.0, 101, viewer-port listener, nothing listening → BAD); resume/handoff refused on PROBE-BAD with gate not started / scrub not run |
| P2-2 | 9110/9210 missing from reserved viewer ports | **Confirmed.** | Added to `RESERVED_VIEWER_PORTS`; the live probe also covers a later agent listener on the viewer port (point-in-time) | suite: 9110/9210 refused for that specific reason |

Refuted or out of scope (not changed):

- P1-3 "stopped registrations are not targets": not relevant to the fix; the
  listing (stored + live), not targets, is used.
- P2 suggestions on rootless/iptables engine checks, `NEO_SCREEN` pinning,
  mcp-gate Host/Origin filtering, per-session viewer token: not in the
  confirmed scope of this round; left as open suggestions.

## Evidence

- Offline suite (`scripts/test_browseros_neo.sh`, run as a retained copy with
  artifacts kept): aa54759 baseline 186/0; fixed tree 211/0; new tests on an
  aa54759 export 183 passed / 27 failed (every row above RED).
- Unit (`scripts/test_browseros_neo_handoff_unit.py`): fixed 27/0; on the
  aa54759 `neo-handoff.py` 15/12.
- `bash -n`, `py_compile` and `shellcheck -S warning` were clean.

## Limits that still hold

- New handoff steps (cookies, required listing, frame reader) and the live
  probe are proven only against mocks and controlled loopback listeners, not
  on the pinned image; `scripts/test_browseros_neo_handoff_live.sh` was not
  re-run. If Chromium 155 does not emit an empty listing for zero
  registrations, handoff fails closed.
- Cannot prove that a generic unknown hostile page has no other way in, or
  anything against another process with the same host UID.
- The probe and gate checks are point-in-time; Cache Storage/other storage on
  origins with no listed worker and not a `--clear-origin` is not cleared.
- Real-account login remains NO-GO pending an operator decision.
