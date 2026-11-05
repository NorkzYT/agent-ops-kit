#!/usr/bin/env python3
"""Design helpers for the gstack design-consultation and design-review method adapters.

Pure functions, no I/O, no network, no browser: WCAG contrast arithmetic, the
consultation rubric pieces that need no workflow context, a static HTML/CSS
auditor, a design-token auditor and the offline preview builder.

The static auditor reads markup and stylesheets only. It never renders a page,
so it cannot judge visual hierarchy, rendered contrast of overlapping layers,
target sizes, reflow, keyboard order or screen-reader output: those checks are
reported as ``not-evaluated`` and can never back a finding.

Method adapted from gstack (https://github.com/garrytan/gstack) at
622e8b1f76d293b647eca7b33d9dd060ab618b21, MIT License, Copyright (c) 2026 Garry
Tan; the notice and file list are in hermes/skills/gstack-design-*/references/.
"""

from __future__ import annotations

import html
import math
import re
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Sequence, Tuple

GSTACK_REVISION = "622e8b1f76d293b647eca7b33d9dd060ab618b21"

TOKEN_NAMES = ("bg", "surface", "text", "text-muted", "accent", "on-accent", "border", "focus",
               "success", "warning", "danger", "info")
# (foreground, background, minimum ratio): text and status colours are used as text in the
# preview (4.5:1); borders and the focus ring are non-text UI (3:1).
CONTRAST_RULES: Tuple[Tuple[str, str, float], ...] = tuple(
    [(fg, bg, 4.5) for fg in ("text", "text-muted", "accent", "success", "warning", "danger", "info") for bg in ("bg", "surface")]
    + [("on-accent", "accent", 4.5)]
    + [(fg, bg, 3.0) for fg in ("border", "focus") for bg in ("bg", "surface")])

BANNED_FACES = {"papyrus", "comic sans", "comic sans ms", "lobster", "impact", "jokerman", "bleeding cowboys",
                "permanent marker", "bradley hand", "brush script", "hobo", "trajan", "raleway", "clash display",
                "courier new"}
OVERUSED_FACES = {"inter", "roboto", "arial", "helvetica", "open sans", "lato", "montserrat", "poppins", "space grotesk",
                  "space mono", "fraunces", "playfair display", "cormorant", "lora", "crimson", "newsreader", "syne",
                  "ibm plex sans", "ibm plex serif", "dm sans", "dm serif", "outfit", "plus jakarta sans",
                  "instrument sans", "geist"}
FINE_AS_BODY = {"dm sans", "instrument sans", "ibm plex sans"}
BUZZWORDS = ("seamless", "seamlessly", "effortless", "effortlessly", "supercharge", "streamline", "revolutionize",
             "revolutionise", "unlock", "empower", "unleash", "elevate", "next-gen", "game-changer", "delve")
PLACEHOLDERS = ("john doe", "jane doe", "acme", "lorem ipsum", "smartflow")
GENERIC_CTAS = {"get started", "learn more", "click here", "submit"}
REQUIRED_STATES = ("default", "empty", "loading", "error")
RECOVERY_STATES = {"empty", "error", "permission-denied", "offline"}
REQUIRED_A11Y = ("contrast", "focus-visible", "keyboard", "target-size", "reduced-motion", "landmarks", "form-errors",
                 "text-resize", "status-messages")
NOT_STATIC_A11Y = {"keyboard", "target-size", "text-resize", "status-messages"}

# Phrases that report research, usability, assistive-technology or screenshot results. Text that
# reads this way must rest on supplied research or captured evidence, never on the author's word.
RESEARCH_CLAIM = re.compile(
    r"\b(users?|customers?|participants?|testers?|people|agents?|trial users)\s+(said|told|reported|complained|struggled|"
    r"preferred|asked for)\b"
    r"|\b(interviews?|usability (?:tests?|sessions?|study|studies)|user (?:research|testing|interviews?)|surveys?|research|"
    r"studies|our data)\s+(show|shows|showed|shown|found|revealed|confirmed|proved|indicates?|indicated|suggests?|suggested)\b"
    r"|\bwe\s+(interviewed|surveyed|tested with|observed|asked)\b"
    r"|\b(screen ?readers?|voiceover|nvda|jaws|talkback)\s+(announced?|announces|read|reads|said|says)\b"
    r"|\b(axe|lighthouse|wave)\s+(found|reported|flagged|scored?)\b"
    r"|\b(in|on|from) the (screenshot|recording)\b|\b(in|during) (the|a) usability (session|test)\b"
    r"|\b(agents?|users?|participants?) could(?:n[o']t| not)\b", re.I)
PATCH_RE = re.compile(r"^(---\s|\+\+\+\s|@@\s|diff --git)", re.M)


# ---------------------------------------------------------------- contrast

def _channel(c: float) -> float:
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def luminance(hexcolor: str) -> float:
    h = hexcolor.lstrip("#")
    if len(h) == 3:
        h = "".join(ch * 2 for ch in h)
    r, g, b = (_channel(int(h[i:i + 2], 16) / 255) for i in (0, 2, 4))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def token_contrast(tokens: Dict[str, str]) -> List[Tuple[str, str, float, float]]:
    """(fg, bg, ratio, minimum) for every contrast rule of one theme."""
    return [(fg, bg, contrast(tokens[fg], tokens[bg]), mn) for fg, bg, mn in CONTRAST_RULES]


def type_scale(base_px: int, ratio: float) -> List[int]:
    """Body, then five steps up (h5 .. h1)."""
    return [int(math.floor(base_px * ratio ** n + 0.5)) for n in range(6)]


# ---------------------------------------------------------------- copy rules

def copy_problems(text: str) -> List[str]:
    low = text.lower()
    out = [f"'{w}' describes nothing (method anti-slop list)" for w in BUZZWORDS if re.search(rf"\b{re.escape(w)}\b", low)]
    out += [f"placeholder person/brand '{p}'; use supplied or plainly illustrative content" for p in PLACEHOLDERS if p in low]
    if re.search(r"\boops\b", low):
        out.append("'Oops' says nothing; say what happened and what to do")
    return out


def research_claim(text: str) -> Optional[str]:
    m = RESEARCH_CLAIM.search(text)
    return m.group(0) if m else None


AT_CLAIM = re.compile(r"screen ?readers?|voiceover|nvda|jaws|talkback|\b(axe|lighthouse|wave)\b", re.I)
CAPTURE_ONLY_CLAIM = re.compile(r"\b(in|on|from) the (screenshot|recording)\b", re.I)


def research_claims(text: str) -> List[Tuple[str, Optional[str]]]:
    """Every research-claim phrase with the evidence it needs: 'assistive_tech' (screen reader or
    accessibility scanner output), 'usability_test' (anything users/agents said, did or could not do,
    interviews, surveys, research), or None (a reference to the screenshot/recording itself)."""
    out: List[Tuple[str, Optional[str]]] = []
    for m in RESEARCH_CLAIM.finditer(text):
        hit = m.group(0)
        need = "assistive_tech" if AT_CLAIM.search(hit) else None if CAPTURE_ONLY_CLAIM.fullmatch(hit) else "usability_test"
        out.append((hit, need))
    return out


# ---------------------------------------------------------------- CSS parsing

COLOR_HEX = re.compile(r"^#([0-9a-f]{3}|[0-9a-f]{6})$", re.I)
NAMED = {"white": "#ffffff", "black": "#000000"}


def parse_css(text: str) -> Tuple[List[Tuple[str, str, List[Tuple[str, str]]]], List[str]]:
    """(rules, imports): rules are (media, selector, [(property, value)]) in order; @media/@supports
    blocks nest their media text; other at-rule blocks (@keyframes, @font-face) keep '@' selectors."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    rules: List[Tuple[str, str, List[Tuple[str, str]]]] = []
    imports: List[str] = []
    ctx: List[str] = []
    i, n, buf = 0, len(text), []
    while i < n:
        ch = text[i]
        if ch == "{":
            prelude = "".join(buf).strip()
            buf = []
            if prelude.startswith("@") and re.match(r"@(media|supports|container|layer)\b", prelude):
                ctx.append(prelude)
                i += 1
                continue
            depth, j = 1, i + 1
            while j < n and depth:
                depth += {"{": 1, "}": -1}.get(text[j], 0)
                j += 1
            body = text[i + 1:j - 1]
            if prelude.startswith("@keyframes") or prelude.startswith("@-webkit-keyframes"):
                rules.append((" ".join(ctx), prelude, []))
            else:
                decls = []
                for part in body.split(";"):
                    if ":" in part:
                        k, v = part.split(":", 1)
                        decls.append((k.strip().lower(), v.strip()))
                rules.append((" ".join(ctx), prelude, decls))
            i = j
            continue
        if ch == "}":
            if ctx:
                ctx.pop()
            buf = []
        elif ch == ";" and "".join(buf).strip().startswith("@import"):
            imports.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
        i += 1
    return rules, imports


def _resolve(value: str, local: Dict[str, str], root: Dict[str, str], depth: int = 0) -> str:
    if depth > 6:
        return value

    def sub(m: "re.Match[str]") -> str:
        name, fallback = m.group(1), m.group(2)
        got = local.get(name, root.get(name))
        if got is None:
            return fallback.strip() if fallback else m.group(0)
        return _resolve(got, local, root, depth + 1)

    return re.sub(r"var\(\s*(--[A-Za-z0-9_-]+)\s*(?:,\s*([^)]*))?\)", sub, value)


def _as_color(value: str) -> Optional[str]:
    v = value.strip().lower().replace("!important", "").strip()
    if COLOR_HEX.match(v):
        return v
    if v in NAMED:
        return NAMED[v]
    m = re.match(r"^rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*(1|1\.0+))?\s*\)$", v)
    if m:
        return "#" + "".join(f"{min(255, int(x)):02x}" for x in m.groups()[:3])
    return None


# ---------------------------------------------------------------- HTML auditor

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
OPTIONAL_END = {"p", "li", "td", "th", "tr", "thead", "tbody", "tfoot", "option", "dt", "dd", "colgroup", "caption",
                "html", "head", "body", "optgroup"}
RESOURCE_ATTRS = {("link", "href"), ("script", "src"), ("img", "src"), ("img", "srcset"), ("iframe", "src"),
                  ("source", "src"), ("source", "srcset"), ("video", "src"), ("video", "poster"), ("audio", "src"),
                  ("object", "data"), ("embed", "src"), ("track", "src"), ("input", "src")}
REMOTE = re.compile(r"^\s*(https?:|//|ftp:)", re.I)
NOT_STATIC = {
    "target-size": "Rendered box sizes are needed to measure targets.",
    "keyboard-order": "Tab order and keyboard operation need a browser session.",
    "screen-reader-output": "What assistive technology announces needs an assistive-technology run.",
    "visual-hierarchy": "Hierarchy, density and the squint test need a rendered screenshot.",
    "reflow-320": "Reflow at 320 CSS pixels and 200 percent text needs a rendered page.",
    "rendered-contrast": "Contrast of layered, image or gradient backgrounds needs a rendered page.",
}
CHECK_META = {  # id -> (severity, category)
    "html-structure": ("medium", "accessibility"), "doc-lang": ("medium", "accessibility"),
    "doc-title": ("medium", "content"), "meta-viewport": ("high", "responsive"),
    "landmark-main": ("medium", "accessibility"), "skip-link": ("medium", "accessibility"),
    "heading-order": ("medium", "accessibility"), "img-alt": ("high", "accessibility"),
    "form-label": ("high", "accessibility"), "control-name": ("high", "accessibility"),
    "error-association": ("medium", "accessibility"), "link-target": ("medium", "content"),
    "focus-visible": ("high", "interaction-states"), "tabindex-positive": ("medium", "accessibility"),
    "inline-handler": ("polish", "performance"), "script": ("polish", "performance"),
    "external-resource": ("medium", "performance"), "reduced-motion": ("medium", "motion"),
    "duplicate-id": ("medium", "accessibility"), "copy-slop": ("medium", "content"),
    "css-contrast": ("high", "color-contrast"), "css-slop": ("medium", "ai-slop"),
}


def _check(cid: str, status: str, details: Sequence[str] = ()) -> Dict[str, Any]:
    sev, cat = CHECK_META.get(cid, ("medium", "accessibility"))
    return {"status": status, "severity": sev, "category": cat, "details": [d[:300] for d in list(details)[:20]]}


class _Doc(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: List[Dict[str, Any]] = []
        self.structure: List[str] = []
        self.elements: List[Tuple[str, Dict[str, str]]] = []
        self.focusables: List[Dict[str, Any]] = []
        self.controls: List[Tuple[str, Dict[str, str], bool]] = []
        self.named: List[Dict[str, Any]] = []
        self.label_for: set = set()
        self.headings: List[int] = []
        self.text: List[str] = []
        self.title: List[str] = []
        self.css: List[str] = []

    def _open(self, tag: str) -> bool:
        return any(e["tag"] == tag for e in self.stack)

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        a = {k.lower(): (v if v is not None else "") for k, v in attrs}
        self.elements.append((tag, a))
        entry = {"tag": tag, "attrs": a, "text": [], "named": False}
        if tag == "img" and a.get("alt", "").strip():
            for e in self.stack:
                if e["tag"] in ("a", "button"):
                    e["named"] = True
        if tag == "label" and a.get("for"):
            self.label_for.add(a["for"])
        if re.fullmatch(r"h[1-6]", tag):
            self.headings.append(int(tag[1]))
        if tag in ("input", "select", "textarea") and a.get("type", "").lower() not in ("hidden", "submit", "button", "reset", "image"):
            self.controls.append((tag, a, self._open("label")))
        focusable = ((tag == "a" and "href" in a) or tag in ("button", "select", "textarea")
                     or (tag == "input" and a.get("type", "").lower() != "hidden"))
        try:
            ti = int(a.get("tabindex", "x"))
        except ValueError:
            ti = None
        if (focusable and (ti is None or ti >= 0)) or (ti is not None and ti >= 0):
            self.focusables.append(entry)
        if tag in ("button",) or (tag == "a" and "href" in a):
            self.named.append(entry)
        if tag not in VOID:
            self.stack.append(entry)

    def handle_startendtag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in VOID:
            return
        if not any(e["tag"] == tag for e in self.stack):
            self.structure.append(f"stray </{tag}>")
            return
        while self.stack:
            e = self.stack.pop()
            if e["tag"] == tag:
                break
            if e["tag"] not in OPTIONAL_END:
                self.structure.append(f"<{e['tag']}> not closed before </{tag}>")

    def handle_data(self, data: str) -> None:
        top = self.stack[-1]["tag"] if self.stack else ""
        if top == "style":
            self.css.append(data)
            return
        if top == "script":
            return
        if top == "title":
            self.title.append(data)
            return
        self.text.append(data)
        for e in self.stack:
            if e["tag"] in ("a", "button"):
                e["text"].append(data)

    def finish(self) -> None:
        self.close()
        for e in self.stack:
            if e["tag"] not in OPTIONAL_END:
                self.structure.append(f"<{e['tag']}> never closed")


def _accessible_name(e: Dict[str, Any]) -> str:
    a = e["attrs"]
    return ("".join(e["text"]).strip() or a.get("aria-label", "").strip() or a.get("aria-labelledby", "").strip()
            or a.get("title", "").strip() or ("(image alt)" if e["named"] else ""))


def audit_html(doc: str) -> Dict[str, Dict[str, Any]]:
    """Static checks of one HTML document: markup, inline <style> CSS and visible copy only."""
    p = _Doc()
    p.feed(doc)
    p.finish()
    out: Dict[str, Dict[str, Any]] = {}
    els = p.elements

    out["html-structure"] = _check("html-structure", "fail" if p.structure else "pass", p.structure)
    html_el = next((a for t, a in els if t == "html"), None)
    out["doc-lang"] = _check("doc-lang", "pass" if html_el and html_el.get("lang", "").strip() else "fail",
                             [] if html_el and html_el.get("lang") else ["<html> has no lang attribute"])
    title = "".join(p.title).strip()
    out["doc-title"] = _check("doc-title", "pass" if title else "fail", [] if title else ["no non-empty <title>"])
    vp = [a.get("content", "") for t, a in els if t == "meta" and a.get("name", "").lower() == "viewport"]
    vp_bad = [c for c in vp if re.search(r"user-scalable\s*=\s*(no|0)|maximum-scale\s*=\s*1(\.0*)?\b", c, re.I)]
    out["meta-viewport"] = _check("meta-viewport", "fail" if (not vp or vp_bad) else "pass",
                                  (["no viewport meta"] if not vp else []) + [f"blocks zoom: {c}" for c in vp_bad])
    mains = [a for t, a in els if t == "main" or a.get("role") == "main"]
    out["landmark-main"] = _check("landmark-main", "pass" if len(mains) == 1 else "fail",
                                  [] if len(mains) == 1 else [f"{len(mains)} main landmarks (want exactly 1)"])
    main_id = mains[0].get("id") if len(mains) == 1 else None
    first = p.focusables[0] if p.focusables else None
    skip_ok = bool(first and first["tag"] == "a" and main_id and first["attrs"].get("href") == f"#{main_id}"
                   and "skip" in "".join(first["text"]).lower())
    out["skip-link"] = _check("skip-link", "pass" if skip_ok else "fail",
                              [] if skip_ok else ["the first focusable element is not a skip link to the main landmark"])
    hd = []
    if p.headings.count(1) != 1:
        hd.append(f"{p.headings.count(1)} h1 elements (want exactly 1)")
    prev = 0
    for lvl in p.headings:
        if prev and lvl > prev + 1:
            hd.append(f"h{prev} -> h{lvl} skips a level")
        prev = lvl
    out["heading-order"] = _check("heading-order", "fail" if hd else "pass", hd)
    imgs = [a for t, a in els if t == "img"]
    no_alt = [a.get("src", "?")[:60] for a in imgs if "alt" not in a]
    out["img-alt"] = (_check("img-alt", "fail" if no_alt else "pass", [f"<img> without alt: {s}" for s in no_alt])
                      if imgs else _check("img-alt", "not-applicable"))
    unlabeled = []
    for tag, a, inside in p.controls:
        if not (inside or a.get("aria-label", "").strip() or a.get("aria-labelledby", "").strip()
                or (a.get("id") and a["id"] in p.label_for)):
            unlabeled.append(f"<{tag} type={a.get('type', 'text')}> {('placeholder ' + repr(a['placeholder'])) if a.get('placeholder') else ''}".strip())
    out["form-label"] = (_check("form-label", "fail" if unlabeled else "pass", [f"no label: {u}" for u in unlabeled])
                         if p.controls else _check("form-label", "not-applicable"))
    nameless = [f"<{e['tag']}> #{i + 1}" for i, e in enumerate(p.named) if not _accessible_name(e)]
    out["control-name"] = _check("control-name", "fail" if nameless else "pass", [f"no accessible name: {x}" for x in nameless])
    ids = [a["id"] for _, a in els if a.get("id")]
    idset = set(ids)
    bad_assoc = []
    for t, a in els:
        if a.get("aria-invalid", "").lower() == "true":
            refs = a.get("aria-describedby", "").split()
            if not refs or any(r not in idset for r in refs):
                bad_assoc.append(f"<{t} id={a.get('id', '?')}> aria-invalid without a resolvable aria-describedby")
    invalids = [1 for _, a in els if a.get("aria-invalid", "").lower() == "true"]
    out["error-association"] = (_check("error-association", "fail" if bad_assoc else "pass", bad_assoc)
                                if invalids else _check("error-association", "not-applicable"))
    dead = [a.get("href") for t, a in els if t == "a" and (a.get("href", None) == "#" or a.get("href", "").lower().startswith("javascript:"))]
    out["link-target"] = _check("link-target", "fail" if dead else "pass", [f"link goes nowhere: href={h!r}" for h in dead])
    pos_ti = []
    for t, a in els:
        try:
            if int(a.get("tabindex", "0")) > 0:
                pos_ti.append(f"<{t}> tabindex={a['tabindex']}")
        except ValueError:
            pass
    out["tabindex-positive"] = _check("tabindex-positive", "fail" if pos_ti else "pass", pos_ti)
    handlers = [f"<{t}> {k}" for t, a in els for k in a if k.startswith("on")]
    out["inline-handler"] = _check("inline-handler", "fail" if handlers else "pass", handlers)
    scripts = [a.get("src", "inline") for t, a in els if t == "script"]
    out["script"] = (_check("script", "not-evaluated", [f"{len(scripts)} script element(s); behaviour is not evaluated statically"] + scripts)
                     if scripts else _check("script", "pass", ["no script elements"]))
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    out["duplicate-id"] = _check("duplicate-id", "fail" if dupes else "pass", [f"duplicate id {d!r}" for d in dupes])

    # Inline style="..." attributes are CSS too (as a rule on their element; braces cannot nest).
    inline = [f"{t}[style] {{ {re.sub(r'[{}]', ' ', a['style'])} }}" for t, a in els if a.get("style")]
    css = "\n".join(list(p.css) + inline)
    rules, imports = parse_css(css)

    def _urls(k: str, v: str) -> List[str]:  # every srcset candidate, not just the first
        return [c.strip().split()[0] for c in v.split(",") if c.strip()] if k == "srcset" else [v]
    remote = [f"<{t} {k}={a[k]}>" for t, a in els for k in a
              if (t, k) in RESOURCE_ATTRS and any(REMOTE.match(u) for u in _urls(k, a[k] or ""))]
    remote += [f"css url({m})" for m in re.findall(r"url\(\s*['\"]?\s*((?:https?:|//)[^'\")\s]*)", css, re.I)]
    remote += [f"css {imp}" for imp in imports]
    out["external-resource"] = _check("external-resource", "fail" if remote else "pass",
                                      [f"remote resource (not fetched by this review): {r}" for r in remote])

    root: Dict[str, str] = {}
    dark_root: Dict[str, str] = {}
    for media, sel, decls in rules:
        if sel.strip() == ":root":
            target = root if not media else (dark_root if "prefers-color-scheme: dark" in media.replace("  ", " ") else None)
            if target is not None:
                target.update({k: v for k, v in decls if k.startswith("--")})

    motion = [(sel, k, v) for media, sel, decls in rules for k, v in decls
              if k in ("transition", "animation", "transition-duration", "animation-name") and v.strip().lower() not in ("none", "0", "0s")]
    reduced = any(re.search(r"prefers-reduced-motion\s*:\s*reduce\b", media) for media, _, _ in rules)
    out["reduced-motion"] = (_check("reduced-motion", "pass" if reduced else "fail",
                                    [] if reduced else [f"{s} {k}: {v} with no prefers-reduced-motion rule" for s, k, v in motion])
                             if motion else _check("reduced-motion", "not-applicable"))
    removed = [sel for _, sel, decls in rules for k, v in decls
               if (k == "outline" and v.strip().lower() in ("none", "0", "0px")) or (k == "outline-style" and v.strip().lower() == "none")]
    replaced = any(":focus-visible" in sel and any(k in ("outline", "box-shadow", "outline-color") for k, _ in decls)
                   for _, sel, decls in rules)
    out["focus-visible"] = _check("focus-visible", "fail" if removed and not replaced else "pass",
                                  [f"outline removed on '{s}' with no :focus-visible replacement" for s in removed] if removed and not replaced else [])

    pairs, low = 0, []
    schemes = [("", root)] + ([(" (dark scheme)", dict(root, **dark_root))] if dark_root else [])
    for label, rootvars in schemes:
        for media, sel, decls in rules:
            if media and "prefers-color-scheme" not in media:
                continue
            if label and media and "prefers-color-scheme: dark" not in media:
                continue
            if not label and "prefers-color-scheme: dark" in media:
                continue
            local = {k: v for k, v in decls if k.startswith("--")}
            d = dict(decls)
            fg = _as_color(_resolve(d.get("color", ""), local, rootvars)) if "color" in d else None
            bgv = d.get("background-color", d.get("background"))
            bg = _as_color(_resolve(bgv, local, rootvars)) if bgv else None
            if fg and bg:
                pairs += 1
                r = contrast(fg, bg)
                if r < 4.5:
                    low.append(f"'{sel.strip()}'{label}: {fg} on {bg} = {r:.2f}:1 (< 4.5:1; normal-size text assumed)")
    out["css-contrast"] = (_check("css-contrast", "fail" if low else "pass", low or [f"{pairs} declared colour pair(s) at or above 4.5:1"])
                           if pairs else _check("css-contrast", "not-evaluated", ["no rule declares both a text and a background colour"]))

    slop = []
    radii = []
    for _, sel, decls in rules:
        local = {k: v for k, v in decls if k.startswith("--")}
        for k, v in decls:
            rv = _resolve(v, local, root).lower()
            if k in ("background", "background-image") and re.search(r"(linear|radial|conic)-gradient", rv):
                slop.append(f"gradient background on '{sel.strip()}'")
            if k in ("background-clip", "-webkit-background-clip") and "text" in rv:
                slop.append(f"gradient text on '{sel.strip()}'")
            if k in ("transition", "animation", "transition-timing-function", "animation-timing-function") and re.search(r"\bease-in\b(?!-)", rv):
                slop.append(f"plain ease-in easing on '{sel.strip()}'")
            if k == "border-radius":
                m = re.match(r"^\s*(\d+(?:\.\d+)?)px\s*$", rv)
                radii.append(float(m.group(1)) if m else None)
    known = [r for r in radii if r is not None]
    if len(known) >= 2 and len(set(known)) == 1 and len(known) == len(radii) and known[0] >= 16:
        slop.append(f"the same {known[0]:g}px radius on every rounded rule ({len(known)} rules)")
    out["css-slop"] = _check("css-slop", "fail" if slop else "pass", slop)

    visible = " ".join(" ".join(p.text).split())
    problems = copy_problems(visible)
    problems += [f"generic call to action '{n}'" for e in p.named for n in [" ".join("".join(e["text"]).split())]
                 if n.lower() in GENERIC_CTAS]
    out["copy-slop"] = _check("copy-slop", "fail" if problems else "pass", problems)

    for cid, why in NOT_STATIC.items():
        out[cid] = {"status": "not-evaluated", "severity": "polish", "category": "accessibility", "details": [why]}
    return out


def html_stats(doc: str) -> Dict[str, Any]:
    """Element ids, script count and CSP meta of a document (for preview completeness)."""
    p = _Doc()
    p.feed(doc)
    p.finish()
    csp = [a.get("content", "") for t, a in p.elements if t == "meta" and a.get("http-equiv", "").lower() == "content-security-policy"]
    return {"ids": {a["id"] for _, a in p.elements if a.get("id")}, "scripts": sum(1 for t, _ in p.elements if t == "script"),
            "csp": csp[0] if csp else "", "text": " ".join(" ".join(p.text).split()), "css": "\n".join(p.css)}


# ---------------------------------------------------------------- token auditor

def _tcheck(status: str, severity: str, category: str, details: Sequence[str]) -> Dict[str, Any]:
    return {"status": status, "severity": severity, "category": category, "details": list(details)}


def _int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def audit_tokens(doc: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Static checks of a design-tokens.v1 document (recomputed here, never trusted from the file).
    A field of the wrong type fails its check; nothing here raises on malformed input."""
    out: Dict[str, Dict[str, Any]] = {}
    themes = doc.get("themes", {})
    if not isinstance(themes, dict):
        out["contrast:themes"] = _tcheck("fail", "high", "color-contrast", ["themes is not an object of theme -> tokens"])
        themes = {}
    for theme, tokens in sorted(themes.items()):
        if not isinstance(tokens, dict) or any(not isinstance(tokens.get(k), str) or not COLOR_HEX.match(tokens[k]) for k in TOKEN_NAMES):
            out[f"contrast:{theme}:tokens"] = _tcheck("fail", "high", "color-contrast", ["theme lacks one of the semantic tokens"])
            continue
        for fg, bg, r, mn in token_contrast(tokens):
            out[f"contrast:{theme}:{fg}-on-{bg}"] = _tcheck("pass" if r >= mn else "fail", "high", "color-contrast",
                                                             [f"{tokens[fg]} on {tokens[bg]} = {r:.2f}:1 (min {mn:g}:1)"])
    sp = doc.get("spacing_px", [])
    if not isinstance(sp, list):
        bad: List[Any] = ["spacing_px is not a list"]
    else:
        bad = [v for v in sp if not _int(v) or v % 4]
        bad += ["not ascending"] if not bad and sp != sorted(sp) else []
    out["spacing-scale"] = _tcheck("fail" if bad or not sp else "pass", "medium", "spacing-layout",
                                   [f"off-scale or unordered: {bad}"] if bad else [f"4px-based scale {sp}"])
    rad = doc.get("radius_px", {})
    vals = [rad.get(k) for k in ("small", "medium", "large")] if isinstance(rad, dict) else [None] * 3
    rbad = []
    if not all(_num(v) for v in vals) or vals != sorted(vals):
        rbad.append(f"radius must rise small <= medium <= large, got {vals}")
    elif len(set(vals)) == 1 and vals[0] >= 12:
        rbad.append(f"uniform {vals[0]}px radius on everything")
    out["radius-hierarchy"] = _tcheck("fail" if rbad else "pass", "medium", "spacing-layout", rbad or [f"{vals}"])
    scale = doc.get("type_scale_px", [])
    ok = isinstance(scale, list) and len(scale) == 6 and all(_num(v) for v in scale) and all(b > a for a, b in zip(scale, scale[1:]))
    sbad = [] if ok else [f"type scale steps are not distinct: {scale}"]
    out["type-scale"] = _tcheck("fail" if sbad else "pass", "medium", "typography", sbad or [f"{scale}"])
    mo = doc.get("motion", {})
    mo = mo if isinstance(mo, dict) else {}
    mbad = [f"{k}={mo.get(k)}" for k in ("short_ms", "long_ms") if not _int(mo.get(k)) or not 50 <= mo[k] <= 700]
    out["motion-duration"] = _tcheck("fail" if mbad else "pass", "medium", "motion", mbad or ["durations within 50-700ms"])
    typ = doc.get("type", {})
    roles = typ.get("roles", []) if isinstance(typ, dict) else None
    if not isinstance(roles, list) or not all(isinstance(r, dict) for r in roles):
        out["font-verification"] = _tcheck("fail", "polish", "typography", ["type.roles is not a list of role objects"])
        return out
    pending = [f"{r.get('family')} ({r.get('role')})" for r in roles if r.get("verification") != "verified"]
    out["font-verification"] = _tcheck("not-evaluated" if pending else "pass", "polish", "typography",
                                       [f"pending verification: {', '.join(pending)}"] if pending else ["all faces recorded as verified"])
    return out


# ---------------------------------------------------------------- preview builder

def esc(text: Any) -> str:
    return html.escape(str(text), quote=True)


STATE_LABEL = {"default": "Default", "empty": "Empty", "loading": "Loading", "error": "Error", "success": "Success",
               "permission-denied": "No access", "offline": "Offline"}
STATE_STYLE = {"error": "msg-error", "success": "msg-success", "permission-denied": "msg-warning", "offline": "msg-info",
               "empty": "msg-neutral", "loading": "msg-neutral"}


def _vars(tokens: Dict[str, str]) -> str:
    return " ".join(f"--color-{k}: {tokens[k].lower()};" for k in TOKEN_NAMES)


def build_preview(a: Dict[str, Any], *, synthetic: bool, statements: Dict[str, Dict[str, Any]],
                  provenance: str) -> str:
    """Self-contained HTML preview of a validated design-consultation analysis: no script, no
    external asset, every value escaped; CSS values come only from schema-validated tokens."""
    theme = a["use_scene"]["theme"]
    colors = a["color"]
    typo = a["typography"]
    roles = {r["role"]: r for r in typo["roles"]}
    scale = type_scale(typo["base_px"], typo["ratio"])
    fam = {r: f"\"{roles[r]['family']}\", {roles[r]['fallback']}" for r in roles}
    fam.setdefault("label", fam["body"])
    fam.setdefault("mono", "monospace")
    sp = a["spacing_px"]
    space = {i + 1: v for i, v in enumerate(sp)}
    rad = a["radius_px"]
    mo = a["motion"]
    bps = a["responsive"]["breakpoints"]

    def s(i: int) -> str:
        return f"var(--space-{min(i, len(sp))})"

    if theme == "both":
        root_colors = f"color-scheme: light dark; {_vars(colors['light'])}"
        scheme_css = f"@media (prefers-color-scheme: dark) {{\n  :root {{ {_vars(colors['dark'])} }}\n}}\n"
    else:
        root_colors = f"color-scheme: {theme}; {_vars(colors[theme])}"
        scheme_css = ""
    root = [root_colors]
    root += [f"--space-{i}: {v}px;" for i, v in space.items()]
    root += [f"--radius-s: {rad['small']}px; --radius-m: {rad['medium']}px; --radius-l: {rad['large']}px;",
             f"--font-display: {fam['display']}; --font-body: {fam['body']}; --font-label: {fam['label']}; --font-mono: {fam['mono']};",
             " ".join(f"--fs-{i}: {v}px;" for i, v in enumerate(scale)),
             f"--dur-short: {mo['short_ms']}ms; --dur-long: {mo['long_ms']}ms; --ease-enter: {mo['enter_easing']}; --ease-move: {mo['move_easing']};",
             f"--measure: {typo['measure_ch']}ch; --maxw: {a['responsive']['max_content_width_px']}px; --lh: {typo['body_line_height']};"]
    css = [":root { " + " ".join(root) + " }", scheme_css,
           "*, *::before, *::after { box-sizing: border-box; }",
           "body { margin: 0; background: var(--color-bg); color: var(--color-text); font-family: var(--font-body); "
           "font-size: var(--fs-0); line-height: var(--lh); }",
           f".wrap {{ max-width: var(--maxw); margin: 0 auto; padding: 0 {s(3)}; }}",
           f".skip-link {{ position: absolute; left: {s(2)}; top: -200px; z-index: 2; padding: {s(2)}; background: var(--color-surface); color: var(--color-text); }}",
           f".skip-link:focus {{ position: static; display: inline-block; margin: {s(2)}; }}",
           ":focus-visible { outline: 3px solid var(--color-focus); outline-offset: 2px; }",
           "::selection { background: var(--color-accent); color: var(--color-on-accent); }",
           "h1, h2, h3, h4, h5 { font-family: var(--font-display); line-height: 1.2; margin: 0 0 " + s(2) + "; }",
           "h1 { font-size: clamp(var(--fs-4), 6vw + 1rem, var(--fs-5)); overflow-wrap: break-word; } "
           "h2 { font-size: clamp(var(--fs-3), 5vw + 0.5rem, var(--fs-4)); overflow-wrap: break-word; } h3 { font-size: var(--fs-3); } "
           "h4 { font-size: var(--fs-2); } h5 { font-size: var(--fs-1); }",
           f"p, li {{ max-width: var(--measure); }} section {{ margin: {s(6)} 0; }}",
           "a { color: var(--color-accent); text-underline-offset: 3px; }",
           f".site-header, .site-footer {{ background: var(--color-surface); color: var(--color-text); border-color: var(--color-border); border-style: solid; border-width: 0; padding: {s(2)} 0; }}",
           ".site-header { border-bottom-width: 1px; } .site-footer { border-top-width: 1px; margin-top: " + s(6) + "; }",
           ".product { font-family: var(--font-display); font-weight: 700; margin: 0; }",
           f"nav ul {{ display: flex; flex-wrap: wrap; gap: {s(1)}; list-style: none; margin: 0; padding: 0; }}",
           f"nav a {{ display: inline-flex; align-items: center; min-height: 44px; padding: 0 {s(2)}; }}",
           f".banner {{ background: var(--color-surface); color: var(--color-text); border: 1px solid var(--color-border); border-radius: var(--radius-m); padding: {s(2)} {s(3)}; }}",
           f".state-grid {{ display: grid; grid-template-columns: minmax(0, 1fr); gap: {s(4)}; }}",
           f".state {{ background: var(--color-surface); color: var(--color-text); border: 1px solid var(--color-border); border-radius: var(--radius-l); padding: {s(3)}; min-width: 0; }}",
           ".state-label { font-family: var(--font-mono); font-size: var(--fs-0); color: var(--color-text-muted); margin: 0 0 " + s(1) + "; }",
           f".field {{ display: flex; flex-direction: column; gap: {s(1)}; margin: {s(2)} 0; }}",
           "label { font-weight: 600; font-family: var(--font-label); }",
           f"input {{ min-height: 44px; padding: 0 {s(2)}; border: 1px solid var(--color-border); border-radius: var(--radius-s); background: var(--color-bg); color: var(--color-text); font: inherit; max-width: 100%; }}",
           "input[aria-invalid=\"true\"] { border: 2px solid var(--color-danger); }",
           ".hint { color: var(--color-text-muted); margin: 0; }",
           f".actions {{ display: flex; flex-wrap: wrap; gap: {s(2)}; margin-top: {s(3)}; }}",
           f".btn {{ min-height: 44px; min-width: 44px; padding: 0 {s(3)}; border: 1px solid var(--color-border); border-radius: var(--radius-s); "
           "background: var(--color-surface); color: var(--color-text); font: inherit; font-weight: 600; cursor: pointer; "
           "transition: background-color var(--dur-short) var(--ease-move); }",
           ".btn:hover { text-decoration: underline; }",
           ".btn-primary { background: var(--color-accent); color: var(--color-on-accent); border-color: var(--color-accent); }",
           f".msg {{ padding: {s(2)} {s(3)}; border: 1px solid var(--color-border); border-radius: var(--radius-s); margin: {s(2)} 0; }}",
           ".msg-error { color: var(--color-danger); background: var(--color-bg); border-color: var(--color-danger); }",
           ".msg-success { color: var(--color-success); background: var(--color-bg); border-color: var(--color-success); }",
           ".msg-warning { color: var(--color-warning); background: var(--color-bg); border-color: var(--color-warning); }",
           ".msg-info { color: var(--color-info); background: var(--color-bg); border-color: var(--color-info); }",
           ".msg-neutral { color: var(--color-text); background: var(--color-bg); }",
           f".skeleton {{ display: block; height: {s(3)}; margin: {s(2)} 0; border-radius: var(--radius-s); background: var(--color-border); opacity: 0.45; }}",
           ".skeleton.short { width: 60%; }",
           f".metrics {{ display: flex; flex-wrap: wrap; gap: {s(3)}; }} .metric {{ display: flex; flex-direction: column; min-width: 0; }}",
           ".metric-value { font-family: var(--font-mono); font-size: var(--fs-3); font-variant-numeric: tabular-nums; }",
           ".src { color: var(--color-text-muted); font-size: var(--fs-0); }",
           ".table-wrap { overflow-x: auto; max-width: 100%; }",
           "table { border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; }",
           f"caption {{ text-align: left; color: var(--color-text-muted); padding-bottom: {s(1)}; }}",
           f"th, td {{ text-align: left; padding: {s(1)} {s(2)}; border-bottom: 1px solid var(--color-border); }}",
           ".mono { font-family: var(--font-mono); }",
           f".panel {{ border: 1px solid var(--color-border); border-radius: var(--radius-m); padding: {s(3)}; margin: {s(3)} 0; }}",
           f".swatch {{ display: inline-block; width: {s(5)}; height: {s(4)}; border: 1px solid var(--color-border); vertical-align: middle; }}",
           f".spec {{ margin: {s(2)} 0; }}",
           f".bar {{ display: block; height: {s(2)}; background: var(--color-accent); margin: {s(1)} 0; }}",
           ]
    for t in ("light", "dark"):
        if t in colors:
            css.append(f".theme-{t} {{ {_vars(colors[t])} background: var(--color-bg); color: var(--color-text); }}")
            css += [f".sw-{t}-{k} {{ background: {colors[t][k].lower()}; }}" for k in TOKEN_NAMES]
    css += [f".sp-{i} {{ width: {v}px; }}" for i, v in space.items()]
    css += [f".spec-{r} {{ font-family: var(--font-{r}); }}" for r in ("display", "body", "label", "mono")]
    for bp in bps[1:]:
        css.append(f"@media (min-width: {bp['min_width_px']}px) {{\n  .state-grid {{ grid-template-columns: repeat({bp['columns']}, minmax(0, 1fr)); }}\n}}")
    css.append("@media (prefers-reduced-motion: reduce) {\n  *, *::before, *::after { transition: none !important; animation: none !important; scroll-behavior: auto !important; }\n}")

    name = esc(a["product_name"])
    states_by: Dict[str, List[Dict[str, Any]]] = {}
    for st in a["states"]:
        states_by.setdefault(st["screen_id"], []).append(st)

    def block_html(sid: str, stname: str, i: int, b: Dict[str, Any], invalid_msg: Optional[str] = None) -> List[str]:
        k = b["kind"]
        if k == "heading":
            return [f"<h5>{esc(b['text'])}</h5>"]
        if k == "text":
            return [f"<p>{esc(b['text'])}</p>"]
        if k == "field":
            fid = f"f-{sid}-{stname}-{i}"
            hid = f"h-{sid}-{stname}-{i}"
            described = " ".join(x for x in (invalid_msg, hid if "hint" in b else None) if x)
            attrs = f' aria-describedby="{described}"' if described else ""
            attrs += ' aria-invalid="true"' if invalid_msg else ""
            out = [f'<div class="field"><label for="{fid}">{esc(b["label"])}</label>',
                   f'<input id="{fid}" name="{fid}" type="{b["input"]}"{attrs}>']
            if "hint" in b:
                out.append(f'<p class="hint" id="{hid}">{esc(b["hint"])}</p>')
            return out + ["</div>"]
        if k == "action":
            cls = "btn btn-primary" if b["emphasis"] == "primary" else "btn"
            return [f'<button type="button" class="{cls}">{esc(b["label"])}</button>']
        if k == "metric":
            st = statements.get(b["statement_id"], {})
            return [f'<div class="metric"><span class="metric-value">{esc(b["value"])}</span>'
                    f'<span class="metric-label">{esc(b["label"])}</span>'
                    f'<span class="src">Source: {esc(b["statement_id"])} ({esc(st.get("class", "?"))})</span></div>']
        if k == "claim":
            st = statements.get(b["statement_id"], {})
            return [f'<p class="claim">{esc(b["text"])} <span class="src">[{esc(b["statement_id"])}, {esc(st.get("class", "?"))}]</span></p>']
        if k == "table":
            out = ['<div class="table-wrap">', f"<table><caption>{esc(b['caption'])}</caption><thead><tr>"]
            out += [f'<th scope="col">{esc(c)}</th>' for c in b["columns"]]
            out.append("</tr></thead><tbody>")
            for row in b["rows"]:
                out.append("<tr>" + "".join(f"<td>{esc(c)}</td>" for c in row) + "</tr>")
            return out + ["</tbody></table></div>"]
        return []

    def render_blocks(sid: str, stname: str, blocks: Sequence[Dict[str, Any]], invalid_msg: Optional[str] = None) -> List[str]:
        out: List[str] = []
        metrics: List[str] = []
        actions: List[str] = []
        first_field = True
        for i, b in enumerate(blocks):
            if b["kind"] == "metric":
                metrics += block_html(sid, stname, i, b)
                continue
            if metrics:
                out += ['<div class="metrics">'] + metrics + ["</div>"]
                metrics = []
            if b["kind"] == "action":
                actions += block_html(sid, stname, i, b)
                continue
            out += block_html(sid, stname, i, b, invalid_msg if (b["kind"] == "field" and first_field) else None)
            if b["kind"] == "field":
                first_field = False
        if metrics:
            out += ['<div class="metrics">'] + metrics + ["</div>"]
        if actions:
            out += ['<div class="actions">'] + actions + ["</div>"]
        return out

    body: List[str] = []
    for sc in a["screens"]:
        sid = sc["id"]
        body += [f'<section class="screen" id="screen-{sid}" aria-labelledby="screen-{sid}-h">',
                 f'<h3 id="screen-{sid}-h">{esc(sc["name"])}</h3>', f"<p>{esc(sc['purpose'])}</p>",
                 '<div class="state-grid">']
        for st in states_by.get(sid, []):
            stn = st["state"]
            aid = f"screen-{sid}-{stn}"
            body += [f'<article class="state" id="{aid}" data-state="{stn}" aria-labelledby="{aid}-h">',
                     f'<p class="state-label">State: {esc(STATE_LABEL[stn])}</p>',
                     f'<h4 id="{aid}-h">{esc(sc["name"])}: {esc(STATE_LABEL[stn].lower())}</h4>']
            headings = [b for b in sc["blocks"] if b["kind"] == "heading"]
            mid = f"m-{sid}-{stn}"
            msg = esc(st.get("message", ""))
            recovery = (f'<div class="actions"><button type="button" class="btn btn-primary">{esc(st["recovery_action"])}</button></div>'
                        if "recovery_action" in st else "")
            if stn == "default":
                body += render_blocks(sid, stn, sc["blocks"])
            elif stn == "loading":
                body += render_blocks(sid, stn, headings)
                body += ['<span class="skeleton" aria-hidden="true"></span>', '<span class="skeleton short" aria-hidden="true"></span>',
                         f'<p class="msg msg-neutral" role="status" id="{mid}">{msg}</p>']
            elif stn == "error":
                fields = [b for b in sc["blocks"] if b["kind"] in ("heading", "field")]
                body += render_blocks(sid, stn, fields, invalid_msg=mid if any(b["kind"] == "field" for b in fields) else None)
                body += [f'<p class="msg msg-error" role="alert" id="{mid}"><strong>Error:</strong> {msg}</p>', recovery]
            else:
                live = ' role="status"' if stn in ("success", "offline") else ""
                prefix = {"success": "Done:", "permission-denied": "No access:", "offline": "Offline:", "empty": "Nothing here yet:"}[stn]
                body += render_blocks(sid, stn, headings)
                body += [f'<p class="msg {STATE_STYLE[stn]}"{live} id="{mid}"><strong>{prefix}</strong> {msg}</p>', recovery]
            body.append("</article>")
        body += ["</div>", "</section>"]

    screens_by = {sc["id"]: sc for sc in a["screens"]}
    journey = ['<ol class="journey">']
    for j in a["journey"]:
        sc = screens_by[j["screen_id"]]
        journey.append(f'<li id="journey-{esc(j["id"])}"><strong>{esc(j["goal"])}</strong> on '
                       f'<a href="#screen-{sc["id"]}">{esc(sc["name"])}</a>: {esc(j["action"])} '
                       f'Success: {esc(j["success_signal"])} If it fails: '
                       f'<a href="#screen-{sc["id"]}-{j["failure_state"]}">{esc(STATE_LABEL[j["failure_state"]].lower())} state</a>.</li>')
    journey.append("</ol>")

    tokens_html: List[str] = []
    for t in ("light", "dark"):
        if t not in colors:
            continue
        tokens_html += [f'<div class="panel theme-{t}">', f"<h3>{t.capitalize()} palette</h3>",
                        "<div class=\"table-wrap\"><table><caption>Semantic colour tokens</caption><thead><tr>"
                        '<th scope="col">Token</th><th scope="col">Value</th><th scope="col">Swatch</th></tr></thead><tbody>']
        tokens_html += [f'<tr><td class="mono">--color-{k}</td><td class="mono">{colors[t][k].lower()}</td>'
                        f'<td><span class="swatch sw-{t}-{k}" aria-hidden="true"></span></td></tr>' for k in TOKEN_NAMES]
        tokens_html.append("</tbody></table></div>")
        tokens_html.append("<div class=\"table-wrap\"><table><caption>Contrast (computed from the tokens)</caption><thead><tr>"
                           '<th scope="col">Pair</th><th scope="col">Ratio</th><th scope="col">Minimum</th><th scope="col">Result</th></tr></thead><tbody>')
        tokens_html += [f"<tr><td>{fg} on {bg}</td><td class=\"mono\">{r:.2f}:1</td><td class=\"mono\">{mn:g}:1</td>"
                        f"<td>{'pass' if r >= mn else 'FAIL'}</td></tr>" for fg, bg, r, mn in token_contrast(colors[t])]
        tokens_html += ["</tbody></table></div>", "</div>"]
    tokens_html += ["<h3>Type</h3>"]
    for r in typo["roles"]:
        ver = "verified" if r["verification"]["status"] == "verified" else "pending verification"
        tokens_html.append(f'<p class="spec spec-{r["role"]}">{name}: {esc(r["role"])} in {esc(r["family"])} '
                           f'({esc(r["fallback"])} fallback), weights {", ".join(str(w) for w in r["weights"])}; {ver}.</p>')
    tokens_html.append("<p>Scale (px): " + ", ".join(str(v) for v in scale) + f"; body line height {typo['body_line_height']}; "
                       f"measure {typo['measure_ch']} characters.</p>")
    tokens_html += ["<h3>Spacing and radius</h3>", "<ul>"]
    tokens_html += [f'<li><span class="mono">--space-{i}: {v}px</span><span class="bar sp-{i}" aria-hidden="true"></span></li>' for i, v in space.items()]
    tokens_html += ["</ul>", f"<p>Radius small {rad['small']}px, medium {rad['medium']}px, large {rad['large']}px. "
                    f"Motion {mo['short_ms']}ms / {mo['long_ms']}ms, enter {esc(mo['enter_easing'])}, move {esc(mo['move_easing'])}; "
                    "removed when reduced motion is requested.</p>"]

    resp = ["<div class=\"table-wrap\"><table><caption>Breakpoints (mobile first)</caption><thead><tr>"
            '<th scope="col">Name</th><th scope="col">From</th><th scope="col">Columns</th><th scope="col">Navigation</th><th scope="col">Notes</th></tr></thead><tbody>']
    resp += [f"<tr><td>{bp['name']}</td><td class=\"mono\">{bp['min_width_px']}px</td><td>{bp['columns']}</td>"
             f"<td>{bp['navigation']}</td><td>{esc(bp['notes'])}</td></tr>" for bp in bps]
    resp += ["</tbody></table></div>", f"<p>Content width is capped at {a['responsive']['max_content_width_px']}px.</p>"]

    acc = ["<ul>"] + [f"<li><strong>{esc(r['topic'])}</strong> ({esc(r['verification'])}): {esc(r['requirement'])}</li>"
                      for r in a["accessibility"]["requirements"]] + ["</ul>"]
    assets = ["<ul>"] + [f"<li>{esc(x['kind'])} {esc(x['name'])}: rights {esc(x['rights'])} ({esc(x['license'])})</li>"
                         for x in a["assets"]] + (["<li>No assets are embedded in this preview.</li>"] if not a["assets"] else []) + ["</ul>"]

    label = ("SYNTHETIC fixture content, not real research or customer data. " if synthetic else "")
    doc = ["<!DOCTYPE html>", '<html lang="en">', "<head>", '<meta charset="utf-8">',
           '<meta name="viewport" content="width=device-width, initial-scale=1">',
           "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
           "base-uri 'none'; form-action 'none'\">",
           f'<meta name="description" content="Draft design preview for {name}">',
           '<link rel="icon" href="data:,">',
           f"<title>{name}: design preview (draft)</title>",
           "<style>", "\n".join(c for c in css if c), "</style>", "</head>", "<body>",
           '<a class="skip-link" href="#main">Skip to content</a>',
           '<header class="site-header"><div class="wrap">', f'<p class="product">{name}</p>',
           '<nav aria-label="Preview sections"><ul>',
           '<li><a href="#journey">Journey</a></li><li><a href="#screens">Screens and states</a></li>'
           '<li><a href="#tokens">Tokens</a></li><li><a href="#responsive">Responsive</a></li>'
           '<li><a href="#accessibility">Accessibility</a></li>',
           "</ul></nav>", "</div></header>",
           '<main id="main" class="wrap" tabindex="-1">', f"<h1>{name}: design preview</h1>",
           f'<p class="banner" role="note">Draft. {esc(label)}{esc(provenance)} Not checked in a browser, with assistive '
           "technology or with users; faces render only if installed locally (no web fonts are loaded).</p>",
           '<section id="journey" aria-labelledby="journey-h"><h2 id="journey-h">User journey</h2>', *journey, "</section>",
           '<section id="screens" aria-labelledby="screens-h"><h2 id="screens-h">Screens and states</h2>', *body, "</section>",
           '<section id="tokens" aria-labelledby="tokens-h"><h2 id="tokens-h">Tokens</h2>', *tokens_html, "</section>",
           '<section id="responsive" aria-labelledby="responsive-h"><h2 id="responsive-h">Responsive behaviour</h2>', *resp, "</section>",
           '<section id="accessibility" aria-labelledby="accessibility-h"><h2 id="accessibility-h">Accessibility requirements</h2>', *acc,
           '<h3>Asset rights</h3>', *assets, "</section>",
           "</main>",
           '<footer class="site-footer"><div class="wrap"><p>Generated offline by scripts/capability_workflow.py from a '
           "coordinator-authored analysis; it adds no facts. Method adapted from gstack design-consultation "
           f"(MIT, Copyright (c) 2026 Garry Tan) at {GSTACK_REVISION[:12]}.</p></div></footer>",
           "</body>", "</html>", ""]
    return "\n".join(doc)


def preview_errors(a: Dict[str, Any], doc: str, *, synthetic: bool) -> List[str]:
    """Completeness and safety of a rendered preview: static audit clean, no script or remote
    asset, CSP present, every screen/state, token and breakpoint present."""
    errs: List[str] = []
    stats = html_stats(doc)
    if stats["scripts"]:
        errs.append("preview contains a script element (the preview runs no script)")
    if "default-src 'none'" not in stats["csp"]:
        errs.append("preview lacks the default-src 'none' content security policy")
    for cid, res in audit_html(doc).items():
        if res["status"] == "fail":
            errs.append(f"preview fails {cid}: {'; '.join(res['details'][:3])}")
    for st in a["states"]:
        sid = f"screen-{st['screen_id']}-{st['state']}"
        if sid not in stats["ids"]:
            errs.append(f"preview misses {sid}")
    for t, tokens in a["color"].items():
        for k in TOKEN_NAMES:
            if f"--color-{k}: {tokens[k].lower()};" not in stats["css"]:
                errs.append(f"preview misses the {t} token --color-{k}")
    for bp in a["responsive"]["breakpoints"][1:]:
        if f"@media (min-width: {bp['min_width_px']}px)" not in stats["css"]:
            errs.append(f"preview misses the {bp['name']} breakpoint")
    if "prefers-reduced-motion: reduce" not in stats["css"]:
        errs.append("preview lacks a reduced-motion rule")
    if a["product_name"] not in stats["text"]:
        errs.append("preview does not show the product name")
    if synthetic and "SYNTHETIC" not in stats["text"]:
        errs.append("preview of a synthetic task lacks the SYNTHETIC label")
    return errs
