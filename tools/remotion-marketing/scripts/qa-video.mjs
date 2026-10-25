#!/usr/bin/env node
// QA gate. Probes the rendered MP4 with ffprobe/ffmpeg and checks it against
// the props it was rendered from: duration, audio track, resolution, fps,
// captions, safe margins and web loudness. Emits a JSON report.
import {execFileSync, spawnSync} from 'node:child_process';
import {existsSync, writeFileSync} from 'node:fs';
import {parseArgs, loadJson, finish} from './lib.mjs';

// Kept in sync with src/schema.ts DIMENSIONS.
export const DIMENSIONS = {
  '16:9': {width: 1920, height: 1080},
  '9:16': {width: 1080, height: 1920},
  '1:1': {width: 1080, height: 1080},
};

const LOUDNESS_TARGET = -14; // LUFS, web master
const LOUDNESS_PASS = [-19, -9];
const LOUDNESS_IDEAL = [-16, -12];

function parseFps(rate) {
  if (!rate) return 0;
  const [n, d] = rate.split('/').map(Number);
  return d ? n / d : n;
}

export function probe(video) {
  const out = execFileSync(
    'ffprobe',
    ['-v', 'quiet', '-print_format', 'json', '-show_format', '-show_streams', video],
    {encoding: 'utf8'},
  );
  return JSON.parse(out);
}

export function measureLoudness(video) {
  // ebur128 prints its summary on stderr; spawnSync captures it whether ffmpeg
  // exits 0 or not.
  const res = spawnSync(
    'ffmpeg',
    ['-hide_banner', '-nostats', '-i', video, '-af', 'ebur128', '-f', 'null', '-'],
    {encoding: 'utf8'},
  );
  const stderr = res.stderr ?? '';
  const matches = [...stderr.matchAll(/I:\s*(-?\d+(?:\.\d+)?)\s*LUFS/g)];
  if (matches.length === 0) return null;
  return Number(matches[matches.length - 1][1]);
}

/**
 * Pure evaluation so it can be unit-tested without ffmpeg.
 * @returns {{violations: string[], warnings: string[], summary: object}}
 */
export function evaluateQa({props, probeData, loudness, posterExists}) {
  const violations = [];
  const warnings = [];

  const streams = probeData.streams ?? [];
  const v = streams.find((s) => s.codec_type === 'video');
  const a = streams.find((s) => s.codec_type === 'audio');

  const expected = DIMENSIONS[props.aspectRatio];
  const fps = props.fps;
  const totalFrames = (props.scenes ?? []).reduce(
    (sum, s) => sum + s.durationInFrames,
    0,
  );
  const expectedDuration = totalFrames / fps;
  const duration = Number(probeData.format?.duration ?? 0);
  const actualFps = v ? parseFps(v.avg_frame_rate || v.r_frame_rate) : 0;

  if (!v) violations.push('no video stream found');
  if (v && (v.width !== expected.width || v.height !== expected.height)) {
    violations.push(
      `resolution ${v.width}x${v.height} != expected ${expected.width}x${expected.height} for ${props.aspectRatio}`,
    );
  }
  if (Math.abs(actualFps - fps) > 0.1) {
    violations.push(`fps ${actualFps.toFixed(3)} != expected ${fps}`);
  }
  if (Math.abs(duration - expectedDuration) > 0.2) {
    violations.push(
      `duration ${duration.toFixed(2)}s != expected ${expectedDuration.toFixed(2)}s`,
    );
  }

  const needsAudio = props.music && props.music.mode !== 'none';
  if (needsAudio && !a) violations.push('audio track required by music mode but none found');
  if (!needsAudio && a) warnings.push('audio track present though music.mode is none');

  if (needsAudio) {
    if (loudness === null) {
      warnings.push('could not measure loudness');
    } else if (loudness < LOUDNESS_PASS[0] || loudness > LOUDNESS_PASS[1]) {
      violations.push(
        `integrated loudness ${loudness} LUFS outside web range [${LOUDNESS_PASS[0]}, ${LOUDNESS_PASS[1]}] (target ${LOUDNESS_TARGET})`,
      );
    } else if (loudness < LOUDNESS_IDEAL[0] || loudness > LOUDNESS_IDEAL[1]) {
      warnings.push(
        `loudness ${loudness} LUFS is acceptable but off target ${LOUDNESS_TARGET}`,
      );
    }
  }

  // Captions and safe margins are structural guarantees from props: every scene
  // must carry a caption when captions are on, and safe margins must be enabled.
  if (props.captions) {
    const missing = (props.scenes ?? []).filter((s) => !s.caption).map((s) => s.id);
    if (missing.length) {
      violations.push(`captions enabled but scenes lack captions: ${missing.join(', ')}`);
    }
  }
  if (!props.safeMargins) {
    warnings.push('safe margins disabled; text may clip on cropped players');
  }

  if (posterExists === false) violations.push('poster image missing');

  const summary = {
    resolution: v ? `${v.width}x${v.height}` : null,
    fps: Number(actualFps.toFixed(3)),
    durationSec: Number(duration.toFixed(3)),
    expectedDurationSec: Number(expectedDuration.toFixed(3)),
    videoCodec: v?.codec_name ?? null,
    audioCodec: a?.codec_name ?? null,
    hasAudio: Boolean(a),
    loudnessLUFS: loudness,
    loudnessTarget: LOUDNESS_TARGET,
    aspectRatio: props.aspectRatio,
    captions: props.captions,
    safeMargins: props.safeMargins,
    posterExists: posterExists ?? null,
  };
  return {violations, warnings, summary};
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  if (!args.video || !args.props) {
    console.error(
      'usage: qa-video.mjs --video <file.mp4> --props <props.json> [--poster <file.png>] [--report <out.json>]',
    );
    process.exit(2);
  }
  const props = loadJson(args.props);
  const probeData = probe(args.video);
  const loudness = props.music && props.music.mode !== 'none' ? measureLoudness(args.video) : null;
  const posterExists = args.poster ? existsSync(args.poster) : undefined;
  const {violations, warnings, summary} = evaluateQa({
    props,
    probeData,
    loudness,
    posterExists,
  });
  const report = {ok: violations.length === 0, violations, warnings, summary};
  if (args.report) writeFileSync(args.report, JSON.stringify(report, null, 2));
  finish('Video QA check', violations, warnings, `  ${JSON.stringify(summary)}`);
}

if (import.meta.url === `file://${process.argv[1]}`) main();
