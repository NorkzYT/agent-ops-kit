#!/usr/bin/env python3
"""knowledge_review — the readable review artifacts knowledge-classify.py writes next to
every report and plan:

  <report>.review.md    a complete Markdown review: totals, assignments per profile, every
                        new-profile proposal and every source in original order, with the
                        classifier's facts kept apart from deterministic review guidance;
  <report>.review.html  a self-contained, offline decision workspace: search, filters by
                        status/decision/profile, one readable panel per item and per
                        new-profile proposal, client-side editing of decision / profile /
                        proposal approval / spec fields, and export of the revised plan JSON.

Both are derived from the report and the plan; the plan JSON stays the authority and
nothing here assigns, creates or approves anything. The HTML embeds no external
JavaScript, CSS or fonts, no book text (titles, rationales and metadata only) and no
secrets: every string reaches the page either HTML-escaped (static markup) or inside a
JSON data block whose ``<``, ``>`` and ``&`` are ``\\u``-escaped, and the application
script renders through text nodes only (never innerHTML). The export merges the
operator's edits into a deep copy of the original plan and touches only the editable
fields (item decision and profile; proposal approved, purpose, in_scope, out_of_scope,
parent, overlap, boundaries): schema, hash and identity fields are preserved verbatim.

No review reason is a risk label: ``suspicious-text`` is an integrity check on the
untrusted book text (prompt-injection phrasing), never a topic judgement.
"""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

REVIEW_MD_SUFFIX = ".review.md"
REVIEW_HTML_SUFFIX = ".review.html"
DECISIONS = ("assign", "skip", "existing", "review")
SPEC_LIST_FIELDS = ("in_scope", "out_of_scope", "boundaries")
# Per-item report fields the HTML carries (never staged file paths or excerpts).
ITEM_FACT_FIELDS = ("status", "classifier", "evidence", "model_requested", "effort_requested", "model_served",
                    "latency_s", "usage", "topics", "privacy", "library_exemption", "stage_problems", "action",
                    "action_note", "needs_review", "review_reasons", "secondary_reason")


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #
def review_paths_for(report: Path) -> Tuple[Path, Path]:
    """``<report stem>.review.md`` / ``.review.html`` next to the report (never ``*.json``)."""
    stem = report.name[:-5] if report.name.endswith(".json") else report.name
    return report.with_name(stem + REVIEW_MD_SUFFIX), report.with_name(stem + REVIEW_HTML_SUFFIX)


def md_text(value: object) -> str:
    """Inline Markdown-safe text: HTML-escaped, pipes and line breaks neutralised."""
    text = "" if value is None else str(value)
    text = html.escape(text, quote=False).replace("|", "\\|")
    return " ".join(text.split())


def md_code(value: object) -> str:
    text = md_text(value).replace("`", "'")
    return f"`{text}`" if text else "-"


def title_of(source: str) -> str:
    return source.rsplit("/", 1)[-1] if "/" in source else source


def pct(conf: object) -> str:
    if isinstance(conf, bool) or not isinstance(conf, (int, float)):
        return "not provided"
    return f"{round(float(conf) * 100)}%"


def plan_index(plan: Dict[str, object]) -> Dict[str, Dict[str, object]]:
    return {str(i.get("source")): i for i in plan.get("items", []) if isinstance(i, dict)}


def proposal_index(plan: Dict[str, object]) -> Dict[str, Dict[str, object]]:
    return {str(p.get("name")): p for p in plan.get("new_profiles", []) if isinstance(p, dict)}


def guidance_for(item: Dict[str, object], plan_item: Dict[str, object], threshold: float) -> Tuple[str, str]:
    """Deterministic review guidance (headline, text) derived from status and review reasons.

    This is navigation help for the operator, not a decision and not a judgement of the
    book's topic; the classifier's own output is reported separately as facts."""
    status = str(item.get("status", ""))
    reasons = [str(r) for r in item.get("review_reasons") or []]
    owners = [str(o) for o in item.get("owners") or []]
    conf = item.get("confidence")
    proposal = item.get("new_profile") or {}
    if status == "already-imported":
        if len(owners) > 1:
            return ("Existing book with several owners",
                    "Its text is already staged in more than one profile (owners are profile names, not authors). "
                    "Nothing is re-ingested; keep `existing`, or reconcile ownership separately with "
                    "`make knowledge-reconcile`.")
        return ("Existing staged book",
                "Its text is already staged; keep `existing`. Nothing is classified, copied or re-ingested.")
    if status == "error":
        return ("Classifier or path error",
                f"{item.get('action_note') or 'the source could not be processed'}. Re-run "
                "`make knowledge-classify SOURCE='<source>'` or set a decision by hand.")
    if status == "skipped":
        return ("Not sent to any classifier",
                f"{item.get('action_note') or 'skipped'}. Decide by hand (`assign` with a profile, or `skip`).")
    if status == "approved":
        return ("Operator approval", "Assigned from an explicit approval; no model call was made.")
    parts: List[str] = []
    if any(r.startswith("classifier-") for r in reasons):
        parts.append("The answer was malformed, refused or unknown: nothing from it can be trusted; classify again "
                     "or decide by hand.")
    if "suspicious-text" in reasons:
        parts.append("`suspicious-text` is an integrity check: the metadata or excerpt contains prompt-injection-like "
                     "phrasing (for example 'ignore previous instructions'). Inspect the staged text before trusting "
                     "the proposal; it says nothing about the topic.")
    if "no-profile" in reasons:
        if proposal:
            parts.append(f"No existing profile fits; the classifier proposed a new profile `{md_text(proposal.get('name'))}` "
                         "(listed under new profile proposals). Either approve that proposal (complete spec, "
                         "`approved: true`) and keep this item on it, or pick an existing profile; then set the "
                         "decision to `assign`.")
        else:
            parts.append("No existing profile fits and no new profile was proposed: pick a profile or `skip`.")
    if "low-confidence" in reasons:
        parts.append(f"Confidence {pct(conf)} is below the threshold {round(threshold * 100)}%: check the intended "
                     "use before accepting the proposed profile.")
    if "multiple-owners" in reasons or (owners and status != "already-imported"):
        parts.append(f"Part of its text is already staged in {', '.join(owners)}: compare editions before assigning "
                     "it again (one canonical profile per book).")
    for r in ("metadata-only", "no-text", "private-source", "private-excerpt", "content-unchecked"):
        if r in reasons:
            parts.append(f"`{r}`: the evidence was thin or partly withheld, so the proposal rests on less than usual.")
    if parts:
        return ("Resolve the review flags", " ".join(parts))
    if isinstance(conf, (int, float)) and not isinstance(conf, bool) and float(conf) < 0.85:
        return ("Reasonable fit; double-check purpose",
                f"Confidence {pct(conf)} passed the threshold but is not high: confirm the proposed profile matches "
                "how you intend to use the book.")
    return ("Accept if scope matches",
            "The proposal passed every guardrail. Keep `assign` unless another profile matches the intended use "
            "better (change `profile`), or `skip` it for this run.")


def totals(report: Dict[str, object], plan: Dict[str, object]) -> Dict[str, int]:
    pidx = plan_index(plan)
    out = {"books": 0, "assign": 0, "review": 0, "existing": 0, "skip": 0, "errors": 0, "new_profiles": 0}
    for it in report.get("items", []):
        out["books"] += 1
        d = str(pidx.get(str(it.get("source")), {}).get("decision", "review"))
        out[d if d in out else "review"] += 1
        if it.get("error"):
            out["errors"] += 1
    out["new_profiles"] = len(plan.get("new_profiles", []))
    return out


def per_profile(report: Dict[str, object], plan: Dict[str, object]) -> List[Tuple[str, int, int, int]]:
    """(profile, assign, review, existing-owner) rows, most assignments first."""
    pidx = plan_index(plan)
    rows: Dict[str, List[int]] = {}
    for it in report.get("items", []):
        pi = pidx.get(str(it.get("source")), {})
        d = str(pi.get("decision", "review"))
        target = str(pi.get("profile") or it.get("profile") or "")
        if d in ("assign", "review") and target:
            rows.setdefault(target, [0, 0, 0])[0 if d == "assign" else 1] += 1
        for o in it.get("owners") or []:
            rows.setdefault(str(o), [0, 0, 0])[2] += 1
    return sorted(((k, v[0], v[1], v[2]) for k, v in rows.items()), key=lambda r: (-r[1], -r[2], -r[3], r[0]))


# --------------------------------------------------------------------------- #
# Markdown
# --------------------------------------------------------------------------- #
def render_markdown(report: Dict[str, object], plan: Dict[str, object]) -> str:
    run = report.get("run", {}) if isinstance(report.get("run"), dict) else {}
    items = [i for i in report.get("items", []) if isinstance(i, dict)]
    pidx, props = plan_index(plan), proposal_index(plan)
    threshold = float(run.get("threshold") or 0.75)
    plan_path = str(plan.get("_path") or plan.get("plan_path") or "")
    report_path = str(plan.get("report") or "")
    md_path, html_path = review_paths_for(Path(report_path)) if report_path else (Path("<report>.review.md"), Path("<report>.review.html"))
    plan_name = Path(plan_path).name if plan_path else ("plan-" + Path(report_path).name if report_path else "plan-<report>.json")
    who = md_code(f"{run.get('classifier', '')}:{run.get('model', '')}" + (f"/{run['effort']}" if run.get("effort") else ""))
    t = totals(report, plan)
    L: List[str] = []
    L.append(f"# Book classification review — {md_text(Path(report_path).name if report_path else 'report')}")
    L.append("")
    L.append(f"**Generated:** {md_text(run.get('generated', ''))}  ")
    L.append(f"**Classifier:** {who}" + (f" (served by: {md_text(', '.join(run.get('models_served') or []))})" if run.get("models_served") else "") + "  ")
    L.append(f"**Threshold:** {round(threshold * 100)}%; **excerpt:** {md_text(run.get('excerpt_chars', ''))} chars; "
             f"**library:** {md_code(run.get('library', ''))} (read-only); **applied:** {md_text(run.get('applied', False))}  ")
    L.append(f"**Plan (the authority):** {md_code(plan_path or plan_name)}  ")
    L.append(f"**Workspace:** {md_code(html_path.name)} (offline HTML: search, filter, edit decisions and specs, export the revised plan)")
    L.append("")
    L.append("> **Classifier facts** are what the model and the deterministic guardrails produced (profile, confidence, "
             "rationale, review reasons, request metadata). **Review guidance** is navigation help derived from the "
             "status and flags; it decides nothing. Edit the plan (or export it from the workspace), then "
             f"`make knowledge-apply PLAN={md_text(plan_path or plan_name)} DRY_RUN=1`.")
    L.append("")
    L.append("## Totals")
    L.append("")
    L.append("| Metric | Count |")
    L.append("|---|---|")
    for key, label in (("books", "Books"), ("assign", "Decision `assign` (ready)"), ("review", "Decision `review` (blocks apply)"),
                       ("existing", "Decision `existing` (already imported)"), ("skip", "Decision `skip`"),
                       ("errors", "Errors"), ("new_profiles", "New profile proposals (unapproved)")):
        L.append(f"| {label} | {t[key]} |")
    L.append("")
    L.append("## Assignments per profile")
    L.append("")
    L.append("| Profile | Assign | Review | Existing owner of |")
    L.append("|---|---|---|---|")
    for name, a, r, e in per_profile(report, plan):
        tag = " (proposed new profile)" if name in props else ""
        L.append(f"| {md_text(name)}{tag} | {a} | {r} | {e} |")
    L.append("")
    L.append("## New profile proposals")
    L.append("")
    if not props:
        L.append("None.")
    for name, spec in sorted(props.items()):
        L.append(f"### {md_code(name)}")
        L.append("")
        L.append(f"- **Approved:** {md_text(spec.get('approved', False))}; **spec status:** {md_code(spec.get('spec_status', ''))}")
        L.append(f"- **Purpose:** {md_text(spec.get('purpose', ''))}")
        L.append(f"- **Parent:** {md_code(spec.get('parent') or 'none')}; **overlap:** {md_text(', '.join(spec.get('overlap') or []) or '-')}")
        for key in SPEC_LIST_FIELDS:
            L.append(f"- **{key.replace('_', ' ').capitalize()}:**")
            for x in spec.get(key) or []:
                L.append(f"  - {md_text(x)}")
        L.append(f"- **Sources:** {md_text('; '.join(spec.get('sources') or []))}")
        L.append("- **Review guidance:** a proposal only; nothing exists until the plan is applied with "
                 "`approved: true` and a complete spec. Prefer one broad profile over several near-duplicates "
                 "when proposals overlap.")
        L.append("")
    L.append("## Every source (original order)")
    L.append("")
    for n, it in enumerate(items, 1):
        src = str(it.get("source", ""))
        pi = pidx.get(src, {})
        head, text = guidance_for(it, pi, threshold)
        owners = [str(o) for o in it.get("owners") or []]
        proposal = it.get("new_profile") or {}
        L.append(f"### {n:02d}. {md_text(title_of(src))}")
        L.append("")
        L.append(f"- **Source:** {md_code(src)}")
        L.append(f"- **Status:** {md_code(it.get('status'))}; **decision (plan):** {md_code(pi.get('decision', 'review'))}")
        L.append(f"- **Existing owners:** {md_text(', '.join(owners)) if owners else 'none'}")
        L.append(f"- **Proposed profile:** {md_code(it.get('profile') or (proposal.get('name') + ' (new)' if proposal else ''))}; "
                 f"**selected profile (plan):** {md_code(pi.get('profile', ''))}")
        L.append(f"- **Confidence:** {pct(it.get('confidence'))}")
        L.append(f"- **Review reasons:** {md_text(', '.join(str(r) for r in it.get('review_reasons') or [])) or 'none'}")
        facts = []
        if it.get("rationale"):
            facts.append(f"rationale: {md_text(it.get('rationale'))}")
        if it.get("topics"):
            facts.append(f"topics: {md_text(', '.join(str(x) for x in it.get('topics') or []))}")
        if it.get("classifier"):
            sent = f"sent to {md_code(it.get('classifier'))}"
            if it.get("effort_requested"):
                sent += f" at effort {md_code(it.get('effort_requested'))}"
            sent += f" ({md_text(it.get('evidence'))}, {md_text(it.get('sent_chars', 0))} chars)"
            if it.get("model_served"):
                sent += f"; served by {md_code(it.get('model_served'))}"
            if it.get("latency_s"):
                sent += f"; {md_text(it.get('latency_s'))}s"
            facts.append(sent)
        if it.get("imported_as"):
            facts.append("imported as: " + "; ".join(
                f"{md_text(m.get('profile'))}/{md_text(m.get('staged'))} ({md_text(m.get('evidence'))} {md_text(m.get('score'))})"
                for m in it.get("imported_as") or [] if isinstance(m, dict)))
        if it.get("privacy"):
            facts.append(f"privacy detectors: {md_text(', '.join(it.get('privacy') or []))}"
                         + (" (book-library exemption used)" if it.get("library_exemption") else ""))
        if it.get("stage_problems"):
            facts.append(f"staging: {md_text('; '.join(it.get('stage_problems') or []))}")
        if it.get("action_note"):
            facts.append(f"note: {md_text(it.get('action_note'))}")
        L.append("- **Classifier facts:** " + ("; ".join(facts) if facts else "none (not classified)"))
        L.append(f"- **Review guidance — {head}:** {text}")
        L.append(f"- **Source ID:** {md_code(pi.get('source_id') or '')}")
        if proposal:
            L.append(f"- **New-profile spec:** see `{md_text(proposal.get('name'))}` under new profile proposals "
                     f"(purpose: {md_text(proposal.get('purpose'))})")
        L.append("")
    L.append("## Next steps")
    L.append("")
    L.append(f"1. Open {md_code(html_path.name)} (search, filter, edit decisions and specs, export the revised plan) "
             f"or edit {md_code(plan_name)} directly: every decision `assign` (with profile), `skip` or `existing`; "
             "approve a new profile only with a complete spec and `approved: true`.")
    L.append(f"2. `make knowledge-apply PLAN={md_text(plan_path or plan_name)} DRY_RUN=1`")
    L.append(f"3. `make knowledge-apply PLAN={md_text(plan_path or plan_name)} APPROVE=1`")
    L.append("")
    return "\n".join(L)


# --------------------------------------------------------------------------- #
# HTML workspace
# --------------------------------------------------------------------------- #
def json_for_html(data: object) -> str:
    """JSON that is inert inside a <script type=application/json> block: no '<', '>' or '&'."""
    return (json.dumps(data, ensure_ascii=True, separators=(",", ":"))
            .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))


def report_for_html(report: Dict[str, object]) -> Dict[str, object]:
    run = report.get("run", {}) if isinstance(report.get("run"), dict) else {}
    items = []
    for it in report.get("items", []):
        if not isinstance(it, dict):
            continue
        row: Dict[str, object] = {"source": it.get("source", ""), "profile": it.get("profile", ""),
                                  "confidence": it.get("confidence"), "rationale": it.get("rationale", ""),
                                  "owners": it.get("owners") or [], "new_profile": it.get("new_profile") or {},
                                  "sent_chars": it.get("sent_chars", 0), "error": bool(it.get("error"))}
        for key in ITEM_FACT_FIELDS:
            if key in it:
                row[key] = it[key]
        row["imported_as"] = [{k: m.get(k) for k in ("profile", "staged", "evidence", "score")}
                              for m in it.get("imported_as") or [] if isinstance(m, dict)]
        items.append(row)
    keep = ("generated", "applied", "library", "classifier", "model", "effort", "effort_note", "models_served",
            "threshold", "excerpt_chars", "library_exemption")
    return {"run": {k: run.get(k) for k in keep if k in run}, "items": items}


def app_script() -> str:
    """The workspace application. ``KCReview.revisedPlan(original, edits)`` is pure and testable in node;
    ``boot()`` runs only in a browser."""
    return APP_JS


def render_html(report: Dict[str, object], plan: Dict[str, object]) -> str:
    run = report.get("run", {}) if isinstance(report.get("run"), dict) else {}
    report_name = Path(str(plan.get("report") or "report")).name
    plan_public = {k: v for k, v in plan.items() if not str(k).startswith("_")}
    title = html.escape(f"Book classification workspace — {report_name}")
    t = totals(report, plan)
    chips = "".join(f'<span class="chip"><b>{v}</b> {html.escape(k.replace("_", " "))}</span>' for k, v in t.items())
    who = html.escape(f"{run.get('classifier', '')}:{run.get('model', '')}" + (f"/{run['effort']}" if run.get("effort") else ""))
    fields = {"title": title, "who": who, "generated": html.escape(str(run.get("generated", ""))), "chips": chips,
              "plan_name": html.escape(Path(str(plan.get("_path") or "plan-" + report_name)).name),
              "css": CSS, "plan_json": json_for_html(plan_public),
              "report_json": json_for_html(report_for_html(report)), "app": APP_JS}
    page = HTML_TEMPLATE
    for key, value in fields.items():            # plain substitution: the CSS/JS contain braces
        page = page.replace("@@" + key.upper() + "@@", value)
    return page


def write_review(report_path: Path, report: Dict[str, object], plan: Dict[str, object]) -> Tuple[Path, Path]:
    md_path, html_path = review_paths_for(report_path)
    md_path.write_text(render_markdown(report, plan), encoding="utf-8")
    html_path.write_text(render_html(report, plan), encoding="utf-8")
    return md_path, html_path


CSS = """
:root{--bg:#f6f7f9;--fg:#1d2430;--mut:#5a6472;--line:#d9dee5;--card:#fff;--acc:#2558a6;--warn:#9a5b00;--ok:#1d7a4a;--bad:#a12d2d}
*{box-sizing:border-box}body{margin:0;font:14px/1.45 system-ui,sans-serif;color:var(--fg);background:var(--bg)}
header,section,aside{padding:12px 16px}header{background:var(--card);border-bottom:1px solid var(--line)}
h1{font-size:18px;margin:0 0 4px}h2{font-size:15px;margin:12px 0 6px}h3{font-size:14px;margin:8px 0 4px}
.muted{color:var(--mut)}.chip{display:inline-block;margin:2px 6px 2px 0;padding:2px 8px;border:1px solid var(--line);border-radius:12px;background:var(--card)}
#controls{display:flex;flex-wrap:wrap;gap:8px;align-items:center;background:var(--card);border-bottom:1px solid var(--line)}
input,select,textarea,button{font:inherit;padding:4px 6px;border:1px solid var(--line);border-radius:4px;background:#fff}
button{cursor:pointer;background:var(--acc);color:#fff;border-color:var(--acc)}button.plain{background:#fff;color:var(--fg);border-color:var(--line)}
main{display:grid;grid-template-columns:minmax(260px,34%) 1fr;gap:0}
#list{border-right:1px solid var(--line);max-height:70vh;overflow:auto;background:var(--card)}
.row{padding:6px 8px;border-bottom:1px solid var(--line);cursor:pointer}.row:hover,.row.sel{background:#e9f0fb}
.row .t{font-weight:600}.row .m{font-size:12px;color:var(--mut)}
.tag{display:inline-block;font-size:11px;padding:0 6px;border-radius:8px;border:1px solid var(--line);margin-left:4px}
.tag.assign{color:var(--ok)}.tag.review{color:var(--warn)}.tag.existing{color:var(--acc)}.tag.skip{color:var(--mut)}.tag.edited{color:var(--bad)}
#detail{max-height:70vh;overflow:auto}.card{background:var(--card);border:1px solid var(--line);border-radius:6px;padding:10px 12px;margin-bottom:10px}
dl{display:grid;grid-template-columns:max-content 1fr;gap:4px 12px;margin:0}dt{color:var(--mut)}dd{margin:0;word-break:break-word}
.facts{border-left:3px solid var(--acc);padding-left:8px}.guide{border-left:3px solid var(--warn);padding-left:8px}
label.f{display:block;margin:6px 0}label.f span{display:inline-block;min-width:110px;color:var(--mut)}textarea{width:100%;min-height:56px}
#export-json{width:100%;min-height:140px;font:12px/1.4 ui-monospace,monospace}
.prop{margin-bottom:10px}.counts{margin-left:auto}
"""

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>@@TITLE@@</title>
<style>@@CSS@@</style>
</head>
<body>
<header>
  <h1>@@TITLE@@</h1>
  <div class="muted">Classifier @@WHO@@ · generated @@GENERATED@@ · plan file @@PLAN_NAME@@ is the authority; this page only prepares edits to it.</div>
  <div>@@CHIPS@@</div>
  <div class="muted">Classifier facts (blue) are what the model and guardrails produced. Review guidance (amber) is navigation help, not a decision. Nothing on this page assigns, creates or approves anything until you export the revised plan and run knowledge-apply.</div>
</header>
<section id="controls">
  <input id="search" type="search" placeholder="Search title, profile, rationale, reason, source id" size="40">
  <label>status <select id="f-status"><option value="">all</option></select></label>
  <label>decision <select id="f-decision"><option value="">all</option></select></label>
  <label>profile <select id="f-profile"><option value="">all</option></select></label>
  <span class="counts muted" id="counts"></span>
  <button id="btn-export" type="button" title="Download the revised plan JSON">Export revised plan (download)</button>
  <button id="btn-show" type="button" class="plain">Show JSON</button>
  <button id="btn-reset" type="button" class="plain">Reset edits</button>
</section>
<main>
  <aside id="list"></aside>
  <section id="detail"><div class="card muted">Select an item.</div></section>
</main>
<section id="proposals"><h2>New profile proposals</h2><div id="proposal-list"></div></section>
<section id="export"><h2>Revised plan JSON</h2><div class="muted">Also written by the download button. Save it next to the original plan (or over it) and run: make knowledge-apply PLAN=&lt;file&gt; DRY_RUN=1</div><textarea id="export-json" readonly></textarea></section>
<script id="plan-data" type="application/json">@@PLAN_JSON@@</script>
<script id="report-data" type="application/json">@@REPORT_JSON@@</script>
<script>
@@APP@@
</script>
</body>
</html>
"""

APP_JS = r"""
'use strict';
var KCReview = (function () {
  var DECISIONS = ['assign', 'skip', 'existing', 'review'];
  var PROFILE_RE = /^[a-z0-9][a-z0-9-]{0,62}$/;
  var PLACEHOLDER_RE = /^\s*<.*>\s*$|^\s*(tbd|todo|fill ?in)\s*$/i;
  var LIST_FIELDS = ['in_scope', 'out_of_scope', 'boundaries'];

  function clone(x) { return JSON.parse(JSON.stringify(x)); }
  function isStrList(v) { return Array.isArray(v) && v.every(function (x) { return typeof x === 'string'; }); }
  function cleanList(v) {
    if (!isStrList(v)) { return null; }
    return v.map(function (x) { return x.trim(); }).filter(function (x) { return x; });
  }
  function specStatus(spec) {
    var missing = [];
    ['purpose', 'parent'].forEach(function (k) {
      var v = spec[k];
      if (typeof v !== 'string' || !v.trim() || PLACEHOLDER_RE.test(v)) { missing.push(k); }
    });
    LIST_FIELDS.forEach(function (k) {
      var v = spec[k];
      if (!isStrList(v) || !v.length || v.some(function (x) { return PLACEHOLDER_RE.test(x); })) { missing.push(k); }
    });
    return missing.length ? 'incomplete:' + missing.join(',') : 'complete';
  }

  // Pure: a deep copy of the original plan with only the editable fields changed.
  // edits = {items: {source: {decision, profile}}, new_profiles: {name: {approved, purpose, in_scope, out_of_scope, parent, overlap, boundaries}}}
  function revisedPlan(original, edits) {
    var out = clone(original);
    var itemEdits = (edits && edits.items) || {};
    var propEdits = (edits && edits.new_profiles) || {};
    out.items.forEach(function (it) {
      var e = itemEdits[it.source];
      if (!e) { return; }
      if (e.decision !== undefined) {
        if (DECISIONS.indexOf(e.decision) < 0) { throw new Error('invalid decision for ' + it.source + ': ' + e.decision); }
        it.decision = e.decision;
      }
      if (e.profile !== undefined) {
        var p = String(e.profile).trim();
        if (p && !PROFILE_RE.test(p)) { throw new Error('invalid profile name for ' + it.source + ': ' + p); }
        it.profile = p;
      }
    });
    out.new_profiles.forEach(function (spec) {
      var e = propEdits[spec.name];
      if (!e) { return; }
      if (e.approved !== undefined) { spec.approved = e.approved === true; }
      if (typeof e.purpose === 'string') { spec.purpose = e.purpose.trim(); }
      if (typeof e.parent === 'string') {
        var par = e.parent.trim();
        if (par && par !== 'none' && !PROFILE_RE.test(par)) { throw new Error('invalid parent for ' + spec.name + ': ' + par); }
        spec.parent = par;
      }
      var ov = cleanList(e.overlap);
      if (ov) { spec.overlap = ov; }
      LIST_FIELDS.forEach(function (k) {
        var v = cleanList(e[k]);
        if (v) { spec[k] = v; }
      });
      if (spec.spec_status !== undefined) { spec.spec_status = specStatus(spec); }
    });
    return out;
  }

  return { revisedPlan: revisedPlan, specStatus: specStatus, DECISIONS: DECISIONS };
}());

function boot() {
  var plan = JSON.parse(document.getElementById('plan-data').textContent);
  var report = JSON.parse(document.getElementById('report-data').textContent);
  var facts = {};
  report.items.forEach(function (it) { facts[it.source] = it; });
  var edits = { items: {}, new_profiles: {} };
  var selected = plan.items.length ? plan.items[0].source : '';
  var threshold = Number(report.run.threshold || 0.75);

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) { e.className = cls; }
    if (text !== undefined && text !== null) { e.textContent = String(text); }
    return e;
  }
  function clear(node) { while (node.firstChild) { node.removeChild(node.firstChild); } }
  function title(src) { var i = src.lastIndexOf('/'); return i >= 0 ? src.slice(i + 1) : src; }
  function pct(c) { return (typeof c === 'number') ? Math.round(c * 100) + '%' : 'not provided'; }
  function current(it) {
    var e = edits.items[it.source] || {};
    return { decision: e.decision !== undefined ? e.decision : it.decision,
             profile: e.profile !== undefined ? e.profile : it.profile };
  }
  function edited(src) { return Object.prototype.hasOwnProperty.call(edits.items, src); }
  function proposalNames() { return plan.new_profiles.map(function (p) { return p.name; }); }
  function knownProfiles() {
    var s = {};
    (plan.profiles || []).forEach(function (p) { s[p] = 1; });
    proposalNames().forEach(function (p) { s[p] = 1; });
    return Object.keys(s).sort();
  }

  function guidance(it, f) {
    var reasons = (f && f.review_reasons) || [];
    var owners = (f && f.owners) || [];
    var status = f ? f.status : it.status;
    var out = [];
    if (status === 'already-imported') {
      return [owners.length > 1 ? 'Existing book with several owners' : 'Existing staged book',
              'Its text is already staged (owners are profile names, not authors). Keep existing; reconcile ownership separately with make knowledge-reconcile.'];
    }
    if (status === 'error' || status === 'skipped') {
      return ['Not classified', (f && f.action_note) || status + ': decide by hand or re-run the classifier for this source.'];
    }
    if (reasons.some(function (r) { return r.indexOf('classifier-') === 0; })) {
      out.push('The answer was malformed, refused or unknown: nothing from it can be trusted; classify again or decide by hand.');
    }
    if (reasons.indexOf('suspicious-text') >= 0) {
      out.push('suspicious-text is an integrity check: prompt-injection-like phrasing in the metadata or excerpt. Inspect the staged text before trusting the proposal; it says nothing about the topic.');
    }
    if (reasons.indexOf('no-profile') >= 0) {
      out.push(it.new_profile_suggestion && it.new_profile_suggestion.name
        ? 'No existing profile fits; the classifier proposed the new profile ' + it.new_profile_suggestion.name + '. Approve that proposal below (complete spec, approved) and keep this item on it, or pick an existing profile; then set decision assign.'
        : 'No existing profile fits and nothing was proposed: pick a profile or skip.');
    }
    if (reasons.indexOf('low-confidence') >= 0) {
      out.push('Confidence ' + pct(it.confidence) + ' is below the threshold ' + Math.round(threshold * 100) + '%: check the intended use before accepting.');
    }
    if (owners.length) { out.push('Part of its text is already staged in ' + owners.join(', ') + ': compare editions before assigning it again.'); }
    ['metadata-only', 'no-text', 'private-source', 'private-excerpt', 'content-unchecked'].forEach(function (r) {
      if (reasons.indexOf(r) >= 0) { out.push(r + ': the evidence was thin or partly withheld.'); }
    });
    if (out.length) { return ['Resolve the review flags', out.join(' ')]; }
    if (typeof it.confidence === 'number' && it.confidence < 0.85) {
      return ['Reasonable fit; double-check purpose', 'Confidence ' + pct(it.confidence) + ' passed the threshold but is not high: confirm the proposed profile matches the intended use.'];
    }
    return ['Accept if scope matches', 'The proposal passed every guardrail. Keep assign unless another profile matches better, or skip it for this run.'];
  }

  function matches(it) {
    var q = document.getElementById('search').value.trim().toLowerCase();
    var fs = document.getElementById('f-status').value;
    var fd = document.getElementById('f-decision').value;
    var fp = document.getElementById('f-profile').value;
    var cur = current(it);
    var f = facts[it.source] || {};
    if (fs && it.status !== fs) { return false; }
    if (fd && cur.decision !== fd) { return false; }
    if (fp && cur.profile !== fp && (f.owners || []).indexOf(fp) < 0) { return false; }
    if (!q) { return true; }
    var hay = [it.source, cur.profile, it.proposed_profile, it.rationale, (it.review_reasons || []).join(' '),
               it.source_id, (f.owners || []).join(' '), (f.topics || []).join(' ')].join(' ').toLowerCase();
    return hay.indexOf(q) >= 0;
  }

  function renderList() {
    var list = document.getElementById('list');
    clear(list);
    var shown = 0;
    plan.items.forEach(function (it) {
      if (!matches(it)) { return; }
      shown += 1;
      var cur = current(it);
      var row = el('div', 'row' + (it.source === selected ? ' sel' : ''));
      row.setAttribute('data-source', it.source);
      var t = el('div', 't', title(it.source));
      t.appendChild(el('span', 'tag ' + cur.decision, cur.decision));
      if (edited(it.source)) { t.appendChild(el('span', 'tag edited', 'edited')); }
      row.appendChild(t);
      row.appendChild(el('div', 'm', (cur.profile || (facts[it.source] && facts[it.source].owners || []).join(', ') || 'no profile') +
        ' · ' + it.status + ' · ' + pct(it.confidence)));
      row.addEventListener('click', function () { selected = it.source; renderList(); renderDetail(); });
      list.appendChild(row);
    });
    document.getElementById('counts').textContent = shown + ' of ' + plan.items.length + ' shown · ' +
      Object.keys(edits.items).length + ' item edits · ' + Object.keys(edits.new_profiles).length + ' proposal edits';
  }

  function dl(parent, pairs) {
    var d = el('dl');
    pairs.forEach(function (p) {
      if (p[1] === undefined || p[1] === null || p[1] === '') { return; }
      d.appendChild(el('dt', '', p[0]));
      d.appendChild(el('dd', '', p[1]));
    });
    parent.appendChild(d);
  }

  function renderDetail() {
    var box = document.getElementById('detail');
    clear(box);
    var it = null;
    plan.items.forEach(function (x) { if (x.source === selected) { it = x; } });
    if (!it) { box.appendChild(el('div', 'card muted', 'Select an item.')); return; }
    var f = facts[it.source] || {};
    var cur = current(it);
    var card = el('div', 'card');
    card.appendChild(el('h2', '', title(it.source)));
    card.appendChild(el('div', 'muted', it.source));
    var factsBox = el('div', 'facts');
    factsBox.appendChild(el('h3', '', 'Classifier facts'));
    var sent = f.classifier ? f.classifier + (f.effort_requested ? ' at effort ' + f.effort_requested : '') +
      ' (' + f.evidence + ', ' + (f.sent_chars || 0) + ' chars' + (f.latency_s ? ', ' + f.latency_s + 's' : '') + ')' +
      (f.model_served && f.model_served !== f.model_requested ? '; served by ' + f.model_served : '') : '';
    dl(factsBox, [['status', it.status], ['proposed profile', it.proposed_profile || (it.new_profile_suggestion && it.new_profile_suggestion.name ? it.new_profile_suggestion.name + ' (new)' : '')],
      ['confidence', pct(it.confidence)], ['existing owners', (f.owners || []).join(', ')],
      ['review reasons', (it.review_reasons || []).join(', ') || 'none'], ['rationale', it.rationale],
      ['topics', (f.topics || []).join(', ')], ['request', sent],
      ['tokens', f.usage && Object.keys(f.usage).length ? Object.keys(f.usage).map(function (k) { return k + '=' + f.usage[k]; }).join(' ') : ''],
      ['imported as', (f.imported_as || []).map(function (m) { return m.profile + '/' + m.staged + ' (' + m.evidence + ' ' + m.score + ')'; }).join('; ')],
      ['privacy detectors', (f.privacy || []).join(', ') + (f.library_exemption ? ' (book-library exemption used)' : '')],
      ['staging', (f.stage_problems || []).join('; ')], ['note', f.action_note || it.note],
      ['source id', it.source_id], ['sha256', it.source_sha256]]);
    card.appendChild(factsBox);
    var g = guidance(it, f);
    var guide = el('div', 'guide');
    guide.appendChild(el('h3', '', 'Review guidance — ' + g[0]));
    guide.appendChild(el('p', '', g[1]));
    card.appendChild(guide);

    var form = el('div');
    form.appendChild(el('h3', '', 'Decision (edits stay in this page until you export)'));
    var ld = el('label', 'f'); ld.appendChild(el('span', '', 'decision'));
    var sd = el('select');
    KCReview.DECISIONS.forEach(function (d) { var o = el('option', '', d); o.value = d; if (d === cur.decision) { o.selected = true; } sd.appendChild(o); });
    sd.addEventListener('change', function () { setItem(it.source, 'decision', sd.value); });
    ld.appendChild(sd); form.appendChild(ld);
    var lp = el('label', 'f'); lp.appendChild(el('span', '', 'profile'));
    var ip = el('input'); ip.type = 'text'; ip.value = cur.profile; ip.setAttribute('list', 'profile-names');
    ip.addEventListener('change', function () { setItem(it.source, 'profile', ip.value.trim()); });
    lp.appendChild(ip); form.appendChild(lp);
    var dlst = el('datalist'); dlst.id = 'profile-names';
    knownProfiles().forEach(function (p) { var o = el('option'); o.value = p; dlst.appendChild(o); });
    form.appendChild(dlst);
    if (edited(it.source)) {
      var undo = el('button', 'plain', 'Undo edits for this item'); undo.type = 'button';
      undo.addEventListener('click', function () { delete edits.items[it.source]; renderAll(); });
      form.appendChild(undo);
    }
    card.appendChild(form);
    box.appendChild(card);
  }

  function setItem(src, key, value) {
    var e = edits.items[src] || {};
    e[key] = value;
    edits.items[src] = e;
    renderList();
    renderDetail();
  }

  function listField(parent, spec, key, label) {
    var cur = (edits.new_profiles[spec.name] && edits.new_profiles[spec.name][key]) || spec[key] || [];
    var l = el('label', 'f'); l.appendChild(el('span', '', label));
    var ta = el('textarea'); ta.value = cur.join('\n'); ta.title = 'one statement per line';
    ta.addEventListener('change', function () {
      setProp(spec.name, key, ta.value.split('\n').map(function (x) { return x.trim(); }).filter(function (x) { return x; }));
    });
    l.appendChild(ta); parent.appendChild(l);
  }

  function textField(parent, spec, key, label) {
    var e = edits.new_profiles[spec.name] || {};
    var l = el('label', 'f'); l.appendChild(el('span', '', label));
    var inp = el('input'); inp.type = 'text'; inp.value = e[key] !== undefined ? e[key] : (spec[key] || '');
    inp.addEventListener('change', function () { setProp(spec.name, key, inp.value); });
    l.appendChild(inp); parent.appendChild(l);
  }

  function setProp(name, key, value) {
    var e = edits.new_profiles[name] || {};
    e[key] = value;
    edits.new_profiles[name] = e;
    renderProposals();
    renderList();
  }

  function renderProposals() {
    var box = document.getElementById('proposal-list');
    clear(box);
    if (!plan.new_profiles.length) { box.appendChild(el('div', 'muted', 'None proposed.')); return; }
    plan.new_profiles.forEach(function (spec) {
      var e = edits.new_profiles[spec.name] || {};
      var merged = {}; Object.keys(spec).forEach(function (k) { merged[k] = spec[k]; }); Object.keys(e).forEach(function (k) { merged[k] = e[k]; });
      var card = el('div', 'card prop');
      var h = el('h3', '', spec.name + ' — ' + (merged.approved ? 'APPROVED (will be created by knowledge-apply)' : 'proposal, not approved'));
      card.appendChild(h);
      card.appendChild(el('div', 'muted', 'spec status: ' + KCReview.specStatus(merged) + ' · sources: ' + (spec.sources || []).join('; ')));
      var la = el('label', 'f'); var cb = el('input'); cb.type = 'checkbox'; cb.checked = !!merged.approved;
      cb.addEventListener('change', function () { setProp(spec.name, 'approved', cb.checked); });
      la.appendChild(cb); la.appendChild(el('span', '', ' approved (creates the profile when the plan is applied)'));
      card.appendChild(la);
      textField(card, spec, 'purpose', 'purpose');
      textField(card, spec, 'parent', 'parent');
      listField(card, spec, 'in_scope', 'in scope');
      listField(card, spec, 'out_of_scope', 'out of scope');
      listField(card, spec, 'boundaries', 'boundaries');
      var lo = el('label', 'f'); lo.appendChild(el('span', '', 'overlap'));
      var io = el('input'); io.type = 'text'; io.value = (merged.overlap || []).join(', ');
      io.addEventListener('change', function () { setProp(spec.name, 'overlap', io.value.split(',').map(function (x) { return x.trim(); }).filter(function (x) { return x; })); });
      lo.appendChild(io); card.appendChild(lo);
      card.appendChild(el('div', 'guide', 'Review guidance: a proposal only. Prefer one broad profile over several near-duplicates; nothing exists until the exported plan is applied.'));
      box.appendChild(card);
    });
  }

  function fillFilters() {
    var st = {}, dc = {}, pf = {};
    plan.items.forEach(function (it) {
      st[it.status] = 1; dc[current(it).decision] = 1;
      if (it.profile) { pf[it.profile] = 1; }
      ((facts[it.source] || {}).owners || []).forEach(function (o) { pf[o] = 1; });
    });
    KCReview.DECISIONS.forEach(function (d) { dc[d] = 1; });
    [['f-status', st], ['f-decision', dc], ['f-profile', pf]].forEach(function (pair) {
      var sel = document.getElementById(pair[0]);
      var keep = sel.value;
      while (sel.options.length > 1) { sel.remove(1); }
      Object.keys(pair[1]).sort().forEach(function (v) { var o = el('option', '', v); o.value = v; sel.appendChild(o); });
      sel.value = keep;
    });
  }

  function exported() {
    var out = KCReview.revisedPlan(plan, edits);
    return JSON.stringify(out, null, 2) + '\n';
  }

  function renderAll() { fillFilters(); renderList(); renderDetail(); renderProposals(); }

  ['search', 'f-status', 'f-decision', 'f-profile'].forEach(function (id) {
    document.getElementById(id).addEventListener('input', renderList);
    document.getElementById(id).addEventListener('change', renderList);
  });
  document.getElementById('btn-show').addEventListener('click', function () {
    try { document.getElementById('export-json').value = exported(); } catch (err) { window.alert(String(err.message || err)); }
  });
  document.getElementById('btn-export').addEventListener('click', function () {
    var text;
    try { text = exported(); } catch (err) { window.alert(String(err.message || err)); return; }
    document.getElementById('export-json').value = text;
    var blob = new Blob([text], { type: 'application/json' });
    var a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    var base = (plan.report || 'plan').split('/').pop().replace(/\.json$/, '');
    a.download = 'plan-' + base + '.revised.json';
    document.body.appendChild(a); a.click(); document.body.removeChild(a);
    setTimeout(function () { URL.revokeObjectURL(a.href); }, 1000);
  });
  document.getElementById('btn-reset').addEventListener('click', function () {
    edits = { items: {}, new_profiles: {} }; renderAll();
  });
  renderAll();
}

if (typeof document !== 'undefined' && document.getElementById && document.getElementById('plan-data')) { boot(); }
"""


if __name__ == "__main__":  # pragma: no cover - tiny CLI for re-rendering an existing report
    import sys
    if len(sys.argv) != 2:
        print("usage: knowledge_review.py <classify-report.json>   (re-writes <report>.review.md/.html from the report and its plan)")
        sys.exit(2)
    rp = Path(sys.argv[1])
    rep = json.loads(rp.read_text(encoding="utf-8"))
    pp = rp.with_name("plan-" + rp.name)
    pl = json.loads(pp.read_text(encoding="utf-8"))
    pl["_path"] = str(pp)
    m, h = write_review(rp, rep, pl)
    print(m)
    print(h)
