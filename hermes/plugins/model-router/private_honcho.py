"""Private Honcho route for ``#private`` / ``#privacy`` turns — fail-closed.

Goal: a marked turn's memory goes to a *separate* self-hosted Honcho whose
reasoning (deriver, summary, dream, dialectic) and embeddings run on a local
Ollama, never to the public Honcho (whose reasoning goes through CLIProxyAPI).

What this module does today:

  * resolves the private route from ``PRIVATE_HONCHO_*`` settings, leaving the
    public Honcho settings (``HONCHO_*``, ``honcho.json``) untouched;
  * validates it fail-closed (:func:`validate`): the reasoning and embedding
    endpoints must be a local Ollama, never CLIProxyAPI, claude-max-proxy or any
    public host; the private Honcho API must be local and a different instance
    from the public one (a workspace switch on the same server is not
    isolation, because that server's reasoning goes through CLIProxyAPI);
  * decides the memory route for one request (:func:`decide`): unmarked turns
    keep the public route unchanged; marked turns get the private route only if
    the config is valid AND Hermes exposes a per-turn memory-route seam.

Hermes 0.21.2 has no such seam: ``MemoryManager.sync_all(user, assistant, *,
session_id)``, ``prefetch_all`` / ``queue_prefetch_all(query, *, session_id)``
and ``on_pre_compress`` take no host or route argument, and the Honcho provider
binds one ``baseUrl`` per profile at startup. So :data:`HERMES_MEMORY_ROUTE_SEAM`
is ``False`` and every marked turn resolves to a typed refusal
(``kind="seam"``): nothing is dispatched to either Honcho by this module. That
does not stop Hermes core from syncing the turn to the public Honcho itself —
see docs/model-routing.md "Honcho and private sessions".
"""

from __future__ import annotations

import os
import urllib.parse
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Tuple

from .local_backend import is_local_endpoint

# Flip only when Hermes core gains a plugin-drivable per-turn memory route
# (see docs/model-routing.md, "Pending design", item 1) and that seam is wired
# here. Until then a marked turn can never be given the private route.
HERMES_MEMORY_ROUTE_SEAM = False

OLLAMA_PORT = 11434
# Cloud-backed proxies in this stack. Their hosts/ports are rejected even on
# loopback, because what sits behind them is a cloud model.
_CLOUD_PROXY_HOSTS = ("cliproxyapi", "cliproxy", "claude-max-proxy", "claude-max-api-proxy")
_CLOUD_PROXY_PORTS = (8317, 3456)
_DEFAULT_PUBLIC_HONCHO = "http://127.0.0.1:8000"


class PrivateHonchoRefused(RuntimeError):
    """The private Honcho route cannot be used. ``kind`` is a stable label
    (``disabled``, ``config``, ``cloud-proxy``, ``public-endpoint``,
    ``not-ollama``, ``same-instance``, ``seam``); the message never carries
    request content."""

    def __init__(self, message: str, kind: str) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class PrivateHonchoConfig:
    base_url: str = ""            # the private Honcho API (separate instance)
    workspace: str = ""
    reasoning_base_url: str = ""  # deriver/summary/dream/dialectic endpoint
    reasoning_model: str = ""
    embed_base_url: str = ""


@dataclass(frozen=True)
class PublicHonchoConfig:
    base_url: str = _DEFAULT_PUBLIC_HONCHO
    workspace: str = ""
    cloud_proxy_urls: Tuple[str, ...] = ()  # CLIProxyAPI / claude-max-proxy as configured


@dataclass(frozen=True)
class MemoryRoute:
    """``route`` is ``public`` (unchanged public Honcho), ``private`` (the
    validated private instance) or ``refuse`` (neither — fail closed)."""

    route: str
    reason: str = ""
    public: Optional[PublicHonchoConfig] = None
    private: Optional[PrivateHonchoConfig] = None


def _env(env: Optional[Mapping[str, str]]) -> Mapping[str, str]:
    return os.environ if env is None else env


def load_private(env: Optional[Mapping[str, str]] = None) -> PrivateHonchoConfig:
    e = _env(env)
    get = lambda k: str(e.get(k, "") or "").strip()  # noqa: E731
    return PrivateHonchoConfig(
        base_url=get("PRIVATE_HONCHO_BASE_URL"),
        workspace=get("PRIVATE_HONCHO_WORKSPACE"),
        reasoning_base_url=get("PRIVATE_HONCHO_REASONING_BASE_URL"),
        reasoning_model=get("PRIVATE_HONCHO_MODEL"),
        embed_base_url=get("PRIVATE_HONCHO_EMBED_BASE_URL"),
    )


def load_public(env: Optional[Mapping[str, str]] = None) -> PublicHonchoConfig:
    """The public Honcho as the stack already configures it. Read-only."""
    e = _env(env)
    port = str(e.get("HONCHO_PORT", "") or "").strip() or "8000"
    base = str(e.get("HONCHO_BASE_URL", "") or "").strip() or f"http://127.0.0.1:{port}"
    proxies = [f"http://127.0.0.1:{str(e.get('CLIPROXY_PORT', '') or '8317').strip()}",
               f"http://127.0.0.1:{str(e.get('CLAUDE_MAX_PROXY_PORT', '') or '3456').strip()}"]
    for key in ("CLIPROXY_BASE_URL", "CLAUDE_MAX_PROXY_BASE_URL", "OPENAI_BASE_URL"):
        if str(e.get(key, "") or "").strip():
            proxies.append(str(e[key]).strip())
    return PublicHonchoConfig(base_url=base, workspace=str(e.get("HONCHO_WORKSPACE", "") or "").strip(),
                              cloud_proxy_urls=tuple(proxies))


def _host_port(url: str) -> Tuple[str, Optional[int]]:
    try:
        parts = urllib.parse.urlsplit(url or "")
        host = (parts.hostname or "").strip().lower()
        port = parts.port
    except ValueError:
        return "", None
    if port is None and parts.scheme in ("http", "https"):
        port = 443 if parts.scheme == "https" else 80
    return host, port


_LOOPBACK = ("127.0.0.1", "localhost", "::1", "0.0.0.0")


def _same_endpoint(a: str, b: str) -> bool:
    ha, pa = _host_port(a)
    hb, pb = _host_port(b)
    if not ha or not hb or pa != pb:
        return False
    return ha == hb or (ha in _LOOPBACK and hb in _LOOPBACK)


def _check_local_ollama(label: str, url: str, public: PublicHonchoConfig) -> None:
    host, port = _host_port(url)
    if not host:
        raise PrivateHonchoRefused(f"{label} is not set or not a URL", "config")
    if any(p in host for p in _CLOUD_PROXY_HOSTS) or port in _CLOUD_PROXY_PORTS or any(
            _same_endpoint(url, p) for p in public.cloud_proxy_urls):
        raise PrivateHonchoRefused(f"{label} points at a cloud-backed proxy (CLIProxyAPI/claude-max-proxy)",
                                   "cloud-proxy")
    if not is_local_endpoint(url):
        raise PrivateHonchoRefused(f"{label} is not a local endpoint", "public-endpoint")
    if port != OLLAMA_PORT and host != "ollama" and not host.startswith("ollama"):
        raise PrivateHonchoRefused(f"{label} must be a local Ollama endpoint (:{OLLAMA_PORT} or host 'ollama')",
                                   "not-ollama")


def validate(private: PrivateHonchoConfig, public: PublicHonchoConfig) -> PrivateHonchoConfig:
    """Return ``private`` unchanged when it is safe; raise otherwise."""
    if not private.base_url:
        raise PrivateHonchoRefused("PRIVATE_HONCHO_BASE_URL is not set", "disabled")
    for name, var in (("workspace", "PRIVATE_HONCHO_WORKSPACE"),
                      ("reasoning_base_url", "PRIVATE_HONCHO_REASONING_BASE_URL"),
                      ("reasoning_model", "PRIVATE_HONCHO_MODEL"),
                      ("embed_base_url", "PRIVATE_HONCHO_EMBED_BASE_URL")):
        if not getattr(private, name):
            raise PrivateHonchoRefused(f"{var} is not set", "config")
    _check_local_ollama("PRIVATE_HONCHO_REASONING_BASE_URL", private.reasoning_base_url, public)
    _check_local_ollama("PRIVATE_HONCHO_EMBED_BASE_URL", private.embed_base_url, public)
    host, port = _host_port(private.base_url)
    if not host:
        raise PrivateHonchoRefused("PRIVATE_HONCHO_BASE_URL is not a URL", "config")
    if any(p in host for p in _CLOUD_PROXY_HOSTS) or port in _CLOUD_PROXY_PORTS:
        raise PrivateHonchoRefused("PRIVATE_HONCHO_BASE_URL points at a cloud-backed proxy", "cloud-proxy")
    if not is_local_endpoint(private.base_url):
        raise PrivateHonchoRefused("PRIVATE_HONCHO_BASE_URL is not a local endpoint", "public-endpoint")
    if _same_endpoint(private.base_url, public.base_url):
        raise PrivateHonchoRefused("PRIVATE_HONCHO_BASE_URL is the public Honcho instance; a separate "
                                   "workspace on it is not isolation", "same-instance")
    return private


def decide(marked: bool, *, private: Optional[PrivateHonchoConfig] = None,
           public: Optional[PublicHonchoConfig] = None,
           seam: Optional[bool] = None) -> MemoryRoute:
    """Memory route for one request. Unmarked: the public config, as-is.
    Marked: ``private`` only with a valid config and a Hermes seam, else
    ``refuse`` — never ``public``."""
    public = public if public is not None else load_public()
    if not marked:
        return MemoryRoute("public", public=public)
    seam = HERMES_MEMORY_ROUTE_SEAM if seam is None else seam
    try:
        cfg = validate(private if private is not None else load_private(), public)
    except PrivateHonchoRefused as exc:
        return MemoryRoute("refuse", reason=exc.kind)
    if not seam:
        return MemoryRoute("refuse", reason="seam")
    return MemoryRoute("private", private=cfg)


def dispatch(route: MemoryRoute, *, public_call: Callable[[PublicHonchoConfig], Any],
             private_call: Callable[[PrivateHonchoConfig], Any]) -> Any:
    """Run the one call the route allows. ``refuse`` runs neither and raises
    :class:`PrivateHonchoRefused`, so a caller cannot fall through to public."""
    if route.route == "public" and route.public is not None:
        return public_call(route.public)
    if route.route == "private" and route.private is not None:
        return private_call(route.private)
    raise PrivateHonchoRefused(f"private memory refused ({route.reason or 'invalid route'})",
                               route.reason or "config")
