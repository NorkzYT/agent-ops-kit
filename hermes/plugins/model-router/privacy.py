"""Deterministic private-data detection for the model-router plugin.

Pure, dependency-free, side-effect-free: given the outbound provider request
(system + messages + tool results) it returns a :class:`PrivacyDecision`. It
never inspects the network, never logs prompt content, and returns only stable
detector *labels* — never the matched bytes. Fail-closed callers treat any
raised exception here as "private".

Signals (all deterministic):
  * explicit ``#private`` / ``#local`` tags
  * US SSN (area/group/serial validity filtered)
  * Luhn-valid PAN (13-19 digits)
  * ABA routing number (9 digits, checksum)
  * IBAN (mod-97)
  * private keys / provider secrets / bearer tokens / generic key=secret
  * credential / login pairs (user+password, or creds-in-URL)
  * operator-configured private terms and path fragments
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, List, Sequence


# --------------------------------------------------------------------------- #
# Decision object
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PrivacyDecision:
    """Outcome of a privacy scan. ``reasons`` are detector labels only."""

    private: bool
    reasons: tuple = ()

    @property
    def label(self) -> str:
        return ",".join(self.reasons) if self.reasons else ("private" if self.private else "public")


# --------------------------------------------------------------------------- #
# Payload flattening — inspect the ENTIRE outbound request
# --------------------------------------------------------------------------- #
def _content_to_text(content: Any) -> List[str]:
    """Flatten one message ``content`` field (str, or list of parts) to text."""
    out: List[str] = []
    if isinstance(content, str):
        out.append(content)
    elif isinstance(content, dict):
        for key in ("text", "content", "input", "output"):
            v = content.get(key)
            if isinstance(v, str):
                out.append(v)
    elif isinstance(content, (list, tuple)):
        for part in content:
            out.extend(_content_to_text(part))
    return out


def extract_texts(request: Any) -> List[str]:
    """Every human-readable string in a provider request.

    Covers chat_completions (``messages``/``system``), Responses API
    (``input``/``instructions``) and tool-result messages (``role`` == tool /
    ``tool_call_id`` present). Order is stable; content is never mutated.
    """
    texts: List[str] = []
    if not isinstance(request, dict):
        return texts
    for key in ("system", "instructions"):
        v = request.get(key)
        if isinstance(v, str):
            texts.append(v)
        else:
            texts.extend(_content_to_text(v))
    for key in ("messages", "input"):
        msgs = request.get(key)
        if isinstance(msgs, (list, tuple)):
            for m in msgs:
                if isinstance(m, dict):
                    texts.extend(_content_to_text(m.get("content")))
                    # tool results sometimes ride in dedicated fields
                    for extra in ("tool_result", "output", "result"):
                        texts.extend(_content_to_text(m.get(extra)))
                elif isinstance(m, str):
                    texts.append(m)
    return [t for t in texts if isinstance(t, str) and t]


# --------------------------------------------------------------------------- #
# Individual deterministic detectors
# --------------------------------------------------------------------------- #
_TAG_RE = re.compile(r"(?<![A-Za-z0-9_])#(?:private|local)\b", re.IGNORECASE)
_SSN_RE = re.compile(r"\b(\d{3})-(\d{2})-(\d{4})\b")
_DIGITS_RUN_RE = re.compile(r"(?<![\d.])(?:\d[ -]?){13,19}(?![\d.])")
_ABA_RE = re.compile(r"\b(\d{9})\b")
_IBAN_RE = re.compile(r"\b([A-Z]{2}\d{2}[A-Z0-9]{11,30})\b")

_PEM_RE = re.compile(r"-----BEGIN (?:[A-Z0-9 ]*)?PRIVATE KEY-----")
_SECRET_LITERALS = (
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                       # AWS access key id
    re.compile(r"\bASIA[0-9A-Z]{16}\b"),                       # AWS STS key id
    re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b"),                 # OpenAI-style secret
    re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}\b"),             # Anthropic-style secret
    re.compile(r"\bghp_[A-Za-z0-9]{30,}\b"),                   # GitHub PAT
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),           # GitHub fine-grained PAT
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),           # Slack token
    re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),                 # Google API key
)
_BEARER_RE = re.compile(r"(?i)authorization\s*:\s*bearer\s+[A-Za-z0-9._\-]{8,}")
_ASSIGN_SECRET_RE = re.compile(
    r"(?i)\b(?:api[_-]?key|secret(?:[_-]?key)?|access[_-]?token|auth[_-]?token|"
    r"client[_-]?secret|private[_-]?key)\b\s*[:=]\s*[\"']?[A-Za-z0-9/+_\-\.=]{6,}"
)
_PASSWORD_RE = re.compile(r"(?i)\b(?:password|passwd|pwd)\b\s*[:=]\s*[\"']?\S{4,}")
_USERNAME_RE = re.compile(r"(?i)\b(?:user(?:name)?|login|email)\b\s*[:=]\s*[\"']?\S{3,}")
_URL_CREDS_RE = re.compile(r"[a-z][a-z0-9+.\-]*://[^/\s:@]+:[^/\s:@]+@", re.IGNORECASE)


def _luhn_ok(digits: str) -> bool:
    if not (13 <= len(digits) <= 19):
        return False
    total, alt = 0, False
    for ch in reversed(digits):
        d = ord(ch) - 48
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


def _ssn_valid(area: str, group: str, serial: str) -> bool:
    a = int(area)
    if a == 0 or a == 666 or a >= 900:
        return False
    if int(group) == 0 or int(serial) == 0:
        return False
    return True


def _aba_valid(d: str) -> bool:
    if len(d) != 9 or d == "000000000":
        return False
    n = [ord(c) - 48 for c in d]
    checksum = (3 * (n[0] + n[3] + n[6]) + 7 * (n[1] + n[4] + n[7]) + (n[2] + n[5] + n[8]))
    if checksum % 10 != 0:
        return False
    lead = int(d[:2])
    return lead <= 12 or 21 <= lead <= 32 or 61 <= lead <= 72 or lead == 80


def _iban_valid(s: str) -> bool:
    s = s.upper()
    if not (15 <= len(s) <= 34):
        return False
    rearranged = s[4:] + s[:4]
    try:
        numeric = "".join(str(int(c, 36)) for c in rearranged)
        return int(numeric) % 97 == 1
    except ValueError:
        return False


# --------------------------------------------------------------------------- #
# Scan
# --------------------------------------------------------------------------- #
def _scan_text(text: str, private_terms: Sequence[str], private_paths: Sequence[str]) -> List[str]:
    reasons: List[str] = []
    low = text.lower()

    if _TAG_RE.search(text):
        reasons.append("tag")
    for term in private_terms:
        if term and term.lower() in low:
            reasons.append("term")
            break
    for path in private_paths:
        if path and path.lower() in low:
            reasons.append("path")
            break

    for area, group, serial in _SSN_RE.findall(text):
        if _ssn_valid(area, group, serial):
            reasons.append("ssn")
            break

    for run in _DIGITS_RUN_RE.findall(text):
        if _luhn_ok(re.sub(r"[ -]", "", run)):
            reasons.append("pan")
            break

    if any(_aba_valid(m) for m in _ABA_RE.findall(text)):
        reasons.append("aba")
    if any(_iban_valid(m) for m in _IBAN_RE.findall(text)):
        reasons.append("iban")

    if _PEM_RE.search(text):
        reasons.append("private_key")
    if any(rx.search(text) for rx in _SECRET_LITERALS) or _ASSIGN_SECRET_RE.search(text) or _BEARER_RE.search(text):
        reasons.append("secret")

    if _URL_CREDS_RE.search(text) or (_PASSWORD_RE.search(text) and _USERNAME_RE.search(text)):
        reasons.append("credentials")
    elif _PASSWORD_RE.search(text):
        reasons.append("credentials")

    return reasons


def scan_request(
    request: Any,
    *,
    private_terms: Iterable[str] = (),
    private_paths: Iterable[str] = (),
) -> PrivacyDecision:
    """Scan a full provider request. Returns a fail-closed-friendly decision.

    Raises nothing on malformed input — an empty request is simply public. The
    caller is responsible for treating *its own* exceptions as private.
    """
    terms = tuple(private_terms or ())
    paths = tuple(private_paths or ())
    reasons: List[str] = []
    for text in extract_texts(request):
        reasons.extend(_scan_text(text, terms, paths))
    unique = tuple(sorted(set(reasons)))
    return PrivacyDecision(private=bool(unique), reasons=unique)
