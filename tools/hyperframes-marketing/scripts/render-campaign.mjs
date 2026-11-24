#!/usr/bin/env node
// Default marketing-video render: claims gate → provenance gate → generated
// tone → HyperFrames project build → project asset gate → pinned local
// `hyperframes render` (Chrome via a dead-proxy launcher, telemetry/updates
// off, private HOME; the Node CLI itself is not network-sandboxed)
// → poster + motion sample → ffprobe QA. Any gate failure stops before render.
// There is no fallback to Remotion; the legacy lane is a separate, explicit
// command (tools/remotion-marketing).
import {spawnSync, execFileSync} from 'node:child_process';
import {chmodSync, closeSync, constants as fsc, copyFileSync, existsSync, mkdirSync, mkdtempSync, openSync, readFileSync, renameSync, rmSync, writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {createHash} from 'node:crypto';
import {dirname, join, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';
import {parseArgs, loadJson, usedAssetIds} from '../../remotion-marketing/scripts/lib.mjs';
import {checkClaims} from '../../remotion-marketing/scripts/check-claims.mjs';
import {checkProvenance} from '../../remotion-marketing/scripts/check-provenance.mjs';
import {evaluateQa, probe, measureLoudness} from '../../remotion-marketing/scripts/qa-video.mjs';
import {generateTone} from '../../remotion-marketing/scripts/generate-tone.mjs';
import {buildProject, DIMENSIONS, ensureAssetParent, resolveAssetPath} from './build-composition.mjs';
import {checkHtmlAssets, loadProjectFiles} from './check-html-assets.mjs';

const TOOL = resolve(dirname(fileURLToPath(import.meta.url)), '..');
export const PINNED_VERSION = '0.9.80';

function die(msg, code = 1) {
  console.error(`\n✗ ${msg}`);
  process.exit(code);
}

export function sha256(path) {
  return createHash('sha256').update(readFileSync(path)).digest('hex');
}

function report(title, {violations, warnings}) {
  console.log(`\n== ${title} ==`);
  for (const w of warnings) console.log(`  ! ${w}`);
  for (const v of violations) console.log(`  ✗ ${v}`);
  if (violations.length === 0) console.log('  ✓ passed');
  return violations.length === 0;
}

/** The pinned CLI from this project's node_modules, version-checked. */
export function pinnedCli() {
  const pkgPath = join(TOOL, 'node_modules', 'hyperframes', 'package.json');
  if (!existsSync(pkgPath)) die(`HyperFrames is not installed: run \`npm ci\` in ${TOOL}`, 3);
  const version = JSON.parse(readFileSync(pkgPath, 'utf8')).version;
  if (version !== PINNED_VERSION) die(`HyperFrames ${version} installed, ${PINNED_VERSION} pinned: run \`npm ci\``, 3);
  return {bin: join(TOOL, 'node_modules', 'hyperframes', 'bin', 'hyperframes.mjs'), version};
}

// Chrome puts its singleton socket under TMPDIR; a long path overflows the
// 108-byte Unix socket limit and Chrome dies with SIGTRAP at launch.
export const MAX_TMPDIR_LEN = 64;

/** A short private temp dir for one render (caller removes it). */
export function renderTmp(root = tmpdir()) {
  const dir = mkdtempSync(join(root, 'hfr-'));
  if (dir.length > MAX_TMPDIR_LEN) {
    rmSync(dir, {recursive: true, force: true});
    throw new Error(`temp dir ${dir} is ${dir.length} chars; Chrome needs <= ${MAX_TMPDIR_LEN}. Set TMPDIR to a shorter private directory.`);
  }
  chmodSync(dir, 0o700);
  return dir;
}

/** Minimal, explicit environment: no inherited credentials or tokens. */
export function renderEnv({home, chrome, tmp}) {
  return {
    PATH: `${dirname(process.execPath)}:/usr/local/bin:/usr/bin:/bin`,
    HOME: home,
    XDG_CACHE_HOME: join(home, '.cache'),
    XDG_CONFIG_HOME: join(home, '.config'),
    TMPDIR: tmp,
    LANG: 'C.UTF-8',
    NO_COLOR: '1',
    HYPERFRAMES_NO_TELEMETRY: '1',
    DO_NOT_TRACK: '1',
    HYPERFRAMES_NO_UPDATE_CHECK: '1',
    HYPERFRAMES_NO_AUTO_INSTALL: '1',
    HYPERFRAMES_SKIP_SKILLS: '1',
    HYPERFRAMES_CACHE_DIR: join(home, 'hyperframes-cache'),
    HYPERFRAMES_BROWSER_PATH: join(TOOL, 'scripts', 'chrome-offline.sh'),
    MARKETING_CHROME: chrome,
  };
}

/** Poster time: 60% into the scene that holds the video midpoint, so entry
 * animations have settled (a scene boundary would show an empty frame). */
export function posterTime(props) {
  const total = props.scenes.reduce((s, x) => s + x.durationInFrames, 0);
  let start = 0;
  for (const sc of props.scenes) {
    if (start + sc.durationInFrames > total / 2) return (start + sc.durationInFrames * 0.6) / props.fps;
    start += sc.durationInFrames;
  }
  return total / 2 / props.fps;
}

/**
 * Validate every asset path the props use before anything is written: URLs,
 * absolute paths, `..` escapes and symlinks under the assets root are refused.
 * Returns the first problem as a message, or null.
 */
export function assetPathProblem(props, assetsRoot) {
  for (const id of usedAssetIds(props)) {
    const rel = props.assetPaths?.[id];
    if (rel === undefined) continue; // reported by the step that needs it
    try { resolveAssetPath(assetsRoot, rel); } catch (e) { return `asset "${id}": ${e.message}`; }
  }
  return null;
}

function frameHash(video, t) {
  return execFileSync('ffmpeg', ['-v', 'error', '-ss', String(t), '-i', video, '-frames:v', '1', '-f', 'md5', '-'], {encoding: 'utf8'}).trim();
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  const propsPath = resolve(args.props ?? join(TOOL, 'props/sample.json'));
  const provPath = resolve(args.provenance ?? join(TOOL, 'props/sample.provenance.json'));
  const assetsRoot = resolve(args.assets ?? join(dirname(propsPath), 'assets'));
  const outDir = resolve(args.out ?? join(TOOL, 'out'));
  const chrome = args.chrome ?? process.env.MARKETING_CHROME ?? '/usr/bin/google-chrome';
  const ratios = String(args.ratios ?? '16:9').split(',').map((s) => s.trim());
  const quality = args.quality ?? 'looks';
  const gatesOnly = args['gates-only'] === true || args['gates-only'] === 'true';
  if (!['draft', 'looks', 'delivery'].includes(quality)) die(`unknown --quality "${quality}"`, 2);
  for (const r of ratios) if (!DIMENSIONS[r]) die(`unknown aspect ratio "${r}"`, 2);

  const props = loadJson(propsPath);
  const provenance = loadJson(provPath);
  // 0. Asset paths: refused before any file is written (also with --gates-only).
  const pathProblem = assetPathProblem(props, assetsRoot);
  if (pathProblem) die(pathProblem, 2);
  mkdirSync(outDir, {recursive: true});

  // 1. Gates on the brief: abort before anything is built or rendered.
  if (!report('Claims / evidence gate', checkClaims(props))) die('claims gate failed: soften or cite the flagged copy');
  if (!report('Provenance / rights gate', checkProvenance(props, provenance))) die('provenance gate failed: assets lack commercial-use rights');

  // 2. Rights-clean generated music, sized to the video.
  const totalFrames = props.scenes.reduce((s, x) => s + x.durationInFrames, 0);
  const durationSec = totalFrames / props.fps;
  if (props.music?.mode !== 'none' && props.music?.assetId) {
    const rel = props.assetPaths?.[props.music.assetId];
    if (!rel) die(`music asset "${props.music.assetId}" missing from assetPaths`);
    let abs = resolveAssetPath(assetsRoot, rel);
    if (!existsSync(abs)) {
      if (props.music.mode !== 'generated') die(`music file missing and mode is "${props.music.mode}": ${abs}`);
      console.log(`\n== Generating rights-clean tone (${durationSec}s) ==`);
      // ffmpeg writes into a private temp dir; rename() then places the file
      // without following anything planted at the destination.
      try { abs = ensureAssetParent(assetsRoot, rel); } catch (e) { die(e.message, 2); }
      const work = mkdtempSync(join(dirname(abs), '.tone-'));
      try {
        const staged = join(work, 'tone' + (rel.match(/\.[A-Za-z0-9]+$/)?.[0] ?? '.wav'));
        generateTone({out: staged, duration: durationSec});
        try { abs = resolveAssetPath(assetsRoot, rel); } catch (e) { die(e.message, 2); }
        if (existsSync(abs)) die(`music destination appeared during generation: ${abs}`, 2);
        renameSync(staged, abs);
      } finally {
        rmSync(work, {recursive: true, force: true});
      }
    }
  }

  // 3. Font files: copy from the provenance `localSource` only when its sha256
  // matches the record, and re-verify any file already in place. A relative
  // `localSource` is resolved against the provenance file's directory (the
  // sample's vendored fonts), never the caller's working directory.
  for (const face of props.typography?.faces ?? []) {
    const rel = props.assetPaths?.[face.assetId];
    const rec = (provenance.assets ?? []).find((a) => a.id === face.assetId);
    if (!rel || !rec) die(`font asset "${face.assetId}" missing from assetPaths or provenance`);
    let abs = resolveAssetPath(assetsRoot, rel);
    if (!existsSync(abs)) {
      if (!rec.localSource || !rec.sha256) die(`font file missing and provenance has no localSource+sha256: ${abs}`);
      const src = resolve(dirname(provPath), rec.localSource);
      if (!existsSync(src)) die(`font source not found: ${src} (supply the file named in acquiredFrom)`);
      if (sha256(src) !== rec.sha256) die(`font source ${src} does not match the provenance sha256`);
      try { abs = ensureAssetParent(assetsRoot, rel); } catch (e) { die(e.message, 2); }
      // COPYFILE_EXCL: O_EXCL refuses an existing file or a planted symlink.
      copyFileSync(src, abs, fsc.COPYFILE_EXCL);
      console.log(`\n== Copied font ${face.assetId} (sha256 verified) ==`);
    }
    if (rec.sha256 && sha256(abs) !== rec.sha256) die(`font file ${abs} does not match the provenance sha256`);
  }

  const cli = gatesOnly ? null : pinnedCli();
  const home = join(outDir, '.hyperframes-home');
  mkdirSync(home, {recursive: true});
  chmodSync(home, 0o700);
  const manifest = {engine: 'hyperframes', version: cli?.version ?? null, props: propsPath, provenance: provPath, renders: []};
  const results = [];

  for (const ratio of ratios) {
    const slug = ratio.replace(':', 'x');
    const project = join(outDir, `project.${slug}`);
    const ratioProps = {...props, aspectRatio: ratio};

    // 4. Build the editable project, then gate exactly what will be rendered.
    console.log(`\n== Building HyperFrames project ${ratio} ==`);
    buildProject(ratioProps, {outDir: project, assetsRoot, aspectRatio: ratio});
    console.log(`  ${project}`);
    const assetGate = checkHtmlAssets({files: loadProjectFiles(project), provenance, assetPaths: props.assetPaths});
    if (!report(`Project asset gate ${ratio}`, assetGate)) die('project asset gate failed: unlicensed or external reference in the HyperFrames project');
    if (gatesOnly) continue;

    // 5. Pinned local render. Lint errors fail the render (--strict).
    const finalOut = join(outDir, `final.${slug}.mp4`);
    const logPath = join(outDir, `render.${slug}.log`);
    const cmd = [process.execPath, cli.bin, 'render', project, '-o', finalOut, '--quality', quality,
      '--fps', String(props.fps), '--strict', '--refuse-outside-files', '--no-wait'];
    console.log(`\n== Rendering ${ratio} with HyperFrames ${cli.version} (log: ${logPath}) ==`);
    const fd = openSync(logPath, 'w');
    const started = Date.now();
    let tmp;
    try { tmp = renderTmp(); } catch (e) { die(e.message); }
    const res = spawnSync(cmd[0], cmd.slice(1), {cwd: outDir, env: renderEnv({home, chrome, tmp}), stdio: ['ignore', fd, fd], timeout: Number(args.timeout ?? 900) * 1000});
    closeSync(fd);
    rmSync(tmp, {recursive: true, force: true});
    const entry = {ratio, command: cmd, exit: res.status, signal: res.signal, error: res.error?.message ?? null, seconds: (Date.now() - started) / 1000, log: logPath, output: finalOut};
    manifest.renders.push(entry);
    writeFileSync(join(outDir, 'render-manifest.json'), JSON.stringify(manifest, null, 2));
    if (res.status !== 0 || !existsSync(finalOut)) {
      const tail = existsSync(logPath) ? readFileSync(logPath, 'utf8').split('\n').slice(-25).join('\n') : '';
      console.error(tail);
      die(`hyperframes render failed (exit ${res.status}${res.signal ? `, signal ${res.signal}` : ''}); no fallback engine is used`);
    }

    // 6. Poster (mid frame) and a motion sample: a render whose frames never
    // change means the animation did not run.
    const poster = join(outDir, ratios.length === 1 ? 'poster.png' : `poster.${slug}.png`);
    execFileSync('ffmpeg', ['-v', 'error', '-y', '-ss', String(posterTime(props)), '-i', finalOut, '-frames:v', '1', poster]);
    const firstScene = props.scenes[0].durationInFrames / props.fps;
    const hashes = [0.02, firstScene * 0.5, durationSec * 0.75].map((t) => frameHash(finalOut, t));
    const moving = new Set(hashes).size > 1;

    const probeData = probe(finalOut);
    const loudness = ratioProps.music?.mode !== 'none' ? measureLoudness(finalOut) : null;
    const qa = evaluateQa({props: ratioProps, probeData, loudness, posterExists: existsSync(poster)});
    if (!moving) qa.violations.push('sampled frames are identical: the composition did not animate');
    qa.summary.motionSampleHashes = hashes;
    const qaReport = {ok: qa.violations.length === 0, engine: 'hyperframes', engineVersion: cli.version, ...qa};
    const reportPath = join(outDir, `qa.${slug}.json`);
    writeFileSync(reportPath, JSON.stringify(qaReport, null, 2));
    report(`QA ${ratio}`, qa);
    console.log(`  summary: ${JSON.stringify(qa.summary)}`);
    results.push({ratio, finalOut, reportPath, ok: qaReport.ok});
  }

  writeFileSync(join(outDir, 'provenance.json'), JSON.stringify(provenance, null, 2));
  console.log('\n== Done ==');
  if (gatesOnly) {
    console.log('gates passed; no render requested (--gates-only)');
    return;
  }
  for (const r of results) console.log(`${r.ok ? '✓' : '✗'} ${r.ratio}: ${r.finalOut}\n  qa: ${r.reportPath}`);
  console.log(`used assets: ${[...usedAssetIds(props)].join(', ') || '(none)'}`);
  if (results.some((r) => !r.ok)) process.exit(1);
}

if (import.meta.url === `file://${process.argv[1]}`) main();
