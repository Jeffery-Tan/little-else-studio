import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {loadApp, makeDom} from './dom-fixture.mjs';

test('the central materials view shows only recorded files and opens the original', () => {
  const app = loadApp({dom: makeDom({ids: [
    'journey-assets', 'journey-tab-assets', 'viewer', 'viewer-name', 'viewer-body',
  ]})});
  app.ctx.__materials = [
    {name: 'bag front.jpg', kind: 'image', bytes: 2048, usable: true},
    {name: 'unavailable.mp4', kind: 'video', bytes: 0, usable: false},
  ];
  vm.runInContext('state.runId="preview"; state.materials=globalThis.__materials; renderCanvasMaterials()', app.ctx);

  const host = app.document.getElementById('journey-assets');
  const tiles = host.querySelectorAll('.tile');
  assert.equal(tiles.length, 2);
  assert.match(host.textContent, /bag front.jpg/);
  assert.match(host.textContent, /unavailable.mp4/);
  assert.equal(app.document.getElementById('journey-tab-assets').textContent, '素材 1');
  assert.equal(tiles[0].getAttribute('role'), 'button');
  assert.equal(tiles[1].getAttribute('role'), null);

  tiles[0].onkeydown({key: 'Enter', preventDefault() {}});
  assert.equal(app.document.getElementById('viewer').hidden, false);
  assert.match(app.document.getElementById('viewer-body').querySelector('img').src,
    /\/api\/runs\/preview\/materials\/bag%20front\.jpg$/);
});
