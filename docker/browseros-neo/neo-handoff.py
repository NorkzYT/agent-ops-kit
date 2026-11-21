"""Prepare a running Neo for a human sign-in: restart the browser, then scrub and verify.

Run INSIDE the neo container as the neo user, fed on stdin by
`scripts/browseros-neo.sh handoff` (never mounted, so the checked-out file is
what runs):

  docker exec -i -u neo <neo> /usr/bin/python3 -I - [--clear-origin ORIGIN]... < neo-handoff.py

The agent's MCP path must already be closed and drained (the wrapper checks).
Steps, each verified; any failure exits non-zero with a JSON report:

1. Quit the browser the image's own way (neo-quit, CDP Browser.close; SIGTERM
   only if it does not exit) and wait for the entrypoint to start a new one.
   Every page, timer, injected script, listener, dedicated/shared worker,
   running service worker and per-session CDP override dies with the old
   process. The managed policy (chromium-policy.json) forces RestoreOnStartup
   to the New Tab page, so no tab comes back.
2. Over the new browser's loopback CDP port: unregister every service worker
   registration in the profile's listing (the first
   ServiceWorker.workerRegistrationUpdated after enable; no listing within
   LISTING_TIMEOUT fails, silence is never read as "none") and clear
   service-worker and Cache Storage data for its origin; clear all site data
   for each --clear-origin (the sign-in origins); delete every cookie that
   would be sent to a --clear-origin host (host-only on that host, or a
   Domain= cookie on it or a parent domain, so a cookie tossed from a sibling
   subdomain is included) and every cookie partitioned under a site of that
   host; clear the HTTP cache; close every tab except one about:blank.
3. Re-enumerate from a fresh CDP session: zero service-worker registrations,
   zero service-worker/Cache Storage bytes on those origins, zero bytes of
   every quota-managed type on each --clear-origin (Storage.getUsageAndQuota:
   on the pinned Chromium 155 its breakdown lists only cache_storage,
   file_systems, indexeddb and service_workers; a breakdown missing any of
   the types checked fails), no cookie matching the rule above
   (Storage.getCookies; usage has no cookie entry), and
   only about:blank plus the browser's own chrome://*.top-chrome UI targets;
   the kept about:blank page must still exist and still be about:blank.
   Anything else fails. Cache Storage and other storage on origins that had
   no service worker and are not a --clear-origin is not cleared or checked.
   localStorage/sessionStorage are not verified: usage does not count them
   and DOMStorage needs a frame on the origin (loading it runs its code).
   clearDataForOrigin("all") removed a planted localStorage value on the
   pinned image, but handoff does not re-check it.

At least one --clear-origin is required; without it nothing is touched.
Limits (also in the report): the same persistent profile is resumed by a new
browser process (not the same tab); verified zero usage covers only the
scrubbed origins and does not prove the whole profile clean. Real-account
login stays NO-GO (docs/browseros-neo.md, 'Login status').

Stdlib only (the image has python3 but no WebSocket client).
"""
import base64
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from urllib.parse import urlparse

CDP_PORT = 9110           # pinned by the image's entrypoint
READY_URL = "http://127.0.0.1:9010/system/ready"
BROWSER = "/usr/lib/browserclaw/browserclaw"
ALLOWED_UI = ("chrome://omnibox-popup.top-chrome/",)
LISTING_TIMEOUT = 30      # seconds to wait for the first registration listing
LISTING_QUIET = 1.0       # then keep reading this long for follow-up updates
MAX_MESSAGE = 64 << 20    # a larger CDP message (agent-controlled size) fails


class Fail(Exception):
    pass


REPORT = {}  # filled as steps complete; printed on success and on failure


def browser_pids():
    """Main browser processes (the browserclaw binary without --type=)."""
    out = []
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            with open(f"/proc/{d}/cmdline", "rb") as f:
                argv = f.read().split(b"\0")
        except OSError:
            continue
        if argv and argv[0].decode(errors="replace") == BROWSER and not any(a.startswith(b"--type=") for a in argv):
            out.append(int(d))
    return out


def one_browser_pid():
    pids = browser_pids()
    if len(pids) != 1:
        raise Fail(f"expected exactly one browser process, found {pids}")
    return pids[0]


def wait(pred, seconds, what):
    end = time.time() + seconds
    while time.time() < end:
        try:
            v = pred()
            if v:
                return v
        except (OSError, ValueError, Fail):
            pass
        time.sleep(0.5)
    raise Fail(f"timed out after {seconds}s waiting for {what}")


class CDP:
    def __init__(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json/version", timeout=5) as r:
            url = urlparse(json.load(r)["webSocketDebuggerUrl"])
        if url.hostname != "127.0.0.1" or url.port != CDP_PORT:
            raise Fail(f"unexpected CDP endpoint {url.geturl()}")
        self.s = socket.create_connection((url.hostname, url.port), timeout=30)
        key = base64.b64encode(os.urandom(16)).decode()
        self.s.sendall((f"GET {url.path} HTTP/1.1\r\nHost: {url.netloc}\r\nUpgrade: websocket\r\n"
                        f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.s.recv(4096)
            if not chunk:
                raise Fail("CDP handshake closed")
            buf += chunk
        head, self.buf = buf.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise Fail("CDP handshake refused")
        self.n = 0
        self.events = []
        self.partial = b""

    def _frame(self):
        """(b1, payload) of the first complete frame in the buffer, or None.

        Nothing is consumed until the whole frame is buffered, so a socket
        timeout mid-frame (collect() uses short timeouts) loses no bytes.
        """
        b = self.buf
        if len(b) < 2:
            return None
        n, off = b[1] & 0x7F, 2
        if n == 126:
            if len(b) < 4:
                return None
            n, off = int.from_bytes(b[2:4], "big"), 4
        elif n == 127:
            if len(b) < 10:
                return None
            n, off = int.from_bytes(b[2:10], "big"), 10
        if n + len(self.partial) > MAX_MESSAGE:
            raise Fail(f"CDP message larger than {MAX_MESSAGE} bytes")
        if len(b) < off + n:
            return None
        self.buf = b[off + n:]
        return b[0], b[off:off + n]

    def recv(self):
        while True:
            f = self._frame()
            if f is None:
                chunk = self.s.recv(65536)  # a timeout here leaves buf/partial intact
                if not chunk:
                    raise Fail("CDP connection closed")
                self.buf += chunk
                continue
            b1, payload = f
            op = b1 & 0x0F
            if op == 8:
                raise Fail("CDP connection closed")
            if op in (9, 10):
                continue
            self.partial += payload
            if b1 & 0x80:
                msg, self.partial = self.partial, b""
                return json.loads(msg)

    def send(self, obj):
        data = json.dumps(obj).encode()
        mask = os.urandom(4)
        hdr = bytearray([0x81])
        if len(data) < 126:
            hdr.append(0x80 | len(data))
        elif len(data) < 65536:
            hdr += bytes([0x80 | 126]) + len(data).to_bytes(2, "big")
        else:
            hdr += bytes([0x80 | 127]) + len(data).to_bytes(8, "big")
        self.s.sendall(bytes(hdr) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def call(self, method, params=None, session=None):
        self.n += 1
        msg = {"id": self.n, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        self.send(msg)
        end = time.time() + 30
        while time.time() < end:
            r = self.recv()
            if r.get("id") == self.n:
                if "error" in r:
                    raise Fail(f"{method}: {r['error']}")
                return r.get("result", {})
            self.events.append(r)
        raise Fail(f"{method}: no answer")

    def collect(self, seconds, until=None):
        """Read events for `seconds`, or until until(events) is true; returns whether it was."""
        end = time.time() + seconds
        self.s.settimeout(0.25)
        try:
            while time.time() < end:
                if until is not None and until(self.events):
                    return True
                try:
                    self.events.append(self.recv())
                except (socket.timeout, TimeoutError):
                    pass
        finally:
            self.s.settimeout(30)
        return until is not None and until(self.events)

    def close(self):
        self.s.close()


def origin_of(url):
    u = urlparse(url)
    return f"{u.scheme}://{u.netloc}"


def _is_listing(e):
    return e.get("method") == "ServiceWorker.workerRegistrationUpdated"


def registrations(c, session):
    """Every service-worker registration in the profile, from a fresh enable.

    Chromium answers enable with one workerRegistrationUpdated listing the
    stored and live registrations (an empty list when there are none). Its
    absence is a failure, never "zero registrations".
    """
    c.events = []
    c.call("ServiceWorker.enable", session=session)
    if not c.collect(LISTING_TIMEOUT, until=lambda ev: any(_is_listing(e) for e in ev)):
        raise Fail(f"no service-worker registration listing within {LISTING_TIMEOUT}s; "
                   "cannot show that no registration exists")
    c.collect(LISTING_QUIET)
    regs = {}
    for e in c.events:
        if e.get("method") == "ServiceWorker.workerRegistrationUpdated":
            for r in e["params"]["registrations"]:
                regs[r["registrationId"]] = r
    return [r for r in regs.values() if not r.get("isDeleted")]


def _host_match(host, domain):
    return host == domain or host.endswith("." + domain)


def _partition_site(ck):
    pk = ck.get("partitionKey")
    site = pk.get("topLevelSite") if isinstance(pk, dict) else pk
    return (urlparse(site).hostname or "").lower() if site else ""


def cookie_hits(cookies, clear_origins):
    """Cookies a sign-in on any --clear-origin could receive or carry.

    A host-only cookie (no leading dot) matches only its host; a Domain=
    cookie matches its domain and every subdomain (so .example.com set from
    www.example.com reaches accounts.example.com); a partitioned cookie also
    matches when its partition's top-level site is the host or a parent of
    it (sent to embeds of the sign-in page). Paths, ports and Secure are
    ignored, which only widens the match.
    """
    hosts = [urlparse(o).hostname.lower() for o in clear_origins]
    out = []
    for ck in cookies:
        raw = str(ck.get("domain") or "").lower()
        dom = raw.lstrip(".")
        site = _partition_site(ck)
        for h in hosts:
            if (h == dom if not raw.startswith(".") else _host_match(h, dom)) or (site and _host_match(h, site)):
                out.append(ck)
                break
    return out


def cookie_label(ck):
    site = _partition_site(ck)
    return f"{ck.get('name')}@{ck.get('domain')}{ck.get('path') or ''}" + (f" [partition {site}]" if site else "")


def all_cookies(c):
    return c.call("Storage.getCookies").get("cookies", [])


def delete_cookies(c, session, clear_origins):
    """Delete every cookie_hits() match, one exact (name, domain, path, partition) at a time."""
    hits = cookie_hits(all_cookies(c), clear_origins)
    for ck in hits:
        p = {"name": ck["name"], "domain": ck["domain"], "path": ck.get("path") or "/"}
        if ck.get("partitionKey"):
            p["partitionKey"] = ck["partitionKey"]
        c.call("Network.deleteCookies", p, session=session)
    return sorted(cookie_label(ck) for ck in hits)


def quit_browser():
    """neo-quit's own stdout goes to stderr: stdout carries only the JSON report."""
    return subprocess.run(["/usr/local/bin/neo-quit", str(CDP_PORT)], timeout=20,
                          stdin=subprocess.DEVNULL, stdout=sys.stderr).returncode


def scratch_tab(c):
    tid = c.call("Target.createTarget", {"url": "about:blank"})["targetId"]
    sid = c.call("Target.attachToTarget", {"targetId": tid, "flatten": True})["sessionId"]
    return tid, sid


def target_problems(c, keep):
    """Every target except the kept about:blank page and allowed browser UI.

    The kept target is not trusted by ID alone: it must still exist, still be
    a page and still be about:blank, or verification fails.
    """
    bad = []
    kept = []
    for t in c.call("Target.getTargets")["targetInfos"]:
        if t["targetId"] == keep:
            kept.append(t)
            if t.get("type") != "page" or t.get("url") != "about:blank":
                bad.append(f"kept target moved: {t.get('type')} {str(t.get('url'))[:120]}")
            continue
        if t["type"] == "browser_ui" and t["url"].startswith(ALLOWED_UI):
            continue
        bad.append(f"{t['type']} {t['url'][:120]}")
    if not kept:
        bad.append("kept about:blank target missing")
    return bad


def parse_args(argv):
    """--clear-origin ORIGIN, at least once; refused before anything is touched."""
    clear_origins = []
    it = iter(argv)
    for a in it:
        if a != "--clear-origin":
            raise Fail(f"unknown argument {a!r}")
        o = next(it, "")
        u = urlparse(o)
        try:
            port_ok = u.port is None or 1 <= u.port <= 65535
        except ValueError:
            port_ok = False
        if u.scheme != "https" or not u.hostname or u.username is not None or u.password is not None \
                or "@" in u.netloc or not port_ok or u.path not in ("", "/") or u.query or u.fragment \
                or origin_of(o) != o.rstrip("/"):
            raise Fail(f"--clear-origin must be an https origin like https://accounts.example.com, got {o!r}")
        clear_origins.append(origin_of(o))
    if not clear_origins:
        raise Fail("at least one explicit --clear-origin is required (the disposable sign-in origins); nothing was touched")
    return clear_origins


# usageBreakdown types the pinned Chromium 155 reports (observed live,
# 2026-11-20); every --clear-origin must report all of them.
QUOTA_TYPES = ("cache_storage", "file_systems", "indexeddb", "service_workers")
SW_TYPES = ("cache_storage", "service_workers")


def verify(c, sid, keep, sw_origins, clear_origins):
    """Re-enumerate from a fresh session; returns the report fields and ok."""
    sw_left = registrations(c, sid)
    # Storage.getUsageAndQuota needs no frame on the origin (CacheStorage.*
    # does, and loading the origin would run its code again). It counts only
    # quota-managed storage (QUOTA_TYPES on the pinned Chromium; no
    # localStorage, sessionStorage or cookies). Every type left non-zero on a
    # scrubbed origin fails the handoff, and so does a breakdown that omits a
    # type being checked (a vacuous zero is never "clean").
    storage_left, storage_unchecked = {}, {}
    for o, types in [(o, SW_TYPES) for o in sw_origins] + [(o, QUOTA_TYPES) for o in clear_origins]:
        u = c.call("Storage.getUsageAndQuota", {"origin": o}, session=sid)
        breakdown = u.get("usageBreakdown", [])
        reported = {b["storageType"] for b in breakdown}
        missing = sorted(set(types) - reported)
        if missing:
            storage_unchecked.setdefault(o, set()).update(missing)
        usage = {b["storageType"]: b["usage"] for b in breakdown
                 if b["usage"] and (types is QUOTA_TYPES or b["storageType"] in types)}
        if usage:
            storage_left.setdefault(o, {}).update(usage)
    storage_unchecked = {o: sorted(v) for o, v in storage_unchecked.items()}
    cookies_left = sorted(cookie_label(ck) for ck in cookie_hits(all_cookies(c), clear_origins))
    bad = target_problems(c, keep)
    fields = dict(service_workers_left=sorted(r["scopeURL"] for r in sw_left),
                  storage_left=storage_left, storage_unchecked=storage_unchecked,
                  cookies_left=cookies_left, unexpected_targets=bad)
    return fields, not (sw_left or storage_left or storage_unchecked or cookies_left or bad)


LIMITS = [
    "the browser process restarted on the same persistent profile; no tab was resumed",
    "only the listed --clear-origin origins had all site data cleared; cookies that would be sent to their "
    "hosts (host-only, parent-domain Domain= cookies, cookies partitioned under their sites) were deleted and "
    "verified absent; other origins keep cookies/localStorage/IndexedDB/history/preferences",
    "service-worker/Cache Storage bytes were verified zero only on origins that had a listed service worker; "
    "Cache Storage on other origins (no worker, not a --clear-origin) was not cleared or checked",
    "verified zero usage covers quota-managed storage only (cache_storage, file_systems, indexeddb, "
    "service_workers); localStorage and sessionStorage on --clear-origin origins were cleared but not verified",
    "this does not prove the whole profile is clean",
    "real-account login remains NO-GO; disposable test accounts only: host processes (including an agent's "
    "terminal) can reach Neo's unauthenticated MCP/settings ports on its bridge address and drive the viewer "
    "with a forged Origin, gate paused or not",
]


def main(argv):
    clear_origins = parse_args(argv)

    report = REPORT
    report["clear_origins"] = clear_origins
    report["limits"] = LIMITS
    old = one_browser_pid()
    report["old_browser_pid"] = old

    # 1. restart the browser process
    rc = quit_browser()
    report["neo_quit_rc"] = rc
    try:
        wait(lambda: not os.path.exists(f"/proc/{old}"), 30, "the old browser to exit")
    except Fail:
        os.kill(old, signal.SIGTERM)
        report["sigterm"] = True
        wait(lambda: not os.path.exists(f"/proc/{old}"), 20, "the old browser to exit after SIGTERM")
    new = wait(lambda: (lambda p: p if p != old else None)(one_browser_pid()), 90, "the entrypoint to start a new browser")
    report["new_browser_pid"] = new
    wait(lambda: urllib.request.urlopen(READY_URL, timeout=3).status == 200, 120, "claw-server to reconnect")

    # 2. scrub
    c = CDP()
    keep, sid = scratch_tab(c)
    regs = registrations(c, sid)
    report["service_workers_found"] = sorted(r["scopeURL"] for r in regs)
    sw_origins = sorted({origin_of(r["scopeURL"]) for r in regs})
    for r in regs:
        c.call("ServiceWorker.unregister", {"scopeURL": r["scopeURL"]}, session=sid)
    c.call("ServiceWorker.disable", session=sid)
    for o in sw_origins:
        c.call("Storage.clearDataForOrigin", {"origin": o, "storageTypes": "service_workers,cache_storage"}, session=sid)
    for o in clear_origins:
        c.call("Storage.clearDataForOrigin", {"origin": o, "storageTypes": "all"}, session=sid)
    report["cookies_deleted"] = delete_cookies(c, sid, clear_origins)
    c.call("Network.enable", session=sid)
    c.call("Network.clearBrowserCache", session=sid)
    c.call("Network.disable", session=sid)
    closed = []
    for t in c.call("Target.getTargets")["targetInfos"]:
        if t["type"] == "page" and t["targetId"] != keep:
            c.call("Target.closeTarget", {"targetId": t["targetId"]})
            closed.append(t["url"][:120])
    report["closed_tabs"] = closed
    report["scope"] = {"service_worker_origins_cleared": sw_origins,
                       "all_site_data_cleared": clear_origins,
                       "cookies_deleted_for_hosts_and_parent_domains": clear_origins,
                       "http_cache_cleared": True}
    c.close()

    # 3. verify from a fresh connection
    time.sleep(2)
    if one_browser_pid() != new:
        raise Fail("the browser restarted again during the scrub")
    c = CDP()
    sid = c.call("Target.attachToTarget", {"targetId": keep, "flatten": True})["sessionId"]
    fields, ok = verify(c, sid, keep, sw_origins, clear_origins)
    c.close()
    report.update(fields)
    if not ok:
        raise Fail("scrub did not verify")
    report["result"] = "HANDOFF-READY"
    return report


if __name__ == "__main__":
    try:
        rep = main(sys.argv[1:])
        print(json.dumps(rep, sort_keys=True))
        sys.exit(0)
    except (Fail, OSError, ValueError, KeyError, subprocess.SubprocessError) as e:
        REPORT.update(result="FAILED", error=str(e) if isinstance(e, Fail) else repr(e))
        print(json.dumps(REPORT, sort_keys=True))
        sys.exit(1)
