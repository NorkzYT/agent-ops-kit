#!/usr/bin/env node
// Run the pinned axe-core against owned fixture pages served on 127.0.0.1.
//   node scripts/run-axe.mjs [--page bad|clean] ...   (default: bad and clean)
//   node scripts/run-axe.mjs --url http://127.0.0.1:<port>/<page>.html  (a fixture
//     already served by scripts/serve.mjs; any other host is refused)
//   node scripts/run-axe.mjs --project /abs/site [--page-path index.html ...] [--out dir]
//     (your own static pages, served by this script on 127.0.0.1; lib/project.mjs)
// Writes axe-<page>.json into a new run directory: reports/<run-id>/ (or
// <--out>/<run-id>/, lib/output.mjs; nothing existing is overwritten or deleted). Exit: 0 no violations, 2 violations found,
// 1 scan error (no report is a clean report).
import fs from 'node:fs';
import { createRequire } from 'node:module';
import path from 'node:path';
import { parseArgs } from 'node:util';
import { connect, launchChrome, pageTarget } from './lib/chrome.mjs';
import { REPORTS, pageFile } from './lib/paths.mjs';
import { assertUniqueNames, runDirFor, writeNew } from './lib/output.mjs';
import { startServer } from './lib/server.mjs';
import { PROJECT_OPTIONS, projectArgs, projectTarget } from './lib/project.mjs';
import { assertLoopbackTarget } from './lib/target.mjs';

const require = createRequire(import.meta.url);
const AXE_PATH = require.resolve('axe-core/axe.min.js');
const AXE_VERSION = require('axe-core/package.json').version;

function summarize(results) {
  const nodes = (r) => r.nodes.map((n) => ({ target: n.target, html: n.html, summary: n.failureSummary || null }));
  return {
    violations: results.violations.map((r) => ({ id: r.id, impact: r.impact, help: r.help, helpUrl: r.helpUrl, nodes: nodes(r) })),
    // axe "incomplete": rules it could not decide. Kept as needs-manual-review, never dropped.
    needsManualReview: results.incomplete.map((r) => ({ id: r.id, impact: r.impact, help: r.help, nodes: r.nodes.map((n) => ({ target: n.target, html: n.html, reasons: [...n.any, ...n.all, ...n.none].map((c) => c.message) })) })),
    passCount: results.passes.length,
    inapplicableCount: results.inapplicable.length,
  };
}

// Only the navigation target is guarded (lib/target.mjs). Subresources the page
// requests are not blocked by this runner: non-127.0.0.1 hosts go to Chrome's
// dead proxy / NOTFOUND resolver, but 127.0.0.1 on ANY port is reachable. Both
// kinds are recorded so they are visible in the report, not silently audited.
export function classifyRequests(requests, target) {
  const origin = new URL(target).origin;
  const nonLoopback = [];
  const otherLoopback = [];
  for (const u of requests) {
    if (u.startsWith('data:') || u.startsWith('about:') || u.startsWith('blob:')) continue;
    let p;
    try { p = new URL(u); } catch { nonLoopback.push(u); continue; }
    if (p.hostname !== '127.0.0.1') nonLoopback.push(u);
    else if (p.origin !== origin) otherLoopback.push(u);
  }
  return { nonLoopback, otherLoopback };
}

export async function auditPage(name, rawUrl, { chrome, allowed }) {
  const url = assertLoopbackTarget(rawUrl, allowed);
  const tgt = await pageTarget(chrome.port);
  const cdp = await connect(tgt.webSocketDebuggerUrl);
  const requests = [];
  try {
    cdp.on((m) => { if (m.method === 'Network.requestWillBeSent') requests.push(m.params.request.url); });
    await cdp.send('Network.enable');
    await cdp.send('Page.enable');
    const loaded = cdp.waitFor('Page.loadEventFired');
    const nav = await cdp.send('Page.navigate', { url });
    if (nav.errorText) throw new Error(`navigation failed: ${nav.errorText}`);
    await loaded;
    const inj = await cdp.send('Runtime.evaluate', { expression: fs.readFileSync(AXE_PATH, 'utf8') });
    if (inj.exceptionDetails) throw new Error(`axe injection failed: ${inj.exceptionDetails.text}`);
    const res = await cdp.send('Runtime.evaluate', {
      expression: `axe.run(document, { resultTypes: ['violations', 'incomplete'] }).then((r) => JSON.stringify(r))`,
      awaitPromise: true,
      returnByValue: true,
    });
    if (res.exceptionDetails) throw new Error(`axe.run failed: ${res.exceptionDetails.exception?.description || res.exceptionDetails.text}`);
    const raw = JSON.parse(res.result.value);
    const { nonLoopback, otherLoopback } = classifyRequests(requests, url);
    return { tool: 'axe-core', axeVersion: raw.testEngine?.version || AXE_VERSION, page: name, url, timestamp: raw.timestamp, nonLoopbackRequests: nonLoopback, otherLoopbackRequests: otherLoopback, ...summarize(raw), raw };
  } finally {
    cdp.close();
  }
}

async function main() {
  const { values } = parseArgs({ options: { page: { type: 'string', multiple: true }, url: { type: 'string' }, out: { type: 'string' }, ...PROJECT_OPTIONS } });
  // Validate every target before a server or browser starts.
  const project = projectArgs(values);
  const fixedUrl = values.url ? assertLoopbackTarget(values.url) : null;
  const pages = fixedUrl ? ['url'] : values.page?.length ? values.page : ['bad', 'clean'];
  if (!fixedUrl && !project) { pages.forEach(pageFile); assertUniqueNames(pages); }
  const outDir = runDirFor(values, project, { fallback: REPORTS });
  const srv = await startServer(project ? project.root : undefined);
  let chrome;
  let violations = 0;
  try {
    const targets = project ? projectTarget(srv, project.pages) : null;
    const jobs = targets ? targets.pages.map((p) => [p.name, p.url]) : pages.map((name) => [name, fixedUrl || srv.url(pageFile(name))]);
    chrome = await launchChrome();
    for (const [name, url] of jobs) {
      const report = await auditPage(name, url, { chrome, allowed: targets?.allowed });
      if (project) report.project = project.root;
      const file = path.join(outDir, `axe-${name}.json`);
      writeNew(file, JSON.stringify(report, null, 2));
      violations += report.violations.length;
      console.log(`axe ${report.axeVersion} ${name}: ${report.violations.length} violation rule(s) [${report.violations.map((v) => v.id).join(', ')}], ` +
        `${report.needsManualReview.length} needs-manual-review [${report.needsManualReview.map((v) => v.id).join(', ')}], ${report.passCount} passed -> ${path.relative(process.cwd(), file)}`);
      if (report.nonLoopbackRequests.length) console.error(`WARNING ${name}: non-loopback requests attempted: ${report.nonLoopbackRequests.join(' ')}`);
      if (report.otherLoopbackRequests.length) console.error(`WARNING ${name}: requests to other 127.0.0.1 ports (not blocked): ${report.otherLoopbackRequests.join(' ')}`);
    }
  } finally {
    if (chrome) await chrome.close();
    await srv.close();
  }
  console.log('Note: a clean axe scan is not accessibility sign-off; check keyboard use and accessible names manually.');
  return violations ? 2 : 0;
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().then((c) => { process.exitCode = c; }, (err) => { console.error(`axe SCAN ERROR: ${err.message}`); process.exitCode = 1; });
}
