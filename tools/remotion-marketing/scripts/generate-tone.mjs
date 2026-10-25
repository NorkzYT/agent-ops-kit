#!/usr/bin/env node
// Generates an original, rights-clean audio bed by synthesising a soft sine
// chord with ffmpeg. No third-party samples or melodies are involved, so the
// output is safe for commercial use (license: generated-original).
import {execFileSync} from 'node:child_process';
import {mkdirSync} from 'node:fs';
import {dirname} from 'node:path';
import {parseArgs} from './lib.mjs';

export function generateTone({out, duration = 9}) {
  mkdirSync(dirname(out), {recursive: true});
  const d = Number(duration);
  const fadeOut = Math.max(0, d - 1);
  // A soft major-triad pad: A3 + C#4 + E4, mixed, gentle fades, low volume.
  const filter = [
    '[0:a][1:a][2:a]amix=inputs=3:normalize=1[mix]',
    // Fades + EBU R128 normalisation to a web master target of -14 LUFS.
    `[mix]afade=t=in:st=0:d=1,afade=t=out:st=${fadeOut}:d=1,loudnorm=I=-14:TP=-1.5:LRA=11[a]`,
  ].join(';');
  execFileSync(
    'ffmpeg',
    [
      '-y',
      '-f', 'lavfi', '-i', `sine=frequency=220:duration=${d}`,
      '-f', 'lavfi', '-i', `sine=frequency=277.18:duration=${d}`,
      '-f', 'lavfi', '-i', `sine=frequency=329.63:duration=${d}`,
      '-filter_complex', filter,
      '-map', '[a]',
      '-ar', '44100',
      '-ac', '2',
      out,
    ],
    {stdio: ['ignore', 'ignore', 'inherit']},
  );
  return out;
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  const out = args.out;
  if (!out) {
    console.error('usage: generate-tone.mjs --out <file.wav> [--duration <sec>]');
    process.exit(2);
  }
  generateTone({out, duration: args.duration ?? 9});
  console.log(`generated original tone → ${out}`);
}

if (import.meta.url === `file://${process.argv[1]}`) main();
