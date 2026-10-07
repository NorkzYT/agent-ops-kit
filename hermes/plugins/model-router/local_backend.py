"""Local inference backend + provider-shaped responses for short-circuiting.

The privacy middleware short-circuits the cloud provider by *returning* an
object shaped like an OpenAI ChatCompletion (``.choices[0].message.content``,
``.model``, ``.usage``). Hermes reads those attributes downstream, so a plain
dict will not do — we build lightweight namespace objects.

``LocalBackend`` talks to a local OpenAI-compatible endpoint (Ollama's
``/v1/chat/completions``) using only the standard library, so the plugin adds
no dependencies. Any failure raises :class:`LocalUnavailable`; the caller turns
that into a refusal — private traffic never falls back to a cloud provider.
"""

from __future__ import annotations

import ipaddress
import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple


class LocalUnavailable(RuntimeError):
    """Local inference could not produce a result.

    ``kind`` is a stable label (``config``, ``connect``, ``timeout``, ``http``,
    ``shape``) used to decide whether another local model is worth trying; it
    never carries payload or provider text.
    """

    def __init__(self, message: str, kind: str = "error") -> None:
        super().__init__(message)
        self.kind = kind


# Hostname suffixes that only resolve on a private network (Tailscale MagicDNS,
# mDNS, home/LAN zones). A bare single-label host (``ollama``) is a container or
# LAN name and also counts as local.
_LOCAL_SUFFIXES = (".local", ".lan", ".internal", ".home.arpa", ".ts.net")
_CGNAT = ipaddress.ip_network("100.64.0.0/10")


def is_local_endpoint(url: str) -> bool:
    """True when ``url`` points at loopback, RFC-1918, CGNAT (Tailscale),
    link-local, ``localhost``, a single-label host or a private-zone suffix.

    Decided from the URL text alone (no DNS), so a public hostname is never
    treated as local. Private traffic and the specialist classifier refuse a
    non-local endpoint instead of sending to it.
    """
    try:
        host = (urllib.parse.urlsplit(url or "").hostname or "").strip().lower()
    except ValueError:
        return False
    if not host:
        return False
    if host == "localhost" or host.endswith(_LOCAL_SUFFIXES):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        try:  # numeric shorthands the resolver accepts: 134744072, 0x08080808, 10.1
            ip = ipaddress.ip_address(socket.inet_ntoa(socket.inet_aton(host)))
        except (OSError, ValueError):
            return "." not in host
    return bool(ip.is_loopback or ip.is_private or ip.is_link_local
                or (ip.version == 4 and ip in _CGNAT))


def _message(content: str) -> SimpleNamespace:
    return SimpleNamespace(role="assistant", content=content, tool_calls=None, refusal=None)


def make_response(content: str, *, model: str, finish_reason: str = "stop") -> SimpleNamespace:
    """A minimal ChatCompletion-shaped object safe to return from middleware."""
    choice = SimpleNamespace(index=0, message=_message(content), finish_reason=finish_reason, delta=None)
    usage = SimpleNamespace(prompt_tokens=0, completion_tokens=0, total_tokens=0,
                            input_tokens=0, output_tokens=0)
    return SimpleNamespace(id="local-router", object="chat.completion", model=model,
                           choices=[choice], usage=usage, _model_router_local=True)


DEFAULT_REFUSAL = (
    "This request was classified as private and no local model is available to "
    "handle it, so it was not sent to any cloud provider. Connect a local Ollama "
    "chat model (see docs/model-routing.md) or remove the private data to proceed."
)


def refusal_response(model: str = "model-router/refusal", *, reason: str = "", text: str = "") -> SimpleNamespace:
    """A fail-closed refusal. Never contains the private content, only a label.

    Terminal ``finish_reason="stop"``, never ``content_filter``: Hermes treats an
    HTTP-200 ``content_filter`` as a provider safety refusal and answers it by
    activating the configured fallback chain (``handle_content_policy_refusal``),
    which moves a private session onto a cloud provider. The router's refusal is
    a final answer; it is typed by ``_model_router_refusal`` and the model label.
    """
    body = text or DEFAULT_REFUSAL
    if reason:
        body = f"{body}\n\n(router: {reason})"
    resp = make_response(body, model=model, finish_reason="stop")
    resp._model_router_refusal = True
    resp._model_router_refusal_reason = reason or "refused"
    return resp


# No proxy handler: an http_proxy/https_proxy in the gateway environment must
# never relay a private payload off the host.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class LocalBackend:
    """OpenAI-compatible local chat client (Ollama). Stdlib-only."""

    def __init__(self, base_url: str, model: str, *, timeout: float = 60.0) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.model = model
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.model)

    def chat(self, messages: List[Dict[str, Any]], *, options: Optional[Dict[str, Any]] = None,
             timeout: Optional[float] = None) -> SimpleNamespace:
        """Run a local completion. Raises :class:`LocalUnavailable` on any error.

        ``options`` adds OpenAI-compatible request fields (``temperature``,
        ``max_tokens``, ``reasoning_effort``); ``timeout`` overrides the
        per-call timeout (used to keep a fallback inside a total budget).
        """
        if not self.configured:
            raise LocalUnavailable("local backend not configured", "config")
        url = f"{self.base_url}/chat/completions"
        body: Dict[str, Any] = dict(options or {})
        body.update({"model": self.model, "messages": messages, "stream": False})
        payload = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=payload, method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with _OPENER.open(req, timeout=timeout or self.timeout) as fh:  # nosec B310 - fixed local URL
                data = json.loads(fh.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:  # model missing/unloadable: another model may work
            raise LocalUnavailable(f"local backend call failed: HTTP {exc.code}", "http") from exc
        except (TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            kind = "timeout" if isinstance(exc, TimeoutError) or isinstance(reason, TimeoutError) else "connect"
            raise LocalUnavailable(f"local backend call failed: {type(exc).__name__}", kind) from exc
        except ValueError as exc:
            raise LocalUnavailable("local backend returned invalid JSON", "shape") from exc
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LocalUnavailable("local backend returned an unexpected shape", "shape") from exc
        text = content if isinstance(content, str) else ("" if content is None else str(content))
        if not text.strip():
            # A thinking-only/empty answer is not a result: returned upstream it
            # trips Hermes' empty-response recovery, whose last rung activates the
            # configured (cloud) fallback chain.
            raise LocalUnavailable("local backend returned empty content", "shape")
        return make_response(text, model=data.get("model") or self.model)


# Endpoint-level failures: the fallback model lives on the same endpoint, so
# trying it cannot help.
_STOP_KINDS = ("config", "connect")

BackendFactory = Callable[[str], Any]


def run_local_chain(
    models: Sequence[str],
    messages: List[Dict[str, Any]],
    factory: BackendFactory,
    *,
    budget: Optional[float] = None,
    options: Optional[Dict[str, Any]] = None,
) -> Tuple[SimpleNamespace, str, int]:
    """Try ``models`` in order on the local endpoint; never anything else.

    Returns ``(response, model, attempt)``. Bounded: duplicates are skipped,
    an endpoint-level failure (``config``/``connect``) stops the walk, and when
    ``budget`` (seconds, total) is set each attempt gets only the time left.
    Raises :class:`LocalUnavailable` when every model failed; the caller turns
    that into a refusal or an empty result — never a cloud call.
    """
    chain = [m for m in dict.fromkeys(str(m).strip() for m in models) if m]
    if not chain:
        raise LocalUnavailable("no local model configured", "config")
    deadline = time.monotonic() + budget if budget else None
    last: Optional[LocalUnavailable] = None
    for attempt, model in enumerate(chain, 1):
        kwargs: Dict[str, Any] = {}
        if options:
            kwargs["options"] = options
        if deadline is not None:
            left = deadline - time.monotonic()
            if left <= 0.5:
                break
            kwargs["timeout"] = left
        try:
            return factory(model).chat(messages, **kwargs), model, attempt
        except LocalUnavailable as exc:
            last = exc
            if getattr(exc, "kind", "") in _STOP_KINDS:
                break
    raise last or LocalUnavailable("local budget exhausted", "timeout")


def messages_from_request(request: Any) -> List[Dict[str, Any]]:
    """Extract chat messages for the local backend from a provider request."""
    if isinstance(request, dict):
        msgs = request.get("messages")
        if isinstance(msgs, list) and msgs:
            return [m for m in msgs if isinstance(m, dict)]
        # Responses API shape: synthesize a single user turn from flattened text.
    from .privacy import extract_texts

    texts = extract_texts(request)
    return [{"role": "user", "content": "\n".join(texts)}] if texts else [{"role": "user", "content": ""}]
