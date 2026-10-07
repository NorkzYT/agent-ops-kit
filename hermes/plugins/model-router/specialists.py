"""Deterministic specialist routing for the book-backed Hermes profiles (pilot).

Given the CURRENT genuine user task (``task_intent.genuine_user_text`` — never
history, system prompts, recalled memory or tool output; an attachment only
when it is the whole verified prompt, see below), pick the
0-4 specialist profiles whose domain the task is about and say how the
orchestrator should run them. Everything here is deterministic, offline and
side-effect free except :func:`knowledge_readiness`, which only ``stat``s a
profile's directories (it never opens a book or a skill reference file).

Rules:

  * **Privacy first.** The raw current user message (before provenance
    stripping, so nothing private hides in a quoted or appended block) and the
    session-sticky flag are checked with the same deterministic detectors as
    the privacy guard (``privacy.py``). A private task routes ``local-only``:
    every cloud-backed specialist is dropped and the optional model classifier
    is never called. Only profiles declared ``local`` (``local-engineer``) stay
    eligible.
  * **Current task only.** Signals come from ``CurrentTask.visible`` — fenced
    code and ``>``-quoted lines are inert, as are control tags. An inline
    attachment relay (``[Content of NAME]:``) is not task text: only the
    sender's own prose routes, separated exactly against the cached file
    (``attachments.separate_sender_prose``); an unprovable boundary routes
    nothing. When the sender typed nothing (the upload is the prompt), the
    verified relay text routes instead, marked ``task_source: attachment``:
    its control lines are inert, wire/runtime text in it routes nothing, and
    the optional classifier never sees it.
  * **High-risk profiles are advisory.** ``health-fitness``, ``herbalism``,
    ``security-reviewer`` and ``red-team`` are routed review/advice only: no
    tool action with external or high-impact effect. The optional classifier
    can never add one; they need deterministic keyword evidence.
  * **Isolated execution.** Each route names how it runs: ``kanban`` (a board
    task assigned to the profile — the dispatcher runs it under that
    profile's own HERMES_HOME) for deep work and implementation, or ``short``
    (``hermes -p <profile> -z <brief> -t skills``) for one bounded answer.
    The specialist loads its own ``/learn`` knowledge skill on demand; no book
    or skill content is ever put into the orchestrator prompt.
  * **Bounded.** At most ``max_profiles`` routes (default 4), ordered by score
    then registry order. Unknown names in settings are reported, never routed.
  * **No content in traces.** A trace carries profile names, stable reason
    labels, a 16-hex task hash and readiness status — never task text.
"""

from __future__ import annotations

import dataclasses
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from . import attachments as att
from . import privacy as pv
from . import task_intent as ti

# A signal is (label, weight, pattern). Strong = 2, weak = 1. A profile is
# selected at score >= SELECT_THRESHOLD: one strong signal or two weak ones.
Signal = Tuple[str, int, "re.Pattern[str]"]
SELECT_THRESHOLD = 2
DEFAULT_MAX_PROFILES = 4
HARD_MAX_PROFILES = 6


def _rx(pattern: str) -> "re.Pattern[str]":
    return re.compile(r"(?i)(?<![A-Za-z0-9_])(?:" + pattern + r")(?![A-Za-z0-9_])")


def _s(label: str, pattern: str) -> Signal:
    return (label, 2, _rx(pattern))


def _w(label: str, pattern: str) -> Signal:
    return (label, 1, _rx(pattern))


_B, _E = r"(?<![A-Za-z0-9_])", r"(?![A-Za-z0-9_])"


def _near(noun: str, context: str, span: int = 160) -> str:
    """``noun`` within ``span`` chars of a ``context`` word, either order.

    Disambiguates a profile's own domain noun that is weak on its own
    ("offer" the verb, "strategy" in chess) when the task names its context
    ("strategy, marketing, offer"). Both words are whole-word bounded."""
    a, b = f"{_B}(?:{noun}){_E}", f"{_B}(?:{context}){_E}"
    return rf"{a}[\s\S]{{0,{span}}}?{b}|{b}[\s\S]{{0,{span}}}?{a}"


_COMMERCE = (r"marketing|sales|pricing|prices?|funnels?|launch(?:es)?|positioning|"
             r"customers?|clients?|revenue|conversions?|leads|upsells?|competitors?|"
             r"go[- ]to[- ]market|gtm|business model|execution plan")
# "strategy" when it is the task's own noun, not a modifier-bound one
# ("testing strategy" is engineering, "marketing strategy" is marketing).
_STRATEGY_NOUN = (r"(?<!testing )(?<!test )(?<!content )(?<!marketing )(?<!sales )"
                  r"(?<!pricing )(?<!chess )(?<!opening )strateg(?:y|ies)")


@dataclass(frozen=True)
class Specialist:
    name: str
    purpose: str
    signals: Tuple[Signal, ...]
    high_risk: bool = False      # advisory/review only, never acts
    local: bool = False          # runs on the private local model; eligible when private
    implementer: bool = False    # writes code (the rest are advisory by design)


REGISTRY: Tuple[Specialist, ...] = (
    Specialist("strategy", "business model, prioritisation, competitive position, decisions", (
        _s("strategic", r"strategic|business strategy|corporate strategy|competitive (?:advantage|position(?:ing)?|landscape|analysis)"),
        _s("moat", r"moats?|business models?|market entry|which market|swot|five forces|blue ocean|pivot"),
        _s("decision", r"decision analysis|one[- ]way doors?|long[- ]term plan|prioriti[sz](?:e|ation|ing)"),
        _s("strategy-topic", _near(_STRATEGY_NOUN, _COMMERCE)),
        _w("strategy", _STRATEGY_NOUN),
        _w("competitors", r"competitors?|competition|trade[- ]?offs?|roadmap"),
    )),
    Specialist("business", "operations, finance analysis, execution planning", (
        _s("finance", r"unit economics|cash ?flow|p&l|profit and loss|runway|burn rate|break[- ]even|financial (?:model|plan|projection)s?"),
        _s("ops", r"business plan|operations plan|operating plan|bookkeeping|revenue forecast|cost structure|gross margins?"),
        _w("business", r"business|small business|startup|margins?|budget(?:ing)?|kpis?|invoic(?:e|es|ing)|operations"),
    )),
    Specialist("offers-sales", "offer design, pricing, lead generation, sales funnels", (
        _s("offer", r"offer (?:design|stack)|irresistible offer|grand slam offer|value stack|guarantees?"),
        _s("pricing", r"pricing|price point|how much (?:should|to) (?:i |we )?charge|upsells?|cross[- ]sells?"),
        _s("sales", r"lead gen(?:eration)?|sales (?:funnel|call|script|pitch|pipeline|process)|cold (?:email|outreach|call)(?:ing|s)?|close rate|closing (?:deals|sales)"),
        _s("offer-topic", _near(r"(?<!job )offers?(?! letter)", _COMMERCE)),
        _w("sell", r"offers?|sales|sell(?:ing)?|leads|prospects?|conversions?"),
    )),
    Specialist("marketing", "positioning, messaging, channels, funnel measurement", (
        _s("marketing", r"marketing|go[- ]to[- ]market|gtm|positioning|messaging|brand(?:ing)?|campaigns?"),
        _s("channels", r"seo|content (?:plan|calendar|strategy)|landing pages?|ad copy|copywriting|social media|newsletters?|email list"),
        _w("growth", r"growth|launch|audience|copy|followers|engagement|ads"),
    )),
    Specialist("leadership-teamwork", "team design, delegation, feedback, conflict, hiring", (
        _s("leadership", r"leadership|lead(?:ing)? (?:a|my|the|our) team|manag(?:e|ing) (?:people|a team|my team|our team)|teamwork"),
        _s("people", r"one[- ]on[- ]ones?|1:1s?|direct reports?|performance reviews?|hiring|hire (?:a|an|my|our)|team culture|accountability"),
        _s("feedback", r"(?:give|giving) feedback|feedback (?:to|for) (?:my|an?|our) (?:employee|report|team|hire|engineer)"),
        _w("team", r"team|teams|manager|employees?|coworkers?|colleagues?|delegat(?:e|ion|ing)|culture"),
    )),
    Specialist("software-engineering", "architecture, review standards, testing strategy (advisory)", (
        _s("architecture", r"architecture|architectural|design review|microservices?|monolith|design patterns?|"
                            r"(?:system|api|auth(?:entication|ori[sz]ation|n|z)?|login|session|token|schema|database|db|data model|"
                            r"service|module|component|protocol|interface|backend|frontend|cache|caching|queue|pipeline|plugin) design"),
        _s("quality", r"code review|review standards?|testing strategy|test strategy|technical debt|tech debt|maintainability"),
        _s("reliability", r"reliability|observability|slos?|scalab(?:le|ility)|ci/cd|deployment pipeline"),
        _w("code", r"code|codebase|repo|repository|service|database|refactor(?:ing)?|tests?|modules?"),
    )),
    Specialist("security-reviewer", "defensive security review (advisory)", (
        _s("review", r"security (?:review|audit|assessment)|threat model(?:ing)?|appsec|owasp|harden(?:ing)?"),
        _s("vuln", r"vulnerabilit(?:y|ies)|cve-\d{4}-\d+|xss|csrf|ssrf|sql injection|sqli|rce|auth(?:entication|orization)? bypass|privilege escalation|secrets? (?:leak|exposure)"),
        _s("authz", r"(?:auth(?:entication|ori[sz]ation|n|z)?|login|log[- ]in|sign[- ](?:in|on|up)|passwords?|"
                    r"access[- ]control|secrets?|credentials?|permissions?|rbac|oauth2?|oidc|sso|jwts?|mfa|2fa) "
                    r"(?:design|flow|layer|logic|scheme|model|system|handling|storage|middleware|code|implementation)s?"),
        _w("security", r"security|secure|insecure|auth(?:entication|ori[sz]ation|n|z)?|permissions?|encryption"),
    ), high_risk=True),
    Specialist("red-team", "controlled offensive testing of owned targets (advisory)", (
        _s("redteam", r"red[- ]team(?:ing)?|pen ?test(?:ing)?|penetration test(?:ing)?|offensive security|attack (?:surface|path|chain)s?"),
        _s("exploit", r"exploit(?:s|ation|able)?|proof[- ]of[- ]concept exploit|payloads?|fuzz(?:ing|er)?"),
        _w("attack", r"attack(?:s|er|ers)?|adversar(?:y|ial)"),
    ), high_risk=True),
    Specialist("negotiation-communication", "negotiation plans, hard conversations, message drafts", (
        _s("negotiate", r"negotiat(?:e|es|ed|ion|ions|ing)|counter[- ]?offers?|batna|walk[- ]away (?:point|price)|concessions?"),
        _s("conversation", r"(?:difficult|hard|tough|awkward) (?:conversation|talk|message|email)s?|talking points|push ?back on|objection handling|persua(?:de|sion|sive)"),
        _s("draft", r"(?:draft|redraft|rewrite|reword|rephrase|compose|polish|soften|tighten|firm up|tone down) "
                    r"(?:the |this |my |an? |that |our |up )?(?:message|e-?mail|reply|letter|dm)s?"),
        _w("message", r"(?:email|message|reply) to|convince|communicat(?:e|ion)|conflict"),
        _w("tone", r"(?:clear|firm|polite|professional|friendly|formal|warm|diplomatic|tactful|assertive|respectful)"
                   r",? (?:and|but|yet) (?:clear|firm|polite|professional|friendly|formal|warm|diplomatic|tactful|assertive|respectful)|"
                   r"firm(?:ly)? but|tactful(?:ly)?|diplomatic(?:ally)?|tone of voice|"
                   r"(?:firm|polite|professional|friendly|formal|neutral|warm|diplomatic|right) tone"),
    )),
    Specialist("health-fitness", "training, nutrition, recovery (educational, advisory)", (
        _s("training", r"workouts?|training (?:plan|program|block)|strength training|hypertrophy|cardio|deload|squats?|deadlifts?|bench press|vo2 ?max"),
        _s("nutrition", r"calories|protein|macros|diet(?:s|ing)?|nutrition|fat loss|lose weight|weight loss|body composition|fasting|longevity"),
        _w("health", r"fitness|health|healthy|exercise|gym|muscles?|sleep|recovery|injur(?:y|ies)"),
    ), high_risk=True),
    Specialist("herbalism", "botanical knowledge, traditional uses (educational, advisory)", (
        _s("herbal", r"herbs?|herbal(?:ism|ist)?|tinctures?|botanicals?|medicinal plants?|decoctions?|poultices?|salves?|essential oils?"),
        _s("plant", r"plant identification|identify (?:this|a) plant|forag(?:e|ing)|chamomile|echinacea|yarrow|valerian|ashwagandha|elderberr(?:y|ies)|nettles?"),
        _w("plants", r"plants?|remed(?:y|ies)|teas?|infusions?"),
    ), high_risk=True),
    Specialist("survival", "lawful, low-risk preparedness and emergency skills", (
        _s("prep", r"survival|prepper|prepping|preparedness|bug[- ]out|emergency (?:kit|plan|supplies|preparedness)|off[- ]grid|power outage"),
        _s("skills", r"water purification|purify water|fire starting|start a fire|first aid|land navigation|food storage|wilderness|ham radio|shelter building"),
        _w("outdoors", r"emergency|camping|outdoors|shelter|compass|disaster"),
    )),
    Specialist("martial-arts", "grappling technique, conditioning, injury-aware progression", (
        _s("art", r"jiu[- ]?jitsu|bjj|grappling|sambo|judo|wrestling|martial arts?|mma|muay thai|kickboxing|self[- ]defen[cs]e"),
        _s("technique", r"guard pass(?:ing)?|armbars?|triangle chokes?|rear naked chokes?|kimuras?|takedowns?|sparring|submissions?"),
        _w("mat", r"dojo|belt|rolling|mat time|boxing"),
    )),
    Specialist("philosophy", "philosophy, mental models, decision reflection", (
        _s("philosophy", r"philosoph(?:y|ies|ical|er|ers)|stoic(?:s|ism)?|ethics|ethical|morality|existential(?:ism)?|epistemolog(?:y|ical)|metaphysics"),
        _s("models", r"mental models?|first principles|meaning of life|virtue ethics|marcus aurelius|seneca|epictetus|nietzsche|kant|aristotle|plato|socrates|confucius|lao tzu|tao te ching"),
        _w("reflect", r"wisdom|virtue|moral|meaning|reflect(?:ion)?"),
    )),
    Specialist("local-engineer", "private local-model implementation, tests and fixes", (
        _s("implement", r"implement(?:ation)?|write (?:the |a |an |some )?(?:\w+ )?(?:code|function|script|tests?|patch)|add (?:a |an |some )?(?:\w+ )?tests?|fix(?:es|ing)? (?:the |this |a |that )?(?:\w+ ){0,2}(?:bug|tests?|build|error|crash|regression)"),
        _s("change", r"patch (?:the|this)|refactor (?:the|this)|open a (?:pr|pull request)|make the tests pass"),
    ), local=True, implementer=True),
)

BY_NAME: Dict[str, Specialist] = {s.name: s for s in REGISTRY}
HIGH_RISK: Tuple[str, ...] = tuple(s.name for s in REGISTRY if s.high_risk)

# Wraps the injected note. ``task_intent`` cuts the current user message at the
# opening line, so neither router ever classifies its own suggestion.
PLAN_OPEN = "<specialist-routing>"
PLAN_CLOSE = "</specialist-routing>"

# ``short`` runs ``hermes -p <profile> -z <brief> -t skills``: its own HERMES_HOME,
# one answer, and only the skills toolset (load the /learn knowledge skill) —
# no terminal, file, web, browser, messaging or kanban tools.
ADVISORY_TOOLSETS = "skills"

ADVISORY_CONSTRAINT = (
    "review/advice only: no external actions (messages, purchases, posts, scans, "
    "network calls against third parties) and no high-impact changes"
)


# --------------------------------------------------------------------------- #
# Knowledge readiness (stat-only)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Readiness:
    status: str            # ready | needs-learn | no-knowledge | not-installed | unknown-profile
    installed: bool = False
    knowledge_files: int = 0
    skill: str = ""        # skill directory name relative to the profile's skills/
    stale: bool = False    # knowledge newer than the skill (re-run /learn)

    @property
    def ready(self) -> bool:
        return self.status == "ready"

    def as_dict(self) -> Dict[str, Any]:
        return {"status": self.status, "installed": self.installed,
                "knowledge_files": self.knowledge_files, "skill": self.skill,
                "stale": self.stale}


_KNOWLEDGE_EXT = (".md", ".txt")
_SCAN_CAP = 5000  # bound directory walks; a profile corpus is far smaller


def _knowledge_stats(root: Path) -> Tuple[int, float]:
    count, newest = 0, 0.0
    if not root.is_dir():
        return 0, 0.0
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            if f.lower().endswith(_KNOWLEDGE_EXT):
                count += 1
                try:
                    newest = max(newest, (Path(dirpath) / f).stat().st_mtime)
                except OSError:
                    pass
                if count >= _SCAN_CAP:
                    return count, newest
    return count, newest


def _frontmatter(path: Path, limit: int = 4096) -> str:
    """The YAML frontmatter block of a SKILL.md (bounded read; body never read)."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            head = fh.read(limit)
    except OSError:
        return ""
    if not head.startswith("---"):
        return ""
    end = head.find("\n---", 3)
    return head[3:end] if end != -1 else ""


def _skill_mtime(skill_dir: Path) -> float:
    newest = 0.0
    for dirpath, _dirs, files in os.walk(skill_dir):
        for f in files:
            try:
                newest = max(newest, (Path(dirpath) / f).stat().st_mtime)
            except OSError:
                pass
    return newest


def _iter_skill_mds(root: Path, cap: int = 500) -> Iterable[Path]:
    """SKILL.md files under ``root``, skipping hidden dirs (curator backups)."""
    seen = 0
    for skill_md in sorted(root.rglob("SKILL.md")):
        rel = skill_md.relative_to(root).parts
        if any(part.startswith(".") for part in rel):
            continue
        seen += 1
        if seen > cap:
            return
        yield skill_md


def _bundled_names(skills: Path) -> set:
    try:
        lines = (skills / ".bundled_manifest").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return set()
    return {ln.split(":", 1)[0].strip() for ln in lines if ln.strip()}


def find_knowledge_skill(profile_home: Path, profile: str,
                         shared_skills: Optional[Path] = None) -> Optional[Path]:
    """The profile's ``/learn`` knowledge-base skill directory, if one exists.

    ``/learn`` writes ``<profile>/skills/<category>/<name>/SKILL.md`` plus
    per-chapter ``references/`` files and leaves no provenance marker in the
    frontmatter, so detection is a stat-level heuristic, in order:

      1. a skill named ``<profile>-knowledge`` (the name the docs ask for);
      2. otherwise skills bundled with Hermes (``.bundled_manifest``) or
         shared with the root profile's skills (the kit installs those in
         every profile) are skipped, then: SKILL.md frontmatter that mentions
         ``profiles/<profile>/knowledge`` or ``knowledge-base`` (frontmatter
         only; the body is never read);
      3. else the first remaining skill with ``references/*.md``.

    Repo SOUL/templates never count — readiness is what ``/learn`` wrote.
    """
    skills = profile_home / "skills"
    if not skills.is_dir():
        return None
    knowledge_ref = f"profiles/{profile}/knowledge"
    bundled = _bundled_names(skills)
    shared = ({p.parent.name for p in _iter_skill_mds(shared_skills)}
              if shared_skills is not None and shared_skills.is_dir() else set())
    fallback: Optional[Path] = None
    for skill_md in _iter_skill_mds(skills):
        skill_dir = skill_md.parent
        name = skill_dir.name
        if name == f"{profile}-knowledge":
            return skill_dir
        if name in bundled or name in shared:
            continue
        fm = _frontmatter(skill_md).lower()
        if knowledge_ref in fm or "knowledge-base" in fm:
            return skill_dir
        if fallback is None:
            refs = skill_dir / "references"
            if refs.is_dir() and any(f.suffix == ".md" for f in refs.iterdir()):
                fallback = skill_dir
    return fallback


def knowledge_readiness(profile: str, profiles_root: Optional[Path]) -> Readiness:
    if profile not in BY_NAME:
        return Readiness("unknown-profile")
    if profiles_root is None:
        return Readiness("not-installed")
    root = Path(profiles_root)
    home = root / profile
    if not home.is_dir():
        return Readiness("not-installed")
    files, newest_knowledge = _knowledge_stats(home / "knowledge")
    skill = find_knowledge_skill(home, profile, root.parent / "skills")
    if skill is not None:
        stale = bool(newest_knowledge and newest_knowledge > _skill_mtime(skill))
        rel = str(skill.relative_to(home / "skills"))
        return Readiness("ready", True, files, rel, stale)
    if files:
        return Readiness("needs-learn", True, files)
    return Readiness("no-knowledge", True, 0)


def default_profiles_root() -> Optional[Path]:
    """``$HERMES_HOME/profiles`` or ``~/.hermes/profiles``.

    Profiles always live under the ROOT Hermes home; when the orchestrator
    itself runs as a profile, ``HERMES_HOME`` points at ``.../profiles/<name>``
    and the root is two levels up.
    """
    home = os.environ.get("HERMES_HOME")
    base = Path(home).expanduser() if home else Path.home() / ".hermes"
    if base.parent.name == "profiles":
        base = base.parent.parent
    return base / "profiles"


# --------------------------------------------------------------------------- #
# Routing
# --------------------------------------------------------------------------- #
@dataclass
class SpecialistSettings:
    enabled: bool = False
    max_profiles: int = DEFAULT_MAX_PROFILES
    allowed: Tuple[str, ...] = ()          # empty = every registry profile
    require_ready: bool = False            # drop profiles whose knowledge is not ready
    classifier: str = "none"               # none | local (Ollama; non-private, ambiguous only)
    profiles_root: Optional[Path] = None   # None = default_profiles_root()
    private_terms: Tuple[str, ...] = ()
    private_paths: Tuple[str, ...] = ()
    unknown: Tuple[str, ...] = field(default_factory=tuple)  # rejected allowlist names


def clamp_max(raw: Any) -> int:
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_PROFILES
    return max(0, min(HARD_MAX_PROFILES, n))


def split_allowed(raw: Iterable[str]) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    known, unknown = [], []
    for name in raw or ():
        n = str(name).strip().lower()
        if not n:
            continue
        (known if n in BY_NAME else unknown).append(n)
    return tuple(dict.fromkeys(known)), tuple(dict.fromkeys(unknown))


_PROGRESS_RE = re.compile(
    r"(?i)\b(?:status|progress|update on|any updates?|done yet|finished|eta|is it (?:done|ready)|did (?:it|you|that))\b")

Classifier = Callable[[str, Sequence[str]], Sequence[str]]


def _score(spec: Specialist, visible: str) -> Tuple[int, List[str]]:
    score, labels = 0, []
    for label, weight, rx in spec.signals:
        if rx.search(visible):
            score += weight
            labels.append(f"kw:{label}")
    return score, labels


def _privacy(raw_text: str, settings: SpecialistSettings, session_private: bool) -> pv.PrivacyDecision:
    if session_private:
        return pv.PrivacyDecision(True, ("session-sticky",))
    try:
        return pv.scan_request({"messages": [{"role": "user", "content": raw_text}]},
                               private_terms=settings.private_terms,
                               private_paths=settings.private_paths)
    except Exception:  # fail closed
        return pv.PrivacyDecision(True, ("classifier-error",))


def _execution(tier: str, spec: Specialist) -> str:
    """``kanban`` (durable board task) for deep work and implementation;
    ``short`` (one bounded answer, read-only toolsets) for a small review.
    High-risk profiles are always ``short``: their tool surface is cut to
    :data:`ADVISORY_TOOLSETS`, which is what makes "advisory only" enforceable."""
    if spec.high_risk:
        return "short"
    if spec.implementer or tier in ("elevated", "high", "max"):
        return "kanban"
    return "short"


def route_text(
    raw_text: str,
    settings: SpecialistSettings,
    *,
    session_private: bool = False,
    classifier: Optional[Classifier] = None,
    readiness: Optional[Callable[[str], Readiness]] = None,
) -> Dict[str, Any]:
    """Route one current-turn user message. Returns a content-free trace dict.

    ``raw_text`` is the current user message as it arrived (wire format is
    fine — provenance stripping happens here). Pure apart from ``readiness``
    (stat-only), the optional ``classifier``, and bounded reads of the
    attachment files a gateway note names in the document cache.
    """
    task = ti.task_from_text(raw_text or "")
    trace: Dict[str, Any] = {
        "enabled": bool(settings.enabled),
        "task_found": task.found,
        "task": task.task_hash or "-",
        "privacy": "public",
        "privacy_reasons": [],
        "tier": "-",
        "max_profiles": settings.max_profiles,
        "routes": [],
        "dropped": [],
        "classifier": "off" if settings.classifier == "none" or classifier is None else "not-needed",
        "unknown_allowed": list(settings.unknown),
    }
    if not task.found:
        trace["reason"] = "no-current-task"
        return trace
    if ti.runtime_envelope(raw_text or ""):
        # Hermes-generated notification (async delegation result, Kanban wake,
        # background process notice): the operator asked for that work earlier;
        # the embedded report/card must not fan out specialists again.
        trace["reason"] = "runtime-envelope"
        return trace
    sep = att.separate_sender_prose(raw_text or "")
    source = "sender"
    if sep.attachment:
        # Inline attachment relay: the file text is not the sender's prose.
        # Route on the prose only, verbatim (trace keeps the full-turn hash);
        # when the boundary cannot be proven exactly, route nothing.
        trace["attachment"] = sep.reason
        if not sep.proven:
            trace["reason"] = "attachment-unverified"
            return trace
        if sep.prose != task.text:
            task = ti.task_from_sender_text(sep.prose)
            if not task.found:
                # Nothing typed: the verified upload IS the prompt. Its control
                # lines carry no authority and wire text in it routes nothing.
                if not sep.attached:
                    trace["reason"] = "attachment-only"
                    return trace
                if att.wire_text_marker(sep.attached):
                    trace["reason"] = "attachment-envelope"
                    return trace
                task = dataclasses.replace(ti.task_from_sender_text(sep.attached),
                                           authorized_max=False, forced_routine=False, forced_coding="")
                source = "attachment"
    trace["task_source"] = source

    pdec = _privacy(raw_text, settings, session_private)
    if pdec.private:
        trace["privacy"] = "local-only"
        trace["privacy_reasons"] = list(pdec.reasons)

    decision = ti.classify(task)
    trace["tier"] = decision.tier
    # A question is often exactly what a specialist answers ("how should I
    # negotiate ...?"), so only continuations, greetings and progress lookups
    # ("status of the campaign?") skip routing.
    if decision.continuation or "greeting" in decision.reasons or (
            decision.status_query and _PROGRESS_RE.search(task.visible)):
        trace["reason"] = "status-or-continuation"
        return trace

    allowed = set(settings.allowed) if settings.allowed else set(BY_NAME)
    candidates: List[Tuple[int, int, Specialist, List[str]]] = []
    for order, spec in enumerate(REGISTRY):
        score, labels = _score(spec, task.visible)
        if score < SELECT_THRESHOLD:
            continue
        if spec.name not in allowed:
            trace["dropped"].append({"profile": spec.name, "reason": "not-allowed"})
            continue
        if pdec.private and not spec.local:
            trace["dropped"].append({"profile": spec.name, "reason": "privacy:local-only"})
            continue
        candidates.append((-score, order, spec, labels))

    if not candidates and settings.classifier != "none" and classifier is not None:
        if pdec.private:
            trace["classifier"] = "skipped:private"
        elif source == "attachment":
            trace["classifier"] = "skipped:attachment"
        elif not decision.substantive or len(task.visible) < 60:
            trace["classifier"] = "skipped:not-ambiguous"
        else:
            pool = [s.name for s in REGISTRY
                    if s.name in allowed and not s.high_risk and not s.local]
            try:
                picked = [str(p).strip().lower() for p in (classifier(task.visible, pool) or ())]
                trace["classifier"] = "used"
            except Exception as exc:  # classifier is optional; never fatal
                picked = []
                trace["classifier"] = f"error:{type(exc).__name__}"
            for p in dict.fromkeys(picked):
                if p in pool:
                    candidates.append((-1, len(candidates), BY_NAME[p], ["classifier"]))
                else:
                    trace["dropped"].append({"profile": p if p in BY_NAME else "?",
                                             "reason": "classifier:not-eligible"})

    candidates.sort(key=lambda c: (c[0], c[1]))
    rdy = readiness or (lambda name: knowledge_readiness(
        name, settings.profiles_root if settings.profiles_root is not None else default_profiles_root()))
    selected: List[Tuple[Specialist, int, List[str], Readiness]] = []
    for neg_score, _order, spec, labels in candidates:
        r = rdy(spec.name)
        if r.status in ("not-installed", "unknown-profile"):
            trace["dropped"].append({"profile": spec.name, "reason": f"readiness:{r.status}"})
            continue
        if settings.require_ready and not spec.implementer and not r.ready:
            trace["dropped"].append({"profile": spec.name, "reason": f"readiness:{r.status}"})
            continue
        if len(selected) >= settings.max_profiles:
            trace["dropped"].append({"profile": spec.name, "reason": "cap"})
            continue
        selected.append((spec, -neg_score, labels, r))

    for spec, score, labels, r in selected:
        route: Dict[str, Any] = {
            "profile": spec.name,
            "score": score,
            "reasons": labels,
            "high_risk": spec.high_risk,
            "advisory_only": not spec.implementer,
            "execution": _execution(decision.tier, spec),
            "knowledge": r.as_dict(),
        }
        if route["execution"] == "short":
            route["toolsets"] = ADVISORY_TOOLSETS
        if spec.high_risk:
            route["constraint"] = ADVISORY_CONSTRAINT
        if pdec.private:
            route["local_only"] = True
        trace["routes"].append(route)
    if not trace["routes"]:
        trace["reason"] = "no-match" if not trace["dropped"] else "all-dropped"
    return trace


def route_request(request: Any, settings: SpecialistSettings, **kwargs: Any) -> Dict[str, Any]:
    """Route a provider-shaped request by its newest genuine user turn."""
    task = ti.extract_current_user_task(request)
    return route_text(task.text if task.found else "", settings, **kwargs)


def render_plan(trace: Dict[str, Any]) -> str:
    """Short, content-free routing note for the orchestrator's current turn."""
    routes = trace.get("routes") or []
    if not routes:
        return ""
    derived = ("attachment-derived: no typed instruction, routed on the verified attached "
               "file's content; " if trace.get("task_source") == "attachment" else "")
    lines = [PLAN_OPEN, "[specialist-router pilot] Suggested specialists for the current task "
             f"({derived}deterministic, task {trace.get('task', '-')}, privacy {trace.get('privacy')}):"]
    for r in routes:
        k = r["knowledge"]
        knowledge = k["status"] + (" (stale: re-run /learn)" if k.get("stale") else "")
        flags = []
        if r.get("high_risk"):
            flags.append("HIGH-RISK " + ADVISORY_CONSTRAINT)
        elif r.get("advisory_only"):
            flags.append("advisory")
        if r.get("local_only"):
            flags.append("local-only")
        lines.append(f"- {r['profile']}: {r['execution']}; reasons {','.join(r['reasons'])}; "
                     f"knowledge {knowledge}" + (f"; {'; '.join(flags)}" if flags else ""))
    lines.append(
        "Each specialist runs as its own isolated profile, never inline and never via "
        "delegate_task (it cannot carry a profile). kanban: kanban_create with "
        "assignee=<profile> and a self-contained brief. short: terminal "
        f"`hermes -p <profile> -z '<brief>' -t {ADVISORY_TOOLSETS}` for one bounded answer. "
        "Briefs carry only what the task needs: no secrets, no other profiles' output "
        "unless it is the input to review. Private tasks stay local: no cloud-backed "
        "profile. Skip a suggestion that does not fit, then synthesise one answer."
    )
    lines.append(PLAN_CLOSE)
    return "\n".join(lines)
