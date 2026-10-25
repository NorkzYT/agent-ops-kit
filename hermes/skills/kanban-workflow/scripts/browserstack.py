#!/usr/bin/env python3
"""BrowserStack App Automate (XCUITest) stage of the Kanban workflow: activation
preflight and independent verification of a real-device build.

    browserstack.py preflight                        # exit 0 active, 3 disabled, 2 misconfigured
    browserstack.py verify --build-id ID --sha SHA   # re-read a build from the API, print device evidence

Fail-closed and disabled by default. The stage is active only when the operator
has written the activation file (default ~/.hermes/browserstack.env, mode 0600,
owned by this user) with all of:

    KANBAN_BROWSERSTACK_ENABLED=true
    BROWSERSTACK_USERNAME=<user>
    BROWSERSTACK_ACCESS_KEY=<key>
    KANBAN_BROWSERSTACK_DEVICES=iPhone 15-17|iPad Air 13 2025-18   # >= one iPhone and one iPad

Anything else (no file, flag off) is "disabled"; a partial or unsafe file is
"misconfigured", which blocks the workflow instead of skipping the stage.

The Mac runs the build (it has the signed app and the XCUITest runner); this
module never trusts the Mac's word for the result. A device stage passes only
when the BrowserStack API, read here with the coordinator's own credentials,
returns a finished build tagged with the exact SHA whose every iPhone and iPad
session passed. Credentials never leave this process: they are not printed, put
in argv, cards, ledgers or evidence. Stdlib only.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import stat
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional

API = "https://api-cloud.browserstack.com/app-automate/xcuitest/v2"
DASHBOARD = "https://app-automate.browserstack.com/dashboard/v2/builds/"
FLAG = "KANBAN_BROWSERSTACK_ENABLED"
USER_VAR, KEY_VAR = "BROWSERSTACK_USERNAME", "BROWSERSTACK_ACCESS_KEY"
DEVICES_VAR = "KANBAN_BROWSERSTACK_DEVICES"
CONFIG_VAR = "KANBAN_BROWSERSTACK_CONFIG"  # path override (tests, non-default homes)
CONFIG_NAME = "browserstack.env"
DEVICE_KINDS = ("iphone", "ipad")  # both must pass on a real device
BUILD_ID_RE = re.compile(r"^[A-Za-z0-9]{16,64}$")
DEVICE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ().,_-]{1,60}-[0-9]{1,2}(?:\.[0-9]{1,2})?$")
DONE_OK = {"passed", "done"}  # finished build statuses that can pass (sessions decide)
UNFINISHED = {"queued", "running"}
VERIFIED_BY = "browserstack-api"

# (method, url, headers) -> (http status, body bytes)
Http = Callable[[str, str, dict], tuple]


class VerifyError(ValueError):
    """The device evidence cannot be confirmed (never a pass)."""


def _truthy(value: str) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def config_path(env=None) -> Path:
    env = os.environ if env is None else env
    override = (env.get(CONFIG_VAR) or "").strip()
    if override:
        return Path(override).expanduser()
    root = (env.get("HERMES_KANBAN_HOME") or env.get("HERMES_HOME") or "").strip()
    root_path = Path(root).expanduser() if root else Path.home() / ".hermes"
    if root_path.parent.name == "profiles":
        root_path = root_path.parent.parent  # one file for every profile on this host
    return root_path / CONFIG_NAME


def read_config(path: Path) -> dict:
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, value = line.removeprefix("export ").partition("=")
        if sep:
            values[name.strip()] = value.strip().strip("'\"")
    return values


def devices_of(value: str) -> list:
    return [d.strip() for d in (value or "").split("|") if d.strip()]


def device_kind(name: str) -> str:
    low = (name or "").lower()
    return "ipad" if "ipad" in low else "iphone" if "iphone" in low else ""


def activation(env=None) -> dict:
    """{"state": disabled|misconfigured|active, "reason", "devices", "config"}.
    Never contains a credential value."""
    path = config_path(env)
    out = {"state": "disabled", "reason": "disabled", "devices": [], "config": str(path)}
    try:
        st = path.stat()
    except FileNotFoundError:
        return out
    except OSError as exc:
        return {**out, "state": "misconfigured", "reason": f"cannot read {path}: {exc.strerror}"}
    if stat.S_IMODE(st.st_mode) & 0o077 or (hasattr(os, "getuid") and st.st_uid != os.getuid()):
        return {**out, "state": "misconfigured",
                "reason": f"{path} must be owned by this user with mode 0600 (chmod 600 {path})"}
    try:
        cfg = read_config(path)
    except (OSError, UnicodeDecodeError) as exc:
        return {**out, "state": "misconfigured", "reason": f"cannot read {path}: {exc}"}
    if not _truthy(cfg.get(FLAG, "")):
        return out
    problems = [f"{name} is not set" for name in (USER_VAR, KEY_VAR) if not cfg.get(name)]
    devices = devices_of(cfg.get(DEVICES_VAR, ""))
    bad = [d for d in devices if not DEVICE_RE.match(d) or not device_kind(d)]
    if bad:
        problems.append(f"{DEVICES_VAR} has invalid entries {bad} ('<iPhone|iPad model>-<os>', '|'-separated)")
    missing = [k for k in DEVICE_KINDS if k not in {device_kind(d) for d in devices}]
    if missing:
        problems.append(f"{DEVICES_VAR} needs at least one {' and one '.join(missing)} device")
    if problems:
        return {**out, "state": "misconfigured", "reason": f"{path}: " + "; ".join(problems), "devices": devices}
    return {**out, "state": "active", "reason": "active", "devices": devices}


def credentials(env=None) -> tuple:
    """(username, key) from an active activation file, else VerifyError."""
    act = activation(env)
    if act["state"] != "active":
        raise VerifyError(f"BrowserStack stage is {act['state']}: {act['reason']}")
    cfg = read_config(Path(act["config"]))
    return cfg[USER_VAR], cfg[KEY_VAR]


def default_http(method: str, url: str, headers: dict) -> tuple:
    req = urllib.request.Request(url, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 - fixed https API host
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read() or b""
    except (urllib.error.URLError, OSError) as exc:
        raise VerifyError(f"BrowserStack API unreachable: {getattr(exc, 'reason', exc)}") from None


class Client:
    """Read-only BrowserStack API client; `http` is injectable for tests."""

    def __init__(self, username: str, key: str, http: Optional[Http] = None):
        token = base64.b64encode(f"{username}:{key}".encode()).decode()
        self._headers = {"Authorization": f"Basic {token}", "Accept": "application/json"}
        self._http = http or default_http

    def build(self, build_id: str) -> dict:
        if not BUILD_ID_RE.match(build_id or ""):
            raise VerifyError(f"'{str(build_id)[:80]}' is not a BrowserStack build id")
        code, body = self._http("GET", f"{API}/builds/{build_id}", dict(self._headers))
        if code == 401:
            raise VerifyError("BrowserStack rejected the coordinator's credentials (401)")
        if code == 404:
            raise VerifyError(f"BrowserStack has no build {build_id} on this account (404)")
        if code != 200:
            raise VerifyError(f"BrowserStack API returned HTTP {code} for build {build_id}")
        try:
            data = json.loads(body or b"{}")
        except json.JSONDecodeError:
            raise VerifyError("BrowserStack API returned non-JSON for the build") from None
        if not isinstance(data, dict):
            raise VerifyError("BrowserStack API returned an unexpected build object")
        return data


def _count(value) -> int:
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return 0


def verify_build(client: Client, build_id: str, sha: str, devices: list) -> dict:
    """The device evidence the workflow records, built from the API only.
    Raises VerifyError when the build cannot be bound to `sha` or is unfinished;
    returns status "fail" with the failures when it ran and did not pass."""
    data = client.build(build_id)
    if str(data.get("id") or "") != build_id:
        raise VerifyError(f"the API returned build '{str(data.get('id'))[:64]}', not {build_id}")
    framework = str(data.get("framework") or "xcuitest").lower()
    if framework != "xcuitest":
        raise VerifyError(f"build {build_id} is a {framework} build, not xcuitest")
    caps = data.get("input_capabilities") if isinstance(data.get("input_capabilities"), dict) else {}
    tag = str(caps.get("buildTag") or caps.get("build_tag") or data.get("build_tag") or "")
    if tag != sha:
        raise VerifyError(f"build {build_id} is tagged '{tag[:64] or 'nothing'}', not the exact SHA {sha}; "
                          "start it with \"buildTag\": \"<sha>\"")
    status = str(data.get("status") or "").lower()
    if status in UNFINISHED or not status:
        raise VerifyError(f"build {build_id} has not finished (status {status or 'unknown'})")
    out_devices, failures = [], []
    for dev in data.get("devices") or [] if isinstance(data.get("devices"), list) else []:
        if not isinstance(dev, dict):
            continue
        name, osv = str(dev.get("device") or "")[:80], str(dev.get("os_version") or "")[:20]
        sessions = []
        for s in dev.get("sessions") or [] if isinstance(dev.get("sessions"), list) else []:
            if not isinstance(s, dict):
                continue
            tc = s.get("testcases") if isinstance(s.get("testcases"), dict) else {}
            counts = tc.get("status") if isinstance(tc.get("status"), dict) else {}
            sessions.append({"id": str(s.get("id") or "")[:64], "status": str(s.get("status") or "").lower(),
                             "duration": _count(s.get("duration")), "count": _count(tc.get("count")),
                             **{k: _count(counts.get(k)) for k in ("passed", "failed", "skipped", "timedout", "error")}})
        out_devices.append({"kind": device_kind(name), "device": name, "os_version": osv, "sessions": sessions})
        for s in sessions:
            bad = s["failed"] + s["timedout"] + s["error"]
            if s["status"] != "passed" or bad or not s["passed"] or not s["id"]:
                failures.append(f"{name} (iOS {osv}) session {s['id'] or '?'}: status {s['status'] or 'none'}, "
                                f"{s['passed']} passed, {s['failed']} failed, {s['timedout']} timed out, "
                                f"{s['error']} errored")
        if not sessions:
            failures.append(f"{name} (iOS {osv}): no session ran")
    seen = {d["kind"] for d in out_devices if d["sessions"]}
    for kind in DEVICE_KINDS:
        if kind not in seen:
            failures.append(f"no real {kind} session in build {build_id}")
    names = {d["device"].lower() for d in out_devices}
    for wanted in devices:
        model = wanted.rsplit("-", 1)[0].strip().lower()
        if model not in names:
            failures.append(f"requested device '{wanted}' did not run in build {build_id}")
    if status not in DONE_OK:
        failures.insert(0, f"build {build_id} ended with status {status}")
    return {"build_id": build_id, "build_status": status, "dashboard_url": DASHBOARD + build_id,
            "verified_by": VERIFIED_BY, "devices": out_devices, "failures": failures,
            "status": "fail" if failures else "pass",
            "app_bundle_id": str((data.get("app_details") or {}).get("bundle_id") or "")[:200]
            if isinstance(data.get("app_details"), dict) else ""}


def verify(build_id: str, sha: str, env=None, http: Optional[Http] = None) -> dict:
    """verify_build with the activation file's credentials and device list."""
    act = activation(env)
    user, key = credentials(env)
    return verify_build(Client(user, key, http), build_id, sha, act["devices"])


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("preflight", help="exit 0 active, 3 disabled, 2 misconfigured; never prints credentials")
    v = sub.add_parser("verify", help="re-read a finished build from the API and print the device evidence")
    v.add_argument("--build-id", required=True)
    v.add_argument("--sha", required=True)
    args = p.parse_args(argv)
    if args.cmd == "preflight":
        act = activation()
        print(json.dumps(act, indent=2))
        return {"active": 0, "disabled": 3}.get(act["state"], 2)
    try:
        result = verify(args.build_id, args.sha)
    except VerifyError as exc:
        print(f"browserstack: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
