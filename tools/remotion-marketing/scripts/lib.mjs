// Shared helpers for the marketing-video CLIs. Node stdlib only.
import {readFileSync} from 'node:fs';

/** Parse `--key value` and `--flag` args into a plain object. */
export function parseArgs(argv) {
  const out = {_: []};
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a.startsWith('--')) {
      const key = a.slice(2);
      const next = argv[i + 1];
      if (next === undefined || next.startsWith('--')) {
        out[key] = true;
      } else {
        out[key] = next;
        i++;
      }
    } else {
      out._.push(a);
    }
  }
  return out;
}

export function loadJson(path) {
  return JSON.parse(readFileSync(path, 'utf8'));
}

/** Collect the asset ids a props object actually references. */
export function usedAssetIds(props) {
  const ids = new Set();
  if (props.brand?.logoAssetId) ids.add(props.brand.logoAssetId);
  for (const scene of props.scenes ?? []) {
    if (scene.mediaAssetId) ids.add(scene.mediaAssetId);
  }
  if (props.music && props.music.mode !== 'none' && props.music.assetId) {
    ids.add(props.music.assetId);
  }
  return ids;
}

export const OK = '✓';
export const X = '✗';

/** Print a report and exit with 0 (pass) or 1 (fail). */
export function finish(title, violations, warnings = [], extra = '') {
  const pass = violations.length === 0;
  console.log(`\n${title}`);
  if (extra) console.log(extra);
  for (const w of warnings) console.log(`  ! ${w}`);
  for (const v of violations) console.log(`  ${X} ${v}`);
  if (pass) console.log(`  ${OK} passed (${warnings.length} warning(s))`);
  else console.log(`  ${X} failed: ${violations.length} violation(s)`);
  process.exit(pass ? 0 : 1);
}
