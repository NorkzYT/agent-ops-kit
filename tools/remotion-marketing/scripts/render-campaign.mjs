#!/usr/bin/env node
// End-to-end render pipeline for one campaign. Order matches the marketing
// workflow: claims gate → provenance gate → ensure assets → still → draft →
// final(s) → QA. Any gate failure aborts before a frame is rendered.
import {execFileSync} from 'node:child_process';
import {existsSync, mkdirSync, writeFileSync} from 'node:fs';
import {dirname, join, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';
import {parseArgs, loadJson, usedAssetIds} from './lib.mjs';
import {checkProvenance} from './check-provenance.mjs';
import {checkClaims} from './check-claims.mjs';
import {evaluateQa, probe, measureLoudness} from './qa-video.mjs';
import {generateTone} from './generate-tone.mjs';

const PROJECT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const PUBLIC = join(PROJECT, 'public');
const COMP_ID = 'MarketingVideo';

const DIMS = {
  '16:9': {width: 1920, height: 1080},
  '9:16': {width: 1080, height: 1920},
  '1:1': {width: 1080, height: 1080},
};

function die(msg) {
  console.error(`\n✗ ${msg}`);
  process.exit(1);
}

function report(title, {violations, warnings}) {
  console.log(`\n== ${title} ==`);
  for (const w of warnings) console.log(`  ! ${w}`);
  for (const v of violations) console.log(`  ✗ ${v}`);
  if (violations.length === 0) console.log('  ✓ passed');
  return violations.length === 0;
}

function remotion(args) {
  const bin = join(PROJECT, 'node_modules', '.bin', 'remotion');
  execFileSync(bin, args, {cwd: PROJECT, stdio: 'inherit'});
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  const propsPath = resolve(args.props ?? join(PROJECT, 'props/sample.json'));
  const provPath = resolve(
    args.provenance ?? join(PROJECT, 'props/sample.provenance.json'),
  );
  const outDir = resolve(args.out ?? join(PROJECT, 'out'));
  const chrome = args.chrome ?? process.env.REMOTION_BROWSER_EXECUTABLE ?? '/usr/bin/google-chrome';
  const ratios = (args.ratios ?? '16:9').split(',').map((s) => s.trim());
  const draft = args.draft === true || args.draft === 'true';

  const props = loadJson(propsPath);
  const provenance = loadJson(provPath);
  mkdirSync(outDir, {recursive: true});

  // 1. Gates — abort before rendering anything.
  if (!report('Claims / evidence gate', checkClaims(props))) {
    die('claims gate failed: soften or cite the flagged copy');
  }
  if (!report('Provenance / rights gate', checkProvenance(props, provenance))) {
    die('provenance gate failed: assets lack commercial-use rights');
  }

  // 2. Ensure generated music exists (rights-clean tone) sized to the video.
  const totalFrames = props.scenes.reduce((s, x) => s + x.durationInFrames, 0);
  const durationSec = totalFrames / props.fps;
  if (props.music?.mode !== 'none' && props.music?.assetId) {
    const rel = props.assetPaths?.[props.music.assetId];
    if (!rel) die(`music asset "${props.music.assetId}" missing from assetPaths`);
    const abs = join(PUBLIC, rel);
    if (!existsSync(abs)) {
      if (props.music.mode === 'generated') {
        console.log(`\n== Generating rights-clean tone (${durationSec}s) ==`);
        generateTone({out: abs, duration: durationSec});
        console.log(`  ✓ ${abs}`);
      } else {
        die(`music file missing and mode is "${props.music.mode}" (not generated): ${abs}`);
      }
    }
  }

  const chromeArgs = existsSync(chrome) ? [`--browser-executable=${chrome}`] : [];
  const commonProps = [`--props=${propsPath}`, '--log=error', ...chromeArgs];

  // 3. Poster still (mid-video frame).
  const poster = join(outDir, 'poster.png');
  const posterFrame = Math.floor(totalFrames / 2);
  console.log('\n== Rendering poster still ==');
  remotion(['still', COMP_ID, poster, `--frame=${posterFrame}`, ...commonProps]);

  // 4. Low-res draft (fast preview).
  if (draft) {
    const draftOut = join(outDir, 'draft.mp4');
    console.log('\n== Rendering low-res draft ==');
    remotion(['render', COMP_ID, draftOut, '--scale=0.5', '--jpeg-quality=70', ...commonProps]);
  }

  // 5. Final render per requested aspect ratio, then QA each.
  const results = [];
  for (const ratio of ratios) {
    if (!DIMS[ratio]) die(`unknown aspect ratio "${ratio}"`);
    const ratioProps = {...props, aspectRatio: ratio};
    const ratioPropsPath = join(outDir, `props.${ratio.replace(':', 'x')}.json`);
    writeFileSync(ratioPropsPath, JSON.stringify(ratioProps, null, 2));
    const slug = ratio.replace(':', 'x');
    const finalOut = join(outDir, `final.${slug}.mp4`);
    console.log(`\n== Rendering final ${ratio} ==`);
    remotion(['render', COMP_ID, finalOut, `--props=${ratioPropsPath}`, '--log=error', ...chromeArgs]);

    console.log(`\n== QA ${ratio} ==`);
    const probeData = probe(finalOut);
    const loudness = ratioProps.music?.mode !== 'none' ? measureLoudness(finalOut) : null;
    const qa = evaluateQa({props: ratioProps, probeData, loudness, posterExists: existsSync(poster)});
    const qaReport = {ok: qa.violations.length === 0, ...qa};
    const reportPath = join(outDir, `qa.${slug}.json`);
    writeFileSync(reportPath, JSON.stringify(qaReport, null, 2));
    report(`QA ${ratio}`, qa);
    console.log(`  summary: ${JSON.stringify(qa.summary)}`);
    results.push({ratio, finalOut, reportPath, ok: qaReport.ok});
  }

  // 6. Copy provenance next to the renders for attachment.
  const provOut = join(outDir, 'provenance.json');
  writeFileSync(provOut, JSON.stringify(provenance, null, 2));

  console.log('\n== Done ==');
  console.log(`poster:     ${poster}`);
  for (const r of results) {
    console.log(`${r.ok ? '✓' : '✗'} ${r.ratio}: ${r.finalOut}`);
    console.log(`  qa:       ${r.reportPath}`);
  }
  console.log(`provenance: ${provOut}`);
  console.log(`used assets: ${[...usedAssetIds(props)].join(', ') || '(none)'}`);

  if (results.some((r) => !r.ok)) process.exit(1);
}

main();
