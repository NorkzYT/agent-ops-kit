#!/usr/bin/env node
// Provenance / rights gate. Fails the build when any asset the video uses
// lacks a complete provenance record that permits commercial use.
import {parseArgs, loadJson, usedAssetIds, finish} from './lib.mjs';

/** Licenses that permit commercial use in a marketing video. */
export const COMMERCIAL_LICENSES = new Set([
  'generated-original',
  'original',
  'public-domain',
  'cc0',
  'cc-by',
  'cc-by-sa',
  'purchased-royalty-free',
  'licensed-commercial',
]);

const REQUIRED_FIELDS = [
  'id',
  'type',
  'source',
  'license',
  'commercialUse',
  'rightsHolder',
];

/**
 * @returns {{violations: string[], warnings: string[]}}
 */
export function checkProvenance(props, provenance) {
  const violations = [];
  const warnings = [];
  const byId = new Map((provenance.assets ?? []).map((a) => [a.id, a]));
  const used = usedAssetIds(props);

  // Music must declare a rights-permitting mode and an asset unless 'none'.
  const music = props.music ?? {mode: 'none'};
  if (music.mode !== 'none' && !music.assetId) {
    violations.push(
      `music.mode is "${music.mode}" but no music.assetId is set`,
    );
  }

  for (const id of used) {
    const asset = byId.get(id);
    if (!asset) {
      violations.push(`asset "${id}" is used but has no provenance record`);
      continue;
    }
    for (const field of REQUIRED_FIELDS) {
      if (asset[field] === undefined || asset[field] === '') {
        violations.push(`asset "${id}" is missing provenance field "${field}"`);
      }
    }
    if (asset.commercialUse !== true) {
      violations.push(
        `asset "${id}" is not cleared for commercial use (commercialUse !== true)`,
      );
    }
    if (asset.license && !COMMERCIAL_LICENSES.has(asset.license)) {
      violations.push(
        `asset "${id}" has license "${asset.license}" which does not permit commercial use`,
      );
    }
    if (asset.license === 'cc-by' && !asset.attribution && !asset.notes) {
      warnings.push(`asset "${id}" is cc-by; record the required attribution`);
    }
  }

  for (const asset of provenance.assets ?? []) {
    if (!used.has(asset.id)) {
      warnings.push(`provenance record "${asset.id}" is unused by this video`);
    }
  }

  return {violations, warnings};
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  const propsPath = args.props;
  const provPath = args.provenance;
  if (!propsPath || !provPath) {
    console.error(
      'usage: check-provenance.mjs --props <props.json> --provenance <provenance.json>',
    );
    process.exit(2);
  }
  const props = loadJson(propsPath);
  const provenance = loadJson(provPath);
  const {violations, warnings} = checkProvenance(props, provenance);
  finish(
    'Provenance / rights check',
    violations,
    warnings,
    `  assets in use: ${[...usedAssetIds(props)].join(', ') || '(none)'}`,
  );
}

if (import.meta.url === `file://${process.argv[1]}`) main();
