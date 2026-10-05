/* Synthetic projection and real DOM controller calls; no server or model. */
import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {makeDom} from './dom-fixture.mjs';

const require = createRequire(import.meta.url);
const Board = require('../../studio/web/creative-board.js');
const hash = value => value.repeat(64);
const url = name => `/api/runs/fixture/materials/${encodeURIComponent(name)}`;
const step = (stage, version, artifact_sha256, blocks = []) => ({stage, version, artifact_sha256, blocks,
  has_doc: true, approval_decision: 'approved', integrity_ok: true, stale: false});
const fields = values => Object.entries(values).map(([label, value]) => ({label, value}));

function fixture() {
  const writing = step('writing', 6, hash('a'), [
    {type: 'timeline', segments: [
      {tc: '00:00–00:03', seconds: 3, label: 'First public line'},
      {tc: '00:03–00:06', seconds: 3, label: 'Second public line'}]},
    {type: 'cards', title: '逐段', cards: [
      {head: '00:00–00:03', fields: fields({'画面': 'US kitchen, person checks bag', '台词': 'First public line',
        '声音': 'Kitchen sound', '人物身份': 'person-a', '场景身份': 'kitchen'})},
      {head: '00:03–00:06', fields: fields({'画面': 'Close-up of bag', '台词': 'Second public line', '场景身份': 'kitchen'})}]},
    {type: 'text', title: '发布文案', text: 'Synthetic caption'}]);
  const material = step('material_plan', 5, hash('b'), [
    {type: 'cards', title: '共同人物与场景设计', cards: [{head: '美国站创作语境', fields: fields({'日常处境': 'US weekday lunch', '市场': 'US'})}]},
    {type: 'cards', title: '人物与服装', cards: [{head: 'Host（person-a）', fields: fields({'外观': 'Brown hair', '服装': 'Blue shirt'})}]},
    {type: 'cards', title: '场景与道具', cards: [{head: 'Kitchen（kitchen）', fields: fields({'位置': 'US home kitchen', '道具': 'Counter, bag'})}]}]);
  const direction = step('direction', 2, hash('c'), [{type: 'cards', cards: [
    {head: 'A', emphasis: 'chosen', fields: fields({'标题': 'Recommended A', '主张': 'Do not inherit recommendation'})},
    {head: 'B', fields: fields({'标题': 'Adopted B', '主张': 'Chosen creative premise'})}]}]);
  direction.selected_option_id = 'B';
  const targets = {source_version: writing.version, source_sha256: writing.artifact_sha256, complete_list: true,
    shots: [{ordinal: 1, shot_id: 'backend-one', label: 'First shot'}, {ordinal: 2, shot_id: 'backend-two', label: 'Second shot'}]};
  const output = {name: 'current shot.png', sha256: hash('d'), url: url('current shot.png')};
  const draft = {draft_id: 'current-image', shot_id: 'backend-one', kind: 'image', source_version: 6,
    source_sha256: hash('a'), is_current: true, source_ready: true,
    attempts: [{status: 'succeeded', attempt_id: 'synthetic-attempt', output_available: true, output}]};
  const production = {targets: {image: structuredClone(targets), video: structuredClone(targets)}, drafts: [draft],
    materials: [{...output, kind: 'image'}], shots: {
      image: {total: 2, complete_list: true, shots: [
        {ordinal: 1, shot_id: 'backend-one', draft_id: 'current-image', stage: 'completed', stage_label: '已生成', source_ready: true, output},
        {ordinal: 2, shot_id: 'backend-two', draft_id: null, stage: 'unprepared', stage_label: '尚未准备输入', source_ready: false}]},
      video: {total: 2, complete_list: true, shots: [{ordinal: 1, shot_id: 'backend-one', stage: 'unprepared', stage_label: '尚未准备输入'}]}}};
  return {runId: 'fixture', run: {run_id: 'fixture', title: 'Synthetic lunch bag', brief: 'Create a US lunch video', content_format: 'video'},
    timeline: {rail: [step('intake', 1, hash('e')), direction, material, writing]}, production,
    materials: [{name: 'product.png', kind: 'image', url: url('product.png'), purpose: 'product'},
      {...output, kind: 'image'}, {name: 'reference.png', kind: 'image', url: url('reference.png'), purpose: 'reference'}]};
}

function dom() {
  const {document} = makeDom();
  const original = document.createElement.bind(document);
  const enhance = node => {
    node.ownerDocument = document;
    node._events = new Map();
    node.addEventListener = (type, fn) => { if (!node._events.has(type)) node._events.set(type, new Set()); node._events.get(type).add(fn); };
    node.removeEventListener = (type, fn) => node._events.get(type)?.delete(fn);
    node.focus = () => { document.activeElement = node; };
    node.getBoundingClientRect = () => ({left: 0, top: 0, width: node.clientWidth || 1000, height: node.clientHeight || 680});
    node.setPointerCapture = id => { node._capture = id; };
    node.hasPointerCapture = id => node._capture === id;
    node.releasePointerCapture = () => { node._capture = null; };
    const closest = node.closest.bind(node);
    node.closest = selector => selector.split(',').map(part => closest(part.trim())).find(Boolean) || null;
    const replaceChildren = node.replaceChildren.bind(node);
    node.replaceChildren = (...children) => { for (const child of node.children) child.parentNode = null; replaceChildren(...children); };
    return node;
  };
  document.createElement = tag => enhance(original(tag));
  document.createElementNS = (_ns, tag) => document.createElement(tag);
  const host = document.createElement('section'); host.clientWidth = 1000; host.clientHeight = 680;
  document.body.append(host);
  return {document, host};
}

function emit(node, type, values = {}) {
  const event = {target: node, preventDefault() { this.prevented = true; }, ...values};
  for (const handler of node._events.get(type) || []) handler(event);
  return event;
}

test('project joins backend shot IDs, exact current media and actual selected direction', () => {
  const input = fixture(), before = JSON.stringify(input), result = Board.projectBoard(input);
  assert.equal(result.directionId, 'B'); assert.equal(result.directionTitle, 'Adopted B');
  assert.equal(result.directionSummary, 'Chosen creative premise');
  assert.equal(result.rows.length, 2); assert.equal(result.rows[0].shotId, 'backend-one');
  assert.equal(result.rows[1].picture, 'Close-up of bag');
  assert.equal(result.rows[0].line, 'First public line');
  assert.equal(result.media.find(row => row.kind === 'image' && row.shotId === 'backend-one').url, url('current shot.png'));
  assert.equal(result.media.find(row => row.kind === 'image' && row.shotId === 'backend-two').url, '');
  assert.equal(result.design.characters[0].label, 'Host（person-a）');
  assert.equal(result.design.scenes[0].label, 'Kitchen（kitchen）');
  assert.deepEqual(result.thumbs.map(row => row.name), ['product.png']);
  assert.equal(JSON.stringify(input), before, 'read-only projection cannot mutate domain data');
});

test('target version and digest must both match displayed writing, without guessed IDs', () => {
  for (const mutate of [target => { target.source_version = 5; }, target => { target.source_sha256 = hash('f'); }]) {
    const input = fixture(); mutate(input.production.targets.image); mutate(input.production.targets.video);
    const result = Board.projectBoard(input);
    assert.equal(result.rows[0].shotId, null); assert.deepEqual(result.media, []);
  }
  const missing = fixture(); missing.production.targets = {};
  assert.equal(Board.projectBoard(missing).rows[1].shotId, null);
});

test('superseded, stale and historical successes never become current media', () => {
  for (const mutate of [
    input => { input.production.drafts[0].is_current = false; },
    input => { input.production.drafts[0].source_ready = false; },
    input => { input.production.drafts[0].source_version = 5; },
    input => { input.production.drafts[0].source_sha256 = hash('f'); },
    input => { input.production.shots.image.shots[0].source_ready = false; },
    input => { input.timeline.rail.at(-1).stale = true; },
    input => { input.timeline.rail.at(-1).integrity_ok = false; },
    input => { input.production.materials[0].sha256 = hash('f'); },
    input => { input.production.drafts[0].attempts[0].output_available = false; },
    input => { input.production.shots.image.shots[0].stage = 'failed'; },
    input => { input.production.drafts[0].attempts.push({status: 'unknown'}); },
    input => { input.production.drafts[0].attempts.push({status: 'failed'}); },
  ]) {
    const input = fixture(); mutate(input);
    assert.equal(Board.projectBoard(input).media.find(row => row.kind === 'image').url, '');
  }
});

test('current five-shot pending plan retains old images as history, without claiming adoption', () => {
  const input = fixture(), writing = input.timeline.rail.at(-1);
  writing.approval_decision = null;
  input.production.drafts[0].source_version = 4;
  input.production.shots.image.shots[0].draft_id = null;
  input.production.shots.image.shots[0].stage = 'unprepared';
  for (const kind of ['image', 'video']) {
    input.production.targets[kind].shots = Array.from({length: 5}, (_, index) =>
      ({ordinal: index + 1, shot_id: `server-${index + 1}`, label: `Synthetic ${index + 1}`}));
    input.production.shots[kind].shots = input.production.targets[kind].shots.map(row =>
      ({...row, draft_id: null, stage: 'unprepared', source_ready: false, stage_label: '尚未准备输入'}));
  }
  const result = Board.projectBoard(input);
  assert.match(result.writingIssue, /尚未采用/); assert.equal(result.media.filter(row => row.kind === 'image').length, 5);
  assert.ok(result.media.every(row => !row.url)); assert.equal(result.historyCount, 1);
});

test('design can use only visual status bound to the displayed material plan', () => {
  const input = fixture();
  input.production.visual_design_status = {version: 5, sha256: hash('b'), ready: true, design: {
    everyday_context: 'US lunch break', localization: 'US kitchen', style: 'Phone camera',
    characters: [{id: 'person-a', label: 'Current host', appearance: 'Current appearance', wardrobe: 'Blue shirt'}],
    scenes: [{id: 'kitchen', label: 'Current scene', location: 'Apartment kitchen'}], continuity: ['Same bag']}};
  const bound = Board.projectBoard(input).design;
  assert.equal(bound.characters[0].label, 'Current host'); assert.equal(bound.scenes[0].id, 'kitchen');
  input.production.visual_design_status.sha256 = hash('f');
  assert.equal(Board.projectBoard(input).design.characters[0].label, 'Host（person-a）');
  input.timeline.rail.find(row => row.stage === 'material_plan').blocks = [];
  const legacy = Board.projectBoard(input).design;
  assert.equal(legacy.available, false); assert.match(legacy.issue, /历史方案没有共同人物与场景设计/);
});

test('compact shared design keeps actual scene labels and complete expandable details', () => {
  const input = fixture(), {host} = dom();
  input.production.visual_design_status = {version: 5, sha256: hash('b'), ready: true, design: {
    everyday_context: 'Full actual context', localization: 'US context', style: 'Phone camera',
    characters: [{id: 'host', label: 'Actual host', relationship: 'Talks to friend', appearance: 'Actual appearance',
      wardrobe: 'Actual wardrobe', performance: 'Full performance'}],
    scenes: ['Entry sidewalk', 'Office break room', 'Grocery aisle'].map((label, index) =>
      ({id: `scene-${index}`, label, location: `${label} location`, layout: 'Complete layout', props: 'Complete props',
        lighting: 'Complete lighting', local_details: 'Complete local details'})), continuity: ['Full first continuity rule', 'Full second continuity rule']}};
  Board.render(host, input, {});
  const scenes = host.querySelectorAll('.creative-board-scene-details');
  assert.equal(scenes.length, 3);
  assert.deepEqual(scenes.map(node => node.querySelector('summary').textContent), ['Entry sidewalk', 'Office break room', 'Grocery aisle']);
  assert.ok(scenes.every(node => !node.hasAttribute('open')));
  assert.ok(scenes.every(node => /Complete local details/.test(node.querySelector('p').textContent)));
  const continuity = host.querySelector('.creative-board-continuity');
  assert.equal(continuity.querySelector('summary').textContent, '跨镜一致性'); assert.equal(continuity.hasAttribute('open'), false);
  assert.match(continuity.textContent, /Full first continuity rule；Full second continuity rule/);
  const character = host.querySelector('.creative-board-character-summary');
  assert.equal(character.textContent, 'Actual appearance · Actual wardrobe'); assert.match(character.title, /Full performance/);
  Board.destroy(host);
});

test('no-person design is explicit and legacy script does not invent a shot list', () => {
  const input = fixture(), material = input.timeline.rail.find(row => row.stage === 'material_plan');
  material.blocks = [{type: 'cards', title: '共同人物与场景设计', cards: [{head: 'US', fields: []}]},
    {type: 'text', title: '人物与服装', text: '本设计不出现人物。'}];
  input.timeline.rail.at(-1).blocks = [];
  const result = Board.projectBoard(input);
  assert.equal(result.design.noCharacters, true); assert.deepEqual(result.rows, []);
});

test('safe media URLs reject external, other project, mismatched name and encoded traversal', () => {
  assert.equal(Board.safeMediaUrl(url('商品 image.png'), 'fixture', '商品 image.png'), url('商品 image.png'));
  for (const value of ['https://provider.invalid/file.png', '/api/runs/other/materials/product.png',
    '/api/runs/fixture/materials/%2e%2e', '/api/runs/fixture/materials/a%2fb.png',
    '/api/runs/fixture/materials/a%5Cb.png', '/api/runs/fixture/materials/%00.png',
    '/api/runs/fixture/materials/a.png?token=x', '/api/runs/fixture/materials/%broken']) {
    assert.equal(Board.safeMediaUrl(value, 'fixture'), '');
  }
  assert.equal(Board.safeMediaUrl(url('a.png'), 'fixture', 'b.png'), '');
});

test('DOM renders three nodes and safe text with accurate reader, shot and history callbacks', () => {
  const {host} = dom(), calls = [], input = fixture();
  input.run.title = '<img src=x onerror=steal()> title';
  input.timeline.rail.at(-1).blocks[1].cards[0].fields[1].value = '<script>steal()</script>';
  Board.render(host, input, {
    openStage: (...args) => calls.push(['stage', ...args]), openMaterials: () => calls.push(['materials']),
    openShot: (...args) => calls.push(['shot', ...args]), openMedia: name => calls.push(['media', name]),
  });
  assert.equal(host.querySelectorAll('[data-board-node]').length, 3);
  assert.match(host.textContent, /<img src=x onerror=steal\(\)> title/);
  assert.equal(host.querySelectorAll('script').length, 0);
  assert.equal(host.querySelectorAll('img').length, 2, 'only actual product and current result images');
  host.querySelector('[data-board-action="writing"]').click();
  host.querySelector('[data-board-action="design"]').click();
  host.querySelector('[data-board-action="shot:image:backend-two"]').click();
  host.querySelectorAll('[data-board-action]').find(node => node.getAttribute('data-board-action') === 'output:current shot.png').click();
  host.querySelector('[data-board-action="history:image"]').click();
  assert.deepEqual(calls, [['stage', 'writing', 6], ['stage', 'material_plan', 5], ['shot', 'image', 'backend-two'],
    ['media', 'current shot.png'], ['shot', 'image', null]]);
  // Read the metadata without a CSS string escape dependency in the small DOM.
  const fieldNodes = host.querySelectorAll('[data-creator-path]');
  const line = fieldNodes.find(node => JSON.stringify(JSON.parse(node.getAttribute('data-creator-path'))) === '["segments",0,"line"]');
  assert.equal(line.textContent, '<script>steal()</script>');
  assert.equal(line.getAttribute('data-creator-target'), '第 1 段台词');
  assert.ok(fieldNodes.some(node => node.getAttribute('data-creator-path') === '["caption"]'));
  assert.ok(host.querySelectorAll('button').every(button => button.type === 'button'));
  Board.destroy(host);
});

test('photo mode has no video placeholders, actions or media', () => {
  const {host} = dom(), input = fixture(); input.format = 'photo';
  const result = Board.projectBoard(input); assert.ok(result.media.every(row => row.kind === 'image'));
  Board.render(host, input, {});
  assert.equal(host.querySelectorAll('[data-media-kind="video"]').length, 0);
  assert.equal(host.querySelector('[data-board-action="history:video"]'), null);
  assert.equal(host.querySelectorAll('video').length, 0);
  assert.match(host.textContent, /逐页画面与文案/); Board.destroy(host);
});

test('unavailable timeline or production data remains readable without crashing', () => {
  const {host} = dom();
  const result = Board.projectBoard({runId: 'fixture', run: null, timeline: null, production: null, materials: null});
  assert.deepEqual(result.rows, []); assert.deepEqual(result.media, []); assert.deepEqual(result.thumbs, []);
  Board.render(host, {runId: 'fixture', production: null, timeline: null}, {});
  assert.equal(host.querySelectorAll('[data-board-node]').length, 3);
  Board.destroy(host);
});

test('analysis mode shows need and report nodes, without plans or generation placeholders', () => {
  const {host} = dom(), input = fixture(), calls = [];
  input.analysisOnly = true; input.referenceTasks = [{reference_id:'analysis-ref',status:'completed',report: 'Synthetic mechanism report'}];
  Board.render(host, input, {openReference: () => calls.push('reference')});
  assert.equal(host.querySelectorAll('[data-board-node]').length, 2);
  assert.equal(host.querySelector('[data-board-node="plan"]'), null);
  assert.equal(host.querySelector('[data-board-node="results"]'), null);
  assert.match(host.textContent, /Synthetic mechanism report/);
  host.querySelector('[data-board-action="reference"]').click(); assert.deepEqual(calls, ['reference']);
  assert.equal(host.querySelectorAll('video').length, 0); Board.destroy(host);
});

test('zoom anchoring is bounded, fit is deterministic and rerender retains pan and zoom', () => {
  assert.deepEqual(Board.zoomView({zoom: 1, x: 10, y: 20}, 1.2, {x: 100, y: 100}), {zoom: 1.2, x: -8, y: 4});
  assert.equal(Board.zoomView({zoom: 1, x: 0, y: 0}, 100, {x: 0, y: 0}).zoom, 1.6);
  assert.equal(Board.zoomView({zoom: 1, x: 0, y: 0}, .001, {x: 0, y: 0}).zoom, .35);
  const {host} = dom(), input = fixture(); const api = Board.render(host, input, {});
  const viewport = host.querySelector('.creative-board-viewport');
  api.zoomBy(1.2); emit(viewport, 'pointerdown', {button: 0, pointerId: 1, clientX: 10, clientY: 20});
  emit(viewport, 'pointermove', {pointerId: 1, clientX: 70, clientY: 90});
  emit(viewport, 'pointerup', {pointerId: 1});
  const moved = api.getView();
  assert.equal(Board.render(host, structuredClone(input), {}), api);
  assert.deepEqual(api.getView(), moved); assert.equal(host.querySelector('.creative-board-viewport'), viewport);
  const other = structuredClone(input); other.runId = 'second-fixture'; Board.render(host, other, {});
  assert.notDeepEqual(api.getView(), moved);
  assert.deepEqual(api.getView(), Board.fitView(1000, 680, {width: 1460, height: 680}));
  Board.destroy(host);
});

test('keyboard zoom and fit work while controls and field interaction do not pan', () => {
  const {host} = dom(), api = Board.render(host, fixture(), {}), viewport = host.querySelector('.creative-board-viewport');
  const before = api.getView(); const key = emit(viewport, 'keydown', {key: '+'});
  assert.ok(key.prevented); assert.ok(api.getView().zoom > before.zoom);
  emit(viewport, 'keydown', {key: 'ArrowRight'}); assert.ok(api.getView().x < before.x);
  emit(viewport, 'keydown', {key: '0'}); assert.deepEqual(api.getView(), before);
  const button = host.querySelector('[data-board-action="writing"]');
  emit(viewport, 'pointerdown', {target: button, button: 0, pointerId: 2, clientX: 0, clientY: 0});
  emit(viewport, 'pointermove', {pointerId: 2, clientX: 100, clientY: 100});
  assert.deepEqual(api.getView(), before);
  const table = host.querySelector('.creative-board-table-scroll');
  assert.equal(emit(viewport, 'wheel', {target: table, deltaX: 0, deltaY: 60}).prevented, undefined);
  assert.deepEqual(api.getView(), before);
  assert.equal(emit(viewport, 'wheel', {target: viewport, deltaX: 10, deltaY: 20}).prevented, true);
  assert.notDeepEqual(api.getView(), before); Board.destroy(host);
});

test('focus plan restores readable zoom in small viewports and fit restores the overview', () => {
  const {host} = dom(); host.clientWidth = 540; host.clientHeight = 600;
  const api = Board.render(host, fixture(), {}), overview = api.getView();
  assert.equal(overview.zoom, .35);
  host.querySelector('[data-board-action="focus-plan"]').click();
  assert.ok(api.getView().zoom >= .7); assert.ok(api.getView().zoom <= 1);
  const view = api.getView(), middle = 420 * view.zoom + view.x;
  assert.equal(middle + 600 * view.zoom / 2, 270, 'middle node is centered');
  host.querySelector('[data-board-action="fit"]').click(); assert.deepEqual(api.getView(), overview);
  const analysis = fixture(); analysis.analysisOnly = true; Board.render(host, analysis, {});
  assert.equal(host.querySelector('[data-board-action="focus-plan"]').textContent, '看报告');
  Board.destroy(host);
});

test('focus plan considers real node height so the first storyboard rows remain in view', () => {
  const {host} = dom(), api = Board.render(host, fixture(), {});
  const plan = host.querySelector('.creative-board-plan'), viewport = host.querySelector('.creative-board-viewport');
  plan.offsetHeight = 830; viewport.clientWidth = 1000; viewport.clientHeight = 580;
  api.focusPlan(); assert.equal(api.getView().zoom, .7, 'tall design does not force a 100% cropped view');
  assert.equal(plan.querySelectorAll('[data-creator-path]').length > 0, true, 'summary display retains exact field targets');
  Board.destroy(host);
});

test('destroy removes listeners and makes detached buttons inert; remount is fresh', () => {
  const {host} = dom(), calls = []; const api = Board.render(host, fixture(), {openMaterials: () => calls.push(true)});
  const viewport = host.querySelector('.creative-board-viewport'), button = host.querySelector('[data-board-action="materials"]');
  const count = [...viewport._events.values()].reduce((total, handlers) => total + handlers.size, 0);
  Board.render(host, fixture(), {});
  assert.equal([...viewport._events.values()].reduce((total, handlers) => total + handlers.size, 0), count);
  api.destroy(); assert.equal(host.children.length, 0);
  assert.ok([...viewport._events.values()].every(handlers => handlers.size === 0));
  button.click(); assert.deepEqual(calls, []);
  assert.notEqual(Board.render(host, fixture(), {}), api); Board.destroy(host);
});

test('rerender preserves focused control and binds it to current callbacks', () => {
  const {host, document} = dom(), calls = []; Board.render(host, fixture(), {});
  const button = host.querySelector('[data-board-action="writing"]'); button.focus();
  Board.render(host, fixture(), {openStage: (...args) => calls.push(args)});
  const current = host.querySelector('[data-board-action="writing"]');
  assert.equal(current, button); assert.equal(document.activeElement, current);
  current.click(); assert.deepEqual(calls, [['writing', 6]]); Board.destroy(host);
});

test('polling identical projections preserves video playback, scroll, selection nodes and current callbacks', () => {
  const {host, document} = dom(), input = fixture(), calls = [];
  const output = {name: 'current.mp4', sha256: hash('f'), url: url('current.mp4')};
  input.production.materials.push({...output, kind: 'video'});
  input.production.drafts.push({draft_id: 'current-video', shot_id: 'backend-one', kind: 'video', source_version: 6,
    source_sha256: hash('a'), source_ready: true, is_current: true,
    attempts: [{status: 'succeeded', output_available: true, output}]});
  input.production.shots.video.shots[0] = {ordinal: 1, shot_id: 'backend-one', draft_id: 'current-video',
    stage: 'completed', stage_label: '已生成', source_ready: true, output};
  const api = Board.render(host, input, {}), projection = api.getProjection();
  const video = host.querySelector('video'), table = host.querySelector('.creative-board-table-scroll');
  const results = host.querySelector('.creative-board-result-scroll');
  const selected = host.querySelector('[data-creator-target]'); selected.focus();
  video.currentTime = 2.5; video.paused = false; table.scrollTop = 77; results.scrollTop = 90;
  document.selection = {anchorNode: selected, anchorOffset: 4};
  Board.render(host, structuredClone(input), {openStage: (...args) => calls.push(args)});
  assert.equal(api.getProjection(), projection); assert.equal(host.querySelector('video'), video);
  assert.equal(video.currentTime, 2.5); assert.equal(video.paused, false);
  assert.equal(host.querySelector('.creative-board-table-scroll'), table); assert.equal(table.scrollTop, 77);
  assert.equal(results.scrollTop, 90); assert.equal(document.activeElement, selected);
  assert.ok(host.contains(document.selection.anchorNode));
  host.querySelector('[data-board-action="writing"]').click(); assert.deepEqual(calls, [['writing', 6]]);
  Board.destroy(host);
});

test('changed projection preserves same-script table/results scroll, and a new script resets it', () => {
  const {host} = dom(), input = fixture(), calls = [];
  Board.render(host, input, {openStage: (...args) => calls.push(args)});
  const oldButton = host.querySelector('[data-board-action="writing"]');
  host.querySelector('.creative-board-table-scroll').scrollTop = 77;
  host.querySelector('.creative-board-result-scroll').scrollTop = 90;
  input.production.shots.image.shots[1].stage = 'generating';
  Board.render(host, input, {openStage: (...args) => calls.push(args)});
  assert.equal(host.querySelector('.creative-board-table-scroll').scrollTop, 77);
  assert.equal(host.querySelector('.creative-board-result-scroll').scrollTop, 90);
  oldButton.click(); assert.deepEqual(calls, [], 'detached old controls cannot read a newer object');
  input.timeline.rail.at(-1).version = 7; input.timeline.rail.at(-1).artifact_sha256 = hash('f');
  Board.render(host, input, {});
  assert.equal(host.querySelector('.creative-board-table-scroll').scrollTop, 0);
  assert.equal(host.querySelector('.creative-board-result-scroll').scrollTop, 0);
  Board.destroy(host);
});

test('mobile stacked layout uses native scrolling and focus-plan navigation', () => {
  const prior = globalThis.matchMedia;
  globalThis.matchMedia = () => ({matches: true});
  try {
    const {host} = dom(), api = Board.render(host, fixture(), {});
    const viewport = host.querySelector('.creative-board-viewport'), plan = host.querySelector('.creative-board-plan');
    let focused = 0; plan.scrollIntoView = () => { focused++; };
    host.querySelector('[data-board-action="focus-plan"]').click(); assert.equal(focused, 1);
    viewport.scrollTop = 300; api.fit(); assert.equal(viewport.scrollTop, 0);
    assert.equal(emit(viewport, 'wheel', {deltaX: 0, deltaY: 40}).prevented, undefined);
    assert.equal(emit(viewport, 'pointerdown', {button: 0, pointerId: 1, clientX: 0, clientY: 0}).prevented, undefined);
    Board.destroy(host);
  } finally { if (prior === undefined) delete globalThis.matchMedia; else globalThis.matchMedia = prior; }
});
