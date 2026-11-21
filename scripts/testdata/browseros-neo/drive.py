"""drive.py URL [wait_s]: open URL in a new tab via CDP, wait for window.__done, print __results JSON."""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); import cdp
url = sys.argv[1]; wait = float(sys.argv[2]) if len(sys.argv) > 2 else 30
b, v = cdp.browser()
url = url.replace("__CDP__", v["webSocketDebuggerUrl"])
tid = b.call("Target.createTarget", {"url": url})["targetId"]
sid = b.call("Target.attachToTarget", {"targetId": tid, "flatten": True})["sessionId"]
end = time.time() + wait; res = None
while time.time() < end:
    try:
        r = b.call("Runtime.evaluate", {"expression": "window.__done ? JSON.stringify(window.__results) : ''", "returnByValue": True}, session=sid)
        val = r.get("result", {}).get("value")
        if val: res = json.loads(val); break
    except RuntimeError: pass
    time.sleep(0.5)
print(json.dumps({"url": url.split("?")[0], "targetId": tid, "results": res}, indent=1))
