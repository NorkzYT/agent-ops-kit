// Minimal, explicit child environment for every tool this pilot starts
// (Chrome, LHCI/Lighthouse, the Impeccable engine). Nothing is inherited from
// the parent shell except a short TMPDIR path: no tokens, no proxies, no
// LHCI_* / IMPECCABLE_* overrides, no NODE_OPTIONS, no real HOME or XDG dirs.
// This limits what a tool can read or be redirected by; it is not a network
// sandbox (see README "Boundaries").
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { mkTmp } from './paths.mjs';

export const SAFE_PATH = `${path.dirname(process.execPath)}:/usr/local/bin:/usr/bin:/bin`;

// Chrome puts its singleton Unix socket under $TMPDIR; a long TMPDIR pushes
// that path past the 108-byte sun_path limit and Chrome never opens the
// DevTools port, so a long or non-absolute TMPDIR becomes the system /tmp.
export function shortTmpDir(tmp = process.env.TMPDIR) {
  return tmp && path.isAbsolute(tmp) && tmp.length <= 40 ? tmp : (os.tmpdir().length <= 40 ? os.tmpdir() : '/tmp');
}

export function minimalEnv(home, extra = {}) {
  return {
    PATH: SAFE_PATH,
    HOME: home,
    XDG_CONFIG_HOME: path.join(home, '.config'),
    XDG_CACHE_HOME: path.join(home, '.cache'),
    XDG_DATA_HOME: path.join(home, '.local', 'share'),
    XDG_STATE_HOME: path.join(home, '.local', 'state'),
    TMPDIR: shortTmpDir(),
    LANG: 'C.UTF-8',
    NO_COLOR: '1',
    DO_NOT_TRACK: '1',
    NO_UPDATE_NOTIFIER: '1',
    ...extra,
  };
}

/** A fresh private HOME (0700) and its minimal env; the caller removes `home`. */
export function privateEnv(prefix, extra = {}) {
  const home = mkTmp(prefix);
  fs.chmodSync(home, 0o700);
  return { home, env: minimalEnv(home, typeof extra === 'function' ? extra(home) : extra) };
}
