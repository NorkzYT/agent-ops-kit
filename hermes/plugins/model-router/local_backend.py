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

import json
import urllib.error
import urllib.request
from types import SimpleNamespace
from typing import Any, Dict, List, Optional


class LocalUnavailable(RuntimeError):
    """Local inference could not produce a result."""


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
    """A fail-closed refusal. Never contains the private content, only a label."""
    body = text or DEFAULT_REFUSAL
    if reason:
        body = f"{body}\n\n(router: {reason})"
    resp = make_response(body, model=model, finish_reason="content_filter")
    resp._model_router_refusal = True
    return resp


class LocalBackend:
    """OpenAI-compatible local chat client (Ollama). Stdlib-only."""

    def __init__(self, base_url: str, model: str, *, timeout: float = 60.0) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.model = model
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.model)

    def chat(self, messages: List[Dict[str, Any]]) -> SimpleNamespace:
        """Run a local completion. Raises :class:`LocalUnavailable` on any error."""
        if not self.configured:
            raise LocalUnavailable("local backend not configured")
        url = f"{self.base_url}/chat/completions"
        payload = json.dumps({"model": self.model, "messages": messages, "stream": False}).encode("utf-8")
        req = urllib.request.Request(url, data=payload, method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as fh:  # nosec B310 - fixed local URL
                data = json.loads(fh.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
            raise LocalUnavailable(f"local backend call failed: {type(exc).__name__}") from exc
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LocalUnavailable("local backend returned an unexpected shape") from exc
        return make_response(content if isinstance(content, str) else str(content),
                             model=data.get("model") or self.model)


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
