#!/usr/bin/env node
// Claims gate. Every on-screen factual claim must be backed by a supplied fact
// with evidence. Unsupported numeric/superlative claims block the render so the
// video never asserts something the brief did not support.
import {parseArgs, loadJson, finish} from './lib.mjs';

// Patterns that mark a line as a *factual* claim needing evidence.
const NUMERIC = /\b\d+(?:[.,]\d+)?\s*(?:%|x|×|percent|fps|ms|s|k|m|bn|billion|million|hours?|minutes?|days?|users?|customers?)\b/gi;
const SUPERLATIVE = /\b(?:fastest|slowest|best|#1|number one|no\.?\s*1|the most|world['’]?s? (?:first|best|only)|guaranteed|proven|leading|unlimited|instant(?:ly)?)\b/gi;

function factualTokens(text) {
  if (!text) return [];
  return [
    ...(text.match(NUMERIC) ?? []),
    ...(text.match(SUPERLATIVE) ?? []),
  ].map((t) => t.trim());
}

function supported(token, facts) {
  const needle = token.toLowerCase();
  return facts.some(
    (f) =>
      f.claim.toLowerCase().includes(needle) ||
      f.evidence.toLowerCase().includes(needle),
  );
}

/**
 * @returns {{violations: string[], warnings: string[]}}
 */
export function checkClaims(props, factsList) {
  const violations = [];
  const warnings = [];
  const facts = factsList ?? props.facts ?? [];
  const factIds = new Set(facts.map((f) => f.id));

  for (const f of facts) {
    if (!f.evidence || !f.evidence.trim()) {
      violations.push(`fact "${f.id}" has no evidence`);
    }
  }

  for (const scene of props.scenes ?? []) {
    for (const id of scene.factIds ?? []) {
      if (!factIds.has(id)) {
        violations.push(
          `scene "${scene.id}" references unknown fact "${id}"`,
        );
      }
    }
    // Any factual token in visible text must be backed by a fact.
    for (const field of ['headline', 'subhead', 'caption']) {
      for (const token of factualTokens(scene[field])) {
        if (!supported(token, facts)) {
          violations.push(
            `scene "${scene.id}" ${field} makes an unsupported claim: "${token}" — cite a fact or soften the copy`,
          );
        }
      }
    }
    if (
      (scene.kind === 'proof' || scene.kind === 'feature') &&
      (scene.factIds ?? []).length === 0
    ) {
      warnings.push(
        `scene "${scene.id}" (${scene.kind}) shows no cited facts`,
      );
    }
  }

  return {violations, warnings};
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  if (!args.props) {
    console.error(
      'usage: check-claims.mjs --props <props.json> [--facts <facts.json>]',
    );
    process.exit(2);
  }
  const props = loadJson(args.props);
  const facts = args.facts ? loadJson(args.facts).facts ?? loadJson(args.facts) : null;
  const {violations, warnings} = checkClaims(props, facts);
  finish('Claims / evidence check', violations, warnings);
}

if (import.meta.url === `file://${process.argv[1]}`) main();
