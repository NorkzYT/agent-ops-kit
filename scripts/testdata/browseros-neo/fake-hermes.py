#!/usr/bin/env python3
"""Fake `hermes config {path,get,set,unset}` for the BrowserOS neo hermes-enable tests.

Mirrors what the real CLI does to $HERMES_HOME/config.yaml, including the part the
helper must not trust: on an unparseable file `config get` says "Config key not set"
(exit 1), exactly like the installed Hermes, while `set`/`unset` refuse.

Test controls (environment):
  FAKE_HERMES_LOG       append one line per call: "<FAKE_HERMES_TAG> <args>"
  FAKE_HERMES_BLOCK     "<verb> <key>" to pause on: touch $FAKE_HERMES_DIR/blocked.<pid>, wait for
                        $FAKE_HERMES_DIR/release (60 s at most)
  FAKE_HERMES_FAIL      "<verb> <key>" that fails with FAKE_HERMES_FAIL_MSG on stderr, exit 9
  FAKE_HERMES_NOOP      "<verb> <key>" that exits 0 without writing (a CLI that claims success)
  FAKE_HERMES_WRONG     "set <key>" that exits 0 but writes FAKE_HERMES_WRONG_VALUE instead
"""
import json
import os
import sys
import time

import yaml


def main(argv):
    home = os.environ["HERMES_HOME"]
    cfg = os.path.join(home, "config.yaml")
    if os.environ.get("FAKE_HERMES_LOG"):
        with open(os.environ["FAKE_HERMES_LOG"], "a") as f:
            f.write(f"{os.environ.get('FAKE_HERMES_TAG', '-')} {' '.join(argv)}\n")
    if argv[:2] == ["config", "path"]:
        print(cfg)
        return 0
    verb_key = " ".join(argv[1:3]) if len(argv) > 2 and argv[1] != "get" else " ".join([argv[1], argv[-1]])
    block = os.environ.get("FAKE_HERMES_BLOCK")
    if block and verb_key == block:
        d = os.environ["FAKE_HERMES_DIR"]
        open(os.path.join(d, f"blocked.{os.getpid()}"), "w").close()
        for _ in range(600):
            if os.path.exists(os.path.join(d, "release")):
                break
            time.sleep(0.1)
    if os.environ.get("FAKE_HERMES_FAIL") == verb_key:
        print(os.environ.get("FAKE_HERMES_FAIL_MSG", "injected failure"), file=sys.stderr)
        return 9
    if os.environ.get("FAKE_HERMES_NOOP") == verb_key:
        return 0
    try:
        with open(cfg) as f:
            doc = yaml.safe_load(f) or {}
        bad = None
    except FileNotFoundError:
        doc, bad = {}, None
    except (OSError, yaml.YAMLError) as e:
        doc, bad = {}, e
    if argv[:2] == ["config", "get"]:
        key = argv[-1]
        node = doc
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                print(f"Config key not set: {key}")
                return 1
            node = node[part]
        print(json.dumps(node))
        return 0
    if argv[:2] in (["config", "set"], ["config", "unset"]):
        if bad is not None:
            print("Your settings file has a formatting error, so this change was not saved.", file=sys.stderr)
            return 1
        parts = argv[2].split(".")
        node = doc
        for part in parts[:-1]:
            if argv[1] == "unset" and part not in node:
                return 0
            node = node.setdefault(part, {})
        if argv[1] == "set":
            val = argv[3]
            if os.environ.get("FAKE_HERMES_WRONG") == verb_key:
                val = os.environ.get("FAKE_HERMES_WRONG_VALUE", "wrong")
            node[parts[-1]] = yaml.safe_load(val)
        else:
            node.pop(parts[-1], None)
        tmp = cfg + ".tmp"
        with open(tmp, "w") as f:
            yaml.safe_dump(doc, f, sort_keys=False)
        os.replace(tmp, cfg)
        return 0
    print(f"fake hermes: unexpected {argv}", file=sys.stderr)
    return 99


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
