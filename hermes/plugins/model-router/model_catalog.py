"""Live CLIProxyAPI model catalog for newest-in-family Sol/Astra routing.

``GptModelCatalog.snapshot()`` returns the model ids CLIProxyAPI currently
advertises on ``GET <base>/models`` (OpenAI shape ``{"data": [{"id": ...}]}``),
or ``None`` when the catalog is unavailable. The router uses the snapshot only
to pick a *version* inside a tier it has already decided (``complexity.
resolve_model``); it never decides the tier, never authorizes Astra, and never
reads model ids from conversation text.

Safety properties:

* Content-free: the request is a bodiless GET; no prompt, session or task data.
* Bounded: short per-socket-operation timeout (not a hard total deadline),
  response size and id-count caps; a success is cached for ``ttl`` seconds and
  a failure for ``retry`` seconds, so at most one fetch per window reaches the
  proxy. A failed refresh keeps serving the last good snapshot only while it
  is younger than ``max(stale, ttl)`` seconds.
* Fail-safe: any error (missing key, HTTP error, timeout, malformed body) is
  "unavailable"; the router then falls back to the configured static list or
  the static tier table, exactly as before this module existed.
* Quiet: logs carry an outcome label and id count only — never the key, the
  response body or model ids.
* No environment proxy and no redirects: an ``http(s)_proxy`` in the gateway
  environment or a 3xx answer cannot relay the bearer to another host.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Optional, Tuple

logger = logging.getLogger("hermes.plugin.model_router")

MAX_BYTES = 512 * 1024
MAX_IDS = 2000
FETCH_TIMEOUT = 3.0



class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: urllib would replay the bearer to the new host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


# No environment proxy and no redirects: the bearer only ever goes to the
# configured URL.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


class _Unavailable(Exception):
    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


def _parse(raw: bytes) -> Tuple[str, ...]:
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise _Unavailable("malformed") from None
    data = doc.get("data") if isinstance(doc, dict) else None
    if not isinstance(data, list) or len(data) > MAX_IDS:
        raise _Unavailable("malformed")
    ids = []
    for entry in data:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            raise _Unavailable("malformed")
        ids.append(entry["id"])
    return tuple(ids)


class GptModelCatalog:
    def __init__(self, base_url: str, key_env: str, *, ttl: float = 300.0, retry: float = 30.0,
                 stale: float = 900.0, timeout: float = FETCH_TIMEOUT, opener: Any = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.url = f"{(base_url or '').rstrip('/')}/models"
        self.key_env = key_env
        self.ttl, self.retry, self.stale = ttl, retry, stale
        self.timeout = min(timeout, 5.0)
        self._opener = opener or _OPENER
        self._clock = clock
        self._lock = threading.Lock()
        self._good: Optional[Tuple[str, ...]] = None
        self._good_at = 0.0
        self._next_fetch = 0.0
        self.fetches = 0

    def _fetch(self) -> Tuple[str, ...]:
        if not self.url.startswith(("http://", "https://")):
            raise _Unavailable("bad-url")
        key = os.environ.get(self.key_env or "", "").strip()
        if not key:
            raise _Unavailable("no-key")
        self.fetches += 1
        req = urllib.request.Request(self.url, method="GET",
                                     headers={"Authorization": f"Bearer {key}",
                                              "Accept": "application/json"})
        try:
            with self._opener.open(req, timeout=self.timeout) as fh:  # nosec B310 - operator URL
                raw = fh.read(MAX_BYTES + 1)
        except urllib.error.HTTPError:
            raise _Unavailable("http") from None
        except (TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            timed_out = isinstance(exc, TimeoutError) or isinstance(reason, TimeoutError)
            raise _Unavailable("timeout" if timed_out else "connect") from None
        if len(raw) > MAX_BYTES:
            raise _Unavailable("oversized")
        return _parse(raw)

    def snapshot(self) -> Optional[Tuple[str, ...]]:
        """Current advertised ids, the last good set within ``max(stale, ttl)``, or None."""
        with self._lock:
            now = self._clock()
            if now >= self._next_fetch:
                try:
                    self._good, self._good_at = self._fetch(), now
                    self._next_fetch = now + self.ttl
                    logger.info("model_router: gpt catalog ok ids=%d", len(self._good))
                except _Unavailable as exc:
                    self._next_fetch = now + self.retry
                    logger.warning("model_router: gpt catalog unavailable:%s", exc.kind)
                except Exception:  # never let the catalog break routing
                    self._next_fetch = now + self.retry
                    logger.warning("model_router: gpt catalog unavailable:error")
            if self._good is not None and now - self._good_at <= max(self.stale, self.ttl):
                return self._good
            return None


def catalog_base_url(settings: Any) -> str:
    """Catalog endpoint: explicit ``gpt_catalog_base_url``, else $CLIPROXY_BASE_URL,
    else ``http://127.0.0.1:$CLIPROXY_PORT/v1`` (port default 8317).

    Never ``specialist_classifier_base_url``: that endpoint has its own key, and
    the catalog bearer (``gpt_catalog_key_env``) must not be sent to it.
    """
    explicit = ((getattr(settings, "gpt_catalog_base_url", "") or "").strip()
                or os.environ.get("CLIPROXY_BASE_URL", "").strip())
    if explicit:
        return explicit.rstrip("/")
    port = os.environ.get("CLIPROXY_PORT", "").strip() or "8317"
    return f"http://127.0.0.1:{port}/v1"


def from_settings(settings: Any) -> Optional[GptModelCatalog]:
    """The live catalog for the orchestrator proxy, or None when switched off."""
    if getattr(settings, "gpt_catalog", "off") != "live":
        return None
    base = catalog_base_url(settings)
    return GptModelCatalog(base, getattr(settings, "gpt_catalog_key_env", "") or "CLIPROXY_API_KEY",
                           ttl=getattr(settings, "gpt_catalog_ttl", 300.0))
