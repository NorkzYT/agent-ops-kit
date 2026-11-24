import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

// Hermes adaptation (not upstream): flags every headless Chrome launch in this skill adds.
// Figures are local file:// pages with nothing remote in them; these flags reduce Chrome's own
// traffic. Proxied (HTTP(S)/WebSocket) non-loopback requests go to a dead local proxy and fail,
// and background services (component updates, sync, pings, field trials) stay off. Loopback stays
// direct so a local dev server (http://localhost:3000) can still be captured. This is not network
// isolation: a proxy flag configures Chrome, it does not confine it, no firewall or network
// namespace is applied, and traffic the proxy setting does not cover (for example WebRTC/UDP) is
// not blocked. The launchers also pass
// --no-sandbox on Linux and open a DevTools port on loopback, and a page can reach every loopback
// service on the host, so only pages you built yourself may be opened (see trustedTarget).
export const OFFLINE_CHROME_FLAGS = [
  "--proxy-server=http://127.0.0.1:9",
  "--proxy-bypass-list=<-loopback>;127.0.0.1;localhost",
  "--disable-background-networking",
  "--disable-component-update",
  "--disable-sync",
  "--disable-domain-reliability",
  "--disable-client-side-phishing-detection",
  "--disable-default-apps",
  "--disable-extensions",
  "--metrics-recording-only",
  "--no-pings",
  "--disable-features=OptimizationHints,OptimizationGuideModelDownloading,MediaRouter,Translate,AutofillServerCommunication,CertificateTransparencyComponentUpdater",
];

const LOOPBACK = new Set(["localhost", "127.0.0.1", "[::1]"]);

// The page a launcher may open: a local file (path or file: URL) or an http(s) page on loopback,
// which is where your own figure or dev server lives. Anything else is refused before Chrome starts.
export function trustedTarget(target) {
  if (!/^[a-z][a-z0-9+.-]*:/i.test(target) || /^[a-z]:[\\/]/i.test(target)) return pathToFileURL(resolve(target)).href;
  let url;
  try {
    url = new URL(target);
  } catch {
    throw new Error(`refusing ${target}: not a valid URL`);
  }
  if (url.protocol === "file:") return url.href;
  if ((url.protocol === "http:" || url.protocol === "https:") && LOOPBACK.has(url.hostname)) return url.href;
  throw new Error(`refusing ${target}: open only figures you built, as a local file or a loopback (localhost) URL`);
}
