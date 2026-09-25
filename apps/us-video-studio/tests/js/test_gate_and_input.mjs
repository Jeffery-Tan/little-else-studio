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
import {loadApp, makeDom, parseHtml, webSource} from './dom-fixture.mjs';

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
function wire({routes = {}} = {}) {
  const app = loadApp({dom: makeDom(['chat-input', 'send', 'stage', 'toast']), routes});
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
  assert.equal(post.body.expected_artifact_sha256, SHA);
  assert.equal(app.refreshed, 1);
  assert.equal(app.toastIsError, false);
  assert.match(app.toastText, /重做/);
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
    '请写明需要调整的地方，或覆盖审核的具体理由。');
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

test('a failed amendment write keeps the note and reports it on the card', async () => {
  const failure = Object.assign(new Error('连接中断'), {status: 500});
  const app = wire({routes: {'brief-amendment': failure}});
  const gate = bind(app, mountGate({}, proposalPending()));
  gate.querySelector('.reason').value = '请一并收窄目标';
  gate.querySelector('[data-act="amend_brief"]').click();
  await settle();
  assert.equal(gate.querySelector('.reason').value, '请一并收窄目标');
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

/* ----------------------------------------------------------------- F6 ---- */

/* Drive the real composer: set the textarea, then call the same `submit()` the
 * Enter handler and the send button both call. `send()` is the shipped one and
 * so are its busy/steer/refusal branches; only `submitJob`/`submitSteer` are
 * replaced, through the runtime seam the real page also goes through. */
function composer({busy = false, currentJob = null, recoveryBlocked = false, accept = true,
                   steer = {ok: true}} = {}) {
  const jobs = [];
  const steers = [];
  const app = loadApp({
    dom: makeDom(['chat-input', 'send', 'toast']),
    globals: {
      __submitJob: async (runId, payload) => {
        jobs.push({runId, payload});
        if (app.ctx.__switchTo) {
          app.ctx.__switchTo = false;
          vm.runInContext(`state.runId = 'run-2';`, app.ctx);
          throw new Error('项目已切换');
        }
        if (!accept) throw new Error('拒绝');
      },
      // `runtime.submitSteer` resolves `{ok:true}` when the adjustment is
      // accepted and `{ok:false, reason:'job_not_active'}` when the old turn had
      // already settled; only the second one lets `send` open a new turn.
      __submitSteer: async (runId, jobId, payload) => {
        steers.push({runId, jobId, payload});
        return steer;
      },
      __canSteerJob: (job) => Boolean(job && job.job_kind === 'conversation'
        && (job.status === 'running' || job.status === 'cancelling')),
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

test('Enter during a queued task keeps the message instead of eating it', async () => {
  const app = composer({busy: true,
    currentJob: {job_id: 'j1', job_kind: 'conversation', status: 'queued'}});
  const input = app.ctx.document.getElementById('chat-input');
  input.value = '把开场改成手部特写';
  const result = await app.ctx.submit();
  assert.equal(result.accepted, false);
  assert.equal(input.value, '把开场改成手部特写', 'the unsent text must survive the refusal');
  assert.equal(app.jobs.length, 0);
  assert.match(app.toastText, /仍在执行本轮/);
});

test('Enter during a video analysis keeps the message and opens no turn', async () => {
  const app = composer({busy: true,
    currentJob: {job_id: 'j2', job_kind: 'video_analysis', status: 'running'}});
  const input = app.ctx.document.getElementById('chat-input');
  input.value = '这条也分析一下';
  const result = await app.ctx.submit();
  assert.equal(result.accepted, false);
  assert.equal(input.value, '这条也分析一下');
  assert.equal(app.jobs.length, 0);
  assert.equal(app.steers.length, 0);
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

test('a running conversation is adjusted, not turned into a second request', async () => {
  const app = composer({busy: true, steer: {ok: true},
    currentJob: {job_id: 'j4', job_kind: 'conversation', status: 'running', goal_revision: 2}});
  const input = app.ctx.document.getElementById('chat-input');
  input.value = '改成 12 秒';
  await app.ctx.submit();
  assert.equal(app.steers.length, 1);
  assert.equal(app.steers[0].payload.text, '改成 12 秒');
  assert.equal(app.steers[0].payload.expected_goal_revision, 2);
  assert.equal(app.jobs.length, 0, 'an adjustment is not a new conversation turn');
  assert.equal(input.value, '');
});

test('an adjustment whose old turn already ended opens a fresh turn with the same text', async () => {
  const app = composer({busy: true, steer: {ok: false, reason: 'job_not_active'},
    currentJob: {job_id: 'j6', job_kind: 'conversation', status: 'running', goal_revision: 1}});
  const input = app.ctx.document.getElementById('chat-input');
  input.value = '旧任务已经结束了';
  await app.ctx.submit();
  assert.equal(app.steers.length, 1);
  assert.equal(app.jobs.length, 1, 'the prompt is not lost when the steer target is gone');
  assert.equal(app.jobs[0].payload.text, '旧任务已经结束了');
  assert.equal(input.value, '');
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
  assert.equal(result.kept, false, 'the refusal must not claim to have restored the text');
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

test('the keyboard and the button run the same admission rule', () => {
  const source = webSource('app.js');
  const keydown = source.slice(source.indexOf('$("chat-input").addEventListener("keydown"'),
                               source.indexOf('$("chat-input").addEventListener("input"'));
  assert.match(keydown, /submit\(\)/, 'Enter goes through submit, not a second path');
  assert.match(keydown, /!e\.shiftKey/, 'Shift+Enter still inserts a newline');
  assert.match(source, /\$\("send"\)\.onclick = \(\) => \{ void submit\(\); \};/,
    'the button goes through the same submit');
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
