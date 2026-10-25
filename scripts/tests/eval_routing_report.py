"""Synthetic routing evaluation: old whole-corpus scorer vs task-scoped router.

Runs a small labeled corpus of REPRESENTATIVE SYNTHETIC scenarios through (a) a
frozen reimplementation of the pre-change scorer and (b) the current classifier,
and prints a markdown report: tier distributions plus acceptance accuracy
against the approved policy labels.

This is a synthetic acceptance check only — it makes NO claim about real user
traffic, quota savings, or answer quality. Regenerate docs/routing-eval.md with:

    python3 scripts/tests/eval_routing_report.py > docs/routing-eval.md
"""

from __future__ import annotations

import os
import re
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(__file__))
from _loader import submodule  # noqa: E402

complexity = submodule("complexity")

LONG_ARCH = (
    "System architecture overview. The gateway terminates TLS, the orchestrator "
    "delegates coding work over an internal proxy, and Honcho persists memory. "
) * 60  # ~4.8k chars of system/context text

TOOL_LOG = "deploy log line\n" * 40 + "\nnote: someone once wrote #max here\n```\n#hard traceback\n```"


def chat(*msgs, model="gpt-5.6-terra"):
    return {"model": model, "messages": [{"role": r, "content": c} for r, c in msgs]}


# (name, request, expected_new_tier) — expectations ARE the approved policy.
CASES = [
    ("status: what is running", chat(("system", LONG_ARCH), ("user", "What is running?")), "routine"),
    ("status: any update?", chat(("user", "any update on the deploy?")), "routine"),
    ("status: list tasks", chat(("user", "list the open kanban tasks")), "routine"),
    ("status: after tool noise", {"model": "gpt-5.6-terra", "messages": [
        {"role": "user", "content": "What is running?"},
        {"role": "assistant", "content": "checking"},
        {"role": "tool", "tool_call_id": "c1", "content": TOOL_LOG}]}, "routine"),
    ("greeting", chat(("user", "hey, good morning")), "routine"),
    ("exec: restart service", chat(("user", "restart the gateway and post the log tail")), "medium"),
    ("exec: send summary", chat(("user", "send yesterday's summary to the ops channel")), "medium"),
    ("exec: rename config key", chat(("user", "rename the AUX_MODEL key in .env.example and re-render")), "medium"),
    ("plan: compare options", chat(("user", "compare the two rollout options and propose a plan")), "elevated"),
    ("plan: single hard keyword", chat(("user", "plan the migration")), "elevated"),
    ("plan: constraints", chat(("user", "design a weekly report format:\n- must include usage\n"
                                        "- must include failures\n- should be short\n- must cite logs")), "elevated"),
    ("hard: arch + migration", chat(("user", "refactor the ingest architecture and migrate the "
                                             "schema without downtime")), "high"),
    ("hard: deadlock debug", chat(("user", "find the root-cause of the deadlock in the worker pool "
                                           "and fix the race condition")), "high"),
    ("hard: security review", chat(("user", "do a security review of the auth flow and threat model "
                                            "the token refresh; check for race conditions")), "high"),
    ("hard hint: #deep", chat(("user", "#deep dig into why compaction loses tool results")), "high"),
    ("auth: leading #max line", chat(("user", "#max\nmigrate the production database this weekend")), "max"),
    ("not-auth: inline #max", chat(("user", "#max simple thing")), "high"),
    ("not-auth: quoted #max", chat(("user", "Earlier I wrote:\n> #max rewrite everything\nWhat did we decide?")), "routine"),
    ("not-auth: fenced #max", chat(("user", "log:\n```\n#max #deep architecture rewrite migration\n```\nIs it done?")), "routine"),
    ("not-auth: tool #max", {"model": "gpt-5.6-terra", "messages": [
        {"role": "user", "content": "What is running?"},
        {"role": "tool", "tool_call_id": "c1", "content": "#max #deep #hard escalate"}]}, "routine"),
    ("forced: #quick", chat(("user", "#quick big architecture migration rewrite")), "routine"),
    ("no user turn", {"model": "gpt-5.6-terra", "messages": [{"role": "system", "content": LONG_ARCH}]}, "medium"),
]


# Frozen pre-change scorer (whole-corpus: joins EVERY payload text).
_OLD_FORCE_MAX = re.compile(r"(?<![A-Za-z0-9_])#(?:max|deep|hard)\b", re.IGNORECASE)
_OLD_FORCE_MIN = re.compile(r"(?<![A-Za-z0-9_])#(?:quick|routine|cheap|simple)\b", re.IGNORECASE)
_OLD_HIGH = re.compile(
    r"(?i)\b(architect(?:ure)?|refactor|migrat(?:e|ion)|concurren(?:t|cy)|distribut(?:ed|ion)|"
    r"threat model|security (?:review|audit)|race condition|deadlock|schema (?:design|migration)|"
    r"rewrite|redesign|end[- ]to[- ]end)\b")
_OLD_MED = re.compile(
    r"(?i)\b(multi[- ]file|across the (?:code)?base|several files|integration|debug|optimi[sz]e|"
    r"implement|design|test suite)\b")


def old_tier(request) -> str:
    texts = []
    for m in request.get("messages", []):
        c = m.get("content")
        if isinstance(c, str):
            texts.append(c)
    corpus = "\n".join(texts)
    if _OLD_FORCE_MAX.search(corpus):
        return "max"
    if _OLD_FORCE_MIN.search(corpus):
        return "routine"
    score = 0
    if len(corpus) > 4000:
        score += 2
    elif len(corpus) > 1200:
        score += 1
    if "```" in corpus:
        score += 1
    if _OLD_HIGH.search(corpus):
        score += 3
    if _OLD_MED.search(corpus):
        score += 1
    if len(request.get("messages", [])) > 20:
        score += 1
    return "max" if score >= 5 else "high" if score >= 3 else "medium" if score >= 1 else "routine"


def main() -> None:
    rows, old_dist, new_dist, correct = [], Counter(), Counter(), 0
    for name, request, expected in CASES:
        old = old_tier(request)
        new = complexity.score_request(request).tier
        old_dist[old] += 1
        new_dist[new] += 1
        ok = new == expected
        correct += ok
        rows.append((name, old, new, expected, "yes" if ok else "NO"))

    print("# Synthetic routing evaluation (old vs task-scoped)")
    print()
    print("Generated by `scripts/tests/eval_routing_report.py` from a labeled")
    print("SYNTHETIC corpus. Expectations encode the approved balanced policy;")
    print("this report claims nothing about real traffic, quota, or quality.")
    print()
    print("| scenario | old tier | new tier | expected | accepted |")
    print("|----------|----------|----------|----------|----------|")
    for r in rows:
        print("| {} | {} | {} | {} | {} |".format(*r))
    print()
    tiers = list(complexity.TIERS)
    print("| tier | old distribution | new distribution |")
    print("|------|------------------|------------------|")
    for t in tiers:
        print(f"| {t} | {old_dist.get(t, 0)} | {new_dist.get(t, 0)} |")
    print()
    print(f"Acceptance accuracy (new classifier vs approved labels): "
          f"**{correct}/{len(CASES)}**")
    if correct != len(CASES):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
