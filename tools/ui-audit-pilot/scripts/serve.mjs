#!/usr/bin/env node
// Serve fixtures/ on 127.0.0.1 (ephemeral port) until Ctrl-C, for manual
// keyboard/screen-reader checks or for --url runs of the audit scripts.
import { PAGES } from './lib/paths.mjs';
import { startServer } from './lib/server.mjs';

const srv = await startServer();
for (const [name, file] of Object.entries(PAGES)) console.log(`${name.padEnd(10)} ${srv.url(file)}`);
process.on('SIGINT', () => srv.close().then(() => process.exit(0)));
process.on('SIGTERM', () => srv.close().then(() => process.exit(0)));
