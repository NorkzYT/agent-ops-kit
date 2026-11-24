# iOS/iPad mobile validation

The iOS/iPad-first path of the [Kanban workflow](discord-kanban-workflow.md).
It runs in stages on one exact commit:

```
writer        coordinator (this host, Claude Opus or #fable Claude Fable): writes the code,
              commits, pushes the feature branch, records the full 40-character SHA (record-sha)
mac           the Mac's own Hermes: fetches the branch in its clone under /Users/mac-operator/Documents,
              a detached worktree at the SHA, iPhone simulator AND iPad simulator:
              build, test, install, launch, screenshot -> evidence
browserstack  real iPhone AND iPad on BrowserStack App Automate (XCUITest), only after both
              simulators passed the same SHA. DISABLED until the operator activates it:
              reported "skipped: disabled", never run, never counted as passed
windows       deferred: same flow once the Windows worker is repaired; not dispatched now
```

Every failure goes back to the coordinator automatically:

```
[code N] -> [mac N] -> [round N] advance
   simulator failed                      -> [code N+1] with the failure evidence -> [mac N+1] -> ...
   both passed, BrowserStack disabled    -> Review (report: "browserstack skipped: disabled")
   both passed, BrowserStack active      -> [devices N] -> [devices-round N] advance
        real device failed               -> [code N+1] -> [mac N+1] -> [round N+1] -> [devices N+1] ...
        real devices passed (API-verified) -> Review
   BrowserStack partially activated      -> round blocks (fail closed), never skips
```

The fix loop stops at `--max-rounds` (default 5) and blocks for the operator; an
unblock allows one more round. The reviewer approves the final SHA; the report
states the BrowserStack stage exactly as the gate prints it.

## Rules the script enforces

`workflow.py intake --mobile ios`:

- accepts `--lane mac` only. `--lane windows` is refused: the Windows lane is
  deferred. Windows lanes still work for requests without `--mobile`.
- tells the Mac to find its own clone of the repo URL under
  `/Users/mac-operator/Documents` (never a coordinator path like `/opt/repos`) and
  validate in a detached worktree of it at
  `/Users/mac-operator/Documents/.hermes-validate/<card>`, so the clone's own branch
  and files are never touched. `git rev-parse HEAD` must print the SHA. No
  clone, a dirty reused worktree or a wrong HEAD blocks the card with
  `workspace: …`.

`wait` fails closed. A `pass` counts only with the full SHA, a `repo_dir` under
`/Users/mac-operator/Documents`, and one iPhone and one iPad `simulators` entry, each
with name, OS, UDID, bundle id, a screenshot and exit code 0 for build, test,
install and launch. Missing parts make the evidence unusable (the run is
retried); a non-zero step or failed test is a `fail` for the coordinator.

Every Mac card, mobile or not, uses the Mac's own clone under
`/Users/mac-operator/Documents` and a detached worktree at the SHA; evidence without a
`repo_dir` there is unusable.

The BrowserStack stage fails closed:

- It is active only when the coordinator's activation file
  (`~/.hermes/browserstack.env`, mode 0600, this user's) sets the flag, both
  credentials and at least one iPhone and one iPad. Environment variables alone
  never activate it. No file or the flag off: `skipped: disabled`. A partial
  or group/world-readable file: the round blocks with `browserstack: …`.
- The Mac starts one App Automate build with `"buildTag": "<sha>"` and returns
  its `build_id`. `wait` on this host re-reads that build from the BrowserStack
  API (`browserstack.py`, the coordinator's own credentials). A pass needs a
  finished xcuitest build tagged with the exact SHA, every requested device
  run, and a passed session with no failed, errored or timed-out test on a real
  iPhone and a real iPad. The Mac's status can make the stage fail or block,
  never pass. A missing, unknown or mismatched build id is unusable evidence
  (retried, then the card blocks); it is never recorded as a pass.
- The device result is recorded with the build id, the dashboard URL (session
  videos and device logs) and the devices, as returned by the API. The gate
  prints `browserstack passed … verified via the BrowserStack API` only from
  that record.
- If the stage is turned off while a device card runs, the result cannot be
  verified and the card blocks.

## What the Mac runs

The validation task carries these templates; `<scheme>`, `<bundle-id>`,
`<udid>` and `<app>` are filled in on the Mac, `<sha>` and `<branch>` come from the card.

```bash
C=/Users/mac-operator/Documents/<repo>                       # the clone whose origin is --repo
WT=/Users/mac-operator/Documents/.hermes-validate/<card>
git -C $C fetch origin <branch>
git -C $C worktree add --detach $WT <sha>             # the clone's checkout stays as it is
cd $WT && git rev-parse HEAD                          # must print <sha>

xcrun simctl list devices available --json            # one iPhone, one iPad
xcrun simctl boot <udid> && xcrun simctl bootstatus <udid> -b

W=~/hermes-work/<card>
xcodebuild build-for-testing -scheme <scheme> \
  -destination 'platform=iOS Simulator,id=<iphone-udid>' \
  -destination 'platform=iOS Simulator,id=<ipad-udid>' -derivedDataPath $W/DerivedData
xcodebuild test-without-building -scheme <scheme> -destination 'platform=iOS Simulator,id=<iphone-udid>' \
  -derivedDataPath $W/DerivedData -resultBundlePath $W/iphone.xcresult
xcodebuild test-without-building -scheme <scheme> -destination 'platform=iOS Simulator,id=<ipad-udid>' \
  -derivedDataPath $W/DerivedData -resultBundlePath $W/ipad.xcresult

xcrun simctl install <udid> $W/DerivedData/Build/Products/Debug-iphonesimulator/<app>.app
xcrun simctl launch <udid> <bundle-id>
xcrun simctl io <udid> screenshot $W/<iphone|ipad>.png
git status --porcelain --untracked-files=no          # still nothing
```

Evidence adds `repo_dir` and `simulators` to the standard block:

```json
{"task": "t_…", "status": "pass", "sha_tested": "<40-char sha>", "repo_dir": "/Users/mac-operator/Documents/.hermes-validate/t_…",
 "simulators": [{"kind": "iphone", "name": "iPhone 17", "os": "iOS 26.0", "udid": "…",
   "destination": "platform=iOS Simulator,id=…", "build_exit": 0, "test_exit": 0,
   "install_exit": 0, "launch_exit": 0, "bundle_id": "com.example.app",
   "result_bundle": "~/hermes-work/t_…/iphone.xcresult", "tests": {"passed": 20, "failed": 0, "skipped": 0},
   "screenshots": ["~/hermes-work/t_…/iphone.png"], "log_excerpt": "…"},
  {"kind": "ipad", "…": "same fields"}],
 "dirty_tree": false, "blocker": "", "summary": "…"}
```

## Activate the Mac lane (now)

1. Set up the Mac worker and the `mac-worker` profile:
   [discord-kanban-workflow.md](discord-kanban-workflow.md), "One-time setup",
   steps 1 and 2. The `windows-worker` profile is not needed.
2. On the Mac, as the worker account: clone the app repo under the Mac clone
   root (`HERMES_MAC_REPO_ROOT` in the coordinator's `.env`, rendered into
   `~/.hermes/.env` by `make hermes-install`; the examples here use the
   placeholder `/Users/mac-operator/Documents`, which is also the default) with
   read access, install Xcode with an iOS
   simulator runtime, and check for an iPhone and an iPad:

   ```bash
   git -C /Users/mac-operator/Documents/<repo> remote get-url origin   # the URL you pass as --repo
   xcrun simctl list devices available | grep -E 'iPhone|iPad'
   ```

   **Setting or migrating the clone root.** Earlier kit versions hard-coded one
   operator's Mac path; the root is now `HERMES_MAC_REPO_ROOT`, default the
   placeholder above. If your Mac clones live anywhere else (including the path an
   older kit used), set `HERMES_MAC_REPO_ROOT=/Users/<mac user>/<dir>` in the kit
   `.env`, run `make hermes-install` (it copies the value into `~/.hermes/.env`) and
   restart the gateway from your own terminal; `workflow.py` reads it from the
   environment on every run. The value must be absolute, at least two levels deep,
   normalized, and made only of letters, digits and `._@+-` per component (no
   spaces, no component starting with `.`), because it is written into the git/cd
   commands of the Mac card; anything else falls back to the placeholder. Without a
   usable value, Mac evidence from a clone elsewhere is refused (`not a clone under
   /Users/mac-operator/Documents`). Cards created before the change name the old root
   in their text: re-intake them (or let them fail closed) after setting the value.
   Check what the coordinator resolves with
   `python3 -c 'import sys; sys.path.insert(0, "<skill>/scripts"); import workflow; print(workflow.MAC_REPO_ROOT)'`
   in the same environment as the gateway.

3. On this host, install the skill and the relay prompt:

   ```bash
   make hermes-config-apply
   cp hermes/profiles/mac-worker/SOUL.md ~/.hermes/profiles/mac-worker/SOUL.md
   make hermes-restart          # from your own terminal
   ```

4. Dry run (creates nothing; `stages` lists the order above):

   ```bash
   python3 ~/.hermes/skills/agent-ops-kit/kanban-workflow/scripts/workflow.py intake \
     --title "smoke" --repo <clone URL> --lane mac --mobile ios --key mobile-smoke-1 --dry-run
   ```

5. In Discord: `!work ios <task>`.

Validation worktrees stay on the Mac for inspection. Remove old ones from the
Mac's own terminal when you no longer need them:

```bash
git -C /Users/mac-operator/Documents/<repo> worktree remove /Users/mac-operator/Documents/.hermes-validate/<card>
git -C /Users/mac-operator/Documents/<repo> worktree prune
```

## What the Mac runs for real devices

The `[devices N]` task (see `device_task` in `workflow.py`), in the same kind of
worktree. Credentials are read from the Mac's own `~/.hermes/browserstack.env`
in a subshell and never echoed:

```bash
xcodebuild build-for-testing -scheme <scheme> -destination 'generic/platform=iOS' -derivedDataPath $W/DerivedData
# Payload/<App>.app -> app.ipa ; <App>UITests-Runner.app -> uitests.zip
( set -a; . ~/.hermes/browserstack.env; set +a; curl -sS -u "$BROWSERSTACK_USERNAME:$BROWSERSTACK_ACCESS_KEY" \
  -X POST https://api-cloud.browserstack.com/app-automate/xcuitest/v2/app -F file=@$W/app.ipa )
# same for /test-suite, then POST /build with
# {"app": "<app_url>", "testSuite": "<test_suite_url>", "devices": [...], "project": "hermes-kanban",
#  "buildTag": "<sha>", "deviceLogs": true}; poll GET /builds/<build_id> until it finished
```

The evidence block names `build_id`, `app_url`, `test_suite_url`, `repo_dir`
and the full SHA. Signing prompts, missing credentials, plan limits and HTTP
401/403 are blockers for the operator, not failures.

## Activate BrowserStack (operator only)

Nothing in this repository buys a plan, creates an account or holds
credentials. Until every step is done the stage stays `skipped: disabled`.
Agents never ask for, read, print or write the credentials.

1. Buy a BrowserStack App Automate plan that covers XCUITest on real iOS
   devices (the operator's decision; check the parallel-session limit).
2. Signing (human gate): on the Mac, set up the team, a development or ad hoc
   provisioning profile that BrowserStack accepts for the app and its UI test
   runner, and keychain access for the worker account, so that
   `xcodebuild build-for-testing -destination 'generic/platform=iOS'` signs
   without a prompt. The app needs an XCUITest target covering the functions
   the requests change.
3. Pick the devices from BrowserStack's device list for your plan: at least one
   iPhone and one iPad, written as `<model>-<os>` and separated by `|`.
4. Write the activation file on **both** machines from their own terminal
   (never through chat, a card or an agent), same content, mode 0600:

   ```bash
   umask 077; cat > ~/.hermes/browserstack.env <<'ENV'
   KANBAN_BROWSERSTACK_ENABLED=true
   BROWSERSTACK_USERNAME=<user>
   BROWSERSTACK_ACCESS_KEY=<key>
   KANBAN_BROWSERSTACK_DEVICES=iPhone 15-17|iPad Air 13 2025-18
   ENV
   chmod 600 ~/.hermes/browserstack.env
   ```

   - Coordinator (this host): the shared Hermes root, `~/.hermes/browserstack.env`
     (every profile reads the same file; profiles under `~/.hermes/profiles/`
     resolve to it). `advance` reads the flag and devices; `wait` uses the
     credentials to re-read the build.
   - Mac: the worker account's `~/.hermes/browserstack.env`; the device task
     sources it for the upload and build calls.
5. Check the coordinator side (prints the state, never the key; exit 0 active,
   3 disabled, 2 misconfigured):

   ```bash
   python3 ~/.hermes/skills/agent-ops-kit/kanban-workflow/scripts/browserstack.py preflight
   ```

6. Install and restart from your own terminal: `make hermes-config-apply`,
   copy `hermes/profiles/mac-worker/SOUL.md` to the mac-worker profile, `make hermes-restart`.
7. Smoke run: `!work ios <a small change with a UI test>`. Round 1's `advance`
   must print `SIMULATORS_PASSED … BrowserStack real devices queued`. After the
   device card, confirm the result independently:

   ```bash
   python3 ~/.hermes/skills/agent-ops-kit/kanban-workflow/scripts/browserstack.py verify --build-id <id> --sha <sha>
   ```

   If it says the build is not tagged with the SHA although the Mac sent
   `buildTag`, BrowserStack's API no longer echoes it in
   `input_capabilities`; the stage then fails closed until `verify_build` is
   adjusted to the current API (check BrowserStack's XCUITest build API docs).

To turn the stage off: set `KANBAN_BROWSERSTACK_ENABLED=false` in the
coordinator's file (or delete it). Rounds then report `skipped: disabled`; a
device card that is still running blocks, because its result can no longer be
verified.

Tests: `make test-kanban-workflow` (`MobileTest`: stage order, Windows refusal,
fail-closed simulator evidence, disabled BrowserStack; `BrowserStackActivationTest`
and `BrowserStackStageTest`: activation file, API verification against a fake
API, device cards, fix loop from a real-device failure, gate, no credential on
the board).
