#!/usr/bin/env node
// Lighthouse CI against owned fixture pages on 127.0.0.1:
//   lhci collect -> lhci assert -> lhci upload --target=filesystem
//   node scripts/run-lhci.mjs [--page clean|regression|bad] ...  (default: clean and regression)
//   node scripts/run-lhci.mjs --url http://127.0.0.1:<port>/<page>.html  (fixture served
//     by scripts/serve.mjs; any other host is refused)
//   node scripts/run-lhci.mjs --project /abs/site [--page-path index.html ...] [--out dir]
//     (your own static pages, served by this script on 127.0.0.1; lib/project.mjs)
// Each run gets a new run directory, reports/<run-id>/ or <--out>/<run-id>/
// (lib/output.mjs). Each page runs in <run dir>/lhci/<page>/ (its own, newly
// created .lighthouseci/); reports land in <run dir>/lhci/<page>/upload/. An
// existing page directory is refused, never deleted or reused. Uploads are
// filesystem-only (the config is checked); this is not network isolation, see
// README "Boundaries". Exit: 0 assertions pass, 2 an assertion failed, 1 tool error.
import fs from 'node:fs';
import { createRequire } from 'node:module';
import path from 'node:path';
import { parseArgs } from 'node:util';
import { privateEnv } from './lib/env.mjs';
import { CHROME, REPORTS, ROOT, pageFile } from './lib/paths.mjs';
import { OutputRefused, assertUniqueNames, mkdirNew, runDirFor, writeNew } from './lib/output.mjs';
import { run } from './lib/proc.mjs';
import { startServer } from './lib/server.mjs';
import { PROJECT_OPTIONS, projectArgs, projectTarget } from './lib/project.mjs';
import { assertLoopbackTarget } from './lib/target.mjs';

const require = createRequire(import.meta.url);
const LHCI = path.join(path.dirname(require.resolve('@lhci/cli/package.json')), 'src', 'cli.js');
export const RC = path.join(ROOT, 'lighthouserc.json');

// Refuse anything but a local filesystem upload, before lhci runs.
export function checkUploadConfig(rc) {
  const up = rc?.ci?.upload;
  if (!up || up.target !== 'filesystem') throw new Error(`lighthouserc upload.target must be "filesystem" (got ${JSON.stringify(up?.target)})`);
  for (const k of ['serverBaseUrl', 'token', 'basicAuth', 'githubToken', 'githubAppToken']) {
    if (k in up) throw new Error(`lighthouserc upload.${k} is not allowed (no remote uploads)`);
  }
  if (rc.ci.server || rc.ci.wizard) throw new Error('lighthouserc must not configure an LHCI server');
  if (!up.outputDir || path.isAbsolute(up.outputDir) || up.outputDir.split(/[\\/]/).includes('..')) {
    throw new Error('lighthouserc upload.outputDir must be a relative path inside the run directory');
  }
  return up;
}

// Minimal env (lib/env.mjs): LHCI reads LHCI_* variables as config overrides
// (e.g. LHCI_UPLOAD__TARGET, LHCI_TOKEN) and honours HTTP(S)_PROXY; none are
// inherited, so lighthouserc.json plus the explicit flags are the only config.
export function lhciEnv() {
  return privateEnv('lhci-home', { CI: '1' });
}

const PAGE_NAME = /^[a-z0-9][a-z0-9-]*$/;

// outRoot is a directory inside this run's new run dir; <outRoot>/<name> must
// not exist yet. Nothing is removed: a leftover directory is a refusal.
export async function lhciPage(name, rawUrl, { outRoot, allowed } = {}) {
  if (!outRoot) throw new OutputRefused('refused output: lhciPage needs an outRoot inside a new run directory');
  if (!PAGE_NAME.test(name)) throw new OutputRefused(`refused output: page name ${JSON.stringify(name)} must match ${PAGE_NAME}`);
  const rc = JSON.parse(fs.readFileSync(RC, 'utf8'));
  checkUploadConfig(rc);
  const url = assertLoopbackTarget(rawUrl, allowed);
  fs.mkdirSync(outRoot, { recursive: true });
  const cwd = mkdirNew(path.join(outRoot, name));
  const { env, home } = lhciEnv();
  const steps = [
    ['collect', [LHCI, 'collect', `--config=${RC}`, `--url=${url}`, `--chromePath=${CHROME}`]],
    ['assert', [LHCI, 'assert', `--config=${RC}`]],
    ['upload', [LHCI, 'upload', `--config=${RC}`, '--target=filesystem', `--outputDir=${path.join(cwd, 'upload')}`]],
  ];
  const result = { page: name, url, dir: cwd, steps: {} };
  try {
    for (const [step, args] of steps) {
      const r = await run(process.execPath, args, { cwd, env, timeoutMs: 180000 });
      result.steps[step] = { command: [process.execPath, ...args], code: r.code, stdout: r.stdout, stderr: r.stderr };
      if (step === 'collect' && r.code !== 0) { result.status = 'error'; return result; }
    }
  } finally {
    fs.rmSync(home, { recursive: true, force: true });
  }
  const resultsFile = path.join(cwd, '.lighthouseci', 'assertion-results.json');
  const assertions = fs.existsSync(resultsFile) ? JSON.parse(fs.readFileSync(resultsFile, 'utf8')) : [];
  result.failedAssertions = assertions.filter((a) => !a.passed).map((a) => ({ level: a.level, auditId: a.auditId, name: a.name, expected: a.expected, actual: a.actual }));
  const errors = result.failedAssertions.filter((a) => a.level === 'error');
  if (result.steps.upload.code !== 0) result.status = 'error';
  else if (result.steps.assert.code !== 0 && errors.length) result.status = 'assertion-failed';
  else if (result.steps.assert.code !== 0) result.status = 'error';
  else result.status = 'pass';
  return result;
}

async function main() {
  const { values } = parseArgs({ options: { page: { type: 'string', multiple: true }, url: { type: 'string' }, out: { type: 'string' }, ...PROJECT_OPTIONS } });
  const project = projectArgs(values);
  const fixedUrl = values.url ? assertLoopbackTarget(values.url) : null;
  const pages = fixedUrl ? ['url'] : values.page?.length ? values.page : ['clean', 'regression'];
  if (!fixedUrl && !project) { pages.forEach(pageFile); assertUniqueNames(pages); }
  checkUploadConfig(JSON.parse(fs.readFileSync(RC, 'utf8')));
  const outRoot = path.join(runDirFor(values, project, { fallback: REPORTS }), 'lhci');
  const srv = await startServer(project ? project.root : undefined);
  const summary = [];
  try {
    const targets = project ? projectTarget(srv, project.pages) : null;
    const jobs = targets ? targets.pages.map((p) => [p.name, p.url]) : pages.map((name) => [name, fixedUrl || srv.url(pageFile(name))]);
    for (const [name, url] of jobs) {
      const r = await lhciPage(name, url, { outRoot, allowed: targets?.allowed });
      if (project) r.project = project.root;
      summary.push(r);
      writeNew(path.join(r.dir, 'pilot-summary.json'), JSON.stringify(r, null, 2));
      const failed = (r.failedAssertions || []).map((a) => `${a.level}:${a.auditId}`).join(', ');
      console.log(`lhci ${name}: ${r.status} (collect=${r.steps.collect.code} assert=${r.steps.assert?.code} upload=${r.steps.upload?.code})${failed ? ` failed [${failed}]` : ''} -> ${path.relative(process.cwd(), r.dir)}/upload`);
      if (r.status === 'error') for (const s of Object.values(r.steps)) if (s.code !== 0) console.error(s.stderr.slice(-3000));
    }
  } finally {
    await srv.close();
  }
  if (summary.some((r) => r.status === 'error')) return 1;
  return summary.some((r) => r.status === 'assertion-failed') ? 2 : 0;
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().then((c) => { process.exitCode = c; }, (err) => { console.error(`lhci SCAN ERROR: ${err.message}`); process.exitCode = 1; });
}
