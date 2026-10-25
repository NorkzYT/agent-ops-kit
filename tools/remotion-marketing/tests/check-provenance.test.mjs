import test from 'node:test';
import assert from 'node:assert/strict';
import {checkProvenance} from '../scripts/check-provenance.mjs';

const baseProps = {
  scenes: [{id: 's1', kind: 'media', mediaAssetId: 'img1', factIds: []}],
  music: {mode: 'generated', assetId: 'aud1'},
  brand: {name: 'X'},
};

const goodProv = {
  assets: [
    {id: 'img1', type: 'image', source: 'generated', license: 'generated-original', commercialUse: true, rightsHolder: 'owner'},
    {id: 'aud1', type: 'audio', source: 'generated', license: 'generated-original', commercialUse: true, rightsHolder: 'owner'},
  ],
};

test('passes when every used asset has complete commercial-use provenance', () => {
  const {violations} = checkProvenance(baseProps, goodProv);
  assert.equal(violations.length, 0);
});

test('blocks a used asset with no provenance record', () => {
  const prov = {assets: [goodProv.assets[1]]};
  const {violations} = checkProvenance(baseProps, prov);
  assert.ok(violations.some((v) => v.includes('img1') && v.includes('no provenance')));
});

test('blocks an asset not cleared for commercial use', () => {
  const prov = {assets: [{...goodProv.assets[0], commercialUse: false}, goodProv.assets[1]]};
  const {violations} = checkProvenance(baseProps, prov);
  assert.ok(violations.some((v) => v.includes('img1') && v.includes('commercial use')));
});

test('blocks a non-commercial license (e.g. a popular copyrighted track)', () => {
  const prov = {
    assets: [
      goodProv.assets[0],
      {id: 'aud1', type: 'audio', source: 'spotify', license: 'all-rights-reserved', commercialUse: true, rightsHolder: 'label'},
    ],
  };
  const {violations} = checkProvenance(baseProps, prov);
  assert.ok(violations.some((v) => v.includes('aud1') && v.includes('does not permit')));
});

test('blocks incomplete provenance (missing rightsHolder)', () => {
  const prov = {
    assets: [
      {id: 'img1', type: 'image', source: 'generated', license: 'generated-original', commercialUse: true},
      goodProv.assets[1],
    ],
  };
  const {violations} = checkProvenance(baseProps, prov);
  assert.ok(violations.some((v) => v.includes('rightsHolder')));
});

test('blocks music mode set without an asset id', () => {
  const props = {...baseProps, music: {mode: 'licensed'}, scenes: []};
  const {violations} = checkProvenance(props, {assets: []});
  assert.ok(violations.some((v) => v.includes('music.mode')));
});
