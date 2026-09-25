/* U2 — one submission in flight per project, job or adjustment alike.
 *
 * The pending guard used to read only `studio-job-pending:<run>`, while a
 * running adjustment is saved under `studio-steer-pending:<run>`. So an
 * adjustment whose answer was lost did not enter the admission check: the next
 * Enter sent a second steer under a fresh request_id, overwrote the original
 * pending record, and a rejection of that second attempt then deleted the
 * shared marker — the first request's local verification identity was gone.
 *
 * These tests load the *real* `app.js` and the *real* `runtime.js` the page
 * loads (same order as index.html, via `runtime: 'real'`), and replace only
 * the transport (`fetch`) and the clock. `submit`, `send`, `pendingSubmission`,
 * `submitSteer`, `submitJob` and `restoreJob` are the shipped code.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {loadApp, makeStorage} from './dom-fixture.mjs';

/** Let async click handlers and their promise chains finish before asserting. */
async function settle() {
  for (let i = 0; i < 8; i += 1) await new Promise(resolve => setImmediate(resolve));
}

/* A page with the real runtime. `uuidRef` lets two page loads in one test keep
 * one request-id counter, so a "refresh" does not renumber identities. */
function page({routes = {}, storage = makeStorage(), uuidRef = {n: 0}} = {}) {
  const timers = [];
  const app = loadApp({
    runtime: 'real',
    routes,
    storage,
    globals: {
      crypto: {randomUUID: () => `req-${++uuidRef.n}`},
      setTimeout: (fn, ms) => { timers.push({fn, ms}); return timers.length; },
      clearTimeout: (id) => { if (id && timers[id - 1]) timers[id - 1] = null; },
    },
  });
  app.timers = timers;
  Object.defineProperty(app, 'toastText', {get: () => app.elements.get('toast').textContent});
  Object.defineProperty(app, 'toastIsError', {get: () => app.elements.get('toast').classList.contains('error')});
  return app;
}

const RUNNING = {job_id: 'j1', job_kind: 'conversation', status: 'running',
                 goal_revision: 0, events: []};

function openRun(app, job = RUNNING, {busy = true} = {}) {
  app.ctx.__job = job;
  app.ctx.__busy = busy;
  vm.runInContext(`
    state.runId = 'run-1';
    state.rail = [];
    state.timeline = {questions: []};
    state.busy = globalThis.__busy;
    state.recoveryBlocked = false;
    currentJob = globalThis.__job;
    null;
  `, app.ctx);
}

function descendants(node, predicate) {
  return [...(predicate(node) ? [node] : []), ...node.children.flatMap(c => descendants(c, predicate))];
}

/* `$` and `state` are lexical bindings inside the scripts, not properties of
 * the context object — reach them the way the other controller tests do. */
function el(app, id) { return app.ctx.document.getElementById(id); }
function read(app, expression) { return vm.runInContext(expression, app.ctx); }

/* Steer transport whose per-call behaviour is a script: return a job to accept,
 * throw to lose or reject. Calls are recorded in order. */
function steerTransport(script) {
  const calls = [];
  const route = async ({body}) => { calls.push(body); return script(calls.length, body); };
  route.calls = calls;
  return route;
}

function jobsTransport(script) {
  const calls = [];
  const route = async ({body}) => { calls.push(body); return script(calls.length, body); };
  route.calls = calls;
  return route;
}

const networkLost = () => { throw new TypeError('Failed to fetch'); };
const ACCEPTED_STEER = {job_id: 'j1', job_kind: 'conversation', status: 'running',
                        goal_revision: 1, events: []};

/* --------------------------------------------------- unknown adjustment -- */

test('an unknown adjustment cannot be resent under a new identity', async () => {
  // The server may have received the first adjustment even though the client
  // never saw an answer (Failed to fetch — no HTTP status either way).
  const steer = steerTransport(networkLost);
  const p = page({routes: {'/steer': steer, 'jobs/latest': () => RUNNING}});
  openRun(p);

  const input = el(p, 'chat-input');
  input.value = '调整为十二秒';
  const first = await p.ctx.submit();
  assert.equal(first.uncertain, true, 'lost response is unknown, not refused');
  assert.equal(first.draft_restored, false, 'unknown words are not handed back to resend');
  assert.equal(input.value, '');
  const original = JSON.parse(p.storage.getItem('studio-steer-pending:run-1'));
  assert.equal(original.request_id, 'req-1');
  assert.equal(original.text, '调整为十二秒');

  // Second Enter with the same or another adjustment: no second identity, and
  // the original record must survive untouched.
  input.value = '调整为十二秒';
  const second = await p.ctx.submit();
  assert.equal(second.uncertain, true, 'the reply is still unknown');
  assert.equal(steer.calls.length, 1, 'no second request identity went out');
  const still = JSON.parse(p.storage.getItem('studio-steer-pending:run-1'));
  assert.equal(still.request_id, original.request_id, 'the original identity was not overwritten');
  assert.equal(still.text, '调整为十二秒');
  assert.equal(input.value, '调整为十二秒', 'the newer draft is left alone');
  assert.match(p.toastText, /还没有确认/);
  assert.doesNotMatch(p.toastText, /没有被接受/, 'unknown must not be downgraded to refused');
});

test('the send button refuses the same way while an adjustment is unknown', async () => {
  const steer = steerTransport(networkLost);
  const p = page({routes: {'/steer': steer, 'jobs/latest': () => RUNNING}});
  openRun(p);

  const input = el(p, 'chat-input');
  input.value = '调整为十二秒';
  await p.ctx.submit();
  assert.equal(steer.calls.length, 1);

  // The button runs the same admission rule as Enter (both call submit()).
  input.value = '再调整一次';
  await el(p, 'send').onclick();
  await settle();
  assert.equal(steer.calls.length, 1, 'the button did not create a second identity');
  const still = JSON.parse(p.storage.getItem('studio-steer-pending:run-1'));
  assert.equal(still.request_id, 'req-1');
  assert.equal(input.value, '再调整一次', 'the button refusal keeps the draft');
  assert.match(p.toastText, /还没有确认/);
});

test('an unknown adjustment still blocks a new conversation job once the old turn is over', async () => {
  // The job the adjustment targeted has ended, and the adjustment's answer is
  // still unknown. The next Enter must not open a fresh request identity either.
  const jobs = jobsTransport((n) => ({job_id: `job-${n}`, job_kind: 'conversation',
                                      status: 'running', events: []}));
  const p = page({routes: {'/jobs': jobs, 'jobs/latest': () => RUNNING}});
  p.storage.setItem('studio-steer-pending:run-1', JSON.stringify(
    {text: '调整为十二秒', request_id: 'req-1', job_id: 'j1', expected_goal_revision: 0}));
  openRun(p, {job_id: 'j1', job_kind: 'conversation', status: 'succeeded', events: []},
    {busy: false});

  const input = el(p, 'chat-input');
  input.value = '开始新的一轮';
  const result = await p.ctx.submit();
  assert.equal(result.uncertain, true);
  assert.equal(jobs.calls.length, 0, 'no new request identity while the adjustment is unresolved');
  const still = JSON.parse(p.storage.getItem('studio-steer-pending:run-1'));
  assert.equal(still.request_id, 'req-1', 'the adjustment record is untouched');
  assert.equal(input.value, '开始新的一轮', 'the draft stays for after it is resolved');
});

/* ------------------------------------------------------------- job path -- */

test('an unknown new job still blocks a second Enter on the real runtime', async () => {
  const jobs = jobsTransport(networkLost);
  const p = page({routes: {'/jobs': jobs, 'jobs/latest': () => RUNNING}});
  openRun(p, {job_id: 'j0', job_kind: 'conversation', status: 'succeeded', events: []},
    {busy: false});

  const input = el(p, 'chat-input');
  input.value = '请重做创意方向';
  const first = await p.ctx.submit();
  assert.equal(first.uncertain, true);
  assert.equal(jobs.calls.length, 1);
  const original = JSON.parse(p.storage.getItem('studio-job-pending:run-1'));
  assert.equal(original.request_id, 'req-1');

  input.value = '请重做创意方向';
  const second = await p.ctx.submit();
  assert.equal(second.uncertain, true);
  assert.equal(jobs.calls.length, 1, 'the unknown job keeps its single identity');
  assert.equal(JSON.parse(p.storage.getItem('studio-job-pending:run-1')).request_id,
    original.request_id);
  assert.equal(input.value, '请重做创意方向');
});

test('an accepted job clears its own pending and the next prompt is allowed', async () => {
  const jobs = jobsTransport((n) => ({job_id: `job-${n}`, job_kind: 'conversation',
                                      status: 'running', events: []}));
  const p = page({routes: {'/jobs': jobs, 'jobs/latest': () => RUNNING}});
  openRun(p, {job_id: 'j0', job_kind: 'conversation', status: 'succeeded', events: []},
    {busy: false});

  const input = el(p, 'chat-input');
  input.value = '第一条';
  const first = await p.ctx.submit();
  assert.equal(first.accepted, true);
  assert.equal(p.storage.getItem('studio-job-pending:run-1'), null,
    'acceptance confirms the request_id and clears its marker');

  input.value = '第二条';
  const second = await p.ctx.submit();
  assert.equal(second.accepted, true);
  assert.equal(jobs.calls.length, 2);
  assert.notEqual(jobs.calls[0].request_id, jobs.calls[1].request_id);
});

/* --------------------------------------------------- definite rejection -- */

test('a rejected adjustment clears only its own pending and allows a resend', async () => {
  const steer = steerTransport((n) => {
    if (n === 1) throw Object.assign(new Error('这一轮不能调整。'), {status: 400});
    return ACCEPTED_STEER;
  });
  const p = page({routes: {'/steer': steer, 'jobs/latest': () => RUNNING}});
  openRun(p);

  const input = el(p, 'chat-input');
  input.value = '调整为十二秒';
  const refused = await p.ctx.submit();
  assert.equal(refused.accepted, false);
  assert.equal(refused.uncertain, false, 'a 4xx is a definite refusal');
  assert.equal(refused.draft_restored, true, 'definite refusal returns the words');
  assert.equal(input.value, '调整为十二秒');
  assert.equal(p.storage.getItem('studio-steer-pending:run-1'), null,
    'the refusal cleared the marker for this same request_id');

  // The user corrects and resends — allowed, because the outcome is settled.
  input.value = '调整为十五秒';
  const resent = await p.ctx.submit();
  assert.equal(resent.accepted, true);
  assert.equal(steer.calls.length, 2);
  assert.equal(steer.calls[1].text, '调整为十五秒');
});

test('a newer same-project draft survives a late adjustment rejection on the real transport', async () => {
  let rejectFirst;
  const steer = steerTransport(() => new Promise((_, reject) => { rejectFirst = reject; }));
  const p = page({routes: {'/steer': steer, 'jobs/latest': () => RUNNING}});
  openRun(p);

  const input = el(p, 'chat-input');
  input.value = '第一次调整';
  const pending = p.ctx.submit();
  await settle();
  input.value = '后来写的新想法';
  p.ctx.updatesComposerVersion();
  rejectFirst(Object.assign(new Error('目标版本冲突'), {status: 409}));
  const result = await pending;
  assert.equal(result.accepted, false);
  assert.equal(result.draft_restored, false);
  assert.equal(input.value, '后来写的新想法', 'the late 409 does not overwrite newer words');
  assert.equal(p.storage.getItem('studio-steer-pending:run-1'), null,
    'its own settled marker is gone');
});

/* ------------------------------------------------------- identity rules -- */

test('a request for another identity cannot displace an unresolved adjustment', async () => {
  const steer = steerTransport(() => ACCEPTED_STEER);
  const p = page({routes: {'/steer': steer, 'jobs/latest': () => RUNNING}});
  openRun(p);
  p.storage.setItem('studio-steer-pending:run-1', JSON.stringify(
    {text: '旧调整', request_id: 'req-old', job_id: 'j1', expected_goal_revision: 0}));

  const result = await p.ctx.submitSteer('run-1', 'j1',
    {text: '新调整', request_id: 'req-new', expected_goal_revision: 0});
  assert.equal(result.ok, false, 'the second identity is refused, not sent');
  assert.equal(steer.calls.length, 0, 'nothing went out under a new identity');
  const still = JSON.parse(p.storage.getItem('studio-steer-pending:run-1'));
  assert.equal(still.request_id, 'req-old', 'the unresolved record was not overwritten');
  assert.equal(still.text, '旧调整');
});

test('a request for another identity cannot displace an unresolved job submission', async () => {
  const jobs = jobsTransport((n) => ({job_id: `job-${n}`, job_kind: 'conversation',
                                      status: 'running', events: []}));
  const p = page({routes: {'/jobs': jobs, 'jobs/latest': () => RUNNING}});
  openRun(p, {job_id: 'j0', job_kind: 'conversation', status: 'succeeded', events: []},
    {busy: false});
  p.storage.setItem('studio-job-pending:run-1', JSON.stringify(
    {text: '旧请求', request_id: 'req-old'}));

  const result = await p.ctx.submitJob('run-1', {text: '新请求', request_id: 'req-new'});
  assert.equal(result.accepted, false);
  assert.equal(result.uncertain, true, 'the unresolved one keeps the verdict');
  assert.equal(jobs.calls.length, 0, 'no second identity went out');
  assert.equal(JSON.parse(p.storage.getItem('studio-job-pending:run-1')).request_id, 'req-old');
});

test('an unresolved adjustment in one project does not block another project', async () => {
  const jobs = jobsTransport((n) => ({job_id: `job-${n}`, job_kind: 'conversation',
                                      status: 'running', events: []}));
  const p = page({routes: {'/jobs': jobs, 'jobs/latest': () => RUNNING}});
  openRun(p, {job_id: 'j1', job_kind: 'conversation', status: 'succeeded', events: []},
    {busy: false});
  p.storage.setItem('studio-steer-pending:run-1', JSON.stringify(
    {text: '调整为十二秒', request_id: 'req-1', job_id: 'j1', expected_goal_revision: 0}));

  vm.runInContext(`state.runId = 'run-2'; state.busy = false;`, p.ctx);
  const input = el(p, 'chat-input');
  input.value = '另一个项目的消息';
  const result = await p.ctx.submit();
  assert.equal(result.accepted, true, 'the other project sends normally');
  assert.equal(jobs.calls.length, 1);
  assert.equal(jobs.calls[0].text, '另一个项目的消息');
  const foreign = JSON.parse(p.storage.getItem('studio-steer-pending:run-1'));
  assert.equal(foreign.request_id, 'req-1', 'run-1\'s record is untouched');
});

/* ------------------------------------------------------ refresh recovery -- */

test('an unknown adjustment survives a refresh and the confirm entry retries the original identity', async () => {
  const uuidRef = {n: 0};
  const steer = steerTransport((n) => {
    if (n === 1) return networkLost();
    return {job_id: 'j1', job_kind: 'conversation', status: 'running',
            goal_revision: 1, events: []};
  });
  const latest = () => RUNNING;
  const routes = {'/steer': steer, 'jobs/latest': latest};

  // First page load: send an adjustment, lose the answer.
  const before = page({routes, uuidRef});
  openRun(before);
  const firstInput = el(before, 'chat-input');
  firstInput.value = '调整为十二秒';
  await before.ctx.submit();
  assert.equal(steer.calls.length, 1);
  const original = JSON.parse(before.storage.getItem('studio-steer-pending:run-1'));
  assert.equal(original.request_id, 'req-1');

  // Refresh: a new page, same sessionStorage. The record must still be there
  // and its confirm entry must retry the *saved* payload, not rebuild it.
  const after = page({routes, uuidRef, storage: before.storage});
  openRun(after);
  await after.ctx.restoreJob('run-1');
  const button = descendants(el(after, 'job-status'), n => n.textContent === '确认调整状态')[0];
  assert.ok(button, 'the refresh offers the confirmation entry for the lost adjustment');
  await button.onclick();
  await settle();
  assert.equal(steer.calls.length, 2, 'the confirm entry retried');
  assert.equal(steer.calls[1].request_id, original.request_id, 'same identity, not a new one');
  assert.equal(steer.calls[1].text, '调整为十二秒', 'original payload, not rebuilt from the box');
  assert.equal(after.storage.getItem('studio-steer-pending:run-1'), null,
    'the confirmed identity is settled');

  // Not a permanent lock: with the outcome settled, a new adjustment goes out.
  const input = el(after, 'chat-input');
  input.value = '改成十五秒';
  const next = await after.ctx.submit();
  assert.equal(next.accepted, true);
  assert.equal(steer.calls.length, 3);
  assert.notEqual(steer.calls[2].request_id, original.request_id);
});

/* ------------------------------------------------------------ draft tray -- */

test('the draft tray cannot send through an unresolved adjustment either', async () => {
  const jobs = jobsTransport((n) => ({job_id: `job-${n}`, job_kind: 'conversation',
                                      status: 'running', events: []}));
  const p = page({routes: {'/jobs': jobs, 'jobs/latest': () => RUNNING}});
  p.storage.setItem('studio-steer-pending:run-1', JSON.stringify(
    {text: '调整为十二秒', request_id: 'req-1', job_id: 'j1', expected_goal_revision: 0}));
  openRun(p, {job_id: 'j1', job_kind: 'conversation', status: 'succeeded', events: []},
    {busy: false});

  vm.runInContext(`
    state.draft = {q1: {text: '自然美式英语', question: '台词风格？', prefix: '关于台词：',
                        stage: 'writing', version: 1}};
    renderDraft();
    null;
  `, p.ctx);
  const tray = descendants(el(p, 'draft'), n => n.textContent === '发送')[0];
  assert.ok(tray, 'the tray draws its send button');
  try { await tray.onclick(); } catch (_) { /* a baseline failure must fail assertions, not crash */ }
  await settle();
  assert.equal(jobs.calls.length, 0, 'the tray entry honours the same admission');
  assert.equal(read(p, 'state.draft.q1.text'), '自然美式英语', 'the drafted answers survive');
  assert.match(p.toastText, /还没有确认/);
});
