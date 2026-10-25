import test from 'node:test';
import assert from 'node:assert/strict';
import {checkClaims} from '../scripts/check-claims.mjs';

const facts = [
  {id: 'f1', claim: 'Saves 5 hours a week', evidence: 'PostHog cohort: median 5 hours saved.'},
];

test('passes when a cited numeric claim is backed by a fact', () => {
  const props = {
    facts,
    scenes: [{id: 's1', kind: 'proof', headline: 'Saves 5 hours a week', factIds: ['f1']}],
  };
  const {violations} = checkClaims(props);
  assert.equal(violations.length, 0);
});

test('blocks an unsupported numeric on-screen claim', () => {
  const props = {
    facts,
    scenes: [{id: 's1', kind: 'feature', headline: '10x faster than the rest', factIds: []}],
  };
  const {violations} = checkClaims(props);
  assert.ok(violations.some((v) => v.includes('unsupported claim')));
});

test('blocks an unsupported superlative', () => {
  const props = {
    facts: [],
    scenes: [{id: 's1', kind: 'intro', headline: 'The best CRM ever', factIds: []}],
  };
  const {violations} = checkClaims(props);
  assert.ok(violations.some((v) => v.includes('unsupported claim')));
});

test('blocks a reference to an unknown fact id', () => {
  const props = {
    facts,
    scenes: [{id: 's1', kind: 'proof', headline: 'Fast', factIds: ['nope']}],
  };
  const {violations} = checkClaims(props);
  assert.ok(violations.some((v) => v.includes('unknown fact')));
});

test('blocks a fact with no evidence', () => {
  const props = {
    facts: [{id: 'f1', claim: 'Trusted by many', evidence: ''}],
    scenes: [{id: 's1', kind: 'proof', headline: 'Trusted', factIds: ['f1']}],
  };
  const {violations} = checkClaims(props);
  assert.ok(violations.some((v) => v.includes('no evidence')));
});

test('allows soft, non-factual slogans without a fact', () => {
  const props = {
    facts: [],
    scenes: [{id: 's1', kind: 'intro', headline: 'Marketing videos from a brief', factIds: []}],
  };
  const {violations} = checkClaims(props);
  assert.equal(violations.length, 0);
});
