"""Fixture for test_kanban_workflow.UpstreamCompatTest: apply the kit's native ops with the
real runtime handlers. Run only under the pinned scratch runtime's venv, with the upstream
checkout on sys.path and a throwaway HERMES_HOME (never the live runtime or board).

    python upstream_native.py schema          # {tool: [argument keys]} from the real schemas
    python upstream_native.py apply < ops     # stdin {"ops": [...]}; HERMES_PROFILE is the identity
    python upstream_native.py bind TASK PID   # the worker_bound event bind_worker_context writes, for PID
"""

import json
import sys

import tools.kanban_tools as kt
from tools.kanban_tools_schemas import KANBAN_COMMENT_SCHEMA, KANBAN_CREATE_SCHEMA, KANBAN_LINK_SCHEMA

HANDLERS = {"kanban_create": kt._handle_create, "kanban_comment": kt._handle_comment,
            "kanban_link": kt._handle_link}

if sys.argv[1] == "schema":
    print(json.dumps({s["name"]: sorted(s["parameters"]["properties"])
                      for s in (KANBAN_CREATE_SCHEMA, KANBAN_COMMENT_SCHEMA, KANBAN_LINK_SCHEMA)}))
elif sys.argv[1] == "bind":
    # gateway/session_kanban.py bind_worker_context, minus the exec: the same stale-claim check, the
    # real fingerprint of PID and the same event row, written for the claimed card's current run.
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect_closing
    from hermes_cli.kanban_db_dispatch import UNVERIFIED_WORKER_FINGERPRINT, _process_fingerprint
    task_id, pid = sys.argv[2], int(sys.argv[3])
    with connect_closing() as conn:
        with kb.write_txn(conn):
            task = kb.get_task(conn, task_id)
            if task is None or task.status != "running" or not task.current_run_id or not task.claim_lock:
                raise SystemExit("stale_kanban_claim")
            fingerprint = _process_fingerprint(pid) or UNVERIFIED_WORKER_FINGERPRINT
            kb._append_event(conn, task.id, "worker_bound", {"pid": pid, "claim_lock": task.claim_lock,
                             "started_at": fingerprint}, run_id=task.current_run_id)
    print(json.dumps({"task": task_id, "run_id": task.current_run_id, "pid": pid, "started_at": fingerprint}))
else:
    print(json.dumps([json.loads(HANDLERS[op["tool"]](op["args"])) for op in json.load(sys.stdin)["ops"]]))
