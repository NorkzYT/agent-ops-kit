import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {mkdtempSync, readFileSync, writeFileSync, existsSync, rmSync} from 'node:fs';
import {join, dirname} from 'node:path';
import {fileURLToPath} from 'node:url';
import {tmpdir} from 'node:os';
import {createHash} from 'node:crypto';
import {renderHtml, buildProject, safeAssetRel, escapeHtml} from '../scripts/build-composition.mjs';
import {checkHtmlAssets, loadProjectFiles} from '../scripts/check-html-assets.mjs';
import {renderEnv, renderTmp, posterTime, MAX_TMPDIR_LEN} from '../scripts/render-campaign.mjs';

const TOOL = join(dirname(fileURLToPath(import.meta.url)), '..');
const load = (p) => JSON.parse(readFileSync(join(TOOL, p), 'utf8'));
const sample = load('props/sample.json');
const sampleProv = load('props/sample.provenance.json');
const tmp = () => mkdtempSync(join(process.env.TMPDIR || tmpdir(), 'hf-test-'));

test('composition timing matches props for every ratio', () => {
  for (const [ratio, w, h] of [['16:9', 1920, 1080], ['9:16', 1080, 1920], ['1:1', 1080, 1080]]) {
    const {html, durationSec} = renderHtml(sample, {aspectRatio: ratio});
    assert.equal(durationSec, 9);
    assert.match(html, new RegExp(`data-duration="9" data-width="${w}" data-height="${h}" data-fps="30"`));
    assert.match(html, /id="scene-s_feature" class="clip scene kind-feature" data-start="2" data-duration="2.5"/);
    assert.match(html, /id="caption-s_outro" class="clip caption" data-start="7"/);
    assert.match(html, /<audio id="music" class="clip" src="assets\/audio\/sample-tone.wav"/);
  }
});

test('composition has no network references and escapes copy', () => {
  const props = structuredClone(sample);
  props.scenes[0].headline = '<script>alert(1)</script>';
  const {html} = renderHtml(props);
  assert.doesNotMatch(html, /<script/i);
  assert.doesNotMatch(html, /https?:\/\//);
  assert.match(html, /&lt;script&gt;/);
  assert.equal(escapeHtml(`"'&`), '&quot;&#39;&amp;');
});

test('asset paths must stay local', () => {
  assert.throws(() => safeAssetRel('https://cdn.example/x.png'), /URL/);
  assert.throws(() => safeAssetRel('//cdn.example/x.png'), /URL/);
  assert.throws(() => safeAssetRel('/etc/passwd'), /absolute/);
  assert.throws(() => safeAssetRel('../../secret.png'), /escapes/);
  assert.equal(safeAssetRel('images/a.png'), 'images/a.png');
});

test('built sample project passes the asset gate', () => {
  const dir = tmp();
  const assets = join(dir, 'src');
  const project = join(dir, 'project');
  const wav = join(assets, 'audio', 'sample-tone.wav');
  spawnSync('mkdir', ['-p', dirname(wav)]);
  writeFileSync(wav, 'RIFF-test');
  spawnSync('mkdir', ['-p', join(assets, 'fonts')]);
  for (const f of ['LiberationSans-Regular.ttf', 'LiberationSans-Bold.ttf']) writeFileSync(join(assets, 'fonts', f), 'font');
  buildProject(sample, {outDir: project, assetsRoot: assets});
  const res = checkHtmlAssets({files: loadProjectFiles(project), provenance: sampleProv, assetPaths: sample.assetPaths});
  assert.deepEqual(res.violations, []);
  rmSync(dir, {recursive: true, force: true});
});

const cleanFiles = () => ({
  'index.html': renderHtml(sample).html,
  'assets/audio/sample-tone.wav': null,
  'assets/fonts/LiberationSans-Regular.ttf': null,
  'assets/fonts/LiberationSans-Bold.ttf': null,
});
const gate = (files) => checkHtmlAssets({files, provenance: sampleProv, assetPaths: sample.assetPaths}).violations;

test('asset gate refuses a CDN script added by hand', () => {
  const files = cleanFiles();
  files['index.html'] = files['index.html'].replace('</head>', '<script src="https://cdn.jsdelivr.net/npm/gsap@3.15.0/dist/gsap.min.js"></script></head>');
  assert.ok(gate(files).some((v) => v.includes('external reference')));
});

test('asset gate refuses an uncleared image, CSS url(), data: URI and fetch()', () => {
  for (const edit of [
    (h) => h.replace('</section>', '<img src="assets/images/stock-photo.png"></section>'),
    (h) => h.replace('</style>', '#root{background:url("assets/images/bg.jpg")}</style>'),
    (h) => h.replace('</style>', '@import url("https://fonts.googleapis.com/css2?family=Inter");</style>'),
    (h) => h.replace('</section>', '<img src="data:image/png;base64,AAAA"></section>'),
    (h) => h.replace('</body>', '<script>fetch("x")</script></body>'),
    (h) => h.replace('</section>', '<img src="../outside.png"></section>'),
  ]) {
    const files = cleanFiles();
    files['index.html'] = edit(files['index.html']);
    assert.ok(gate(files).length > 0, `edit not refused: ${edit}`);
  }
});

test('asset gate refuses a stray file in assets/ with no cleared record', () => {
  const files = {...cleanFiles(), 'assets/images/stock-photo.png': null};
  assert.ok(gate(files).some((v) => v.includes('without a provenance record')));
});

test('render env passes no inherited credentials and disables telemetry', () => {
  process.env.GITHUB_TOKEN = 'canary-should-not-leak';
  const env = renderEnv({home: '/x/home', chrome: '/usr/bin/google-chrome', tmp: '/x/t'});
  assert.equal(env.TMPDIR, '/x/t');
  assert.equal(env.GITHUB_TOKEN, undefined);
  assert.equal(env.HYPERFRAMES_NO_TELEMETRY, '1');
  assert.equal(env.DO_NOT_TRACK, '1');
  assert.equal(env.HYPERFRAMES_NO_UPDATE_CHECK, '1');
  assert.equal(env.HOME, '/x/home');
  assert.match(env.HYPERFRAMES_BROWSER_PATH, /chrome-offline\.sh$/);
  delete process.env.GITHUB_TOKEN;
});

test('unlicensed asset is denied before any render (end to end, exit 1)', () => {
  const out = tmp();
  const res = spawnSync(process.execPath, [join(TOOL, 'scripts/render-campaign.mjs'),
    '--props', join(TOOL, 'tests/fixtures/unlicensed.props.json'),
    '--provenance', join(TOOL, 'tests/fixtures/unlicensed.provenance.json'),
    '--assets', join(TOOL, 'tests/fixtures/assets'),
    '--out', out], {encoding: 'utf8'});
  assert.equal(res.status, 1, res.stdout + res.stderr);
  assert.match(res.stdout, /img_stock_photo" is not cleared for commercial use/);
  assert.match(res.stderr, /provenance gate failed/);
  assert.equal(existsSync(join(out, 'project.16x9')), false, 'project must not be built');
  assert.equal(existsSync(join(out, 'final.16x9.mp4')), false, 'nothing may be rendered');
  assert.equal(existsSync(join(out, 'render-manifest.json')), false);
  rmSync(out, {recursive: true, force: true});
});

test('sample passes every gate with --gates-only (no render)', () => {
  const out = tmp();
  const assets = join(out, 'assets');
  // cwd is outside the tool: the vendored font must resolve against the provenance file, not the cwd.
  const res = spawnSync(process.execPath, [join(TOOL, 'scripts/render-campaign.mjs'), '--assets', assets, '--out', out, '--gates-only'], {encoding: 'utf8', cwd: out});
  assert.equal(res.status, 0, res.stdout + res.stderr);
  assert.match(res.stdout, /Project asset gate 16:9 ==\n {2}✓ passed/);
  assert.ok(existsSync(join(out, 'project.16x9', 'index.html')));
  for (const f of ['LiberationSans-Regular.ttf', 'LiberationSans-Bold.ttf']) {
    assert.deepEqual(readFileSync(join(assets, 'fonts', f)), readFileSync(join(TOOL, 'fonts', 'liberation-2.1.5', f)));
  }
  rmSync(out, {recursive: true, force: true});
});

test('render temp dir is short enough for Chrome sockets, long roots refused', () => {
  const short = renderTmp();
  assert.ok(short.length <= MAX_TMPDIR_LEN);
  rmSync(short, {recursive: true, force: true});
  const deep = join(tmp(), 'x'.repeat(80));
  spawnSync('mkdir', ['-p', deep]);
  assert.throws(() => renderTmp(deep), /Chrome needs/);
});

test('poster frame is inside a scene, not on a scene boundary', () => {
  // sample: scenes 0-2s, 2-4.5s, 4.5-7s, 7-9s; midpoint 4.5s is a boundary.
  assert.equal(posterTime(sample), 6);
});

test('asset gate refuses a named font without a local @font-face, allows declared ones', () => {
  const named = cleanFiles();
  named['index.html'] = named['index.html'].replace('</style>', '.x{font-family: "Inter", sans-serif}</style>');
  assert.ok(gate(named).some((v) => v.includes('font-family "inter"')));
  const declared = {...cleanFiles(), 'assets/fonts/brand.woff2': null};
  declared['index.html'] = declared['index.html'].replace('</style>', '@font-face{font-family:"Brand";src:url("assets/fonts/brand.woff2")} .x{font-family:"Brand",sans-serif}</style>');
  const v = checkHtmlAssets({files: declared, provenance: {assets: [...sampleProv.assets, {id: 'font_brand', license: 'licensed-commercial', commercialUse: true}]}, assetPaths: {...sample.assetPaths, font_brand: 'fonts/brand.woff2'}}).violations;
  assert.deepEqual(v, []);
});

test('asset gate refuses a generic primary family (HyperFrames rewrites it to Google-hosted Inter)', () => {
  for (const fam of ['sans-serif', 'system-ui', 'serif, "Campaign Sans"']) {
    const files = cleanFiles();
    files['index.html'] = files['index.html'].replace('</style>', `.x{font-family: ${fam}}</style>`);
    assert.ok(gate(files).some((v) => v.includes('is generic')), fam);
  }
  const ok = cleanFiles();
  ok['index.html'] = ok['index.html'].replace('</style>', '.x{font-family: inherit} .y{font-family:"Campaign Sans", system-ui}</style>');
  assert.deepEqual(gate(ok), []);
});

test('composition embeds props.typography as local @font-face and requires it', () => {
  const {html, assets} = renderHtml(sample);
  assert.match(html, /@font-face \{ font-family: "Campaign Sans"; src: url\("assets\/fonts\/LiberationSans-Bold.ttf"\) format\("truetype"\); font-weight: 700;/);
  assert.match(html, /#root \{[^}]*font-family: "Campaign Sans", sans-serif;/);
  assert.ok(assets.some((a) => a.id === 'fnt_regular'));
  const none = structuredClone(sample);
  delete none.typography;
  assert.throws(() => renderHtml(none), /typography/);
  const bad = structuredClone(sample);
  bad.typography.family = 'x"; } body { background: url(https://e.x)';
  assert.throws(() => renderHtml(bad), /typography/);
  const notFont = structuredClone(sample);
  notFont.typography.faces[0].assetId = 'aud_tone';
  assert.throws(() => renderHtml(notFont), /not a \.ttf/);
});

test('sample font provenance pins OFL license and sha256', () => {
  for (const id of ['fnt_regular', 'fnt_bold']) {
    const rec = sampleProv.assets.find((a) => a.id === id);
    assert.equal(rec.license, 'ofl-1.1');
    assert.match(rec.sha256, /^[0-9a-f]{64}$/);
    // Vendored, tracked bytes (not whatever the host's fonts-liberation ships).
    assert.match(rec.localSource, /^\.\.\/fonts\/liberation-2\.1\.5\/LiberationSans-(Regular|Bold)\.ttf$/);
    const vendored = join(TOOL, 'props', rec.localSource);
    assert.equal(createHash('sha256').update(readFileSync(vendored)).digest('hex'), rec.sha256);
  }
  const notice = readFileSync(join(TOOL, 'fonts', 'liberation-2.1.5', 'COPYRIGHT'), 'utf8');
  assert.match(notice, /SIL OPEN FONT LICENSE Version 1\.1/);
  assert.match(notice, /Red Hat, Inc\. with Reserved Font Name Liberation/);
  const source = readFileSync(join(TOOL, 'fonts', 'liberation-2.1.5', 'SOURCE.md'), 'utf8');
  for (const id of ['fnt_regular', 'fnt_bold']) assert.ok(source.includes(sampleProv.assets.find((a) => a.id === id).sha256));
});

test('font files are refused when they do not match the provenance sha256', () => {
  const out = tmp();
  const prov = structuredClone(sampleProv);
  const fake = join(out, 'fake-font.ttf');
  writeFileSync(fake, 'not the pinned font');
  prov.assets.find((a) => a.id === 'fnt_regular').localSource = fake;
  const provPath = join(out, 'prov.json');
  writeFileSync(provPath, JSON.stringify(prov));
  const run = (assets) => spawnSync(process.execPath, [join(TOOL, 'scripts/render-campaign.mjs'),
    '--provenance', provPath, '--assets', assets, '--out', join(out, 'o'), '--gates-only'], {encoding: 'utf8'});
  // Tampered copy source: refused before anything is copied or built.
  let res = run(join(out, 'a1'));
  assert.equal(res.status, 1, res.stdout + res.stderr);
  assert.match(res.stderr, /does not match the provenance sha256/);
  assert.ok(!existsSync(join(out, 'a1', 'fonts', 'LiberationSans-Regular.ttf')));
  assert.ok(!existsSync(join(out, 'o', 'project.16x9')));
  // A swapped file already in assets/ is refused too.
  spawnSync('mkdir', ['-p', join(out, 'a2', 'fonts')]);
  writeFileSync(join(out, 'a2', 'fonts', 'LiberationSans-Bold.ttf'), 'swapped');
  res = spawnSync(process.execPath, [join(TOOL, 'scripts/render-campaign.mjs'),
    '--assets', join(out, 'a2'), '--out', join(out, 'o2'), '--gates-only'], {encoding: 'utf8'});
  assert.equal(res.status, 1, res.stdout + res.stderr);
  assert.match(res.stderr, /LiberationSans-Bold.ttf does not match the provenance sha256/);
  rmSync(out, {recursive: true, force: true});
});

// --- Path safety before any write (music tone, font copy, project build) ---
import {symlinkSync, mkdirSync, lstatSync, readdirSync} from 'node:fs';
import {resolveAssetPath} from '../scripts/build-composition.mjs';

const runGates = (dir, props, extra = []) => {
  const propsPath = join(dir, 'props.json');
  writeFileSync(propsPath, JSON.stringify(props));
  return spawnSync(process.execPath, [join(TOOL, 'scripts/render-campaign.mjs'),
    '--props', propsPath, '--provenance', join(TOOL, 'props/sample.provenance.json'),
    '--assets', join(dir, 'assets'), '--out', join(dir, 'out'), '--gates-only', ...extra], {encoding: 'utf8'});
};

test('traversal or absolute assetPaths are refused before any write, incl. --gates-only', () => {
  for (const [id, bad] of [['aud_tone', '../../escaped-tone.wav'], ['aud_tone', '/tmp/abs-tone.wav'],
    ['fnt_regular', '../escaped-font.ttf'], ['aud_tone', '.'], ['aud_tone', 'audio/../../x.wav']]) {
    const dir = tmp();
    const props = structuredClone(sample);
    props.assetPaths[id] = bad;
    const res = runGates(dir, props);
    assert.equal(res.status, 2, `${bad}: ${res.stdout}${res.stderr}`);
    assert.match(res.stderr, /asset path/);
    // Nothing was written anywhere: no out dir, no assets dir, no escaped file.
    assert.deepEqual(readdirSync(dir).sort(), ['props.json'], bad);
    assert.ok(!existsSync(join(dir, '..', 'escaped-tone.wav')));
    rmSync(dir, {recursive: true, force: true});
  }
});

test('a symlinked directory inside assets/ cannot redirect generated or copied files', () => {
  const dir = tmp();
  const outside = join(dir, 'outside');
  mkdirSync(outside);
  mkdirSync(join(dir, 'assets'));
  symlinkSync(outside, join(dir, 'assets', 'audio'));
  symlinkSync(outside, join(dir, 'assets', 'fonts'));
  const res = runGates(dir, sample);
  assert.equal(res.status, 2, res.stdout + res.stderr);
  assert.match(res.stderr, /symlink/);
  assert.deepEqual(readdirSync(outside), [], 'nothing written through the symlink');
  assert.ok(!existsSync(join(dir, 'out', 'project.16x9')));
  rmSync(dir, {recursive: true, force: true});
});

test('a dangling symlink at the tone destination is refused, its target never created', () => {
  const dir = tmp();
  mkdirSync(join(dir, 'assets', 'audio'), {recursive: true});
  const target = join(dir, 'planted.wav');
  symlinkSync(target, join(dir, 'assets', 'audio', 'sample-tone.wav'));
  const res = runGates(dir, sample);
  assert.equal(res.status, 2, res.stdout + res.stderr);
  assert.match(res.stderr, /symlink/);
  assert.ok(!existsSync(target));
  assert.ok(lstatSync(join(dir, 'assets', 'audio', 'sample-tone.wav')).isSymbolicLink());
  rmSync(dir, {recursive: true, force: true});
});

test('resolveAssetPath: stays under the root, refuses symlink components', () => {
  const dir = tmp();
  mkdirSync(join(dir, 'r', 'a'), {recursive: true});
  assert.equal(resolveAssetPath(join(dir, 'r'), 'a/x.png'), join(dir, 'r', 'a', 'x.png'));
  assert.equal(resolveAssetPath(join(dir, 'r'), 'new/dir/x.png'), join(dir, 'r', 'new', 'dir', 'x.png'));
  assert.throws(() => resolveAssetPath(join(dir, 'r'), '../x.png'), /escapes/);
  symlinkSync('/etc', join(dir, 'r', 'link'));
  assert.throws(() => resolveAssetPath(join(dir, 'r'), 'link/passwd'), /symlink/);
  rmSync(dir, {recursive: true, force: true});
});

test('buildProject refuses a symlinked source asset (cannot copy arbitrary files in)', () => {
  const dir = tmp();
  const assets = join(dir, 'src');
  mkdirSync(join(assets, 'fonts'), {recursive: true});
  mkdirSync(join(assets, 'audio'), {recursive: true});
  writeFileSync(join(assets, 'fonts', 'LiberationSans-Regular.ttf'), 'font');
  writeFileSync(join(assets, 'audio', 'sample-tone.wav'), 'RIFF');
  const secret = join(dir, 'secret.txt');
  writeFileSync(secret, 'canary');
  symlinkSync(secret, join(assets, 'fonts', 'LiberationSans-Bold.ttf'));
  assert.throws(() => buildProject(sample, {outDir: join(dir, 'p'), assetsRoot: assets}), /symlink/);
  rmSync(dir, {recursive: true, force: true});
});

// --- Asset gate: font shorthand and scripts ---
test('asset gate checks the font shorthand like font-family', () => {
  for (const css of ['.x{font: 700 20px/1.2 "Roboto", sans-serif}', '.x{font:italic bold 12px Inter}',
    '.x{font: 16px system-ui}', '<p style="font: 10px Arial">x</p>', '.x{font: caption}']) {
    const files = cleanFiles();
    files['index.html'] = css.startsWith('<')
      ? files['index.html'].replace('</section>', `${css}</section>`)
      : files['index.html'].replace('</style>', `${css}</style>`);
    assert.ok(gate(files).some((v) => /font/.test(v)), css);
  }
  const ok = cleanFiles();
  ok['index.html'] = ok['index.html'].replace('</style>', '.x{font: 700 20px/1.2 "Campaign Sans", sans-serif} .y{font: inherit} .z{--font: 1px Inter; font-weight: 700}</style>');
  assert.deepEqual(gate(ok), []);
});

test('asset gate scans external JS files and inline module imports', () => {
  const prov = {assets: [...sampleProv.assets, {id: 'js_anim', license: 'licensed-commercial', commercialUse: true}]};
  const assetPaths = {...sample.assetPaths, js_anim: 'js/anim.js'};
  const withJs = (js, inline = '') => {
    const files = {...cleanFiles(), 'assets/js/anim.js': js};
    files['index.html'] = files['index.html'].replace('</body>', `<script src="assets/js/anim.js"></script>${inline}</body>`);
    return checkHtmlAssets({files, provenance: prov, assetPaths}).violations;
  };
  assert.deepEqual(withJs('document.body.dataset.ok = "1";'), []);
  for (const js of ['fetch("https://x.example")', 'import x from "https://cdn.example/m.js";',
    'img.src = "https://t.example/p.gif"', 'new Worker("w.js")', 'const u = "//cdn.example/a.js"',
    'navigator.sendBeacon("/x")', 'el.style.fontFamily = "Roboto"']) {
    assert.ok(withJs(js).length > 0, js);
  }
  assert.ok(withJs('', '<script type="module">import "https://cdn.example/m.js"</script>').length > 0);
  // A script file that is not referenced still gets scanned.
  const files = {...cleanFiles(), 'helper.js': 'fetch("https://x.example")'};
  assert.ok(gate(files).some((v) => v.includes('helper.js')));
});

test('asset gate refuses meta refresh, srcdoc and extra URL attributes', () => {
  for (const tag of ['<meta http-equiv="refresh" content="0;url=https://x.example">', '<iframe srcdoc="&lt;img src=x&gt;"></iframe>',
    '<object data="https://x.example/a.swf"></object>', '<a ping="https://t.example" href="#a">x</a>', '<form action="https://x.example"></form>']) {
    const files = cleanFiles();
    files['index.html'] = files['index.html'].replace('</section>', `${tag}</section>`);
    assert.ok(gate(files).length > 0, tag);
  }
});

test('asset gate scans SVG assets and dynamic script code', () => {
  const prov = {assets: [...sampleProv.assets, {id: 'img_logo', license: 'licensed-commercial', commercialUse: true}]};
  const assetPaths = {...sample.assetPaths, img_logo: 'images/logo.svg'};
  const files = {...cleanFiles(), 'assets/images/logo.svg': '<svg><image href="https://t.example/p.png"/></svg>'};
  files['index.html'] = files['index.html'].replace('</section>', '<object data="assets/images/logo.svg"></object></section>');
  assert.ok(checkHtmlAssets({files, provenance: prov, assetPaths}).violations.some((v) => v.includes('logo.svg: external')));
  const evalFiles = cleanFiles();
  evalFiles['index.html'] = evalFiles['index.html'].replace('</body>', '<script>eval(atob("Zg=="))</script></body>');
  assert.ok(gate(evalFiles).some((v) => v.includes('dynamic code')));
});
