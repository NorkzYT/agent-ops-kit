"""Fail-closed extension of Hermes' auxiliary pre-egress guard.

Auxiliary calls (compression, title generation) run outside the ``llm_request``
/ ``llm_execution`` middleware, so this plugin's ``#privacy`` guard never sees
them. Hermes core guards that path itself (``agent.aux_egress_guard``), with
two gaps for this stack:

  * ``is_local_endpoint`` treats every loopback URL as local, and a local
    endpoint skips the privacy scan. CLIProxyAPI (:8317) and claude-max-proxy
    (:3456) listen on loopback but answer from cloud models, so a private
    compression payload pinned to CLIProxyAPI would be sent unscanned.
  * ``scan_payload`` recognizes ``#private`` / ``#local`` but not ``#privacy``.

:func:`install` wraps both functions in the loaded core module: a cloud proxy
is never local, and a payload carrying a standalone ``#privacy`` / ``#private``
token anywhere is private. Hermes then refuses the primary call
(``AuxCloudEgressRefused``) and every cloud fallback for it. Compression
payloads are serialized transcripts, so the sender provenance the request
guard uses is gone: any occurrence counts (fails toward refusal). Errors fail
closed: a locality error reports "not local", a scan error reports private.
"""

from __future__ import annotations

import importlib
import logging
import os
import urllib.parse
from typing import Any, Iterable, Iterator, Optional, Tuple

from .privacy_marker import has_marker
from .private_honcho import _CLOUD_PROXY_HOSTS, _CLOUD_PROXY_PORTS, _same_endpoint

logger = logging.getLogger("hermes.plugin.model_router")

CORE_MODULE = "agent.aux_egress_guard"
_WRAPPED = "_model_router_aux_guard"


def proxy_urls(settings: Any) -> Tuple[str, ...]:
    """Configured CLIProxyAPI and claude-max-proxy base URLs."""
    claude = (os.environ.get("CLAUDE_MAX_PROXY_BASE_URL", "").strip()
              or f"http://127.0.0.1:{os.environ.get('CLAUDE_MAX_PROXY_PORT', '').strip() or '3456'}/v1")
    return tuple(u for u in dict.fromkeys((settings.cliproxy_base_url(), claude.rstrip("/"))) if u)


def is_cloud_proxy(base_url: Any, proxies: Iterable[str] = ()) -> bool:
    """True for a URL that reaches a cloud model through a local proxy."""
    raw = str(base_url or "").strip()
    if not raw:
        return False
    parts = urllib.parse.urlsplit(raw if "://" in raw else "//" + raw)
    host = (parts.hostname or "").lower()
    if any(name in host for name in _CLOUD_PROXY_HOSTS) or parts.port in _CLOUD_PROXY_PORTS:
        return True
    return any(_same_endpoint(raw, p) for p in proxies)


def _texts(value: Any) -> Iterator[str]:
    """Every string value in a messages structure, however nested."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _texts(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _texts(item)


def _has_marker(messages: Any) -> bool:
    return any(has_marker(text) for text in _texts(messages))


def install(proxies: Iterable[str], core: Optional[Any] = None, import_name: str = CORE_MODULE) -> bool:
    """Wrap the core guard's locality and privacy checks. Idempotent; returns
    False (and logs an error) when the core guard is not importable."""
    if core is None:
        try:
            core = importlib.import_module(import_name)
        except Exception:
            logger.error("model_router: aux privacy guard unavailable (%s not importable); auxiliary "
                         "calls to CLIProxyAPI are not privacy-scanned — pin compression to local "
                         "Ollama (AUX_COMPRESSION_ROUTE=aux)", import_name)
            return False
    if getattr(core.scan_payload, _WRAPPED, False):
        return True
    proxies = tuple(proxies)
    orig_local, orig_scan = core.is_local_endpoint, core.scan_payload

    def is_local_endpoint(base_url: Any) -> bool:
        try:
            return not is_cloud_proxy(base_url, proxies) and bool(orig_local(base_url))
        except Exception:
            return False

    def scan_payload(messages: Any):
        try:
            result = orig_scan(messages)
            if result.private:
                return result
            if _has_marker(messages):
                return core.PrivacyResult(True, "privacy-marker")
            return result
        except Exception:
            return core.PrivacyResult(True, "scan-error")

    for fn in (is_local_endpoint, scan_payload):
        setattr(fn, _WRAPPED, True)
    core.is_local_endpoint, core.scan_payload = is_local_endpoint, scan_payload
    logger.info("model_router: aux privacy guard installed (cloud proxies=%d, marker=#privacy/#private)",
                len(proxies))
    return True
