#!/usr/bin/env node
// Run the pinned axe-core against owned fixture pages served on 127.0.0.1.
//   node scripts/run-axe.mjs [--page bad|clean] ...   (default: bad and clean)
//   node scripts/run-axe.mjs --url http://127.0.0.1:<port>/<page>.html  (a fixture
//     already served by scripts/serve.mjs; any other host is refused)
// Writes reports/axe-<page>.json. Exit: 0 no violations, 2 violations found,
// 1 scan error (no report is a clean report).
import fs from 'node:fs';
import { createRequire } from 'node:module';
import path from 'node:path';
import { parseArgs } from 'node:util';
import { connect, launchChrome, pageTarget } from './lib/chrome.mjs';
import { REPORTS, ensureDir, pageFile } from './lib/paths.mjs';
import { startServer } from './lib/server.mjs';
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

export async function auditPage(name, rawUrl, { chrome }) {
  const url = assertLoopbackTarget(rawUrl);
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
    const nonLoopback = requests.filter((u) => { try { const h = new URL(u).hostname; return !['127.0.0.1'].includes(h) && !u.startsWith('data:') && !u.startsWith('about:'); } catch { return true; } });
    return { tool: 'axe-core', axeVersion: raw.testEngine?.version || AXE_VERSION, page: name, url, timestamp: raw.timestamp, nonLoopbackRequests: nonLoopback, ...summarize(raw), raw };
  } finally {
    cdp.close();
  }
}

async function main() {
  const { values } = parseArgs({ options: { page: { type: 'string', multiple: true }, url: { type: 'string' }, out: { type: 'string' } } });
  // Validate every target before a server or browser starts.
  const fixedUrl = values.url ? assertLoopbackTarget(values.url) : null;
  const pages = fixedUrl ? ['url'] : values.page?.length ? values.page : ['bad', 'clean'];
  if (!fixedUrl) pages.forEach(pageFile);
  const outDir = ensureDir(values.out ? path.resolve(values.out) : REPORTS);
  const srv = await startServer();
  let chrome;
  let violations = 0;
  try {
    chrome = await launchChrome();
    for (const name of pages) {
      const report = await auditPage(name, fixedUrl || srv.url(pageFile(name)), { chrome });
      const file = path.join(outDir, `axe-${name}.json`);
      fs.writeFileSync(file, JSON.stringify(report, null, 2));
      violations += report.violations.length;
      console.log(`axe ${report.axeVersion} ${name}: ${report.violations.length} violation rule(s) [${report.violations.map((v) => v.id).join(', ')}], ` +
        `${report.needsManualReview.length} needs-manual-review [${report.needsManualReview.map((v) => v.id).join(', ')}], ${report.passCount} passed -> ${path.relative(process.cwd(), file)}`);
      if (report.nonLoopbackRequests.length) console.error(`WARNING ${name}: non-loopback requests attempted: ${report.nonLoopbackRequests.join(' ')}`);
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
