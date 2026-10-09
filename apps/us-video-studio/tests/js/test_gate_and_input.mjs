/* F1 + F6 — the user's two write paths, driven through the real controllers.
 *
 * The workspace review rejected tests that only assert a rendered string
 * contains a button. These go through the real code: `stageHtml` produces the
 * markup, the markup is parsed into a DOM, the real `wireGate` binds it, and a
 * real click runs. For the composer the real `submit()`/`send()` pair is driven
 * exactly as the Enter key drives it.
 *
 * What is pinned here is what was broken:
 *   F1 — the brief-amendment card threw on open, so neither of its buttons was
 *        ever bound. Approving and returning now both have to work, keep the
 *        note on failure, and report the failure.
 *   F4 — a card with no real amendment proposal must not offer "approve", and
 *        must never send a diagnosis or a clause id as the new brief text.
 *   F6 — Enter during a task that cannot be adjusted must not eat the message.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import vm from 'node:vm';
import {loadApp, makeDom, makeStorage, parseHtml, webSource} from './dom-fixture.mjs';

const require = createRequire(import.meta.url);
// The page loads these before it loads `app.js`, and `stage.js` calls both as
// bare identifiers — `directionWorkHtml` reaches `legacyWorkHtml`, which is one
// of them. Same wiring the other frontend tests use.
globalThis.escapeHtml = require('../../studio/web/md.js').escapeHtml;
const stageMod = require('../../studio/web/stage.js');
globalThis.gateApproveLabel = stageMod.gateApproveLabel;
globalThis.gateRejectLabel = stageMod.gateRejectLabel;

const SHA = 'a'.repeat(64);
const SHA_B = 'b'.repeat(64);

/** Let a click handler's promise chain finish before asserting on its effects. */
async function settle() {
  for (let i = 0; i < 6; i += 1) await new Promise(resolve => setImmediate(resolve));
}

/* ------------------------------------------------------------ stage html -- */

/* Build a stage panel exactly as `renderPane` does, then mount it in a DOM.
 *
 * `stageHtml` is the real renderer; only the context object is synthesised,
 * because that is what the server sends. `stageHtml` draws a gate only when the
 * pending item names this exact stage, version and fingerprint — so the version
 * and sha come from the pending item, which is what the server would send.
 */
function mountGate(step, pending) {
  const html = stageMod.stageHtml(
    {stage: 'direction', label: '创意方向', has_doc: true,
     version: pending.version, artifact_sha256: pending.artifact_sha256, blocks: [],
     review: null, team_issues: [], ...step},
    {artifactBody: undefined, questions: [], drafts: {}, pending, contentFormat: 'video'}
  );
  const shell = parseHtml(`<div id="host">${html}</div>`)[0];
  return shell.querySelector('.gate');
}

/** Mount a gate inside a host element and run the real binder over it. */
function bind(app, gate) {
  const shell = app.ctx.document.createElement('div');
  shell.appendChild(gate);
  app.ctx.wireGate(shell);
  return gate;
}

function amendmentPending(overrides = {}) {
  return {
    kind: 'brief_amendment',
    stage: 'direction',
    stage_label: '创意方向',
    version: 1,
    artifact_sha256: SHA,
    passed: true,
    headline: '推荐方向与当前有效简报不一致，不能直接采用。',
    body: '可以批准下方的简报修订，或退回要求在原简报下重做。',
    findings: [],
    blockers: [{key: 'A', kind: 'needs_amendment', clause_id: 'goal.primary',
                detail: '方案要求先修订 goal.primary，但没有写出修订正文。'}],
    amend: {
      clause_id: 'goal.primary', op: 'replace', text: '', current_text: '',
      valid: false, reason: '这一版没有提出可批准的简报修订正文。',
      recommended: 'A', key: 'A',
    },
    next_stage_label: '', next_owner: '',
    next_cost_sentence: '修订简报后本版作废，下一步在重做之后才花钱。',
    options: [], options_hint: '',
    ...overrides,
  };
}

function proposalPending(text = '目标收窄为设计细节种草，不要求出镜穿搭',
                         current = 'US 线时尚达人种草向带货视频') {
  return amendmentPending({
    amend: {clause_id: 'goal.primary', op: 'replace', text, current_text: current,
            valid: true, reason: '', recommended: 'A', key: 'A'},
  });
}

function decisionPending(overrides = {}) {
  return {
    kind: 'decision_request', stage: 'direction', stage_label: '创意方向',
    version: 2, artifact_sha256: SHA_B, passed: true,
    headline: '写好了，审核也通过了。', body: '你看一遍。', findings: [],
    next_stage_label: '素材安排', next_owner: '视觉策划', next_cost_sentence: '下一步要花钱。',
    options: [{key: 'A', recommended: false, label: '方向 A'},
              {key: 'B', recommended: true, label: '方向 B'}],
    options_hint: '推荐不代表已选',
    ...overrides,
  };
}

/* --------------------------------------------------------- load context -- */

/* `wireGate` reads `state.runId` for its draft key and calls `refreshTimeline`
 * after a successful write. Both are stood in for so a test can assert the
 * request body and the result handling without a server or a re-render.
 *
 * The stubs are installed by *evaluating* assignment expressions in the
 * context: `state` and the other top-level names in `app.js` are lexical
 * bindings (`const`/`let`), so a script in that realm can reach them but they
 * are not properties of the global object. `toast` is observable through the
 * toast element, because the real `toast()` is what runs.
 */
function wire({routes = {}, storage} = {}) {
  const app = loadApp({dom: makeDom(['chat-input', 'send', 'stage', 'toast']), routes,
                       ...(storage ? {storage} : {})});
  vm.runInContext(`
    state.runId = 'run-1';
    state.rail = [];
    state.timeline = {questions: []};
    refreshTimeline = async () => { globalThis.__refreshed = (globalThis.__refreshed || 0) + 1; };
    restoreJob = async () => {};
    null;
  `, app.ctx);
  Object.defineProperty(app, 'refreshed', {get: () => app.ctx.__refreshed || 0});
  Object.defineProperty(app, 'toastText', {get: () => app.elements.get('toast').textContent});
  Object.defineProperty(app, 'toastIsError', {
    get: () => app.elements.get('toast').classList.contains('error')});
  return app;
}

/* -------------------------------------------------------------- F1 / F4 -- */

test('the brief-amendment card wires both of its buttons instead of throwing', async () => {
  const app = wire({routes: {'brief-amendment': {id: 'amd-1', status: 'approved',
    brief_fingerprint: 'brief-v2-x', redo: {status: 'started', job_id: 'job-1'}}}});
  const gate = bind(app, mountGate({}, proposalPending()));

  // The two nodes `wireGate` binds before any button must exist on this card.
  assert.ok(gate.querySelector('.reason'), 'the note box the binder reads');
  assert.ok(gate.querySelector('.gate-feedback'), 'the status line every failure is reported in');
  const approve = gate.querySelector('[data-act="amend_brief"]');
  const back = gate.querySelector('[data-act="reject"]');
  assert.ok(approve, 'a real proposal offers the approval');
  assert.ok(approve.onclick, 'the approve handler was bound, not skipped by a thrown TypeError');
  assert.ok(back.onclick, 'the return handler was bound too');

  approve.click();
  await settle();
  const post = app.observed.find(call => call.path.endsWith('/brief-amendment'));
  assert.deepEqual(post.body.changes, [{
    clause_id: 'goal.primary', op: 'replace', text: '目标收窄为设计细节种草，不要求出镜穿搭',
  }]);
  assert.equal(post.body.approve, true);
  assert.equal(post.body.note, '', 'approval carries only the displayed replacement text');
  assert.equal(post.body.expected_artifact_sha256, SHA);
  assert.equal(app.refreshed, 1);
  assert.equal(app.toastIsError, false);
  assert.match(app.toastText, /重做/);
});

test('a note never blocks approving a brief amendment, and is not part of it', async () => {
  // The note box is not the amendment body, so text left in it must not refuse
  // the approval — that was the same locked door as on the ordinary gate. The
  // approval carries exactly the displayed replacement text and nothing else.
  const app = wire({routes: {'brief-amendment': {id: 'amd-1', status: 'approved'}}});
  const gate = bind(app, mountGate({}, proposalPending()));
  gate.querySelector('.reason').value = '同意收窄目标，但还要加入价格对比';
  gate.querySelector('[data-act="amend_brief"]').click();
  await settle();
  const post = app.observed.find(call => call.path.endsWith('/brief-amendment'));
  assert.ok(post, 'a note must not block approving the displayed revision');
  assert.equal(post.body.note, '', 'approval carries only the displayed replacement text');
  assert.equal(gate.querySelector('.reason').value, '同意收窄目标，但还要加入价格对比',
    'the note stays where the user left it');
});

test('a real proposal is shown as a before/after diff, not as a clause id', () => {
  const withProposal = mountGate({}, proposalPending('新的目标', '旧的目标'));
  assert.ok(withProposal.querySelector('[data-act="amend_brief"]'));
  const shown = withProposal.textContent;
  assert.ok(shown.includes('旧的目标'), 'shows what it changes from');
  assert.ok(shown.includes('新的目标'), 'shows what it changes to');
});

test('with no proposal there is nothing to approve and no fabricated replacement body', () => {
  const without = mountGate({}, amendmentPending());
  assert.equal(without.querySelector('[data-act="amend_brief"]'), null);
  assert.equal(without.dataset.amendValid, '0');
  assert.equal(without.dataset.amendText, '', 'a diagnosis is not a replacement body');
  // The clause id stays declared so the diagnosis remains bound to it; what must
  // not appear is a body that would replace it.
  assert.equal(without.dataset.amendClause, 'goal.primary');
  assert.ok(without.querySelector('[data-act="reject"]'), 'returning stays available');
  assert.ok(without.textContent.includes('没有提出可批准的简报修订正文'));
});

test('approving is refused client-side when the card carries no real proposal', async () => {
  const app = wire({routes: {'brief-amendment': {id: 'amd-x', status: 'approved'}}});
  const gate = bind(app, mountGate({}, proposalPending()));
  // A stale or hand-rolled card claims validity but carries no body. An empty
  // text must not become a request: the clause id would go as the new goal.
  gate.setAttribute('data-amend-valid', '1');
  gate.setAttribute('data-amend-text', '');
  gate.querySelector('[data-act="amend_brief"]').click();
  await settle();
  assert.equal(app.observed.filter(call => call.path.endsWith('/brief-amendment')).length, 0);
  assert.match(gate.querySelector('.gate-feedback').textContent, /没有可批准的修订正文/);
});

test('returning a blocked amendment sends the note it was given', async () => {
  const app = wire({routes: {
    nonce: {stage: 'direction', nonce: 'n-1'},
    decision: {decision: 'returned', continuation: {status: 'started', job_id: 'job-9'}},
    timeline: {rail: [], questions: [], turns: [], progress: {}, pending: null},
  }});
  const gate = bind(app, mountGate({}, amendmentPending()));
  gate.querySelector('.reason').value = '按原简报重做方向';

  gate.querySelector('[data-act="reject"]').click();
  await settle();

  const post = app.observed.find(call => call.path.endsWith('/decision'));
  assert.equal(post.body.decision, 'returned');
  assert.equal(post.body.note, '按原简报重做方向');
  assert.equal(post.body.nonce, 'n-1');
  assert.equal(post.body.expected_artifact_sha256, SHA);
  assert.equal(app.observed.filter(call => call.path.includes('/nonce')).length, 1);
});

test('a return with no reason is refused in the card and never reaches the server', async () => {
  const app = wire({routes: {'brief-amendment': {}}});
  const gate = bind(app, mountGate({}, amendmentPending()));
  gate.querySelector('[data-act="reject"]').click();
  await settle();
  assert.equal(app.observed.filter(call => call.path.endsWith('/decision')).length, 0);
  assert.equal(gate.querySelector('.gate-feedback').textContent,
    '请写明需要调整的地方。');
});

test('an approval whose redo did not start is reported as not started', async () => {
  const app = wire({routes: {'brief-amendment': {id: 'amd-2', status: 'approved',
    redo: {status: 'not_started', reason: '制片室正在运行另一个任务。'}}}});
  const gate = bind(app, mountGate({}, proposalPending()));
  gate.querySelector('[data-act="amend_brief"]').click();
  await settle();
  assert.equal(app.toastIsError, true, 'the two halves are reported separately');
  assert.match(app.toastText, /尚未启动/);
  assert.match(app.toastText, /正在运行/);
});

test('a failed amendment write reports the failure on the card', async () => {
  const failure = Object.assign(new Error('连接中断'), {status: 500});
  const app = wire({routes: {'brief-amendment': failure}});
  const gate = bind(app, mountGate({}, proposalPending()));
  gate.querySelector('[data-act="amend_brief"]').click();
  await settle();
  assert.match(gate.querySelector('.gate-feedback').textContent, /修订失败/);
  assert.match(gate.querySelector('.gate-feedback').textContent, /草稿仍保留/);
});

test('an incomplete gate reports itself instead of leaving every control dead', () => {
  const app = wire();
  const shell = app.ctx.document.createElement('div');
  // The exact failure F1 names: a `.gate` whose shared nodes are missing, so the
  // binder's first two lines would have thrown and nothing would be wired.
  const broken = app.ctx.document.createElement('section');
  broken.className = 'gate';
  broken.dataset.stage = 'direction';
  const amender = app.ctx.document.createElement('button');
  amender.dataset.act = 'amend_brief';
  broken.appendChild(amender);
  shell.appendChild(broken);

  assert.doesNotThrow(() => app.ctx.wireGate(shell));
  assert.match(broken.textContent, /不能在这里提交/);
  assert.equal(amender.onclick, undefined, 'a control with no status line is not left half-wired');
});

test('the renderer draws the binder contract on every gate kind it can produce', () => {
  const cases = [proposalPending(), amendmentPending(), decisionPending()];
  for (const pending of cases) {
    const gate = mountGate({}, pending);
    assert.ok(gate.querySelector('.reason'), `${pending.kind} must carry the note box`);
    assert.ok(gate.querySelector('.gate-feedback'), `${pending.kind} must carry the status line`);
  }
});

test('the ordinary decision card still carries the note box, choices and both buttons', () => {
  const gate = mountGate({}, decisionPending());
  assert.ok(gate.querySelector('.reason'));
  assert.ok(gate.querySelector('.gate-feedback'));
  assert.equal(gate.querySelectorAll('input[type="radio"]').length, 2);
  assert.ok(gate.querySelector('[data-act="approve"]'));
  assert.ok(gate.querySelector('[data-act="reject"]'));
  assert.ok(gate.textContent.includes('data-gate-act') === false,
    'the action list is an attribute, not text the reader sees');
  assert.equal(gate.querySelector('.gate-actions').getAttribute('data-gate-act'), 'approve reject');
});

/* ----------------------------------------------------------------- R3 ---- */

test('a blocked option is drawn but not selectable, with its reason', () => {
  const gate = mountGate({}, decisionPending({
    options: [
      {key: 'A', recommended: true, label: '方向 A', admissible: false,
       blocked_reason: '方案 A 未声明满足 goal.primary。'},
      {key: 'B', recommended: false, label: '方向 B', admissible: true},
    ],
  }));
  const radios = gate.querySelectorAll('input[type="radio"]');
  assert.equal(radios.length, 2, 'both options are visible');
  const byKey = Object.fromEntries(radios.map(r => [r.value, r]));
  assert.equal(byKey.A.disabled, true, 'the refused option cannot be selected');
  assert.equal(byKey.B.disabled, false, 'the compliant option stays selectable');
  assert.ok(gate.textContent.includes('未声明满足 goal.primary'), 'the reason is on the card');
  assert.ok(gate.querySelector('[data-act="approve"]'), 'adoption is still offered for B');
});

test('a proposal on an option is offered from the same decision card', async () => {
  const app = wire({routes: {'brief-amendment': {id: 'amd-r3', status: 'approved',
    redo: {status: 'started', job_id: 'job-r3'}}}});
  const gate = bind(app, mountGate({}, decisionPending({
    brief_fingerprint: 'brief-v1-abc123',
    options: [
      {key: 'A', recommended: true, label: '方向 A', admissible: false,
       blocked_reason: '方案 A 未声明满足 goal.primary。'},
      {key: 'B', recommended: false, label: '方向 B', admissible: true},
    ],
    amend: {clause_id: 'goal.primary', op: 'replace', text: '目标收窄为设计细节种草',
            current_text: '做一个种草视频', valid: true, reason: '',
            recommended: 'A', key: 'A'},
  })));

  // Both routes out of the card, without a second decision form.
  assert.ok(gate.querySelector('[data-act="approve"]'), 'adopt B');
  const amender = gate.querySelector('[data-act="amend_brief"]');
  assert.ok(amender, 'or change the brief first');
  assert.ok(gate.textContent.includes('做一个种草视频'), 'the diff shows the current clause');

  amender.click();
  await settle();
  const post = app.observed.find(call => call.path.endsWith('/brief-amendment'));
  assert.equal(post.body.changes[0].text, '目标收窄为设计细节种草');
  // A1: the decision carries the brief the user was reading.
  assert.equal(post.body.expected_brief_fingerprint, 'brief-v1-abc123');
});

test('a card with no proposal still offers only the return, never a fabricated body', () => {
  const gate = mountGate({}, decisionPending({
    amend: {clause_id: 'goal.primary', op: 'replace', text: '', current_text: '做一个种草视频',
            valid: false, reason: '这一版没有提出可批准的简报修订正文。',
            recommended: 'A', key: 'A'},
  }));
  assert.equal(gate.querySelector('[data-act="amend_brief"]'), null);
  assert.equal(gate.dataset.amendValid, '0');
  assert.equal(gate.dataset.amendText, '');
  assert.ok(gate.textContent.includes('没有提出可批准的简报修订正文'));
  assert.ok(gate.querySelector('[data-act="approve"]'), 'the ordinary decision is unaffected');
});

test('an unresolved brief amendment blocks the card and says which one', () => {
  const gate = mountGate({}, decisionPending({
    conflicts: [{amendment_clause_id: 'must.c7e124e4', kind: 'amendment_unresolved',
                 detail: '你批准过把一条要求改成「台词克制」，但现在的整理稿里已经找不到它。'}],
  }));
  assert.ok(gate.querySelector('.gate-conflicts'), 'the conflict is visible on the card');
  assert.ok(gate.textContent.includes('台词克制'), 'and says which requirement, by its own words');
  assert.ok(gate.textContent.includes('方向都先不能采用'));
});

test('blocker reasons are words, not raw state codes', () => {
  /* The brief-amendment card prints the stage-level blockers — it used to say
   * `方向 A · unmapped：…`. English state codes are machine fields, not language
   * for the person deciding (Microsoft Writing Style Guide: write like you
   * speak, avoid jargon); the code stays on the element as data. */
  const gate = mountGate({}, amendmentPending({
    blockers: [{key: 'A', kind: 'unmapped', clause_id: 'goal.primary',
                detail: '这一版还没有说明它和当前目标的关系；没有说明就不算数。'}],
  }));
  const row = gate.querySelector('.gate-brief-blockers li');
  assert.ok(row, 'the blocker row renders');
  assert.match(row.textContent, /还没说明和目标的关系/, 'a human label, not the code');
  assert.ok(!row.textContent.includes('unmapped'), 'the raw code is not spoken');
  assert.equal(row.getAttribute('data-kind'), 'unmapped',
    'the code stays machine-readable on the element');
});

test('a stale brief is reported as a decision to redo, not a failed request', async () => {
  const conflict = Object.assign(new Error('有效简报已经变化，这次修订是相对旧简报做出的；请重新阅读当前简报后再决定。'),
    {status: 409});
  const app = wire({routes: {'brief-amendment': conflict}});
  const gate = bind(app, mountGate({}, decisionPending({
    brief_fingerprint: 'brief-v1-stale',
    amend: {clause_id: 'goal.primary', op: 'replace', text: '新目标',
            current_text: '旧目标', valid: true, reason: '', recommended: 'A', key: 'A'},
  })));
  gate.querySelector('[data-act="amend_brief"]').click();
  await settle();
  const feedback = gate.querySelector('.gate-feedback').textContent;
  assert.match(feedback, /有效简报已经变化/);
  assert.match(feedback, /草稿仍保留/);
  assert.ok(gate.querySelector('.gate-reload'), 'and it offers a way to reload');
});

/* ----------------------------------------------------------------- T3 ---- */

/* The screen people actually use is the structured card list, which renders
 * through `directionWorkHtml`'s inline gate — not through `stageDecisionGateHtml`.
 * A test whose `blocks` are empty exercises the fallback form and proves nothing
 * about the real path, which is how the per-option rule went unapplied here. */
function structuredStep(pending, {selected = null, decision = null} = {}) {
  const cards = (pending.options || []).map(o => ({
    head: `方向 ${o.key}`,
    fields: [{label: '主张', value: `主张 ${o.key}`}, {label: '前 3 秒', value: `开场 ${o.key}`}],
  }));
  return {
    stage: 'direction', label: '创意方向', has_doc: true, version: pending.version,
    artifact_sha256: pending.artifact_sha256, blocks: [{type: 'cards', title: '方向', cards}],
    review: null, team_issues: [], approval_decision: decision, selected_option_id: selected,
  };
}

function mountStructuredGate(pending, stepOverrides = {}) {
  const html = stageMod.stageHtml(structuredStep(pending, stepOverrides),
    {artifactBody: undefined, questions: [], drafts: {}, pending, contentFormat: 'video'});
  const shell = parseHtml(`<div id="host">${html}</div>`)[0];
  return {shell, gate: shell.querySelector('.gate')};
}

test('the structured direction cards enforce the same per-option admission', () => {
  const pending = decisionPending({
    options: [
      {key: 'A', recommended: true, label: '方向 A', admissible: false,
       blocked_reason: '方案 A 未声明满足 goal.primary。'},
      {key: 'B', recommended: false, label: '方向 B', admissible: true},
    ],
  });
  const {gate} = mountStructuredGate(pending);
  assert.ok(gate, 'the inline gate is the one on screen');

  const buttons = gate.querySelectorAll('[data-act="pick-option"]');
  assert.equal(buttons.length, 2, 'every option can be used as a revision reference');
  assert.equal(buttons[0].dataset.admissible, '0');
  assert.equal(buttons[1].dataset.admissible, '1');
  assert.equal(gate.querySelectorAll('[data-act="approve"]').length, 1, 'one explicit adoption action');
  assert.ok(gate.textContent.includes('不能采用'), 'the refused option says so');
  assert.ok(gate.textContent.includes('未声明满足 goal.primary'), 'and why');
  const blockedCard = gate.querySelector('[data-direction-card="A"]');
  assert.ok(blockedCard.classList.contains('is-blocked'));
  assert.equal(blockedCard.querySelector('[data-act="approve"]'), null);
});

test('the structured gate carries the brief binding and the amendment entry', async () => {
  const app = wire({routes: {'brief-amendment': {id: 'amd-t3', status: 'approved',
    redo: {status: 'started', job_id: 'job-t3'}}}});
  const pending = decisionPending({
    brief_fingerprint: 'brief-v1-t3',
    options: [
      {key: 'A', recommended: true, label: '方向 A', admissible: false,
       blocked_reason: '方案 A 未声明满足 goal.primary。'},
      {key: 'B', recommended: false, label: '方向 B', admissible: true},
    ],
    amend: {clause_id: 'goal.primary', op: 'replace', text: '目标收窄为设计细节种草',
            current_text: '做一个种草视频', valid: true, reason: '',
            recommended: 'A', key: 'A'},
  });
  const {shell, gate} = mountStructuredGate(pending);
  assert.equal(gate.dataset.briefFp, 'brief-v1-t3', 'the card is bound to the brief it shows');
  assert.equal(gate.dataset.amendText, '目标收窄为设计细节种草');
  bind(app, gate);

  const amender = gate.querySelector('[data-act="amend_brief"]');
  assert.ok(amender, 'the proposal has an entry from the card that proposed it');
  amender.click();
  await settle();
  const post = app.observed.find(call => call.path.endsWith('/brief-amendment'));
  assert.equal(post.body.expected_brief_fingerprint, 'brief-v1-t3');
  assert.equal(post.body.changes[0].text, '目标收窄为设计细节种草');
  assert.ok(shell, 'the pane markup is intact');
});

test('a failed decision leaves the refused option disabled', async () => {
  const failure = Object.assign(new Error('连接中断'), {status: 500});
  const app = wire({routes: {nonce: {stage: 'direction', nonce: 'n-t3'}, decision: failure,
                             timeline: {rail: [], questions: [], turns: [], progress: {}, pending: null}}});
  const pending = decisionPending({
    options: [
      {key: 'A', recommended: true, label: '方向 A', admissible: false,
       blocked_reason: '方案 A 未声明满足 goal.primary。'},
      {key: 'B', recommended: false, label: '方向 B', admissible: true},
    ],
  });
  const {gate} = mountStructuredGate(pending);
  bind(app, gate);
  const blocked = gate.querySelector('[data-direction-card="A"] [data-act="pick-option"]');
  assert.equal(blocked.dataset.admissible, '0');
  gate.querySelector('[data-direction-card="B"] [data-act="pick-option"]').click();
  const adoptB = gate.querySelector('.gate-actions [data-act="approve"]');
  adoptB.click();
  await settle();
  // The failed save re-enables what it disabled — but not the refused option.
  assert.equal(blocked.dataset.admissible, '0');
  assert.equal(adoptB.disabled, false, 'the admissible option is usable again for a retry');
  assert.match(gate.querySelector('.gate-feedback').textContent, /连接中断|失败/);
});

test('the fallback form and the structured cards agree on admission', () => {
  const pending = decisionPending({
    options: [
      {key: 'A', recommended: true, label: '方向 A', admissible: false,
       blocked_reason: '方案 A 未声明满足 goal.primary。'},
      {key: 'B', recommended: false, label: '方向 B', admissible: true},
    ],
  });
  const structured = mountStructuredGate(pending).gate;
  const fallback = mountGate({}, pending);
  assert.equal(structured.querySelector('[data-direction-card="A"] [data-act="pick-option"]').dataset.admissible, '0');
  assert.equal(structured.querySelector('[data-direction-card="B"] [data-act="pick-option"]').dataset.admissible, '1');
  assert.equal(fallback.querySelectorAll('input[type="radio"]')[0].disabled, true);
});

test('a direction change request returns the old version and never approves it', async () => {
  const app = wire({routes: {nonce: {nonce: 'n-revise'}, decision: {decision: 'returned', continuation: {status: 'started'}}}});
  const {gate} = mountStructuredGate(decisionPending());
  bind(app, gate);
  gate.querySelector('[data-direction-card="B"] [data-act="pick-option"]').click();
  gate.querySelector('.reason').value = '开头改成先展示展开动作';
  gate.querySelector('.reason').oninput();
  gate.querySelector('[data-act="reject"]').click();
  await settle();
  const post = app.observed.find(call => call.path.endsWith('/decision'));
  assert.equal(post.body.decision, 'returned');
  assert.equal(post.body.selected_option_id, null);
  assert.match(post.body.note, /方向 B/);
  assert.match(post.body.note, /开头改成先展示展开动作/);
  assert.equal(post.body.continue_after, true);
});

test('a note in the box never blocks adopting, and is never adopted as a change', async () => {
  // Reported 2026-09-30: typing in 希望怎么修改 locked the adopt button behind
  // "采用当前版前请清空修改意见". Adoption carries no note at all, so the text must
  // be inert: the decision goes through and no edit is silently approved.
  const app = wire({routes: {nonce: {nonce: 'n-adopt-note'},
                             decision: {decision: 'approved', continuation: {status: 'started'}}}});
  const {gate} = mountStructuredGate(decisionPending());
  bind(app, gate);
  gate.querySelector('[data-direction-card="A"] [data-act="pick-option"]').click();
  gate.querySelector('.reason').value = '开头改成先展示展开动作';
  gate.querySelector('.reason').oninput();
  gate.querySelector('[data-act="approve"]').click();
  await settle();
  const post = app.observed.find(call => call.path.endsWith('/decision'));
  assert.ok(post, 'adopting must not be refused because the note box has text');
  assert.equal(post.body.decision, 'approved');
  assert.equal(post.body.note, '', 'the note is not part of an adoption');
  assert.equal(post.body.selected_option_id, 'A');
});

test('adopting a direction sends only the exact version and selected option', async () => {
  const app = wire({routes: {nonce: {nonce: 'n-adopt'}, decision: {decision: 'approved', continuation: {status: 'started'}}}});
  const {gate} = mountStructuredGate(decisionPending());
  bind(app, gate);
  gate.querySelector('[data-direction-card="B"] [data-act="pick-option"]').click();
  assert.equal(app.observed.length, 0, 'picking a direction is not an API decision');
  gate.querySelector('[data-act="approve"]').click();
  await settle();
  const post = app.observed.find(call => call.path.endsWith('/decision'));
  assert.equal(post.body.decision, 'approved');
  assert.equal(post.body.note, '');
  assert.equal(post.body.selected_option_id, 'B');
  assert.equal(post.body.expected_version, 2);
  assert.equal(post.body.expected_artifact_sha256, SHA_B);
});

test('a blocked direction can guide revision but cannot be adopted', async () => {
  const app = wire();
  const {gate} = mountStructuredGate(decisionPending({options: [
    {key:'A', admissible:false, blocked_reason:'缺少证据'}, {key:'B', admissible:true},
  ]}));
  bind(app, gate);
  gate.querySelector('[data-direction-card="A"] [data-act="pick-option"]').click();
  gate.querySelector('[data-act="approve"]').click();
  await settle();
  assert.equal(app.observed.length, 0);
  assert.match(gate.querySelector('.gate-feedback').textContent, /不能采用/);
});

test('historical reviewer return requires no override and a note never blocks adoption', async () => {
  const app = wire({routes: {nonce: {nonce: 'n-approve'}, decision: {decision: 'approved', continuation: {status: 'started'}}}});
  const gate = bind(app, mountGate({stage:'writing', label:'脚本'}, decisionPending({
    stage:'writing', stage_label:'脚本', options:[], passed:false,
  })));
  // A note left in the box is inert for adoption: it is neither a reason to
  // refuse nor part of the approved payload.
  gate.querySelector('.reason').value = '修改第一句';
  gate.querySelector('.reason').oninput();
  gate.querySelector('[data-act="approve"]').click();
  await settle();
  const post = app.observed.find(call => call.path.endsWith('/decision'));
  assert.ok(post, 'a note must not block adopting the version on screen');
  assert.equal(post.body.note, '', 'the note is not part of an adoption');
  assert.equal(post.body.override_reason, '');
  assert.equal(post.body.selected_option_id, null);
});

/* ----------------------------------------------------------------- R5 ---- */

test('a refusal from another project never overwrites the visible draft', async () => {
  const app = composer();
  // The adjustment goes out for run-1, the user switches, and only then does the
  // server refuse. The words belong to run-1's composer, not this one.
  app.ctx.__switchTo = true;
  const input = app.ctx.document.getElementById('chat-input');
  input.value = '这条属于旧项目';
  const result = await app.ctx.submit();
  assert.equal(result.accepted, false);
  assert.equal(result.draft_restored, false);
  assert.equal(input.value, '', 'nothing was written into the other project');
});

test('a refusal never overwrites words the user typed after sending', async () => {
  const app = composer();
  // Hold the answer so the user can keep typing while it is in flight — which is
  // the only way this race exists, and the reason a broken restore is invisible
  // in a test whose refusal comes back synchronously.
  let release;
  const held = new Promise(resolve => { release = resolve; });
  app.ctx.__holdSend = held;
  const input = app.ctx.document.getElementById('chat-input');
  input.value = '第一次尝试';
  const pending = app.ctx.submit();
  input.value = '第二次想法';
  release({accepted: false, reason: '这轮没接受'});
  await pending;
  assert.equal(input.value, '第二次想法', 'the newer draft wins over the failed one');
});

test('a refusal from the same project with no newer input puts the words back', async () => {
  const app = composer({accept: false});
  const input = app.ctx.document.getElementById('chat-input');
  input.value = '换了模型再试';
  const result = await app.ctx.submit();
  assert.equal(result.accepted, false);
  assert.equal(result.draft_restored, true);
  assert.equal(input.value, '换了模型再试');
});

test('the composer submits a running task adjustment through the producer', async () => {
  const app = composer({busy: true, steer: {ok: true},
    currentJob: {job_id: 'j9', job_kind: 'conversation', status: 'running', goal_revision: 0}});
  const input = app.ctx.document.getElementById('chat-input');
  input.value = '改成 12 秒';
  const result = await app.ctx.submit();
  assert.equal(result.accepted, true);
  assert.equal(input.value, '');
  assert.equal(app.steers.length, 1);
  assert.equal(app.steers[0].payload.text, '改成 12 秒');
});

test('a selected production draft remains bound in a midflight adjustment', async () => {
  const app = composer({busy: true,
    currentJob: {job_id: 'media-1', job_kind: 'production_batch_generation',
      status: 'running', goal_revision: 1}});
  const target = {kind:'production_draft',run_id:'run-1',draft_id:'shot-3',
    draft_sha256:SHA};
  app.ctx.__target = target;
  vm.runInContext('state.feedbackTarget = globalThis.__target', app.ctx);
  const input = app.ctx.document.getElementById('chat-input');
  input.value = '第 3 张改成室内';
  const result = await app.ctx.submit();
  assert.equal(result.accepted, true);
  assert.equal(app.steers.length, 1);
  assert.equal(app.steers[0].payload.feedback_target.draft_sha256, SHA);
  assert.equal(input.value, '');
});

test('an unknown result keeps its request identity instead of becoming a draft', async () => {
  /* T2: the answer was lost, so the browser does not know whether the server has
   * the request. `submitJob` kept the original request_id and offered to confirm
   * it; putting the text back as an editable draft invited a resend under a new
   * id, which is how one prompt could run twice. */
  const requests = [];
  const app = composer({
    submitJobResult: {accepted: false, uncertain: true, request_id: 'request-1',
                      rejected_text: '请重做创意方向'},
    onSubmitJob: ({payload, sessionStorage}) => {
      requests.push(payload.request_id);
      sessionStorage.setItem('studio-job-pending:run-1',
        JSON.stringify({request_id: payload.request_id, text: payload.text}));
    },
  });
  const input = app.ctx.document.getElementById('chat-input');
  input.value = '请重做创意方向';
  const first = await app.ctx.submit();
  assert.equal(first.accepted, false);
  assert.equal(first.uncertain, true, 'unknown is reported as unknown');
  assert.equal(first.draft_restored, false, 'and is not handed back as a draft');
  assert.equal(input.value, '', 'so the words are not sitting there to be resent');
  assert.match(app.toastText, /尚未确认/);
  assert.doesNotMatch(app.toastText, /没有被接受/, 'unknown must not be downgraded to refused');

  // A second Enter is refused while that identity is unresolved.
  input.value = '请重做创意方向';
  const second = await app.ctx.submit();
  assert.equal(second.uncertain, true);
  assert.equal(requests.length, 1, 'no second request identity was created');
  assert.equal(input.value, '请重做创意方向', 'and the newer draft is left alone');
});

test('a definite rejection still returns the draft', async () => {
  const app = composer({
    submitJobResult: {accepted: false, uncertain: false, request_id: 'request-2',
                      reason: '这一轮已经结束了。'},
  });
  const input = app.ctx.document.getElementById('chat-input');
  input.value = '再试一次';
  const result = await app.ctx.submit();
  assert.equal(result.accepted, false);
  assert.equal(result.uncertain, false);
  assert.equal(result.draft_restored, true, 'a refusal returns the words');
  assert.equal(input.value, '再试一次');
});

test('the real submitSteer path does not overwrite a newer same-project draft', async () => {
  /* T4: the outer draft-version lock was correct, but the real transport wrote
   * the text into the composer itself before returning, defeating it. This
   * exercises `submitSteer`'s own branch (not a stubbed return) against a 409. */
  const app = composer({busy: true,
    currentJob: {job_id: 'j7', job_kind: 'conversation', status: 'running', goal_revision: 3},
    steerError: Object.assign(new Error('任务目标已更新，请刷新后按当前目标调整。'), {status: 409})});
  const input = app.ctx.document.getElementById('chat-input');
  input.value = '第一次调整';
  const pending = app.ctx.send(input.value);
  // The user keeps typing while the adjustment is in flight.
  input.value = '后来写的新想法';
  app.ctx.updatesComposerVersion?.();
  const result = await pending;
  assert.equal(result.accepted, false);
  assert.equal(input.value, '后来写的新想法', 'the newer draft survives the late 409');
  assert.equal(result.rejected_text, '第一次调整');
});

test('switching project and back keeps each draft separate', async () => {
  const app = composer();
  const input = app.ctx.document.getElementById('chat-input');
  input.value = '属于 A 的草稿';
  vm.runInContext(`
    state.chatDrafts = state.chatDrafts || new Map();
    state.chatDrafts.set('run-1', $('chat-input').value);
    state.runId = 'run-2';
    $('chat-input').value = '属于 B 的草稿';
    state.chatDrafts.set('run-2', $('chat-input').value);
    null;
  `, app.ctx);
  assert.equal(input.value, '属于 B 的草稿');
  vm.runInContext(`
    $('chat-input').value = state.chatDrafts.get('run-1');
    null;
  `, app.ctx);
  assert.equal(input.value, '属于 A 的草稿', 'A keeps its own words');
  vm.runInContext(`$('chat-input').value = state.chatDrafts.get('run-2'); null;`, app.ctx);
  assert.equal(input.value, '属于 B 的草稿', 'and B keeps its own');
});

test('the real submitSteer returns the rejected text instead of writing the composer', async () => {
  /* T4 asked for the real transport function, not a stand-in: the outer draft
   * lock was correct and this branch defeated it by writing `chat-input` itself
   * before returning. The whole `runtime.js` is loaded the way the browser loads
   * it, with only `api` faked, so the code under test is the code that ships. */
  const input = {value: '后来写的新想法'};
  const toasts = [];
  let calls = 0;
  const ctx = {
    state: {runId: 'run-1'},
    sessionStorage: {setItem: () => {}, removeItem: () => {}},
    $: (id) => (id === 'chat-input' ? input : {disabled: false, append: () => {}}),
    box: () => ({onclick: null}),
    toast: (message, isError) => toasts.push({message, isError}),
    renderRecoveryPanel: () => {},
    restoreJob: async () => {},
    refreshTimeline: async () => {},
    followJobEvents: () => {},
    EventSource: function EventSource() {},
    api: async () => {
      calls += 1;
      throw Object.assign(new Error('任务目标已更新，请刷新后按当前目标调整。'), {status: 409});
    },
  };
  vm.createContext(ctx);
  vm.runInContext(webSource('runtime.js'), ctx, {filename: 'runtime.js'});
  assert.equal(typeof ctx.submitSteer, 'function', 'the shipped function is reachable');

  const result = await ctx.submitSteer('run-1', 'j7',
    {text: '第一次调整', request_id: 'req-1', expected_goal_revision: 3});

  assert.equal(calls, 1, 'the request really went out');
  assert.equal(result.ok, false);
  assert.equal(result.reason, 'client');
  assert.equal(result.rejected_text, '第一次调整', 'the text comes back as data');
  assert.equal(input.value, '后来写的新想法', 'and the composer is left alone');
  assert.equal(toasts.length, 1, 'the failure is still reported');
});

test('clearing a settled request never takes an unresolved one with it', async () => {
  /* T2's third case: the cleanup that runs after a later request must not remove
   * the marker standing for an *earlier* request whose outcome is unknown. The
   * markers are keyed per request id, so clearing one is not clearing the other.
   */
  const markers = new Map([
    ['studio-job-pending:run-1', JSON.stringify({request_id: 'request-1', text: '第一次'})],
  ]);
  const app = loadApp({
    dom: makeDom(['chat-input', 'send', 'toast']),
    storage: {getItem: k => (markers.has(k) ? markers.get(k) : null),
              setItem: (k, v) => markers.set(k, String(v)),
              removeItem: k => markers.delete(k), clear: () => markers.clear()},
    globals: {
      __submitJob: async () => ({accepted: false, uncertain: false, request_id: 'request-9',
                                 reason: '这一轮已经结束了。'}),
      __submitSteer: async () => ({ok: false, reason: 'job_scope'}),
      __canSteerJob: () => false,
    },
  });
  app.ctx.__job = null;
  vm.runInContext(`state.runId='run-1';state.rail=[];state.timeline={questions:[]};
    state.busy=true;state.recoveryBlocked=false;currentJob=globalThis.__job;
    renderDraft=()=>{};renderPane=()=>{};renderConversation=()=>{};setConversationOpen=()=>{};setProjectFocus=()=>{};null;`, app.ctx);
  const input = app.ctx.document.getElementById('chat-input');

  // The unresolved first request is what blocks a resend — the mechanism that
  // makes the identity worth keeping at all.
  input.value = '第二次';
  const blocked = await app.ctx.submit();
  assert.equal(blocked.uncertain, true);
  assert.match(String(blocked.reason), /还没有确认/);
  assert.deepEqual([...markers.keys()].filter(key=>key.startsWith('studio-job-pending:')),
    ['studio-job-pending:run-1'], 'still just the original request marker; composer drafts have their own key');

  // Once that outcome is settled, a later request proceeds — and its own
  // definite rejection clears only its own marker.
  markers.clear();
  input.value = '第三次';
  const sent = await app.ctx.submit();
  assert.equal(sent.accepted, false);
  assert.equal(sent.uncertain, false);
  assert.ok(!markers.has('studio-job-pending:run-1'),
    'the later request\'s marker is gone, and nothing else was touched');
});

/* ----------------------------------------------------------------- A1 ----- */

test('a retried amendment click reuses the submission identity', async () => {
  /* A lost response is resolved by the server returning the amendment it already
   * recorded, which only works if the retry names the same submission. The page
   * keeps that key until the request is answered, then drops it. */
  const failure = Object.assign(new Error('网络中断'), {status: 500});
  const app = wire({routes: {'brief-amendment': failure}});
  const pending = decisionPending({
    brief_fingerprint: 'brief-v1-retry',
    amend: {clause_id: 'goal.primary', op: 'replace', text: '新目标',
            current_text: '旧目标', valid: true, reason: '', recommended: 'A', key: 'A'},
  });
  const gate = bind(app, mountGate({}, pending));
  gate.querySelector('[data-act="amend_brief"]').click();
  await settle();
  gate.querySelector('[data-act="amend_brief"]').click();
  await settle();

  const posts = app.observed.filter(call => call.path.endsWith('/brief-amendment'));
  assert.equal(posts.length, 2, 'the click was attempted twice');
  assert.ok(posts[0].body.request_id, 'the first attempt carries an identity');
  assert.equal(posts[1].body.request_id, posts[0].body.request_id,
    'and the retry reuses it, so the server resolves instead of duplicating');
});

/* ----------------------------------------------------------------- F6 ---- */

/* Drive the real composer: set the textarea, then call the same `submit()` the
 * Enter handler and the send button both call. `send()` is the shipped one and
 * so are its busy/steer/refusal branches; only `submitJob`/`submitSteer` are
 * replaced, through the runtime seam the real page also goes through. */
function composer({busy = false, currentJob = null, recoveryBlocked = false, accept = true,
                   steer = {ok: true}, submitJobResult = null, onSubmitJob = null,
                   steerError = null} = {}) {
  const jobs = [];
  const steers = [];
  const app = loadApp({
    dom: makeDom(['chat-input', 'send', 'toast']),
    globals: {
      __submitJob: async (runId, payload) => {
        jobs.push({runId, payload});
        if (onSubmitJob) onSubmitJob({payload, runId,
          sessionStorage: app.ctx.sessionStorage, calls: app.observed});
        if (app.ctx.__holdSend) await app.ctx.__holdSend;
        if (app.ctx.__switchTo) {
          app.ctx.__switchTo = false;
          vm.runInContext(`state.runId = 'run-2';`, app.ctx);
          throw new Error('项目已切换');
        }
        if (submitJobResult) return submitJobResult;
        if (!accept) throw new Error('拒绝');
      },
      // `runtime.submitSteer` resolves `{ok:true}` when the adjustment is
      // accepted and `{ok:false, reason:'job_not_active'}` when the old turn had
      // already settled; only the second one lets `send` open a new turn.
      __submitSteer: async (runId, jobId, payload) => {
        steers.push({runId, jobId, payload});
        if (steerError) throw steerError;
        return steer;
      },
      __canSteerJob: (job) => Boolean(job &&
        (job.status === 'running' || (job.status === 'cancelling' && job.steer_status === 'received'))),
    },
  });
  app.ctx.__job = currentJob;
  app.ctx.__busy = busy;
  app.ctx.__recoveryBlocked = recoveryBlocked;
  vm.runInContext(`
    state.runId = 'run-1';
    state.rail = [];
    state.timeline = {questions: []};
    state.busy = globalThis.__busy;
    state.recoveryBlocked = globalThis.__recoveryBlocked;
    currentJob = globalThis.__job;
    renderDraft = () => {};
    renderPane = () => {};
    renderConversation = () => {};
    setConversationOpen = () => {};
    setProjectFocus = () => {};
    null;
  `, app.ctx);
  app.jobs = jobs;
  app.steers = steers;
  Object.defineProperty(app, 'toastText', {get: () => app.elements.get('toast').textContent});
  return app;
}

test('clarification choice waits in the composer and sends its answer with the question id', async () => {
  const app = composer();
  vm.runInContext(webSource('journey.js'), app.ctx, {filename: 'journey.js'});
  const question = {id: 'a'.repeat(32), question: '更偏哪种表达？', recommended: 0,
    options: [{label: '日常分享'}, {label: '直接演示'}]};
  const card = parseHtml(app.ctx.questionCardHtml(question))[0];
  const input = app.ctx.document.getElementById('chat-input');
  input.value = '不要夸张表演';
  app.ctx.bindQuestionChoices(card, question);
  card.querySelector('[data-question-choice="0"]').click();
  assert.match(input.value, /日常分享/);
  assert.match(input.value, /不要夸张表演/);
  assert.equal(app.jobs.length, 0, 'picking an option must not start a model call');
  card.querySelector('[data-question-choice="1"]').click();
  assert.match(input.value, /直接演示/);
  assert.doesNotMatch(input.value, /日常分享/);
  await app.ctx.submit();
  assert.equal(app.jobs.length, 1);
  assert.equal(app.jobs[0].payload.question_id, question.id);
  assert.match(app.jobs[0].payload.text, /不要夸张表演/);
});

test('Enter during a queued task keeps the message instead of eating it', async () => {
  const app = composer({busy: true,
    currentJob: {job_id: 'j1', job_kind: 'conversation', status: 'queued'}});
  const input = app.ctx.document.getElementById('chat-input');
  input.value = '把开场改成手部特写';
  const result = await app.ctx.submit();
  assert.equal(result.accepted, false);
  assert.equal(input.value, '把开场改成手部特写', 'the unsent text must survive the refusal');
  assert.equal(app.jobs.length, 0);
  assert.match(result.reason, /正在停止|正在排队|正在执行/);
});

test('Enter during a video analysis saves a midflight message for the producer', async () => {
  const app = composer({busy: true,
    currentJob: {job_id: 'j2', job_kind: 'video_analysis', status: 'running'}});
  const input = app.ctx.document.getElementById('chat-input');
  input.value = '这条也分析一下';
  const result = await app.ctx.submit();
  assert.equal(result.accepted, true);
  assert.equal(input.value, '');
  assert.equal(app.jobs.length, 0);
  assert.equal(app.steers.length, 1);
  assert.equal(app.steers[0].payload.text, '这条也分析一下');
});

test('a cancelling task also keeps the message rather than dropping it', async () => {
  const app = composer({busy: true,
    currentJob: {job_id: 'j5', job_kind: 'video_analysis', status: 'cancelling'}});
  const input = app.ctx.document.getElementById('chat-input');
  input.value = '停下来之后再说';
  await app.ctx.submit();
  assert.equal(input.value, '停下来之后再说');
  assert.equal(app.jobs.length, 0);
});

test('a task that is actually over still starts a new round', async () => {
  const app = composer({busy: true,
    currentJob: {job_id: 'j3', job_kind: 'video_analysis', status: 'succeeded'}});
  const input = app.ctx.document.getElementById('chat-input');
  input.value = '继续做方案';
  await app.ctx.submit();
  assert.equal(app.jobs.length, 1, 'a finished task must not block the next prompt');
  assert.equal(input.value, '', 'an accepted send clears the box');
});

test('a running conversation accepts an adjustment through the composer', async () => {
  const app = composer({busy: true, steer: {ok: true},
    currentJob: {job_id: 'j4', job_kind: 'conversation', status: 'running', goal_revision: 2}});
  const input = app.ctx.document.getElementById('chat-input');
  input.value = '改成 12 秒';
  const result = await app.ctx.submit();
  assert.equal(result.accepted, true);
  assert.equal(app.steers.length, 1);
  assert.equal(app.jobs.length, 0);
  assert.equal(input.value, '');
});

test('a direct adjustment whose old turn ended opens a fresh turn with the same text', async () => {
  const app = composer({busy: true, steer: {ok: false, reason: 'job_not_active'},
    currentJob: {job_id: 'j6', job_kind: 'conversation', status: 'running', goal_revision: 1}});
  const input = app.ctx.document.getElementById('chat-input');
  input.value = '旧任务已经结束了';
  await app.ctx.send(input.value);
  assert.equal(app.steers.length, 1);
  assert.equal(app.jobs.length, 1, 'the prompt is not lost when the steer target is gone');
  assert.equal(app.jobs[0].payload.text, '旧任务已经结束了');
  assert.equal(input.value, '旧任务已经结束了');
});

test('a refused request puts the text back and reports why', async () => {
  const app = composer({accept: false});
  const input = app.ctx.document.getElementById('chat-input');
  input.value = '换了模型再试';
  const result = await app.ctx.submit();
  assert.equal(result.accepted, false);
  assert.equal(input.value, '换了模型再试');
  assert.equal(app.toastText, '拒绝');
});

test('project switch during a refused send never writes into the other project composer', async () => {
  const app = composer();
  app.ctx.__switchTo = true;
  const input = app.ctx.document.getElementById('chat-input');
  input.value = '这条属于旧项目';
  const result = await app.ctx.submit();
  assert.equal(result.accepted, false);
  assert.equal(result.draft_restored, false, 'the refusal must not claim to have restored the text');
  assert.equal(input.value, '', 'the text belongs to run-1; the visible box is run-2');
});
test('a blocked recovery state refuses before clearing anything', async () => {
  const app = composer({recoveryBlocked: true});
  const input = app.ctx.document.getElementById('chat-input');
  input.value = '等恢复完再说';
  const result = await app.ctx.submit();
  assert.equal(result.accepted, false);
  assert.equal(input.value, '等恢复完再说');
  assert.equal(app.jobs.length, 0);
});

test('Enter and send submit while stop has a separate button', () => {
  const source = webSource('app.js');
  const keydown = source.slice(source.indexOf('$("chat-input").addEventListener("keydown"'),
                               source.indexOf('$("chat-input").addEventListener("input"'));
  assert.match(keydown, /submit\(\)/, 'Enter goes through submit, not a second path');
  assert.match(keydown, /!e\.shiftKey/, 'Shift+Enter still inserts a newline');
  assert.match(source, /\$\("send"\)\.onclick = \(\) => \{/);
  // Both buttons dispatch on the mode the renderer set, so a press can never do
  // the opposite of the word printed on it.
  assert.match(source, /dataset\.mode === 'stop'/, 'the primary button reads its own mode');
  const secondary=source.slice(source.indexOf('$("job-stop").onclick'),source.indexOf('$("chat-input").addEventListener("keydown"'));
  assert.match(secondary,/dataset\.mode==='stop-query'/,'query cancellation reads the rendered secondary mode');
  assert.match(secondary,/stopCurrentJob\(\);return;/);
  assert.match(secondary,/void submit\(\);/,'ordinary adjustment still goes through submit');
});

test('the composer draft tray also clears only after the request is accepted', async () => {
  const source = webSource('app.js');
  const start = source.indexOf('  sendBtn.onclick = async () => {');
  assert.ok(start > 0, 'the draft tray handler is found by its real shape');
  const end = source.indexOf('\n  };', start);

  /* Run the handler with `renderPane` bound to a probe, so the body can read
   * back whether the tray was rebuilt. Refusal returns before touching it. */
  const run = async (accepted) => {
    const button = {disabled: false};
    const state = {runId: 'fixture', busy: false, recoveryBlocked: false,
                   draft: {k: {text: 'Keep me'}}};
    const ctx = {
      sendBtn: button, state,
      composeDraft: () => 'Keep me',
      send: async () => ({accepted}),
      saveDraft: () => {},
      renderDraft: () => {},
      renderPane: () => { ctx.renderPane.wasCalled = true; },
    };
    vm.createContext(ctx);
    vm.runInContext(source.slice(start, end + 5), ctx);
    await button.onclick();
    return ctx.renderPane.wasCalled === true;
  };

  assert.equal(await run(false), false, 'a refused send must not discard the drafted answers');
  assert.equal(await run(true), true, 'an accepted send does clear them');
});

/* ----------------------------------------------------------------- U3 ---- */

/* One submission identity stands for exactly one immutable payload. The id in
 * localStorage is a *retry token* for that payload: the same click retries it,
 * a new decision mints a new one, and an unresolved old record keeps its own
 * verification path instead of being overwritten by the next proposal. */
const AMEND_KEY = 'studio-amendment:run-1:goal.primary';

function storedAmendment(storage) {
  const raw = storage.getItem(AMEND_KEY);
  if (!raw) return null;
  try { return JSON.parse(raw); } catch { return null; }
}

const lostResponse = () => { throw new TypeError('Failed to fetch'); };

function proposalGate(text, extra = {}) {
  return {...proposalPending(text, '做一个种草视频'), ...extra};
}

test('a lost amendment response keeps its full submission record', async () => {
  const storage = makeStorage();
  const app = wire({routes: {'brief-amendment': lostResponse}, storage});
  const gate = bind(app, mountGate({}, proposalGate('只拍设计细节种草')));

  gate.querySelector('[data-act="amend_brief"]').click();
  await settle();

  const record = storedAmendment(storage);
  assert.ok(record, 'the page keeps what it sent, not just a bare id');
  assert.equal(record.unresolved, true, 'and knows its outcome is still unknown');
  assert.equal(record.run_id, 'run-1');
  assert.equal(record.request_id, 'req-1');
  assert.equal(record.body.changes[0].text, '只拍设计细节种草');
  assert.equal(record.body.approve, true);
  assert.match(gate.querySelector('.gate-feedback').textContent, /修订失败/);
});

test('a new proposal does not reuse the lost response identity', async () => {
  /* The full U3 flow across a refresh: B is written server-side, its answer is
   * lost, the page reloads onto a direction proposing C for the same clause.
   * C must neither go out under B's id nor overwrite B's record — the entry
   * directs to confirming B first, then C gets its own identity. */
  const storage = makeStorage();
  const before = wire({routes: {'brief-amendment': lostResponse}, storage});
  const gateB = bind(before, mountGate({}, proposalGate('只拍设计细节种草')));
  gateB.querySelector('[data-act="amend_brief"]').click();
  await settle();
  const original = storedAmendment(storage);
  assert.ok(original, 'B is recorded as unresolved');

  const posts = [];
  const after = wire({
    routes: {'brief-amendment': ({body}) => {
      posts.push(body);
      return {id: 'amd-1', status: 'approved', redo: {status: 'started', job_id: 'job-amend'}};
    }},
    storage,
  });
  const gateC = bind(after, mountGate({}, proposalGate('改回真人出镜种草',
    {artifact_sha256: SHA_B, version: 3, brief_fingerprint: 'brief-v2-xyz'})));

  gateC.querySelector('[data-act="amend_brief"]').click();
  await settle();
  assert.equal(posts.length, 0, 'the new decision did not go out under the old id');
  assert.match(gateC.querySelector('.gate-feedback').textContent, /还没有确认/);
  const still = storedAmendment(storage);
  assert.equal(still.request_id, original.request_id, 'B’s record was not overwritten');
  assert.equal(still.body.changes[0].text, '只拍设计细节种草');
  const confirm = gateC.querySelector('.gate-confirm');
  assert.ok(confirm, 'the card offers the way to confirm the old submission first');

  confirm.click();
  await settle();
  assert.equal(posts.length, 1, 'confirming resends exactly the original submission');
  assert.equal(posts[0].request_id, original.request_id, 'same identity, verbatim payload');
  assert.equal(posts[0].changes[0].text, '只拍设计细节种草');
  assert.equal(posts[0].expected_brief_fingerprint,
    original.body.expected_brief_fingerprint, 'the original baseline, not the current card’s');
  assert.equal(storedAmendment(storage), null, 'the confirmed identity is settled');
  assert.match(gateC.querySelector('.gate-feedback').textContent, /已确认记录/);

  gateC.querySelector('[data-act="amend_brief"]').click();
  await settle();
  assert.equal(posts.length, 2, 'and now the new decision can go');
  assert.notEqual(posts[1].request_id, original.request_id, 'under its own identity');
  assert.equal(posts[1].changes[0].text, '改回真人出镜种草');
  assert.equal(posts[1].expected_brief_fingerprint, 'brief-v2-xyz');
  assert.equal(storedAmendment(storage), null, 'and settles on success');
});

test('a definite rejection clears the submission record', async () => {
  const storage = makeStorage();
  const posts = [];
  const app = wire({
    routes: {'brief-amendment': ({body}) => {
      posts.push(body);
      throw Object.assign(new Error('没有实际变更'), {status: 400});
    }},
    storage,
  });
  const gate = bind(app, mountGate({}, proposalGate('目标收窄为设计细节种草')));

  gate.querySelector('[data-act="amend_brief"]').click();
  await settle();
  assert.equal(posts.length, 1);
  assert.equal(storedAmendment(storage), null,
    'a 4xx means the server refused it — there is no submission left to confirm');

  gate.querySelector('[data-act="amend_brief"]').click();
  await settle();
  assert.equal(posts.length, 2);
  assert.notEqual(posts[1].request_id, posts[0].request_id,
    'the next attempt is a new submission, not a replay of a refused one');
});

test('the structured direction card follows the same one-identity rule', async () => {
  const storage = makeStorage();
  const before = wire({routes: {'brief-amendment': lostResponse}, storage});
  const firstCard = decisionPending({
    version: 1, artifact_sha256: SHA, brief_fingerprint: 'brief-v1',
    amend: {clause_id: 'goal.primary', op: 'replace', text: '只拍设计细节种草',
            current_text: '做一个种草视频', valid: true, reason: '',
            recommended: 'A', key: 'A'},
  });
  const {gate: gateB} = mountStructuredGate(firstCard);
  bind(before, gateB);
  gateB.querySelector('[data-act="amend_brief"]').click();
  await settle();
  const original = storedAmendment(storage);
  assert.ok(original, 'the structured card records the lost submission too');

  const posts = [];
  const after = wire({
    routes: {'brief-amendment': ({body}) => {
      posts.push(body);
      return {id: 'amd-2', status: 'approved', redo: {status: 'started', job_id: 'job-amend-2'}};
    }},
    storage,
  });
  const nextCard = decisionPending({
    version: 3, artifact_sha256: SHA_B, brief_fingerprint: 'brief-v2',
    amend: {clause_id: 'goal.primary', op: 'replace', text: '改回真人出镜种草',
            current_text: '做一个种草视频', valid: true, reason: '',
            recommended: 'A', key: 'A'},
  });
  const {gate: gateC} = mountStructuredGate(nextCard);
  bind(after, gateC);

  gateC.querySelector('[data-act="amend_brief"]').click();
  await settle();
  assert.equal(posts.length, 0, 'the structured card refuses the identity swap too');
  assert.match(gateC.querySelector('.gate-feedback').textContent, /还没有确认/);
  assert.equal(storedAmendment(storage).request_id, original.request_id);

  const confirm = gateC.querySelector('.gate-confirm');
  assert.ok(confirm, 'and offers the same confirmation entry');
  confirm.click();
  await settle();
  assert.equal(posts[0].request_id, original.request_id);
  assert.equal(posts[0].changes[0].text, '只拍设计细节种草');

  gateC.querySelector('[data-act="amend_brief"]').click();
  await settle();
  assert.equal(posts.length, 2);
  assert.notEqual(posts[1].request_id, original.request_id);
  assert.equal(posts[1].changes[0].text, '改回真人出镜种草');
});
