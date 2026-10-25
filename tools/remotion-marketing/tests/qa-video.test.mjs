import test from 'node:test';
import assert from 'node:assert/strict';
import {evaluateQa} from '../scripts/qa-video.mjs';

const props = {
  fps: 30,
  aspectRatio: '16:9',
  captions: true,
  safeMargins: true,
  music: {mode: 'generated', assetId: 'aud1'},
  scenes: [
    {id: 's1', durationInFrames: 60, caption: 'a'},
    {id: 's2', durationInFrames: 90, caption: 'b'},
  ],
};

const goodProbe = {
  format: {duration: '5.0'},
  streams: [
    {codec_type: 'video', codec_name: 'h264', width: 1920, height: 1080, avg_frame_rate: '30/1'},
    {codec_type: 'audio', codec_name: 'aac'},
  ],
};

test('passes a well-formed render', () => {
  const {violations} = evaluateQa({props, probeData: goodProbe, loudness: -14, posterExists: true});
  assert.equal(violations.length, 0);
});

test('flags wrong resolution', () => {
  const probeData = {...goodProbe, streams: [{...goodProbe.streams[0], width: 1280, height: 720}, goodProbe.streams[1]]};
  const {violations} = evaluateQa({props, probeData, loudness: -14, posterExists: true});
  assert.ok(violations.some((v) => v.includes('resolution')));
});

test('flags wrong fps', () => {
  const probeData = {...goodProbe, streams: [{...goodProbe.streams[0], avg_frame_rate: '24/1'}, goodProbe.streams[1]]};
  const {violations} = evaluateQa({props, probeData, loudness: -14, posterExists: true});
  assert.ok(violations.some((v) => v.includes('fps')));
});

test('flags wrong duration', () => {
  const probeData = {...goodProbe, format: {duration: '9.0'}};
  const {violations} = evaluateQa({props, probeData, loudness: -14, posterExists: true});
  assert.ok(violations.some((v) => v.includes('duration')));
});

test('requires an audio track when music mode is not none', () => {
  const probeData = {...goodProbe, streams: [goodProbe.streams[0]]};
  const {violations} = evaluateQa({props, probeData, loudness: null, posterExists: true});
  assert.ok(violations.some((v) => v.includes('audio track required')));
});

test('flags loudness outside the web range', () => {
  const {violations} = evaluateQa({props, probeData: goodProbe, loudness: -3, posterExists: true});
  assert.ok(violations.some((v) => v.includes('loudness')));
});

test('flags a missing caption when captions are enabled', () => {
  const p = {...props, scenes: [{id: 's1', durationInFrames: 150}]};
  const {violations} = evaluateQa({props: p, probeData: goodProbe, loudness: -14, posterExists: true});
  assert.ok(violations.some((v) => v.includes('lack captions')));
});

test('flags a missing poster', () => {
  const {violations} = evaluateQa({props, probeData: goodProbe, loudness: -14, posterExists: false});
  assert.ok(violations.some((v) => v.includes('poster')));
});
