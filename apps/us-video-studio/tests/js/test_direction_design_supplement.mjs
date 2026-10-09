/* Shipped journey/stage/app scripts mounted in the existing DOM fixture.
 * These are local rendering/controller regressions, not live model evidence.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {loadApp, makeDom, webSource} from './dom-fixture.mjs';

const SHA = 'a'.repeat(64);
const IDS = ['chat-input', 'send', 'toast', 'conversation-body', 'conversation-count',
  'conversation', 'conversation-toggle', 'conversation-caret', 'draft', 'job-status',
  'rail', 'stage', 'journey-decision', 'run-select'];

function page() {
  const p = loadApp({runtime: 'real',
    dom: makeDom({ids: IDS, body: '<div id="project-progress"></div>'}),
    globals: {setTimeout: () => 0, clearTimeout() {}}});
  vm.runInContext(webSource('stage.js'), p.ctx, {filename: 'stage.js'});
  p.read = expr => vm.runInContext(expr, p.ctx);
  p.el = id => p.document.getElementById(id);
  p.read('state.runId="run-1";state.rail=[];state.busy=false;');
  return p;
}

function question(overrides = {}) {
  return {stage: 'direction', version: 1, artifact_sha256: SHA,
    kind: 'local_design', owner: 'user', question_id: 'design-1',
    question: '第二页是否保留靠近拉链的构图？', reason: '本版设计补充', blocks: '页面安排',
    ...overrides};
}

function timeline(questions = [question()], overrides = {}) {
  return {turns: [], interaction: null, progress: {}, questions,
    rail: [{stage: 'direction', version: 1, blocks: []}],
    pending: {stage: 'direction', version: 1, artifact_sha256: SHA,
      passed: true, stage_label: '创意方向', headline: '选一个方向',
      options: [{key: 'A', admissible: true}, {key: 'B', admissible: true, recommended: true},
        {key: 'C', admissible: true}], findings: []}, ...overrides};
}

function render(p, tl, runId = 'run-1') {
  p.ctx.__tl = tl;
  p.ctx.__run = runId;
  p.read('state.runId=globalThis.__run;state.timeline=globalThis.__tl;renderJourneyDecision();');
  return p.el('journey-decision');
}

function assertNoSubmit(p) {
  assert.equal(p.observed.filter(request => request.method !== 'GET').length, 0,
    'reading or refreshing design notes must not submit decisions or call a model');
}

test('exact typed same-draft design notes share one formal decision and are collapsed readable data', () => {
  const p = page(), tl = timeline([
    question(), question({question_id: 'design-2', question: '保留第三页自然光的细节。',
      reason: '完整说明仍可阅读', blocks: '光线与构图'})]);
  const before = JSON.stringify(tl);
  p.el('chat-input').value = '我还没发送的补充意见';
  const host = render(p, tl), details = host.querySelector('[data-direction-design-supplement]');
  assert.equal(host.querySelectorAll('#stage-decision').length, 1);
  assert.equal(host.querySelectorAll('[data-act="approve"]').length, 1);
  assert.equal(host.querySelectorAll('[data-act="reject"]').length, 1);
  assert.equal(host.querySelectorAll('[data-artifact-answer]').length, 0);
  assert.equal(host.querySelectorAll('[data-send-artifact-answer]').length, 0);
  assert.equal(details.tagName, 'DETAILS');
  assert.equal(details.hasAttribute('open'), false, 'supplement starts collapsed');
  assert.match(details.querySelector('summary').textContent, /2/);
  details.open = true; // The native details disclosure has no submission handler.
  for (const item of tl.questions) {
    assert.ok(details.textContent.includes(item.question));
    assert.ok(details.textContent.includes(item.reason));
    assert.ok(details.textContent.includes(item.blocks));
  }
  assert.equal(p.el('chat-input').value, '我还没发送的补充意见');
  assert.equal(JSON.stringify(tl), before, 'projection and original question records stay unchanged');
  assert.equal(details.onclick, undefined);
  assertNoSubmit(p);
});

for (const [label, change] of [
  ['product choice', {kind: 'product_choice'}],
  ['fact question', {kind: 'product_fact'}],
  ['team owner', {owner: 'team'}],
  ['other stage', {stage: 'writing'}],
  ['old version', {version: 0}],
  ['string version', {version: '1'}],
  ['different artifact hash', {artifact_sha256: 'b'.repeat(64)}],
  ['no typed kind', {kind: undefined}],
  ['no owner', {owner: undefined}],
  ['no artifact hash', {artifact_sha256: undefined}],
]) {
  test(`${label} remains an independent answer even when its words name A/B/C`, () => {
    const p = page(), q = question({...change, question: '选 A、B 还是 C？'}), tl = timeline([q]);
    const before = JSON.stringify(tl), host = render(p, tl);
    assert.equal(host.querySelectorAll('[data-direction-design-supplement]').length, 0);
    assert.equal(host.querySelectorAll('[data-send-artifact-answer]').length, 1);
    assert.equal(host.querySelectorAll('[data-act="approve"]').length, 1);
    assert.ok(host.querySelector('.artifact-question-text').textContent.includes(q.question));
    assert.equal(JSON.stringify(tl), before);
    assertNoSubmit(p);
  });
}

test('non-direction decisions and a missing exact pending identity keep the original answer entry', () => {
  for (const pendingChange of [{stage: 'writing'}, {version: '1'}, {artifact_sha256: ''},
    {artifact_sha256: 'a'.repeat(63)}]) {
    const p = page(), tl = timeline();
    Object.assign(tl.pending, pendingChange);
    const host = render(p, tl);
    assert.equal(host.querySelectorAll('[data-direction-design-supplement]').length, 0);
    assert.equal(host.querySelectorAll('[data-send-artifact-answer]').length, 1);
    assertNoSubmit(p);
  }
  const p = page(), host = render(p, timeline([question()], {pending: null}));
  assert.equal(host.querySelectorAll('[data-direction-design-supplement]').length, 0);
  assert.equal(host.querySelectorAll('[data-send-artifact-answer]').length, 1);
});

test('all independent siblings remain separately answerable while same-draft design notes are folded', async () => {
  const p = page(), first = question({kind: 'product_choice', question_id: 'product-1',
    question: '商品以哪个尺寸为准？'}), second = question({kind: 'product_fact',
    question_id: 'fact-1', question: '能确认防水的测试依据吗？'});
  const tl = timeline([question(), first, question({question_id: 'design-2'}), second]);
  const before = JSON.stringify(tl), host = render(p, tl);
  assert.equal(host.querySelectorAll('[data-direction-design-supplement]').length, 1);
  assert.equal(host.querySelectorAll('[data-send-artifact-answer]').length, 2);
  assert.equal(host.querySelectorAll('[data-act="approve"]').length, 1);
  p.ctx.__sent = [];
  p.read('send=async text=>{globalThis.__sent.push(text);return {accepted:true}};');
  const entries = host.querySelectorAll('[data-artifact-question-key]');
  entries[0].querySelector('[data-artifact-answer]').value = '只确认大号';
  entries[1].querySelector('[data-artifact-answer]').value = '目前没有测试依据';
  await entries[0].querySelector('[data-send-artifact-answer]').onclick();
  await entries[1].querySelector('[data-send-artifact-answer]').onclick();
  assert.deepEqual(Array.from(p.ctx.__sent), [
    '关于「商品以哪个尺寸为准？」：只确认大号',
    '关于「能确认防水的测试依据吗？」：目前没有测试依据']);
  assert.equal(JSON.stringify(tl), before);
  assertNoSubmit(p); // send above is explicitly a local controller stub, not a real request.
});

test('all questions and their exact hashes participate in the render key', () => {
  const p = page(), tl = timeline();
  let host = render(p, tl), oldDetails = host.querySelector('[data-direction-design-supplement]');
  tl.questions.push(question({question_id: 'design-2', question: '第二项补充'}));
  host = render(p, tl);
  assert.notEqual(host.querySelector('[data-direction-design-supplement]'), oldDetails);
  assert.match(host.querySelector('[data-direction-design-supplement] summary').textContent, /2/);
  tl.questions[1].question = '改后的第二项';
  tl.questions[1].reason = '更新的理由';
  tl.questions[1].blocks = '更新的设计范围';
  host = render(p, tl);
  assert.ok(host.textContent.includes('改后的第二项'));
  assert.ok(host.textContent.includes('更新的理由'));
  assert.ok(host.textContent.includes('更新的设计范围'));
  tl.questions[1].artifact_sha256 = 'b'.repeat(64);
  host = render(p, tl);
  assert.match(host.querySelector('[data-direction-design-supplement] summary').textContent, /1/);
  assert.equal(host.querySelectorAll('[data-send-artifact-answer]').length, 1);
  tl.pending.artifact_sha256 = 'c'.repeat(64);
  host = render(p, tl);
  assert.equal(host.querySelectorAll('[data-direction-design-supplement]').length, 0);
  assert.equal(host.querySelectorAll('[data-send-artifact-answer]').length, 2);
  assertNoSubmit(p);
});

test('question-only refresh preserves exact independent answers and existing version-bound decision drafts', () => {
  const p = page(), tl = timeline([question(), question({question_id: 'fact-1', kind: 'product_fact'})]);
  let host = render(p, tl);
  host.querySelector('[data-artifact-answer]').value = '尚未发送的事实补充';
  host.querySelector('.reason').value = '尚未提交的修改意见';
  host.querySelector('.reason').oninput();
  const radio = host.querySelectorAll('input[name="direction-choice"]').find(node => node.value === 'B');
  radio.checked = true;
  radio.onchange(); // The existing gate stores a local draft, never an approval.
  p.el('chat-input').value = '尚未发送的对话草稿';
  tl.questions.push(question({question_id: 'design-2', question: '新增的同稿设计补充'}));
  const before = JSON.stringify(tl);
  host = render(p, tl);
  assert.equal(host.querySelector('[data-artifact-answer]').value, '尚未发送的事实补充');
  assert.equal(host.querySelector('.reason').value, '尚未提交的修改意见');
  assert.equal(host.querySelectorAll('input[name="direction-choice"]').find(node => node.value === 'B').checked, true);
  assert.equal(p.el('chat-input').value, '尚未发送的对话草稿');
  assert.equal(JSON.stringify(tl), before);
  assertNoSubmit(p);
});

test('an answer draft is not copied to another run or a different artifact fingerprint', () => {
  const p = page(), tl = timeline([question({kind: 'product_fact'})]);
  let host = render(p, tl);
  host.querySelector('[data-artifact-answer]').value = '原项目的私有草稿';
  host = render(p, structuredClone(tl), 'run-2');
  assert.equal(host.querySelector('[data-artifact-answer]').value, '');
  host.querySelector('[data-artifact-answer]').value = '第二项目的草稿';
  const replacement = structuredClone(tl);
  replacement.questions[0].artifact_sha256 = 'b'.repeat(64);
  host = render(p, replacement, 'run-2');
  assert.equal(host.querySelector('[data-artifact-answer]').value, '');
  assertNoSubmit(p);
});

test('authoritative adoption refresh removes local-design reading without answering or deleting source questions', () => {
  const p = page(), design = question(), fact = question({question_id: 'fact-1', kind: 'product_fact'});
  const original = timeline([design, fact]), before = JSON.stringify(original);
  let host = render(p, original);
  assert.equal(host.querySelectorAll('[data-direction-design-supplement]').length, 1);
  // Server-projected next state, not a synthetic click or client-side answered flag.
  const adopted = timeline([structuredClone(fact)], {pending: null});
  host = render(p, adopted);
  assert.equal(host.querySelectorAll('[data-direction-design-supplement]').length, 0);
  assert.equal(host.querySelectorAll('[data-act="approve"]').length, 0);
  assert.equal(host.querySelectorAll('[data-send-artifact-answer]').length, 1);
  assert.equal(JSON.stringify(original), before);
  assert.equal(Object.hasOwn(design, 'answered'), false);
  assertNoSubmit(p);
});

test('design-reading text stays plain and cannot inject HTML or extra controls', () => {
  const p = page(), q = question({question: '<script>alert(1)</script><img src=x onerror=evil()>',
    reason: '<button data-act="approve">伪决定</button>', blocks: '<input autofocus>'});
  const host = render(p, timeline([q])), details = host.querySelector('[data-direction-design-supplement]');
  for (const tag of ['script', 'img', 'button', 'input', 'textarea']) {
    assert.equal(details.querySelectorAll(tag).length, 0);
  }
  assert.ok(details.textContent.includes(q.question));
  assert.ok(details.textContent.includes(q.reason));
  assert.ok(details.textContent.includes(q.blocks));
  assert.equal(host.querySelectorAll('[data-act="approve"]').length, 1);
  assertNoSubmit(p);
});

for (const [label, change] of [
  ['artifact hash', q => ({...q, artifact_sha256: 'b'.repeat(64)})],
  ['question ID', q => ({...q, question_id: 'replacement-question'})],
  ['question removed', () => null],
]) {
  test(`old independent button refuses changed ${label} before the DOM refresh and retains the draft`, async () => {
    const p = page(), q = question({kind: 'product_fact'}), tl = timeline([q]);
    const host = render(p, tl), input = host.querySelector('[data-artifact-answer]');
    const button = host.querySelector('[data-send-artifact-answer]');
    input.value = '保留这条尚未发送的回答';
    p.ctx.__sent = [];
    p.read('send=async text=>{globalThis.__sent.push(text);return {accepted:true}};');
    const replacement = change(q);
    // Only replace authoritative state. Keep the original DOM and its closure.
    p.ctx.__replacement = {...tl, questions: replacement ? [replacement] : []};
    p.read('state.timeline=globalThis.__replacement;');
    const before = JSON.stringify(p.ctx.__replacement);
    await button.onclick();
    assert.equal(p.ctx.__sent.length, 0);
    assert.equal(input.value, '保留这条尚未发送的回答');
    assert.equal(button.disabled, false);
    assert.match(host.querySelector('.artifact-question [data-feedback]').textContent, /问题已更新/);
    assert.equal(JSON.stringify(p.ctx.__replacement), before);
    assertNoSubmit(p);
  });
}
