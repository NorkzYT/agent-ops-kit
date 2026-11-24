#!/usr/bin/env bash
# Chrome launcher for local HyperFrames renders. Chrome's own proxied requests
# for non-loopback hosts go to a dead local proxy so they fail, and
# Chrome's background services (component updates, sync, pings, field trials)
# are switched off. Loopback stays direct: the renderer serves the project from
# 127.0.0.1. This only configures Chrome: it does not confine the HyperFrames
# Node process, and a proxy flag is not a network sandbox (e.g. it does not
# cover every Chrome subsystem). Set MARKETING_CHROME to the real browser.
set -euo pipefail
real="${MARKETING_CHROME:-/usr/bin/google-chrome}"
[[ -x "$real" ]] || { echo "chrome-offline: browser not executable: $real" >&2; exit 127; }
exec "$real" \
  --proxy-server=http://127.0.0.1:9 \
  --proxy-bypass-list='<-loopback>;127.0.0.1;localhost' \
  --disable-background-networking \
  --disable-component-update \
  --disable-sync \
  --disable-domain-reliability \
  --disable-client-side-phishing-detection \
  --disable-default-apps \
  --disable-extensions \
  --metrics-recording-only \
  --no-first-run \
  --no-pings \
  --disable-features=OptimizationHints,OptimizationGuideModelDownloading,MediaRouter,Translate,AutofillServerCommunication,CertificateTransparencyComponentUpdater \
  "$@"
