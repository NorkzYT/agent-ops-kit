"""DES-01: adapted gstack design consultation -> design brief, package, tokens and a real offline preview.

Two coordinator-authored synthetic scenarios consume different accepted lean PRDs:
trial-guide-design (light, verified fonts, completes) and billing-rota-design
(dark, four breakpoints, pending fonts and memorable thing -> needs_input).
Tests exercise the validator, the renderer, the preview auditor and the CLI;
contrast is recomputed here independently of the module under test.
"""

from __future__ import annotations

import copy
import html.parser
import json
import re
import socket
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_capability_discovery import DiscoveryCase, ctx, errors, load, section  # noqa: E402
from workflow_helpers import EVALS, REG, REPO, TempDirCase, cr, mutate, repack, wf  # noqa: E402


def luminance(hexcolor: str) -> float:
    h = hexcolor.lstrip("#")
    ch = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    ch = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in ch]
    return 0.2126 * ch[0] + 0.7152 * ch[1] + 0.0722 * ch[2]


def ratio(a: str, b: str) -> float:
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


class Tags(html.parser.HTMLParser):
    """Collect start tags and attributes of a document (test-side, independent of the module)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags = []
        self.text = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def handle_startendtag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def handle_data(self, data):
        self.text.append(data)


def parse(doc: str) -> Tags:
    t = Tags()
    t.feed(doc)
    t.close()
    return t


def screen(a, sid):
    return next(s for s in a["screens"] if s["id"] == sid)


def state(a, sid, st):
    return next(x for x in a["states"] if x["screen_id"] == sid and x["state"] == st)


def role(a, r):
    return next(x for x in a["typography"]["roles"] if x["role"] == r)


class ConsultCase(DiscoveryCase):
    scenario, analysis = "trial-guide-design", "analysis-consult"


class TrialGuideConsultation(ConsultCase):
    def test_renders_brief_package_tokens_and_preview_and_completes(self):
        out = self.render(self.a)
        names = {p.name for p in out.iterdir()}
        for f in ("design-brief.md", "design-package.md", "design-tokens.json", "design-preview.html", "result.json", "artifacts.json"):
            self.assertIn(f, names)
        brief = (out / "design-brief.md").read_text()
        self.assertIn("SYNTHETIC fixture data", brief)
        self.assertIn("three steps, without waiting for an email", section(brief, "Memorable thing"))
        journey = section(brief, "User journey")
        for j in self.a["journey"]:
            self.assertIn(f"`{j['id']}`", journey)
        self.assertIn("`t-trial-prd.lean-prd`", brief)
        result = cr.load_json(out / "result.json")
        self.assertEqual(cr.validate_result(result, REG), [])
        self.assertEqual(result["status"], "completed")
        index = cr.load_json(out / "artifacts.json")
        self.assertEqual(cr.verify_artifact_contents(index, out), [])
        kinds = {x["location"]: x["kind"] for x in index["artifacts"] if x["produced_by"]}
        self.assertEqual(kinds["design-brief.md"], "design-brief")
        self.assertEqual(kinds["design-preview.html"], "design-package")
        self.assertEqual(kinds["design-tokens.json"], "design-package")
        prev = next(x for x in index["artifacts"] if x["location"] == "design-preview.html")
        self.assertIn({"artifact_id": "t-trial-prd.lean-prd", "digest": self.a["reused_artifacts"][0]["digest"]}, prev["source_refs"])

    def test_package_reports_contrast_computed_from_the_tokens(self):
        out = self.render(self.a)
        pkg = (out / "design-package.md").read_text()
        light = self.a["color"]["light"]
        for fg, bg in (("text", "bg"), ("text-muted", "surface"), ("on-accent", "accent"), ("border", "bg")):
            want = f"{ratio(light[fg], light[bg]):.2f}:1"
            self.assertRegex(pkg, rf"{re.escape(fg)} on {re.escape(bg)}[^\n]*{re.escape(want)}")
        tokens = json.loads((out / "design-tokens.json").read_text())
        self.assertEqual(tokens["themes"]["light"]["text"], light["text"].lower())
        self.assertEqual(tokens["spacing_px"], self.a["spacing_px"])
        self.assertEqual(tokens["type_scale_px"][0], 16)
        self.assertEqual(tokens["type_scale_px"][2], 25)  # 16 * 1.25^2
        self.assertIn("Not verified", pkg)
        for t in ("keyboard", "text-resize", "status-messages"):
            self.assertRegex(section(pkg, "Accessibility requirements"), rf"{t}[^\n]*not verified")

    def test_preview_is_self_contained_usable_and_covers_every_screen_state_and_token(self):
        out = self.render(self.a)
        doc = (out / "design-preview.html").read_text()
        t = parse(doc)
        tags = [x[0] for x in t.tags]
        self.assertNotIn("script", tags)
        self.assertNotIn("img", tags)
        self.assertNotIn("iframe", tags)
        for tag, attrs in t.tags:
            for k, v in attrs.items():
                self.assertFalse(k.startswith("on"), (tag, k))
                self.assertFalse(re.match(r"\s*(https?:|//|javascript:)", v or "", re.I), (tag, k, v))
        self.assertNotRegex(doc, r"url\(|@import|https?://")
        csp = [a for tg, a in t.tags if tg == "meta" and a.get("http-equiv") == "Content-Security-Policy"]
        self.assertTrue(csp and "default-src 'none'" in csp[0]["content"])
        self.assertEqual(next(a for tg, a in t.tags if tg == "html").get("lang"), "en")
        focusable = [(tg, a) for tg, a in t.tags if tg in ("a", "button", "input")]
        self.assertEqual(focusable[0], ("a", {"class": "skip-link", "href": "#main"}))
        self.assertTrue(any(tg == "main" and a.get("id") == "main" for tg, a in t.tags))
        ids = {a.get("id") for _, a in t.tags}
        for s in self.a["states"]:
            self.assertIn(f"screen-{s['screen_id']}-{s['state']}", ids)
        for name, value in self.a["color"]["light"].items():
            self.assertIn(f"--color-{name}: {value.lower()};", doc.lower())
        for bp in self.a["responsive"]["breakpoints"][1:]:
            self.assertIn(f"@media (min-width: {bp['min_width_px']}px)", doc)
        self.assertIn("@media (prefers-reduced-motion: reduce)", doc)
        self.assertIn(":focus-visible", doc)
        self.assertNotIn("user-scalable=no", doc)
        labels = {a.get("for") for tg, a in t.tags if tg == "label"}
        for tg, a in t.tags:
            if tg == "input":
                self.assertIn(a["id"], labels)
        invalid = [a for tg, a in t.tags if a.get("aria-invalid") == "true"]
        self.assertTrue(invalid)
        for a in invalid:
            self.assertIn(a["aria-describedby"].split()[0], ids)
        text = " ".join(t.text)
        self.assertIn("First-step guide", text)
        self.assertIn("SYNTHETIC", text)
        self.assertIn("Choose another name and try again", text)

    def test_preview_follows_the_supplied_analysis_not_a_template(self):
        def vary(b):
            b["product_name"] = "Quarry ledger"
            b["color"]["light"]["accent"] = "#7A2E0E"
            b["responsive"]["breakpoints"][1]["min_width_px"] = 700
            b["screens"].append({"id": "invite", "name": "Invite a teammate", "purpose": "Step three of the guide.",
                                 "requires_permission": False,
                                 "blocks": [{"kind": "field", "label": "Teammate email", "input": "email"},
                                            {"kind": "action", "label": "Send invite", "emphasis": "primary"}]})
            for st, msg in (("default", None), ("empty", "Nobody invited yet."), ("loading", "Loading teammates."),
                            ("error", "The invite was not sent: the address is not valid. Correct it and send again.")):
                x = {"screen_id": "invite", "state": st}
                if msg:
                    x["message"] = msg
                if st in ("empty", "error"):
                    x["recovery_action"] = "Edit the address"
                b["states"].append(x)
            b["journey"].append({"id": "j-invite", "goal": "Invite a teammate", "screen_id": "invite", "action": "Enters an email and sends.",
                                 "success_signal": "The invite shows as sent.", "failure_state": "error", "statement_ids": []})
        b = mutate(self.a, vary)
        self.assertEqual(errors(b, self.pack, self.store), [])
        doc = (self.render(b) / "design-preview.html").read_text()
        self.assertIn("Quarry ledger", doc)
        self.assertNotIn("First-step guide", doc)
        self.assertIn("--color-accent: #7a2e0e;", doc.lower())
        self.assertIn("@media (min-width: 700px)", doc)
        self.assertNotIn("@media (min-width: 768px)", doc)
        self.assertIn('id="screen-invite-error"', doc)
        self.assertIn('type="email"', doc)

    def test_untrusted_text_is_escaped_in_preview_and_markdown(self):
        payload = '"><img src=x onerror=alert(1)><script>alert(2)</script>'

        def inject(b):
            b["product_name"] = "<script>alert(0)</script>"
            screen(b, "guide")["blocks"][1]["text"] = payload
            state(b, "guide", "error")["message"] = "Name taken " + payload + " choose another and retry."
            b["brief"]["problem"] = "| table | break\n## heading " + payload
        b = mutate(self.a, inject)
        self.assertEqual(errors(b, self.pack, self.store), [])
        out = self.render(b)
        doc = (out / "design-preview.html").read_text()
        t = parse(doc)
        self.assertEqual([x for x in t.tags if x[0] in ("script", "img")], [])
        self.assertFalse(any(k.startswith("on") for _, a in t.tags for k in a))
        self.assertIn("&lt;script&gt;alert(0)&lt;/script&gt;", doc)
        brief = (out / "design-brief.md").read_text()
        self.assertNotIn("<script>", brief)
        self.assertNotIn("\n## heading", brief)
        self.assertIn("\\| table", brief)

    def test_font_family_cannot_carry_css(self):
        bad = mutate(self.a, lambda b: role(b, "display").update(family="Source</style><script>"))
        self.assert_error(errors(bad, self.pack, self.store), r"schema: typography/roles/0/family")

    def test_contrast_below_aa_is_rejected_with_the_computed_ratio(self):
        bad = mutate(self.a, lambda b: b["color"]["light"].update({"text-muted": "#9AA0A6"}))
        want = f"{ratio('#9AA0A6', self.a['color']['light']['bg']):.2f}"
        self.assert_error(errors(bad, self.pack, self.store), rf"light: text-muted on bg is {re.escape(want)}:1, below 4.5:1")
        bad = mutate(self.a, lambda b: b["color"]["light"].update({"focus": "#F2C46D"}))
        self.assert_error(errors(bad, self.pack, self.store), r"light: focus on bg is [0-9.]+:1, below 3:1")

    def test_every_screen_needs_default_empty_loading_and_error_states(self):
        bad = mutate(self.a, lambda b: b.update(states=[s for s in b["states"] if (s["screen_id"], s["state"]) != ("help", "error")]))
        self.assert_error(errors(bad, self.pack, self.store), r"screen help has no error state")
        bad = mutate(self.a, lambda b: screen(b, "help").update(requires_permission=True))
        self.assert_error(errors(bad, self.pack, self.store), r"screen help requires permission but has no permission-denied state")
        bad = mutate(self.a, lambda b: state(b, "guide", "error").pop("recovery_action"))
        self.assert_error(errors(bad, self.pack, self.store), r"guide/error needs a recovery action")
        bad = mutate(self.a, lambda b: state(b, "guide", "error").update(message="Oops! Something went wrong."))
        self.assert_error(errors(bad, self.pack, self.store), r"guide/error.*Oops")
        bad = mutate(self.a, lambda b: state(b, "guide", "success").update(message="Project created!"))
        self.assert_error(errors(bad, self.pack, self.store), r"guide/success.*exclamation")
        bad = mutate(self.a, lambda b: b["states"].append({"screen_id": "nope", "state": "default"}))
        self.assert_error(errors(bad, self.pack, self.store), r"state for unknown screen nope")

    def test_journey_covers_screens_and_failure_states_exist(self):
        bad = mutate(self.a, lambda b: b.update(journey=[j for j in b["journey"] if j["screen_id"] != "help"]))
        self.assert_error(errors(bad, self.pack, self.store), r"screen help is not on the user journey")
        bad = mutate(self.a, lambda b: b["journey"][0].update(failure_state="offline"))
        self.assert_error(errors(bad, self.pack, self.store), r"journey j-name: failure state offline is not designed for screen guide")

    def test_font_rules_from_the_method(self):
        for fam, r, pat in (("Papyrus", "display", "banned"), ("Inter", "display", "overused as a display face"),
                            ("Inter", "body", "not a body face for an operate surface")):
            def setf(b, fam=fam, r=r):
                role(b, r)["family"] = fam
                b["assets"].append({"id": f"font-{fam.lower()}-{r}", "kind": "font", "name": fam, "rights": "licensed",
                                    "license": "fixture", "source_id": "D1"})
            self.assert_error(errors(mutate(self.a, setf), self.pack, self.store), pat)
        plex = {"id": "font-plex", "kind": "font", "name": "IBM Plex Sans", "rights": "pending", "license": "Not recorded", "source_id": "none"}
        ok = mutate(self.a, lambda b: (role(b, "body").update(family="IBM Plex Sans", verification={"status": "pending"}),
                                       b["assets"].append(plex)))
        self.assertEqual(errors(ok, self.pack, self.store), [])  # allowed as body on an operate surface; pending -> needs input
        bad = mutate(self.a, lambda b: (role(b, "body").update(family="IBM Plex Sans"), b["assets"].append(plex)))
        self.assert_error(errors(bad, self.pack, self.store), r"IBM Plex Sans.*D1 does not name it")
        bad = mutate(self.a, lambda b: role(b, "mono")["verification"].update(source_id="D2"))
        self.assert_error(errors(bad, self.pack, self.store), r"JetBrains Mono.*D2 does not record a font licence")
        bad = mutate(self.a, lambda b: b.update(assets=[x for x in b["assets"] if x["name"] != "JetBrains Mono"]))
        self.assert_error(errors(bad, self.pack, self.store), r"font JetBrains Mono has no rights entry")

    def test_tokens_scale_and_scene_rules(self):
        self.assert_error(errors(mutate(self.a, lambda b: b.update(radius_px={"small": 16, "medium": 16, "large": 16})), self.pack, self.store),
                          r"uniform")
        self.assert_error(errors(mutate(self.a, lambda b: b.update(radius_px={"small": 8, "medium": 4, "large": 2})), self.pack, self.store),
                          r"radius")
        self.assert_error(errors(mutate(self.a, lambda b: b["use_scene"].update(theme="dark")), self.pack, self.store),
                          r"theme dark .* bright")
        self.assert_error(errors(mutate(self.a, lambda b: b["use_scene"].update(theme="both")), self.pack, self.store),
                          r"needs a dark palette")
        self.assert_error(errors(mutate(self.a, lambda b: b["responsive"]["breakpoints"][0].update(min_width_px=320)), self.pack, self.store),
                          r"first breakpoint")
        self.assert_error(errors(mutate(self.a, lambda b: b["responsive"]["breakpoints"][2].update(min_width_px=900)), self.pack, self.store),
                          r"1024")
        self.assert_error(errors(mutate(self.a, lambda b: b["responsive"]["breakpoints"][1].update(min_width_px=1100)), self.pack, self.store),
                          r"ascending")
        self.assert_error(errors(mutate(self.a, lambda b: b["motion"].update(short_ms=400, long_ms=200)), self.pack, self.store),
                          r"short_ms")

    def test_accessibility_requirements_cover_the_topics_and_verify_honestly(self):
        bad = mutate(self.a, lambda b: b["accessibility"].update(requirements=[r for r in b["accessibility"]["requirements"] if r["topic"] != "keyboard"]))
        self.assert_error(errors(bad, self.pack, self.store), r"no keyboard requirement")
        bad = mutate(self.a, lambda b: next(r for r in b["accessibility"]["requirements"] if r["topic"] == "keyboard").update(verification="static"))
        self.assert_error(errors(bad, self.pack, self.store), r"keyboard cannot be verified statically")

    def test_copy_and_content_rules(self):
        bad = mutate(self.a, lambda b: screen(b, "guide")["blocks"][1].update(text="Seamless onboarding that will supercharge your team."))
        self.assert_error(errors(bad, self.pack, self.store), r"seamless")
        bad = mutate(self.a, lambda b: screen(b, "guide")["blocks"].append({"kind": "action", "label": "Get started", "emphasis": "primary"}))
        self.assert_error(errors(bad, self.pack, self.store), r"screen guide has 2 primary actions")
        bad = mutate(self.a, lambda b: screen(b, "guide")["blocks"].append({"kind": "text", "text": "Jane Doe from Acme created a project."}))
        self.assert_error(errors(bad, self.pack, self.store), r"placeholder")
        bad = mutate(self.a, lambda b: screen(b, "help")["blocks"].append({"kind": "table", "caption": "x", "columns": ["a", "b"], "rows": [["1"]]}))
        self.assert_error(errors(bad, self.pack, self.store), r"row 1 has 1 cells for 2 columns")

    def test_invented_research_and_claims_are_rejected(self):
        bad = mutate(self.a, lambda b: screen(b, "help")["blocks"].append(
            {"kind": "claim", "text": "Users told us the guide saved them an hour.", "statement_id": "st-h-findable"}))
        self.assert_error(errors(bad, self.pack, self.store), r"claim.* cites st-h-findable, a hypothesis")
        bad = mutate(self.a, lambda b: b["brief"].update(audience="Interviews showed trial users want a checklist."))
        self.assert_error(errors(bad, self.pack, self.store), r"brief.audience reads as user research")
        bad = mutate(self.a, lambda b: b["journey"][1].update(action="We interviewed users and they skip the guide."))
        self.assert_error(errors(bad, self.pack, self.store), r"journey j-skip reads as user research")
        bad = mutate(self.a, lambda b: b["statements"].append({"id": "st-x", "text": "Trial users said they like teal.", "class": "observed", "source_ids": ["D1"]}))
        self.assert_error(errors(bad, self.pack, self.store), r"synthetic task has no observed facts")

    def test_requires_an_accepted_current_product_artifact_and_a_sourced_memorable_answer(self):
        self.assert_error(errors(mutate(self.a, lambda b: b.update(reused_artifacts=[])), self.pack, self.store),
                          r"needs an accepted current")
        self.assert_error(errors(mutate(self.a, lambda b: b["memorable_thing"].pop("answer_source_id")), self.pack, self.store),
                          r"memorable thing is answered but cites no supplied answer")

    def test_preview_audit_catches_a_missing_state_or_an_external_asset(self):
        c = ctx(self.pack, self.store)
        files, errs = wf.render_analysis(self.a, c)
        self.assertEqual(errs, [])
        kind = wf.KINDS["design-consultation"]
        self.assertEqual(kind.post_render(self.a, files), [])
        doc = files["design-preview.html"]
        broken = dict(files, **{"design-preview.html": doc.replace('id="screen-help-error"', 'id="screen-help-x"')})
        self.assert_error(kind.post_render(self.a, broken), r"preview misses screen-help-error")
        broken = dict(files, **{"design-preview.html": doc.replace("</head>", '<link rel="stylesheet" href="https://fonts.example/x.css"></head>')})
        self.assert_error(kind.post_render(self.a, broken), r"external-resource")
        broken = dict(files, **{"design-preview.html": doc.replace("</body>", "<script>1</script></body>")})
        self.assert_error(kind.post_render(self.a, broken), r"script")

    def test_render_opens_no_network_connection(self):
        with mock.patch.object(socket, "socket", side_effect=AssertionError("network used")), \
                mock.patch.object(socket, "create_connection", side_effect=AssertionError("network used")):
            self.render(self.a)

    def test_cli_renders_into_a_fresh_directory_with_a_private_home(self):
        d = EVALS / self.scenario
        out = self.tmp / "cli-out"
        p = self.run_cli(REPO / "scripts/capability_workflow.py", "render", "--analysis", d / "analysis-consult.json",
                         "--evidence", d / "evidence-pack.json", "--artifacts", d / "inputs/artifacts.json", "--out", out)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        res = json.loads(p.stdout)
        self.assertEqual(res["status"], "completed")
        self.assertIn("design-preview.html", res["files"])
        p = self.run_cli(REPO / "scripts/capability_workflow.py", "render", "--analysis", d / "analysis-consult.json",
                         "--evidence", d / "evidence-pack.json", "--artifacts", d / "inputs/artifacts.json", "--out", out)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("already exists", p.stderr)


    def test_preview_skip_link_focus_stays_in_flow_and_h1_fits_a_phone(self):
        # Browser evidence (375x812): the focused absolute skip link sat over the first nav link
        # and the fixed largest-step h1 took three lines. Focused, the link joins the normal flow
        # (pushing the header down, so it cannot cover it); main takes programmatic focus; the h1
        # scales between type-scale steps and long words wrap.
        doc = (self.render(self.a) / "design-preview.html").read_text()
        css = doc[doc.index("<style>"):doc.index("</style>")]
        focus = re.search(r"\.skip-link:focus\s*\{([^}]*)\}", css)
        self.assertTrue(focus and re.search(r"position:\s*static", focus.group(1)), focus and focus.group(1))
        self.assertNotRegex(focus.group(1), r"\b(top|left):")
        self.assertIn('<main id="main" class="wrap" tabindex="-1">', doc)
        h1 = re.search(r"(?<![\w-])h1\s*\{([^}]*)\}", css).group(1)
        h2 = re.search(r"(?<![\w-])h2\s*\{([^}]*)\}", css).group(1)
        # Each heading's floor is the next step's ceiling, so h1 > h2 > h3 at every width.
        self.assertRegex(h1, r"font-size:\s*clamp\(var\(--fs-4\),\s*[^,]+,\s*var\(--fs-5\)\)")
        self.assertRegex(h2, r"font-size:\s*clamp\(var\(--fs-3\),\s*[^,]+,\s*var\(--fs-4\)\)")
        self.assertRegex(css, r"(?<![\w-])h3\s*\{\s*font-size:\s*var\(--fs-3\)")
        self.assertRegex(h1, r"overflow-wrap:\s*(anywhere|break-word)")
        t = parse(doc)
        focusable = [(tg, a) for tg, a in t.tags if tg in ("a", "button", "input")]
        self.assertEqual(focusable[0], ("a", {"class": "skip-link", "href": "#main"}))


class BillingRotaConsultation(ConsultCase):
    scenario = "billing-rota-design"

    def test_dark_scene_four_breakpoints_and_pending_inputs_need_input(self):
        out = self.render(self.a)
        doc = (out / "design-preview.html").read_text()
        self.assertIn("color-scheme: dark", doc)
        self.assertNotIn("color-scheme: light", doc)
        for w in (600, 1024, 1440):
            self.assertIn(f"@media (min-width: {w}px)", doc)
        self.assertIn('id="screen-queue-permission-denied"', doc)
        self.assertIn('id="screen-queue-offline"', doc)
        self.assertIn("31 hours", doc)
        self.assertIn("illustrative rows", doc)
        t = parse(doc)
        self.assertTrue(any(tg == "th" and a.get("scope") == "col" for tg, a in t.tags))
        result = cr.load_json(out / "result.json")
        self.assertEqual(cr.validate_result(result, REG), [])
        self.assertEqual(result["status"], "needs_input")
        unknowns = " ".join(result["unknowns"])
        self.assertIn("memorable thing", unknowns)
        self.assertIn("Cabinet Grotesk", unknowns)
        self.assertIn("rights pending", unknowns)
        pkg = (out / "design-package.md").read_text()
        self.assertRegex(section(pkg, "Typography"), r"Cabinet Grotesk[^\n]*pending verification")
        self.assertIn("pending", section(pkg, "Asset rights"))
        brief = (out / "design-brief.md").read_text()
        self.assertIn("pending", section(brief, "Memorable thing"))

    def test_metric_numbers_must_come_from_the_cited_statement(self):
        bad = mutate(self.a, lambda b: screen(b, "queue")["blocks"][1].update(value="45 hours"))
        self.assert_error(errors(bad, self.pack, self.store), r"metric .* 45 is not in b-gap")
        bad = mutate(self.a, lambda b: screen(b, "queue")["blocks"][1].update(statement_id="b-h-macros"))
        self.assert_error(errors(bad, self.pack, self.store), r"metric .* cites b-h-macros, a hypothesis")

    def test_the_two_scenarios_differ_because_their_inputs_differ(self):
        trial, _, _ = load("trial-guide-design", "analysis-consult")
        self.assertNotEqual(trial["color"], self.a["color"])
        a = (self.render(self.a, "billing") / "design-preview.html").read_text()
        tp, ts = load("trial-guide-design", "analysis-consult")[1:]
        c = ctx(tp, ts)
        files, errs = wf.render_analysis(trial, c)
        self.assertEqual(errs, [])
        b = files["design-preview.html"]
        self.assertNotEqual(a, b)
        self.assertIn("Off-hours billing desk", a)
        self.assertNotIn("Off-hours billing desk", b)


class DesignInputsChain(TempDirCase):
    def test_stored_upstream_prd_bytes_equal_a_fresh_render(self):
        for scen, up, analysis in (("trial-guide-design", "trial-activation-prd", "analysis-lean"),
                                   ("billing-rota-design", "support-billing-prd", "analysis-full")):
            with self.subTest(scenario=scen):
                a, pack, store = load(up, analysis)
                files, errs = wf.render_analysis(a, ctx(pack, store))
                self.assertEqual(errs, [])
                stored = (EVALS / scen / "inputs" / "lean-prd.md").read_text()
                self.assertEqual(stored, files["lean-prd.md"])

    def test_reviewed_design_package_bytes_equal_a_fresh_consultation_render(self):
        a, pack, store = load("trial-guide-design", "analysis-consult")
        files, errs = wf.render_analysis(a, ctx(pack, store))
        self.assertEqual(errs, [])
        for name in ("design-brief.md", "design-package.md", "design-tokens.json", "design-preview.html"):
            self.assertEqual((EVALS / "trial-guide-review" / "inputs" / name).read_text(), files[name], name)


if __name__ == "__main__":
    import unittest
    unittest.main()


class MemorableAnswerBindingTest(ConsultCase):
    def test_answered_memorable_thing_must_be_the_recorded_answer_text(self):
        # Review 2026-11-05: any pack item id satisfied it; the text was never compared.
        bad = mutate(self.a, lambda b: b["memorable_thing"].update(text="Invented line nobody said."))
        self.assert_error(errors(bad, self.pack, self.store), r"memorable thing: D2 does not record the answered text")

    def test_a_synthetic_answer_cannot_record_the_memorable_thing_of_a_public_task(self):
        # Final review 2026-11-05: the memorable-thing source class was never compared with the task's.
        def public(d):
            d["binding"]["data_classification"] = "public"

        def fn(b):
            b["binding"]["data_classification"] = "public"
        a, pack = repack(self.tmp, EVALS / self.scenario, mutate(self.a, fn), public)
        self.assert_error(errors(a, pack, self.store),
                          r"memorable thing: D2 is synthetic and cannot answer for a public task")


class AssetRightsSourceTest(ConsultCase):
    """Review 2026-11-05 (wor4): non-pending asset rights cited any pack item (e.g. the product brief)."""

    def logo(self, b, **kw):
        x = {"id": "logo", "kind": "logo", "name": "Brand mark", "rights": "licensed", "license": "x", "source_id": "D2"}
        x.update(kw)
        b["assets"].append(x)

    def test_asset_rights_need_a_licence_source_that_names_the_asset(self):
        errs = errors(mutate(self.a, self.logo), self.pack, self.store)
        self.assert_error(errs, r"asset logo: rights licensed cite D2, which does not record an asset licence \(topic asset-license\)")
        errs = errors(mutate(self.a, lambda b: self.logo(b, source_id="D1")), self.pack, self.store)
        self.assert_error(errs, r"asset logo: rights licensed cite D1, which does not record an asset licence")
        errs = errors(mutate(self.a, lambda b: self.logo(b, kind="font", name="Papyrus", source_id="D1")), self.pack, self.store)
        self.assert_error(errs, r"asset logo: rights licensed cite D1, which does not name Papyrus")
        # pending with no source still validates and asks for input
        ok = mutate(self.a, lambda b: self.logo(b, rights="pending", source_id="none"))
        self.assertEqual(errors(ok, self.pack, self.store), [])

    def test_a_named_asset_licence_record_is_accepted_and_reported_as_recorded(self):
        item = {"source_id": "D9", "kind": "synthetic-fixture", "classification": "synthetic", "title": "Brand kit licence",
                "ref": "fixture:trial-guide-design/brand-kit", "topics": ["asset-license"],
                "excerpt": "Synthetic fixture. The Brand mark logo is owned by the company and licensed for product UI."}
        a, pack = repack(self.tmp, EVALS / "trial-guide-design", mutate(self.a, lambda b: self.logo(b, source_id="D9")),
                         lambda d: d["items"].append(item))
        self.assertEqual(errors(a, pack, self.store), [])
        files, errs = wf.render_analysis(a, ctx(pack, self.store))
        self.assertEqual(errs, [])
        self.assertTrue(any("rights are recorded from the cited supplied sources" in x for x in wf.KINDS["design-consultation"].limitations(a, None)))

    def test_pending_font_with_a_source_gets_an_accurate_message(self):
        # wor10: the message said "cites no source" when the pending role did cite one
        errs = errors(mutate(self.a, lambda b: role(b, "display").update(verification={"status": "pending", "source_id": "D1"})),
                      self.pack, self.store)
        self.assert_error(errs, r"typography display: pending verification cites D1; mark it verified")


class SyntheticResearchPromotionTest(TempDirCase):
    """Review 2026-11-05: a synthetic fixture tagged user-research backs a research claim only in
    a synthetic task; in a public or real task it must not read as real research."""

    def pack(self, cls):
        items = {"F1": {"source_id": "F1", "kind": "synthetic-fixture", "topics": ["user-research"]},
                 "I1": {"source_id": "I1", "kind": "interview-notes", "topics": []}}
        return wf.Pack(doc={"binding": {"data_classification": cls}}, digest="0" * 64, root=None, items=items, captures={})

    def test_fixture_backs_research_only_in_a_synthetic_task(self):
        stmts = {"s": {"id": "s", "class": "synthetic", "source_ids": ["F1"]},
                 "o": {"id": "o", "class": "observed", "source_ids": ["F1"]}}
        self.assertTrue(wf._research_backed(["s"], stmts, self.pack("synthetic")))
        for cls in ("public", "company-private"):
            with self.subTest(cls=cls):
                self.assertFalse(wf._research_backed(["s"], stmts, self.pack(cls)))
                self.assertFalse(wf._research_backed(["o"], stmts, self.pack(cls)))

    def test_real_interview_notes_still_back_an_observed_claim(self):
        stmts = {"o": {"id": "o", "class": "observed", "source_ids": ["I1"]}}
        self.assertTrue(wf._research_backed(["o"], stmts, self.pack("public")))
