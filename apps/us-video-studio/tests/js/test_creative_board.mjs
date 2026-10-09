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

function dom({planHeight = 0, focusEvents = false} = {}) {
  const {document} = makeDom();
  const original = document.createElement.bind(document);
  let host;
  const enhance = node => {
    node.ownerDocument = document;
    node._events = new Map();
    node.addEventListener = (type, fn) => { if (!node._events.has(type)) node._events.set(type, new Set()); node._events.get(type).add(fn); };
    node.removeEventListener = (type, fn) => node._events.get(type)?.delete(fn);
    node.focus = () => { document.activeElement = node;
      if (focusEvents) for (const handler of node.closest('.creative-board-viewport')?._events.get('focusin') || []) handler({target:node}); };
    node.getBoundingClientRect = () => ({left: 0, top: 0, width: node.clientWidth || 1000, height: node.clientHeight || 680});
    node.setPointerCapture = id => { node._capture = id; };
    node.hasPointerCapture = id => node._capture === id;
    node.releasePointerCapture = () => { node._capture = null; };
    node.showModal = () => { node.open = true; };
    node.close = () => { node.open = false; node.onclose?.(); };
    // Only measured, visible viewport dimensions may initialize the default.
    for (const name of ['clientWidth', 'clientHeight']) {
      let measured;
      Object.defineProperty(node, name, {configurable:true,
        get:() => measured ?? (node.classList.contains('creative-board-viewport')
          ? (host?.hidden || node.hidden ? 0 : host?.[name] || 0) : 0),
        set:value => { measured = value; }});
    }
    let height;
    Object.defineProperty(node, 'offsetHeight', {configurable:true,
      get:() => height ?? (node.getAttribute('data-board-node') === 'plan' ? planHeight : 0),
      set:value => {height = value;}});
    const closest = node.closest.bind(node);
    node.closest = selector => selector.split(',').map(part => closest(part.trim())).find(Boolean) || null;
    const replaceChildren = node.replaceChildren.bind(node);
    node.replaceChildren = (...children) => { for (const child of node.children) child.parentNode = null; replaceChildren(...children); };
    return node;
  };
  document.createElement = tag => enhance(original(tag));
  document.createElementNS = (_ns, tag) => document.createElement(tag);
  host = document.createElement('section'); host.clientWidth = 1000; host.clientHeight = 680;
  document.body.append(host);
  return {document, host};
}

function emit(node, type, values = {}) {
  const event = {target: node, preventDefault() { this.prevented = true; }, ...values};
  for (const handler of node._events.get(type) || []) handler(event);
  return event;
}

function localStorageFixture(run) {
  const previous = globalThis.localStorage, values = new Map();
  globalThis.localStorage = {getItem: key => values.get(key) || null,
    setItem: (key, value) => values.set(key, String(value)), removeItem: key => values.delete(key)};
  try { return run(values); }
  finally { if (previous === undefined) delete globalThis.localStorage; else globalThis.localStorage = previous; }
}

function resizeFixture(run) {
  const previous = globalThis.ResizeObserver, observers = [];
  globalThis.ResizeObserver = class {
    constructor(callback) { this.callback = callback; observers.push(this); }
    observe(target) { this.target = target; }
    disconnect() { this.disconnected = true; }
    trigger() { this.callback([{target:this.target}]); }
  };
  try { return run(observers); }
  finally { if (previous === undefined) delete globalThis.ResizeObserver; else globalThis.ResizeObserver = previous; }
}

test('first open with six pictures and a tall plan uses a readable 100 percent top anchor', () => localStorageFixture(values => resizeFixture(observers => {
  const {host} = dom({planHeight:1800}); host.clientWidth = 750; host.clientHeight = 560;
  const input = fixture();
  input.production.drafts = []; input.production.materials = [];
  input.materials = Array.from({length:6}, (_,index) => ({name:`source-${index + 1}.png`,
    kind:'image',sha256:hash(String(index + 1)),url:url(`source-${index + 1}.png`)}));
  const api = Board.render(host,input,{});
  assert.equal(api.getProjection().images.length,6);
  assert.deepEqual(api.getView(),{zoom:1,x:-345,y:-8});
  assert.equal(420 + api.getView().x,75,'the actual plan is horizontally centered');
  assert.equal(32 + api.getView().y,24,'the top begins within the readable viewport');
  assert.equal(values.size,0,'the automatic default does not create a saved viewport');
  observers[0].trigger();
  assert.deepEqual(api.getView(),{zoom:1,x:-345,y:-8}); assert.equal(values.size,0);
  const handle = host.querySelector('[data-image-drag]'), card = handle.closest('[data-board-image]');
  const left = parseFloat(card.style.left);
  emit(host.querySelector('.creative-board-viewport'),'keydown',{target:handle,key:'ArrowRight'});
  assert.equal(parseFloat(card.style.left),left + 10,'image interaction keeps world coordinates');
  host.querySelector('[data-board-action="fit"]').click();
  assert.equal(api.getView().zoom,.35,'the explicitly requested whole-board overview stays available');
  assert.deepEqual(JSON.parse(values.get('studio-creative-board-layout-v1:fixture')).view,api.getView());
  Board.destroy(host);
})));

test('first open without saved material or script anchors the real need instead of empty plan templates', () => {
  for (const mode of ['no-artifacts','direction-only','unsaved-templates']) {
    const {host} = dom(); host.clientWidth = 750;
    const input = fixture();
    input.timeline.rail = mode === 'no-artifacts' ? [] : input.timeline.rail.filter(row => row.stage === 'direction');
    if (mode === 'unsaved-templates') input.timeline.rail.push({stage:'material_plan',has_doc:false},
      {stage:'writing',has_doc:false});
    const api = Board.render(host,input,{});
    assert.deepEqual(api.getView(),{zoom:1,x:210,y:-8},mode);
    assert.equal(api.getProjection().material,null); assert.equal(api.getProjection().writing,null);
    Board.destroy(host);
  }
});

test('either actual material plan or script is enough to anchor the saved plan node', () => {
  for (const stageName of ['material_plan','writing']) {
    const {host} = dom(); host.clientWidth = 750;
    const input = fixture(); input.timeline.rail = input.timeline.rail.filter(row => row.stage === stageName);
    const api = Board.render(host,input,{});
    assert.deepEqual(api.getView(),{zoom:1,x:-345,y:-8},stageName);
    Board.destroy(host);
  }
});

test('analysis anchors only a real report and otherwise begins with the need', () => {
  for (const report of ['', '   ', 'Offline saved analysis']) {
    const {host} = dom(); host.clientWidth = 750;
    const input = fixture(); input.analysisOnly = true;
    input.referenceTasks = [{reference_id:'reference-fixture',name:'reference.mp4',report,
      execution_status:report.trim() ? 'succeeded' : 'running'}];
    const api = Board.render(host,input,{});
    assert.deepEqual(api.getView(),{zoom:1,x:report.trim() ? -345 : 210,y:-8});
    assert.equal(host.querySelector('.creative-board-plan'),null);
    Board.destroy(host);
  }
});

test('hidden first viewport waits for measured visibility and initializes exactly once without saving', () => localStorageFixture(values => resizeFixture(observers => {
  const {host} = dom(); host.hidden = true;
  const api = Board.render(host,fixture(),{});
  assert.deepEqual(api.getView(),{zoom:1,x:32,y:32},'hidden geometry is not the 1000px size fallback');
  observers[0].trigger(); assert.equal(values.size,0);
  host.hidden = false; host.clientWidth = 750; host.clientHeight = 560;
  observers[0].trigger(); assert.deepEqual(api.getView(),{zoom:1,x:-345,y:-8});
  host.clientWidth = 1100; host.clientHeight = 900;
  observers[0].trigger(); assert.deepEqual(api.getView(),{zoom:1,x:-345,y:-8});
  assert.equal(values.size,0); Board.destroy(host);
  observers[0].trigger(); assert.equal(values.size,0,'destroyed controller cannot persist a late observation');
})));

test('zero viewport can initialize on an identical data update once dimensions become real', () => {
  const {host} = dom(); host.clientWidth = 0; host.clientHeight = 0;
  const input = fixture(), api = Board.render(host,input,{});
  assert.deepEqual(api.getView(),{zoom:1,x:32,y:32});
  host.clientWidth = 750; host.clientHeight = 560;
  assert.equal(Board.render(host,structuredClone(input),{}),api);
  assert.deepEqual(api.getView(),{zoom:1,x:-345,y:-8});
  Board.destroy(host);
});

test('subsequent saved plan data and resize never move the initial need view', () => localStorageFixture(values => resizeFixture(observers => {
  const {host} = dom(); host.clientWidth = 750;
  const input = fixture(), pending = structuredClone(input); pending.timeline.rail = [];
  const api = Board.render(host,pending,{}), initial = api.getView();
  assert.deepEqual(initial,{zoom:1,x:210,y:-8});
  Board.render(host,input,{}); observers[0].trigger();
  assert.deepEqual(api.getView(),initial); assert.equal(values.size,0);
  Board.destroy(host);
})));

test('saved content refresh restores focused controls without panning or persisting an automatic view', () => localStorageFixture(values => {
  const {host,document} = dom({focusEvents:true}), input = fixture();
  const create = document.createElement.bind(document);
  document.createElement = tag => {
    const node = create(tag);
    if (tag === 'button') node.getBoundingClientRect = () => ({left:980,top:730,right:1160,bottom:765});
    return node;
  };
  const api = Board.render(host,input,{}), viewport = host.querySelector('.creative-board-viewport');
  viewport.getBoundingClientRect = () => ({left:0,top:0,right:750,bottom:560});
  document.activeElement = host.querySelector('[data-board-action="writing"]');
  api.zoomBy(1.2);
  const chosen = api.getView(), key = 'studio-creative-board-layout-v1:fixture', saved = values.get(key);
  Board.render(host,{...input,run:{...input.run,title:'New saved content'}},{});
  assert.equal(document.activeElement,host.querySelector('[data-board-action="writing"]'));
  assert.deepEqual(api.getView(),chosen); assert.equal(values.get(key),saved);
  host.querySelector('[data-image-drag]').focus();
  assert.notDeepEqual(api.getView(),chosen,'actual keyboard focus still brings its target into view');
  Board.destroy(host);
}));

test('existing legal 35 and 100 percent views and moved images restore byte-for-byte without migration', () => localStorageFixture(values => resizeFixture(observers => {
  for (const zoom of [.35,1]) {
    const input = fixture(), key = 'studio-creative-board-layout-v1:fixture';
    const imageKey = Board.projectBoard(input).images[0].key;
    const saved = JSON.stringify({version:1,view:{zoom,x:-870,y:145},positions:{[imageKey]:{x:-150,y:730}}});
    values.set(key,saved);
    const {host} = dom(); host.hidden = true;
    const api = Board.render(host,input,{});
    assert.deepEqual(api.getView(),{zoom,x:-870,y:145});
    assert.equal(host.querySelector('[data-board-image]').style.left,'-150px');
    assert.equal(host.querySelector('[data-board-image]').style.top,'730px');
    host.hidden = false; host.clientWidth = 750; observers.at(-1).trigger();
    Board.render(host,{...input,run:{...input.run,title:'Refreshed title'}},{});
    assert.deepEqual(api.getView(),{zoom,x:-870,y:145}); assert.equal(values.get(key),saved);
    Board.destroy(host);
  }
})));

test('invalid stored viewport preserves independently valid image placement while default remains unsaved', () => localStorageFixture(values => {
  const input = fixture(), imageKey = Board.projectBoard(input).images[0].key;
  const key = 'studio-creative-board-layout-v1:fixture';
  const saved = JSON.stringify({version:1,view:{zoom:0,x:0,y:0},positions:{[imageKey]:{x:82,y:901}}});
  values.set(key,saved);
  const {host} = dom(); host.clientWidth = 750;
  const api = Board.render(host,input,{});
  assert.deepEqual(api.getView(),{zoom:1,x:-345,y:-8});
  assert.equal(host.querySelector('[data-board-image]').style.left,'82px');
  assert.equal(host.querySelector('[data-board-image]').style.top,'901px');
  assert.equal(values.get(key),saved); Board.destroy(host);
}));

test('manual view interaction before deferred measurement is never overwritten by default anchoring', () => localStorageFixture(values => resizeFixture(observers => {
  const {host} = dom(); host.clientHeight = 0;
  const api = Board.render(host,fixture(),{}); api.zoomBy(1.2);
  const chosen = api.getView(); host.clientHeight = 680; observers[0].trigger();
  assert.deepEqual(api.getView(),chosen);
  assert.deepEqual(JSON.parse(values.get('studio-creative-board-layout-v1:fixture')).view,chosen);
  Board.destroy(host);
})));

test('mobile first reading keeps native vertical position and never initializes or persists desktop panning', () => localStorageFixture(values => resizeFixture(observers => {
  const previous = globalThis.matchMedia; globalThis.matchMedia = () => ({matches:true});
  try {
    const {host} = dom(); host.clientWidth = 390; host.clientHeight = 700;
    const input = fixture(), api = Board.render(host,input,{});
    const viewport = host.querySelector('.creative-board-viewport'); viewport.scrollTop = 230;
    observers[0].trigger(); Board.render(host,{...input,run:{...input.run,title:'Mobile refresh'}},{});
    assert.equal(viewport.scrollTop,230); assert.equal(values.size,0);
    assert.deepEqual(api.getView(),{zoom:1,x:32,y:32});
    assert.equal(host.querySelector('.creative-board-world').querySelector('[data-board-node="brief"]') !== null,true);
    Board.destroy(host);
  } finally { if (previous === undefined) delete globalThis.matchMedia; else globalThis.matchMedia = previous; }
})));

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

test('legacy design gaps show verified images with original version without restoring executable coverage', () => {
  const input = fixture(), draft = input.production.drafts[0], row = input.production.shots.image.shots[0];
  draft.source_ready = false; draft.design_issue = '历史输入未绑定共同人物与场景设计，不能继续执行。';
  row.source_ready = false; row.stage = 'unprepared'; row.stage_label = '需要补齐设计';
  const before = JSON.stringify(input), projected = Board.projectBoard(input);
  assert.equal(projected.media[0].url, '');
  const image = projected.images.find(item => item.name === 'current shot.png');
  assert.equal(image.historical, true); assert.match(image.title, /原输入结果.*脚本第 6 版.*镜头 1/);
  assert.match(image.issue, /未绑定共同人物与场景设计/);
  assert.equal(JSON.stringify(input), before);
  const {host} = dom(); Board.render(host, input, {});
  assert.match(host.querySelector('.is-historical').textContent, /脚本第 6 版.*不能继续执行/s);
  assert.equal(host.querySelector('.is-historical').querySelector('img').src, url('current shot.png'));
  Board.destroy(host);
});

test('superseded and previous successful bytes stay historical while unknown attempts remain unknown', () => {
  const input = fixture(), draft = input.production.drafts[0];
  draft.is_current = false; draft.source_version = 4;
  draft.attempts.push({attempt_id: 'unresolved', status: 'unverified'});
  const projected = Board.projectBoard(input), historical = projected.images.find(item => item.name === 'current shot.png');
  assert.equal(projected.media[0].url, ''); assert.equal(historical.historical, true);
  assert.match(historical.title, /脚本第 4 版.*原镜头 backend-one/);
  assert.match(historical.issue, /上一份已生成结果.*结果待核实.*已被替换/);
  assert.doesNotMatch(historical.issue, /当前尝试已生成/);
  for (const mutate of [value => { value.production.drafts[0].attempts[0].output_available = false; },
    value => { value.production.materials[0].sha256 = hash('f'); },
    value => { value.production.materials[0].url = 'https://unapproved.invalid/picture.png'; }]) {
    const invalid = structuredClone(input); mutate(invalid);
    assert.equal(Board.projectBoard(invalid).images.some(item => item.name === 'current shot.png'), false);
  }
});

test('an unknown submission with a retained verified image stays unconfirmed and read-only', () => {
  const input = fixture(), draft = input.production.drafts[0], row = input.production.shots.image.shots[0];
  draft.source_ready = false; draft.design_issue = '共同设计缺失'; draft.attempts[0].status = 'unknown';
  row.source_ready = false; row.stage = 'unverified'; row.stage_label = '提交结果未确认';
  const before = JSON.stringify(input), projected = Board.projectBoard(input);
  assert.equal(projected.media[0].url, '');
  const image = projected.images.find(item => item.name === 'current shot.png');
  assert.equal(image.unconfirmed, true); assert.equal(image.historical, true);
  assert.match(image.title, /保留文件.*提交结果未确认/); assert.match(image.issue, /不能作为已完成结果/);
  assert.equal(JSON.stringify(input), before);
  const {host} = dom(); Board.render(host, input, {});
  assert.match(host.querySelector('.is-historical').textContent, /保留文件.*提交结果未确认/s);
  assert.equal(host.querySelector('.is-historical').querySelector('img').src, url('current shot.png'));
  assert.ok(host.querySelector('.is-historical').querySelectorAll('button').every(button => /移动|查看/.test(button.textContent || button.getAttribute('aria-label'))));
  Board.destroy(host);
});

test('the superseded picture identifies its previous input when the replacement has no generated output', () => {
  const input = fixture(), old = input.production.drafts[0]; old.is_current = false;
  input.production.drafts.push({...old, draft_id: 'revised-image', is_current: true, attempts: [], source_ready: false});
  input.production.shots.image.shots[0] = {...input.production.shots.image.shots[0], draft_id: 'revised-image',
    stage: 'unprepared', source_ready: false, output: null};
  const image = Board.projectBoard(input).images.find(item => item.name === 'current shot.png');
  assert.match(image.title, /上一版图片.*脚本第 6 版.*镜头 1/);
  assert.match(image.issue, /上一版（修改后尚未重新生成）/);
});

test('image handles drag independently in world coordinates and preview clicks cannot start a drag', () => localStorageFixture(() => {
  const {host} = dom(), calls = [], api = Board.render(host, fixture(), {openMedia: name => calls.push(name)});
  const viewport = host.querySelector('.creative-board-viewport'), cards = host.querySelectorAll('[data-board-image]');
  const card = cards[0], other = cards[1], handle = card.querySelector('[data-image-drag]');
  const before = {...api.getView()}, x = parseFloat(card.style.left), y = parseFloat(card.style.top), otherLeft = other.style.left;
  assert.equal(emit(viewport, 'pointerdown', {target: handle, button: 0, pointerId: 31, clientX: 20, clientY: 20}).prevented, true);
  emit(viewport, 'pointermove', {pointerId: 31, clientX: 20 + before.zoom * 60, clientY: 20 + before.zoom * 40});
  emit(viewport, 'pointerup', {pointerId: 31});
  assert.equal(parseFloat(card.style.left), x + 60); assert.equal(parseFloat(card.style.top), y + 40);
  assert.deepEqual(api.getView(), before); assert.equal(other.style.left, otherLeft);
  const preview = card.querySelector('.creative-board-output'), moved = card.style.left;
  emit(viewport, 'pointerdown', {target: preview, button: 0, pointerId: 32, clientX: 0, clientY: 0});
  emit(viewport, 'pointermove', {pointerId: 32, clientX: 80, clientY: 80}); preview.click();
  assert.equal(card.style.left, moved); assert.deepEqual(api.getView(), before);
  assert.deepEqual(calls, ['current shot.png']);
  assert.equal(emit(viewport, 'keydown', {target: handle, key: 'ArrowLeft'}).prevented, true);
  assert.equal(parseFloat(card.style.left), x + 50);
  emit(viewport, 'keydown', {target: handle, key: 'ArrowDown', shiftKey: true});
  assert.equal(parseFloat(card.style.top), y + 140); Board.destroy(host);
}));

test('browser-local positions and view survive reload and isolate reset to the current project', () => localStorageFixture(values => {
  const input = fixture(), {host} = dom(); let api = Board.render(host, input, {});
  const viewport = host.querySelector('.creative-board-viewport'), handle = host.querySelector('[data-image-drag]');
  emit(viewport, 'keydown', {target: handle, key: 'ArrowRight', shiftKey: true}); api.zoomBy(1.2);
  const key = 'studio-creative-board-layout-v1:fixture', saved = values.get(key), view = api.getView();
  const left = host.querySelector('[data-board-image]').style.left;
  assert.ok(saved); values.set('studio-creative-board-layout-v1:other-project', 'unrelated');
  Board.destroy(host); api = Board.render(host, structuredClone(input), {});
  assert.equal(host.querySelector('[data-board-image]').style.left, left); assert.deepEqual(api.getView(), view);
  input.run.title = 'Changed displayed text'; Board.render(host, input, {});
  assert.equal(host.querySelector('[data-board-image]').style.left, left);
  const second = fixture(); second.runId = 'other-project'; second.run.run_id = second.runId;
  second.materials = second.materials.map(item => ({...item, url: item.url.replace('/fixture/', '/other-project/')}));
  second.production.materials = second.production.materials.map(item => ({...item, url: item.url.replace('/fixture/', '/other-project/')}));
  Board.render(host, second, {}); assert.notEqual(host.querySelector('[data-board-image]').style.left, left);
  Board.render(host, input, {}); assert.equal(host.querySelector('[data-board-image]').style.left, left);
  host.querySelector('[data-board-action="reset-layout"]').click();
  assert.equal(host.querySelector('[data-board-image]').style.left, '1510px');
  assert.equal(values.has(key), false); assert.equal(values.get('studio-creative-board-layout-v1:other-project'), 'unrelated');
  assert.match(host.textContent, /已重置本项目的本机布局/); Board.destroy(host);
}));

test('invalid or unavailable local storage does not block images and reports persistence failure', () => localStorageFixture(values => {
  values.set('studio-creative-board-layout-v1:fixture', JSON.stringify({version: 1,
    view: {x: 1e200, y: 0, zoom: 90}, positions: {['current shot.png:' + hash('d')]: {x: 'bad', y: 0}}}));
  const {host} = dom(), api = Board.render(host, fixture(), {});
  assert.equal(host.querySelector('[data-board-image]').style.left, '1510px');
  assert.ok(Number.isFinite(api.getView().x)); assert.ok(api.getView().zoom <= 1.6);
  globalThis.localStorage.setItem = () => { throw Error('quota'); };
  api.zoomBy(1.2); assert.match(host.textContent, /布局未保存/);
  assert.ok(host.querySelector('[data-board-image]').querySelector('img'));
  Board.destroy(host);
}));

test('fit includes independently placed cards to the left and above the original nodes', () => localStorageFixture(values => {
  values.set('studio-creative-board-layout-v1:fixture', JSON.stringify({version: 1,
    positions: {['current shot.png:' + hash('d')]: {x: -400, y: -300}}}));
  const {host} = dom(), api = Board.render(host, fixture(), {}), card = host.querySelector('[data-board-image]');
  assert.equal(card.style.left, '-400px'); assert.equal(card.style.top, '-300px');
  api.fit(); const view = api.getView();
  assert.ok(-400 * view.zoom + view.x >= 0); assert.ok(-300 * view.zoom + view.y >= 0);
  Board.destroy(host);
}));

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
  assert.equal(host.querySelectorAll('[data-board-image]').length, 3, 'actual source, reference and current result images are movable');
  assert.equal(host.querySelectorAll('img').length, 4, 'movable cards plus the compact product shelf');
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
  assert.match(host.textContent, /逐页画面与文案/);
  assert.match(host.textContent, /按本版页码对应展示/);
  assert.match(host.textContent, /查看这一页/);
  assert.doesNotMatch(host.textContent, /镜头|脚本|逐镜|口播|查看这一镜/);
  assert.match(host.textContent, /第 1 页 · 图文方案第 6 版/);
  assert.ok(host.querySelector('[aria-label="当前图文的图片结果"]'));
  Board.destroy(host);
});

test('an unbound delivery stays neutral and follows the later trusted format', () => {
  const {host} = dom(), input = fixture(), calls = [];
  input.run.content_format = null;
  // The request itself mentions both media types. The board must not guess.
  input.run.brief = 'Create three photos, not a video';
  input.timeline.rail = [];
  input.production = {};
  const original = structuredClone(input);
  const api = Board.render(host, input, {openShot: (...args) => calls.push(args)});
  assert.match(host.textContent, /创作方案|画面与文字|作品结果/);
  assert.equal(host.querySelector('[data-board-action="focus-plan"]').textContent, '看方案');
  assert.equal(host.querySelector('[data-board-action="history:video"]'), null);
  assert.doesNotMatch(host.textContent, /分镜|镜头|口播|视频记录/);
  assert.deepEqual(calls, []);
  assert.deepEqual(input, original, 'reading does not bind a format or modify the request');
  api.update({...input, format:'photo'}, {});
  assert.match(host.textContent, /图文方案|按本版页码对应展示/);
  assert.equal(host.querySelector('[data-board-action="history:video"]'), null);
  api.update({...input, format:'video'}, {});
  assert.equal(host.querySelector('[data-board-action="focus-plan"]').textContent, '看分镜');
  assert.ok(host.querySelector('[data-board-action="history:video"]'));
  Board.destroy(host);
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
  assert.deepEqual(api.getView(), {zoom: 1, x: -220, y: -8});
  Board.destroy(host);
});

test('keyboard zoom and fit work while controls and field interaction do not pan', () => {
  const {host} = dom(), api = Board.render(host, fixture(), {}), viewport = host.querySelector('.creative-board-viewport');
  const before = api.getView(); const key = emit(viewport, 'keydown', {key: '+'});
  assert.ok(key.prevented); assert.ok(api.getView().zoom > before.zoom);
  emit(viewport, 'keydown', {key: 'ArrowRight'}); assert.ok(api.getView().x < before.x);
  emit(viewport, 'keydown', {key: '0'});
  const fitted = api.getView();
  assert.ok(fitted.zoom < before.zoom, 'explicit fit keeps the whole-board overview');
  const button = host.querySelector('[data-board-action="writing"]');
  emit(viewport, 'pointerdown', {target: button, button: 0, pointerId: 2, clientX: 0, clientY: 0});
  emit(viewport, 'pointermove', {pointerId: 2, clientX: 100, clientY: 100});
  assert.deepEqual(api.getView(), fitted);
  const table = host.querySelector('.creative-board-table-scroll');
  assert.equal(emit(viewport, 'wheel', {target: table, deltaX: 0, deltaY: 60}).prevented, undefined);
  assert.deepEqual(api.getView(), fitted);
  assert.equal(emit(viewport, 'wheel', {target: viewport, deltaX: 10, deltaY: 20}).prevented, true);
  assert.notDeepEqual(api.getView(), fitted); Board.destroy(host);
});

test('focus plan restores readable zoom in small viewports and fit restores the overview', () => {
  const {host} = dom(); host.clientWidth = 540; host.clientHeight = 600;
  const api = Board.render(host, fixture(), {});
  assert.equal(api.getView().zoom, 1);
  host.querySelector('[data-board-action="fit"]').click();
  const overview = api.getView();
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


test('offscreen keyboard focus pans the saved view without moving cards or opening work', () => localStorageFixture(() => {
  const {host} = dom(), calls = [], api = Board.render(host, fixture(), {openMedia: name => calls.push(name)});
  const viewport = host.querySelector('.creative-board-viewport'), card = host.querySelector('[data-board-image]');
  const handle = card.querySelector('[data-image-drag]'), before = api.getView(), left = card.style.left;
  viewport.getBoundingClientRect = () => ({left: 0, top: 0, right: 800, bottom: 600});
  handle.getBoundingClientRect = () => ({left: 920, top: 620, right: 1120, bottom: 655});
  emit(viewport, 'focusin', {target: handle});
  const after = api.getView();
  assert.equal(after.zoom, before.zoom);
  assert.ok(1120 + after.x - before.x <= 784);
  assert.ok(655 + after.y - before.y <= 584);
  assert.equal(card.style.left, left); assert.deepEqual(calls, []);
  handle.getBoundingClientRect = () => ({left: 80, top: 50, right: 280, bottom: 85});
  emit(viewport, 'focusin', {target: handle});
  assert.deepEqual(api.getView(), after, 'already visible focus must not disturb browsing');
  Board.destroy(host);
}));

test('pointer dragging and mobile native focus do not invoke keyboard view correction', () => {
  const {host} = dom(), api = Board.render(host, fixture(), {});
  const viewport = host.querySelector('.creative-board-viewport'), handle = host.querySelector('[data-image-drag]');
  viewport.getBoundingClientRect = () => ({left: 0, top: 0, right: 800, bottom: 600});
  handle.getBoundingClientRect = () => ({left: 920, top: 620, right: 1120, bottom: 655});
  const before = api.getView();
  emit(viewport, 'pointerdown', {target: handle, button: 0, pointerId: 33, clientX: 920, clientY: 620});
  emit(viewport, 'focusin', {target: handle});
  assert.deepEqual(api.getView(), before);
  emit(viewport, 'pointerup', {pointerId: 33});
  const prior = globalThis.matchMedia;
  globalThis.matchMedia = () => ({matches: true});
  try { emit(viewport, 'focusin', {target: handle}); assert.deepEqual(api.getView(), before); }
  finally { if (prior === undefined) delete globalThis.matchMedia; else globalThis.matchMedia = prior; }
  Board.destroy(host);
});


function imageAnalysisInput({status='complete',report='Synthetic complete image report',analysisOnly=true}={}) {
  const input=fixture();input.timeline.rail=[];
  input.production={drafts:[],materials:[],targets:{},shots:{}};
  input.materials=[{name:'source-picture.png',kind:'image',sha256:hash('1'),url:url('source-picture.png')}];
  input.timeline.brief={goal:'图片分析',analysis:{kind:'image',analysis_only:analysisOnly,goal:'图片分析',
    status,job_id:'image-only-job',request_id:'exact-request',source_message_id:'exact-message',
    turn_id:'exact-turn',interpretation_id:'exact-interpretation',total:2,
    observed:status==='complete'?2:1,missing:status==='complete'?[]:['second-source.png'],
    title:status==='complete'?'图片分析已完成':'图片分析尚未完成',
    detail:status==='complete'?'已查看本轮两张准确来源图片。':'已保存一张观察，第二张尚未完成。',report}};
  input.referenceTasks=[{reference_id:'older-video-reference',report:'UNRELATED video report'}];
  return input;
}

test('server-bound image-only analysis shows its saved report and sources without empty creative or reference nodes',()=>{
  const {host}=dom(),input=imageAnalysisInput(),before=JSON.stringify(input),calls=[];
  const api=Board.render(host,input,{openReference:()=>calls.push('read-image-report')});
  const data=api.getProjection();
  assert.equal(data.analysisOnly,true);assert.equal(data.imageAnalysis.kind,'image');
  assert.equal(data.reference,null);assert.equal(data.analysis,'Synthetic complete image report');
  assert.equal(host.querySelectorAll('[data-board-node]').length,2);
  assert.equal(host.querySelector('[data-board-node="plan"]'),null);
  assert.equal(host.querySelector('[data-board-node="results"]'),null);
  assert.match(host.textContent,/图片分析报告/);
  assert.match(host.textContent,/图片分析已完成/);
  assert.doesNotMatch(host.textContent,/共同人物与场景设计|方向尚未|参考的表达|UNRELATED/);
  assert.equal(host.querySelector('[data-board-action="reference"]'),null);
  host.querySelector('[data-board-action="image-analysis"]').click();
  assert.deepEqual(calls,['read-image-report']);
  assert.equal(JSON.stringify(input),before);
  Board.destroy(host);
});

for(const status of ['incomplete','working','stopped'])test(`image-only ${status} uses the exact server status instead of claiming a report or creation readiness`,()=>{
  const {host}=dom(),input=imageAnalysisInput({status,report:''});
  Board.render(host,input,{});
  assert.match(host.textContent,/图片分析尚未完成/);
  assert.match(host.textContent,/已保存一张观察，第二张尚未完成/);
  assert.doesNotMatch(host.textContent,/图片分析已完成|完整方案|参考视频|原片/);
  assert.equal(host.querySelector('[data-board-node="plan"]'),null);
  assert.equal(host.querySelectorAll('video').length,0);
  Board.destroy(host);
});

test('image report polling keeps the browser view and dragged source while safely preserving the complete model text',()=>{
  const {host}=dom(),input=imageAnalysisInput({report:'<script>unsafe()</script>\nSaved observation'});
  const api=Board.render(host,input,{}),view=api.getView();
  const image=host.querySelector('[data-board-image]'),left=image.style.left,top=image.style.top;
  assert.match(host.textContent,/<script>unsafe\(\)<\/script>/);
  assert.equal(host.querySelectorAll('script').length,0);
  input.timeline.brief.analysis.detail='Exact status refreshed';
  Board.render(host,input,{});
  assert.deepEqual(api.getView(),view);
  assert.equal(host.querySelector('[data-board-image]').style.left,left);
  assert.equal(host.querySelector('[data-board-image]').style.top,top);
  assert.match(host.textContent,/Saved observation/);
  Board.destroy(host);
});

test('image-looking prose and a non-only analysis projection cannot conceal existing creative work',()=>{
  const pure=imageAnalysisInput({analysisOnly:false});
  assert.equal(Board.projectBoard(pure).analysisOnly,false);
  assert.equal(Board.projectBoard(pure).imageAnalysis,null);
  const creative=fixture();creative.timeline.brief=imageAnalysisInput().timeline.brief;
  const data=Board.projectBoard(creative);
  assert.equal(data.analysisOnly,false);assert.equal(data.imageAnalysis,null);
  assert.ok(data.writing);assert.ok(data.material);assert.ok(data.rows.length);
  const textOnly=imageAnalysisInput();delete textOnly.timeline.brief.analysis;
  textOnly.run.brief='只分析图片，不创作';
  assert.equal(Board.projectBoard(textOnly).analysisOnly,false);
});


test('photo collection reads current pages and exact caption without substituting old or source images', () => {
  const {host} = dom(), input = fixture(), calls = [];
  input.format = 'photo'; input.production.materials[0].approved = false;
  const caption = 'First caption line.\n\nSecond line <script>literal text</script>.';
  input.timeline.rail.find(row => row.stage === 'writing').blocks.at(-1).text = caption;
  input.production.drafts.push({draft_id:'old-second',shot_id:'backend-two',kind:'image',is_current:false,
    source_version:5,source_sha256:hash('b'),source_ready:false,attempts:[{status:'succeeded',output_available:true,
      output:{name:'old-second.png',sha256:hash('f'),url:url('old-second.png')}}]});
  input.production.materials.push({name:'old-second.png',sha256:hash('f'),kind:'image',url:url('old-second.png'),approved:true});
  const original = structuredClone(input);
  Board.render(host,input,{openMedia: (...args)=>calls.push(args),openShot: (...args)=>calls.push(args)});
  assert.ok(host.querySelectorAll('[data-board-image]').length > 1,'the canvas still keeps historical and source cards');
  const opener = host.querySelector('[data-board-action="photo-collection"]'); opener.focus(); opener.click();
  const dialog = host.querySelector('dialog[aria-label="整组图文"]'); assert.equal(dialog.open,true);
  const pages = dialog.querySelectorAll('[data-photo-page]'); assert.equal(pages.length,2);
  assert.equal(pages[0].getAttribute('data-photo-page'),'backend-one');
  assert.equal(pages[1].getAttribute('data-photo-page'),'backend-two');
  assert.equal(dialog.querySelectorAll('img').length,1);
  assert.equal(dialog.querySelector('img').src,url('current shot.png'));
  assert.match(pages[1].textContent,/尚无可核对的当前图片/);
  assert.equal(pages[1].querySelector('a'),null);
  assert.match(dialog.textContent,/1 \/ 2 页有生成结果 · 0 页已确认/);
  assert.match(pages[0].textContent,/已生成 · 待确认/);
  const download = dialog.querySelector('a');
  assert.equal(download.getAttribute('href'),url('current shot.png'));
  assert.equal(download.getAttribute('download'),'current shot.png');
  download.click();
  assert.equal(dialog.querySelector('.creative-photo-collection-caption p').textContent,caption);
  assert.equal(dialog.querySelectorAll('script').length,0);
  assert.deepEqual(calls,[],'viewing and the file link do not ask for decisions, approve or submit');
  assert.deepEqual(input,original);
  dialog.querySelector('[data-board-action="close-photo-collection"]').click();
  assert.equal(host.querySelector('dialog'),null);
  Board.destroy(host);
});

test('current photo status uses only the matching file approval boolean and keeps generation separate', () => {
  for (const [approved,expected] of [[true,'已生成 · 已确认'],[false,'已生成 · 待确认'],[null,'已生成 · 确认状态待核对'],['true','已生成 · 确认状态待核对']]) {
    const {host} = dom(),input = fixture(); input.format='photo'; input.production.materials[0].approved=approved;
    input.production.materials.unshift({...input.production.materials[0],sha256:hash('f'),approved:true});
    Board.render(host,input,{});
    assert.equal(host.querySelector('[data-result-shot="backend-one"] .creative-board-media-label').textContent,`图片 · ${expected}`);
    host.querySelector('[data-board-action="photo-collection"]').click();
    assert.equal(host.querySelector('[data-photo-page="backend-one"] .creative-photo-collection-status').textContent,expected);
    Board.destroy(host);
  }
});

test('collection sorts only server ordinals and refuses duplicate or missing page identity', () => {
  for (const invalid of [null,'duplicate-ordinal','duplicate-id','missing-ordinal']) {
    const {host} = dom(),input = fixture(); input.format='photo';
    const shots=input.production.targets.image.shots;
    shots.reverse();
    if(invalid==='duplicate-ordinal')shots[0].ordinal=shots[1].ordinal;
    if(invalid==='duplicate-id')shots[0].shot_id=shots[1].shot_id;
    if(invalid==='missing-ordinal')delete shots[0].ordinal;
    Board.render(host,input,{});host.querySelector('[data-board-action="photo-collection"]').click();
    const dialog=host.querySelector('dialog');
    if(invalid) {
      assert.equal(dialog.querySelectorAll('[data-photo-page]').length,0);
      assert.equal(dialog.querySelectorAll('img').length,0);
      assert.equal(dialog.querySelectorAll('a').length,0);
      assert.match(dialog.textContent,/清单尚未核对/);
    } else assert.equal(dialog.querySelectorAll('[data-photo-page]')[0].getAttribute('data-photo-page'),'backend-one');
    Board.destroy(host);
  }
});

test('mismatched target, unverified current output and stale writing never expose a collection download', () => {
  const changes=[input=>{input.production.targets.image.source_sha256=hash('f');},
    input=>{input.production.targets.image.complete_list=false;},
    input=>{input.timeline.rail.find(row=>row.stage==='writing').stale=true;},
    input=>{input.production.drafts[0].attempts[0].status='unknown';},
    input=>{input.production.materials[0].url='https://unapproved.invalid/current.png';},
    input=>{input.production.shots.image.shots[0].source_ready=false;}];
  for (const change of changes) {
    const {host} = dom(),input=fixture();input.format='photo';change(input);
    Board.render(host,input,{});host.querySelector('[data-board-action="photo-collection"]').click();
    const dialog=host.querySelector('dialog');
    assert.equal(dialog.querySelectorAll('img').length,0);
    assert.equal(dialog.querySelectorAll('a').length,0);
    assert.equal(dialog.querySelector('.creative-photo-collection-caption p').textContent,'Synthetic caption');
    Board.destroy(host);
  }
});

test('identical polling preserves the collection while new source, result or project closes its snapshot', () => {
  for(const change of [input=>{input.timeline.rail.find(row=>row.stage==='writing').artifact_sha256=hash('f');},
    input=>{input.production.materials[0].approved=true;},input=>{input.runId='another';input.run.run_id='another';}]) {
    const {host}=dom(),input=fixture();input.format='photo';const api=Board.render(host,input,{});
    host.querySelector('[data-board-action="photo-collection"]').click();
    const original=host.querySelector('dialog'),oldClose=original.querySelector('button');
    api.update(structuredClone(input),{});
    assert.equal(host.querySelector('dialog'),original);assert.equal(original.open,true);
    change(input);api.update(input,{});
    assert.equal(host.querySelector('dialog'),null);assert.equal(original.open,false);
    oldClose.click();assert.equal(host.querySelector('dialog'),null);
    Board.destroy(host);
  }
});

test('native cancel and destroy remove the collection without losing canvas view or creating business actions', () => {
  const {host,document}=dom(),input=fixture();input.format='photo';const calls=[];
  const api=Board.render(host,input,{openShot: (...args)=>calls.push(args)});
  const before=api.getView(),opener=host.querySelector('[data-board-action="photo-collection"]');
  opener.focus();opener.click();
  let dialog=host.querySelector('dialog'),prevented=false;
  dialog.oncancel({preventDefault(){prevented=true;}});
  assert.equal(prevented,true);assert.equal(host.querySelector('dialog'),null);
  assert.equal(document.activeElement,opener);assert.deepEqual(api.getView(),before);
  opener.click();dialog=host.querySelector('dialog');Board.destroy(host);
  assert.equal(dialog.open,false);assert.equal(host.querySelector('dialog'),null);assert.deepEqual(calls,[]);
});

test('a failed current image read removes its download and never uses a historical fallback', () => {
  const {host}=dom(),input=fixture();input.format='photo';Board.render(host,input,{});
  host.querySelector('[data-board-action="photo-collection"]').click();const page=host.querySelector('[data-photo-page="backend-one"]');
  page.querySelector('img').onerror();
  assert.equal(page.querySelector('img'),null);assert.equal(page.querySelector('a'),null);
  assert.match(page.textContent,/当前图片未能读取/);assert.match(page.textContent,/不会用旧图替代/);
  Board.destroy(host);
});

test('photo collection is absent from video, analysis and unbound delivery modes', () => {
  for(const format of ['video',null,'analysis']) {
    const {host}=dom(),input=fixture();input.run.content_format=format==='analysis'?'photo':format;
    if(format==='analysis')input.analysisOnly=true;
    Board.render(host,input,{});const opener=host.querySelector('[data-board-action="photo-collection"]');
    assert.equal(opener.hidden,true);opener.click();assert.equal(host.querySelector('dialog'),null);
    Board.destroy(host);
  }
});


function archiveFixture() {
  const input=fixture();input.format='photo';
  input.production.drafts[0].draft_sha256=hash('1');
  input.production.materials[0].approved=false;
  const output={name:'current-second.png',sha256:hash('f'),url:url('current-second.png')};
  const second={...structuredClone(input.production.drafts[0]),draft_id:'current-second',draft_sha256:hash('2'),shot_id:'backend-two',
    attempts:[{status:'succeeded',attempt_id:'second-attempt',output_available:true,output}]};
  input.production.drafts.push(second);input.production.materials.push({...output,kind:'image',approved:false});
  Object.assign(input.production.shots.image.shots[1],{draft_id:second.draft_id,stage:'completed',stage_label:'已完成',source_ready:true,output});
  const current=Board.projectBoard(input).media;
  input.production.photo_delivery={run_id:'fixture',source_version:6,source_sha256:hash('a'),available:true,reason:'',snapshot_sha256:hash('9'),
    pages:current.map(item=>({ordinal:item.ordinal,shot_id:item.shotId,draft_id:item.draftId,draft_sha256:item.draftSha256,
      attempt_id:item.attemptId,name:item.name,sha256:item.outputSha256,approved:item.approved}))};
  return input;
}

async function archiveEnvironment(run) {
  const previousFetch=globalThis.fetch,create=URL.createObjectURL,revoke=URL.revokeObjectURL;
  const blobs=[],revoked=[];
  URL.createObjectURL=blob=>{blobs.push(blob);return 'blob:synthetic-archive';};URL.revokeObjectURL=url=>revoked.push(url);
  try {await run({blobs,revoked});}
  finally {globalThis.fetch=previousFetch;URL.createObjectURL=create;URL.revokeObjectURL=revoke;}
}
const settleArchive=()=>new Promise(resolve=>setImmediate(resolve));

test('photo ZIP is disabled until the displayed complete current snapshot matches every bound identity', async()=>archiveEnvironment(async()=>{
  const changes=[input=>{input.production.photo_delivery.available=false;input.production.photo_delivery.reason='还缺第 2 页当前图片';},
    input=>{input.production.photo_delivery.run_id='another';},input=>{input.production.photo_delivery.source_sha256=hash('b');},
    input=>{input.production.photo_delivery.pages[1].draft_id='previous';},input=>{input.production.photo_delivery.pages[0].sha256=hash('f');},
    input=>{input.production.photo_delivery.pages[1].approved=true;},input=>{input.production.photo_delivery.snapshot_sha256='unknown';},
    input=>{input.production.drafts[0].draft_sha256=undefined;}];
  let calls=0;globalThis.fetch=async()=>{calls++;throw Error('must not request');};
  for(const change of changes){const {host}=dom(),input=archiveFixture();change(input);Board.render(host,input,{});
    host.querySelector('[data-board-action="photo-collection"]').click();const control=host.querySelector('[data-board-action="download-photo-collection"]');
    assert.equal(control.disabled,true);control.click();await settleArchive();assert.equal(calls,0);Board.destroy(host);}
}));

test('photo ZIP requests the displayed token and downloads the returned archive without approval or business callbacks', async()=>archiveEnvironment(async({blobs,revoked})=>{
  const {host,document}=dom(),input=archiveFixture(),calls=[],downloads=[];
  const create=document.createElement.bind(document);document.createElement=tag=>{const node=create(tag);
    if(tag==='a')node.click=()=>downloads.push({href:node.getAttribute('href'),name:node.getAttribute('download')});return node;};
  const requests=[];globalThis.fetch=async(url,options)=>{requests.push({url,options});return {ok:true,headers:{get:()=> 'application/zip'},blob:async()=>new Blob(['OFFLINE_ARCHIVE'])};};
  const original=structuredClone(input);Board.render(host,input,{openShot:(...args)=>calls.push(args)});
  host.querySelector('[data-board-action="photo-collection"]').click();const control=host.querySelector('[data-board-action="download-photo-collection"]');
  assert.equal(control.disabled,false);control.click();await settleArchive();await new Promise(resolve=>setTimeout(resolve,0));
  assert.equal(requests.length,1);assert.equal(requests[0].url,`/api/runs/fixture/delivery/photos/download?expected_snapshot_sha256=${hash('9')}`);
  assert.equal(blobs.length,1);assert.deepEqual(downloads,[{href:'blob:synthetic-archive',name:'fixture-v6-photos.zip'}]);
  assert.deepEqual(revoked,['blob:synthetic-archive']);assert.match(host.querySelector('dialog').textContent,/ZIP 已交给浏览器下载/);
  assert.equal(control.disabled,false);assert.deepEqual(input,original);assert.deepEqual(calls,[]);Board.destroy(host);
}));

test('ZIP conflicts and non-ZIP successes remain inline errors instead of downloaded JSON', async()=>archiveEnvironment(async({blobs})=>{
  for(const response of [{ok:false,json:async()=>({detail:'当前图片已变化，请重新核对'})},
    {ok:true,headers:{get:()=> 'application/json'},blob:async()=>{throw Error('must not read error as blob');}}]){
    const {host}=dom();globalThis.fetch=async()=>response;Board.render(host,archiveFixture(),{});
    host.querySelector('[data-board-action="photo-collection"]').click();host.querySelector('[data-board-action="download-photo-collection"]').click();
    await settleArchive();assert.equal(blobs.length,0);assert.match(host.querySelector('dialog').textContent,/已变化|未收到有效/);
    assert.equal(host.querySelector('[data-board-action="download-photo-collection"]').disabled,false);Board.destroy(host);
  }
}));

test('closing or switching project aborts a pending readonly ZIP and ignores its late response', async()=>archiveEnvironment(async({blobs})=>{
  for(const close of ['cancel','other-project']){
    const {host}=dom(),input=archiveFixture();let resolve,signal;
    globalThis.fetch=(_url,options)=>{signal=options.signal;return new Promise(done=>{resolve=done;});};
    const api=Board.render(host,input,{});host.querySelector('[data-board-action="photo-collection"]').click();
    host.querySelector('[data-board-action="download-photo-collection"]').click();
    if(close==='cancel')host.querySelector('[data-board-action="close-photo-collection"]').click();
    else api.update({...input,runId:'other',run:{...input.run,run_id:'other'}},{});
    assert.equal(signal.aborted,true);resolve({ok:true,headers:{get:()=> 'application/zip'},blob:async()=>new Blob(['late'])});
    await settleArchive();assert.equal(blobs.length,0);assert.equal(host.querySelector('dialog'),null);Board.destroy(host);
  }
}));

test('a failed archive connection preserves the snapshot and never reports success', async()=>archiveEnvironment(async({blobs})=>{
  const {host}=dom();globalThis.fetch=async()=>{throw TypeError('Offline network fixture');};Board.render(host,archiveFixture(),{});
  host.querySelector('[data-board-action="photo-collection"]').click();host.querySelector('[data-board-action="download-photo-collection"]').click();
  await settleArchive();assert.equal(blobs.length,0);assert.match(host.querySelector('dialog').textContent,/连接未能完成/);
  assert.equal(host.querySelector('[data-board-action="download-photo-collection"]').disabled,false);Board.destroy(host);
}));
