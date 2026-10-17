# iOS/iPad mobile validation

The iOS/iPad-first path of the [Kanban workflow](discord-kanban-workflow.md).
It runs in stages on one exact commit:

```
writer      coordinator (this host): writes the code, commits, pushes the feature branch,
            records the full 40-character SHA (record-sha)
mac         the Mac's own Hermes: a detached worktree of its clone under /Users/norkz/Documents,
            iPhone simulator AND iPad simulator: build, test, install, launch, screenshot -> evidence
browserstack  optional real devices, only after both simulators passed the same SHA.
            DISABLED: reported "skipped: disabled", never run, never counted as passed
windows     deferred: not part of this activation; no Windows lane is dispatched
```

Failures go back to the coordinator as the next fix round, as in the standard
loop. The reviewer approves the final SHA; the report states the BrowserStack
stage as the gate prints it.

## Rules the script enforces

`workflow.py intake --mobile ios`:

- accepts `--lane mac` only. `--lane windows` is refused: the Windows lane is
  deferred. Windows lanes still work for requests without `--mobile`.
- tells the Mac to find its own clone of the repo URL under
  `/Users/norkz/Documents` (never a coordinator path like `/opt/repos`) and
  validate in a detached worktree of it at
  `/Users/norkz/Documents/.hermes-validate/<card>`, so the clone's own branch
  and files are never touched. `git rev-parse HEAD` must print the SHA. No
  clone, a dirty reused worktree or a wrong HEAD blocks the card with
  `workspace: …`.

`wait` fails closed. A `pass` counts only with the full SHA, a `repo_dir` under
`/Users/norkz/Documents`, and one iPhone and one iPad `simulators` entry, each
with name, OS, UDID, bundle id, a screenshot and exit code 0 for build, test,
install and launch. Missing parts make the evidence unusable (the run is
retried); a non-zero step or failed test is a `fail` for the coordinator.

## What the Mac runs

The validation task carries these templates; `<scheme>`, `<bundle-id>`,
`<udid>` and `<app>` are filled in on the Mac, `<sha>` and `<branch>` come from the card.

```bash
C=/Users/norkz/Documents/<repo>                       # the clone whose origin is --repo
WT=/Users/norkz/Documents/.hermes-validate/<card>
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
{"task": "t_…", "status": "pass", "sha_tested": "<40-char sha>", "repo_dir": "/Users/norkz/Documents/.hermes-validate/t_…",
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
2. On the Mac, as the worker account: clone the app repo under
   `/Users/norkz/Documents` with read access, install Xcode with an iOS
   simulator runtime, and check for an iPhone and an iPad:

   ```bash
   git -C /Users/norkz/Documents/<repo> remote get-url origin   # the URL you pass as --repo
   xcrun simctl list devices available | grep -E 'iPhone|iPad'
   ```

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
git -C /Users/norkz/Documents/<repo> worktree remove /Users/norkz/Documents/.hermes-validate/<card>
git -C /Users/norkz/Documents/<repo> worktree prune
```

## Enable BrowserStack (later, not part of this change)

No subscription, purchase or credentials come with this change. Until all steps
below are done the stage reports `skipped: disabled`. This version has no
BrowserStack runner: setting the flag alone runs nothing and the stage still
reports `skipped` (with the reason), never `pass`.

1. The operator buys a BrowserStack App Automate plan.
2. On the Mac, in its own terminal, add the credentials to the worker's
   `~/.hermes/.env` (mode 600). Never put them in `config.yaml`, chat, cards
   or evidence; reference them as `${BROWSERSTACK_USERNAME}` /
   `${BROWSERSTACK_ACCESS_KEY}` where a config needs them.

   ```bash
   BROWSERSTACK_USERNAME=<user>
   BROWSERSTACK_ACCESS_KEY=<key>
   ```

3. Real devices need a signed `.ipa` and an XCUITest runner. Signing is a human
   gate: the operator sets up the team and profiles on the Mac.
4. Add the runner to `workflow.py` (a follow-up change): after both simulators
   pass, a card for the same SHA uploads the app and test suite and runs one
   real iPhone and one real iPad. Reference calls (check BrowserStack's current
   API docs first):

   ```bash
   curl -u "$BROWSERSTACK_USERNAME:$BROWSERSTACK_ACCESS_KEY" -X POST \
     https://api-cloud.browserstack.com/app-automate/xcuitest/v2/app -F file=@<app>.ipa
   curl -u "$BROWSERSTACK_USERNAME:$BROWSERSTACK_ACCESS_KEY" -X POST \
     https://api-cloud.browserstack.com/app-automate/xcuitest/v2/test-suite -F file=@<app>UITests-Runner.zip
   ```

5. Turn the stage on in the coordinator's own `~/.hermes/.env` (edit it
   directly: `make hermes-config-apply` does not sync this key) and restart
   the gateway from your own terminal (`make hermes-restart`):

   ```bash
   KANBAN_BROWSERSTACK_ENABLED=true
   ```

   The script reads the flag from the environment of the `advance` step on the
   round card. Check it there: the first mobile round's `advance` output must
   show a `browserstack` stage reason other than `disabled`. If it still says
   `disabled`, the gateway's terminal tool did not pass the variable through.

Tests: `make test-kanban-workflow` (`MobileTest` covers the stage order, the
Windows refusal, fail-closed evidence and the disabled BrowserStack stage).
