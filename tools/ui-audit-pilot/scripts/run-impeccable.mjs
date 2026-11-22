#!/usr/bin/env node
// Advisory Impeccable detector run on owned fixtures only.
//   node scripts/run-impeccable.mjs [--page bad|clean ...] [--file fixtures/<f>.html ...]
// 1. File scan (SARIF 2.1.0) of fixtures/ (or the given --file paths, which must
//    stay inside fixtures/) -> <run dir>/impeccable.sarif
//   node scripts/run-impeccable.mjs --project /abs/site [--page-path index.html ...] [--out dir]
//    file + rendered scans of your own selected pages (lib/project.mjs guards)
// 2. Rendered scan of each page served on 127.0.0.1, attached over --cdp to a
//    Chrome this script launched (background networking off) -> <run dir>/impeccable-rendered-<page>.json
// The run dir is new per run: reports/<run-id>/ or <--out>/<run-id>/ (lib/output.mjs);
// the summary is impeccable-summary.json, which no page name can collide with.
// The engine binary comes from the pinned @impeccable/cli-<os>-<arch> package and
// is checked against the checksum manifest shipped in the pinned `impeccable`
// package; there is no download fallback. Detector exit codes: 0 clean, 2 findings,
// 1 target could not be scanned. This wrapper: findings are advisory (exit 0 with a
// summary); any scan error exits 1 and is reported as SCAN ERROR, never as clean.
import { createHash } from 'node:crypto';
import fs from 'node:fs';
import { createRequire } from 'node:module';
import path from 'node:path';
import { parseArgs } from 'node:util';
import { connect, launchChrome, pageTarget } from './lib/chrome.mjs';
import { FIXTURES, REPORTS, ROOT, pageFile } from './lib/paths.mjs';
import { assertUniqueNames, runDirFor, writeNew } from './lib/output.mjs';
import { privateEnv } from './lib/env.mjs';
import { run } from './lib/proc.mjs';
import { startServer } from './lib/server.mjs';
import { PROJECT_OPTIONS, projectArgs, projectTarget } from './lib/project.mjs';
import { assertLoopbackTarget } from './lib/target.mjs';

const require = createRequire(import.meta.url);
const TARGET = `${{ linux: 'linux', darwin: 'darwin', win32: 'windows' }[process.platform]}-${process.arch}`;

export function engine() {
  const shim = path.dirname(require.resolve('impeccable/package.json'));
  const pkg = require('impeccable/package.json');
  const platPkg = `@impeccable/cli-${TARGET}`;
  const version = pkg.optionalDependencies?.[platPkg];
  if (!version) throw new Error(`impeccable ${pkg.version} has no engine package for ${TARGET}`);
  const bin = path.join(path.dirname(require.resolve(`${platPkg}/package.json`)), 'bin', 'impeccable');
  const manifest = fs.readFileSync(path.join(shim, 'skill', 'scripts', 'engine-checksums.txt'), 'utf8');
  const want = manifest.split(/\r?\n/).map((l) => l.trim().split(/\s+/)).find(([v, a]) => v === version && a === `impeccable-${TARGET}`)?.[2];
  const got = createHash('sha256').update(fs.readFileSync(bin)).digest('hex');
  if (!want || want.toLowerCase() !== got) throw new Error(`engine checksum mismatch for ${bin}: manifest ${want || 'missing'}, file ${got}`);
  return { bin, version, shimVersion: pkg.version, sha256: got };
}

// Minimal env (lib/env.mjs) plus Impeccable's own off switches: telemetry,
// update and staleness checks, live-copy Apply. No IMPECCABLE_API_URL,
// IMPECCABLE_UPDATE_HOST or credential store is inherited.
export function engineEnv() {
  return privateEnv('impeccable-home', (home) => ({
    IMPECCABLE_HOME: path.join(home, 'impeccable'),
    IMPECCABLE_NO_TELEMETRY: '1',
    IMPECCABLE_NO_UPDATE_CHECK: '1',
    IMPECCABLE_NO_STALENESS_CHECK: '1',
    IMPECCABLE_LIVE_COPY_AGENT: 'off',
  }));
}

// Map one detector invocation to the pilot's outcome.
export function classify(code) {
  if (code === 0) return 'clean';
  if (code === 2) return 'findings';
  if (code === 1) return 'scan-error';
  return 'unexpected-exit';
}

// Report files. Page names are [a-z0-9-] only, so the rendered prefix keeps a
// page called "summary" from replacing impeccable-summary.json.
export const SUMMARY_FILE = 'impeccable-summary.json';
export const SARIF_FILE = 'impeccable.sarif';
export const renderedFile = (name) => `impeccable-rendered-${name}.json`;

function insideFixtures(p) {
  const abs = path.resolve(ROOT, p);
  if (abs !== FIXTURES && !abs.startsWith(FIXTURES + path.sep)) throw new Error(`refused file target outside fixtures/: ${p}`);
  return path.relative(ROOT, abs) || '.';
}

export async function fileScan(targets, { env, bin, cwd = ROOT }) {
  const args = ['detect', '--sarif', '--no-config', ...targets];
  const r = await run(bin, args, { cwd, env, timeoutMs: 120000 });
  let sarif = null;
  try { sarif = JSON.parse(r.stdout); } catch { /* reported below */ }
  let outcome = classify(r.code);
  const executed = sarif?.runs?.[0]?.invocations?.[0]?.executionSuccessful;
  if (outcome !== 'scan-error' && (sarif === null || executed === false)) outcome = 'scan-error';
  const results = sarif?.runs?.flatMap((run_) => run_.results || []) || [];
  return { kind: 'file', command: [bin, ...args], code: r.code, outcome, stdout: r.stdout, stderr: r.stderr, sarif, findings: results.map((x) => ({ ruleId: x.ruleId, level: x.level, uri: x.locations?.[0]?.physicalLocation?.artifactLocation?.uri, message: x.message?.text })) };
}

export async function renderedScan(name, { chrome, srv, env, bin, url: rawUrl, target, allowed, cwd = ROOT }) {
  const url = assertLoopbackTarget(rawUrl || srv.url(pageFile(name)), allowed);
  const t = await pageTarget(chrome.port);
  const cdp = await connect(t.webSocketDebuggerUrl);
  try {
    await cdp.send('Page.enable');
    const loaded = cdp.waitFor('Page.loadEventFired');
    const nav = await cdp.send('Page.navigate', { url });
    if (nav.errorText) throw new Error(`navigation failed: ${nav.errorText}`);
    await loaded;
  } finally { cdp.close(); }
  const args = ['detect', '--json', '--no-config', '--cdp', String(chrome.port), '--target', target || pageFile(name)];
  const r = await run(bin, args, { cwd, env, timeoutMs: 120000 });
  let findings = null;
  try { findings = JSON.parse(r.stdout); } catch { /* reported below */ }
  let outcome = classify(r.code);
  if (outcome !== 'scan-error' && !Array.isArray(findings)) outcome = 'scan-error';
  return { kind: 'rendered', page: name, url, command: [bin, ...args], code: r.code, outcome, stdout: r.stdout, stderr: r.stderr, findings: (findings || []).map((f) => ({ ruleId: f.antipattern, severity: f.severity, selector: f.selector, snippet: f.snippet })) };
}

async function main() {
  const { values } = parseArgs({ options: { page: { type: 'string', multiple: true }, file: { type: 'string', multiple: true }, 'no-render': { type: 'boolean' }, out: { type: 'string' }, ...PROJECT_OPTIONS } });
  const project = projectArgs(values);
  const pages = values.page?.length ? values.page : ['bad', 'clean'];
  if (!project) { pages.forEach(pageFile); assertUniqueNames(pages); }
  const files = project ? project.pages : (values.file?.length ? values.file : ['fixtures']).map(insideFixtures);
  const cwd = project ? project.root : ROOT;
  const outDir = runDirFor(values, project, { fallback: REPORTS });
  const eng = engine();
  const { env, home } = engineEnv();
  const scans = [];
  try {
    const fsScan = await fileScan(files, { env, bin: eng.bin, cwd });
    if (fsScan.sarif) writeNew(path.join(outDir, SARIF_FILE), JSON.stringify(fsScan.sarif, null, 2));
    scans.push(fsScan);
    if (!values['no-render']) {
      const srv = await startServer(project ? project.root : undefined);
      let chrome;
      try {
        const targets = project ? projectTarget(srv, project.pages) : null;
        const jobs = targets ? targets.pages.map((p) => ({ name: p.name, url: p.url, target: p.pathname })) : pages.map((name) => ({ name }));
        chrome = await launchChrome();
        for (const j of jobs) {
          const s = await renderedScan(j.name, { chrome, srv, env, bin: eng.bin, url: j.url, target: j.target, allowed: targets?.allowed, cwd });
          writeNew(path.join(outDir, renderedFile(j.name)), JSON.stringify(s, null, 2));
          scans.push(s);
        }
      } finally {
        if (chrome) await chrome.close();
        await srv.close();
      }
    }
  } finally {
    fs.rmSync(home, { recursive: true, force: true });
  }
  const summary = { tool: 'impeccable', project: project?.root, shimVersion: eng.shimVersion, engineVersion: eng.version, engineSha256: eng.sha256, scans: scans.map(({ stdout, sarif, ...s }) => s) };
  writeNew(path.join(outDir, SUMMARY_FILE), JSON.stringify(summary, null, 2));
  console.log(`impeccable ${eng.shimVersion} (engine ${eng.version}, sha256 ${eng.sha256.slice(0, 12)}...) ADVISORY -> ${path.relative(process.cwd(), outDir) || '.'}:`);
  let errors = 0;
  for (const s of scans) {
    const label = s.kind === 'file' ? `file scan [${files.join(' ')}]` : `rendered ${s.page}`;
    if (s.outcome === 'scan-error' || s.outcome === 'unexpected-exit') {
      errors += 1;
      console.error(`  SCAN ERROR ${label}: detector exit ${s.code} (${s.outcome}); this is not a clean result. ${s.stderr.trim().split('\n').slice(-3).join(' | ')}`);
    } else {
      const ids = [...new Set(s.findings.map((f) => f.ruleId))];
      console.log(`  ${label}: ${s.outcome}, ${s.findings.length} finding(s)${ids.length ? ` [${ids.join(', ')}]` : ''}`);
    }
  }
  if (errors) return 1;
  console.log('  Findings are advisory and do not fail this run.');
  return 0;
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().then((c) => { process.exitCode = c; }, (err) => { console.error(`impeccable SCAN ERROR: ${err.message}`); process.exitCode = 1; });
}
