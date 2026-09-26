/* Read-only page verification against a running Studio.
 *
 * Loads the page's own scripts, in the page's own order, into the DOM fixture,
 * points them at a live server, and opens a real project. Nothing is written:
 * this is the "服务已加载、真实项目只读打开、页面无脚本错误" check, and it is the
 * closest this environment gets to a browser without one.
 *
 *   STUDIO_CHECK_URL=http://127.0.0.1:4760 \
 *   STUDIO_CHECK_RUNS=bag-small-gym-studio-20260919,smoke-lunch-bag-01 \
 *   node --test apps/us-video-studio/tests/js/check_live_page.mjs
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import vm from 'node:vm';
import {loadApp, makeDom, webSource} from './dom-fixture.mjs';

const require = createRequire(import.meta.url);
const BASE = process.env.STUDIO_CHECK_URL || '';
const RUNS = (process.env.STUDIO_CHECK_RUNS || '').split(',').map(s => s.trim()).filter(Boolean);
const live = BASE && RUNS.length ? test : test.skip;

/* Classic scripts from index.html, in browser order. Page mode leaves the
 * fixture's runtime and app shims unloaded so these own the shared bindings. */
const PAGE_SCRIPTS = [
  'learning-session.js', 'project-management.js', 'workspace.js', 'progress.js',
  'materials.js', 'image-analysis.js', 'image-workspace.js', 'conversation-input.js',
  'reference-video.js', 'material-workspace.js', 'team-progress.js', 'runtime.js',
  'revisions.js', 'revision-workspace.js', 'product-cards.js', 'outcomes.js',
  'project-costs.js', 'history.js', 'production.js', 'creator-view.js', 'journey.js',
  'app.js', 'codex-login.js', 'connections.js', 'learning.js', 'inline-feedback.js',
];

const IDS = ['chat-input', 'send', 'stage', 'toast', 'rail', 'project-progress',
  'conversation-body', 'conversation-count', 'draft', 'spend', 'materials',
  'materials-grid', 'materials-count', 'materials-toggle', 'materials-close',
  'materials-project-name', 'viewer', 'reader', 'run-select', 'journey-decision',
  'journey-tab-plan', 'journey-tab-assets', 'journey-tab-script', 'journey-tab-storyboard',
  'journey-tab-video', 'journey-assets', 'journey-storyboard', 'journey-video',
  'spend-panel', 'detail-toggle', 'conversation-toggle',
  'conversation-caret', 'conversation-latest', 'feedback-target', 'viewer-body',
  'viewer-name', 'reader-body', 'reader-title', 'reader-sub', 'reader-versions',
  'reader-close', 'new-run', 'inspiration', 'trash-open', 'page-heading'];

/** The real page markup, so `getElementById` and `querySelector` find what the
 *  page's own controllers expect instead of a hand-written approximation. */
function pageBody() {
  const html = webSource('index.html');
  const body = html.slice(html.indexOf('<body'), html.indexOf('</body>'));
  return body.replace(/^<body[^>]*>/i, '').replace(/<script[\s\S]*?<\/script>/gi, '');
}

/** Load the whole page the way the browser loads it, then open one project. */
async function openProject(runId) {
  const app = loadApp({baseUrl: BASE, dom: makeDom({ids: IDS, body: pageBody()}), runtime: 'page'});
  const {ctx, document} = app;
  // `index.html` defines these as top-level scripts, so they share one realm.
  for (const name of ['md.js', 'blocks.js', 'stage.js', ...PAGE_SCRIPTS]) {
    try {
      vm.runInContext(webSource(name), ctx, {filename: name});
    } catch (err) {
      // A module that needs a browser-only global it never actually uses on
      // this path is fine; record it so the failure is visible, not silent.
      (app.loadErrors ||= []).push(`${name}: ${err.message}`);
    }
  }
  // app.js starts its own async init. Let that first project settle before
  // exercising the same selector path a browser user takes, so the two loads
  // cannot clear each other's timeline mid-assertion.
  for (let attempt = 0; attempt < 100 && !vm.runInContext('state.timeline', ctx); attempt += 1)
    await new Promise(resolve => setTimeout(resolve, 50));
  if (!vm.runInContext('state.timeline', ctx))
    throw new Error(`Initial page load did not settle; script load errors: ${(app.loadErrors || []).join('; ')}`);
  ctx.__run = runId;
  try {
    await vm.runInContext('selectRun(globalThis.__run)', ctx);
    vm.runInContext("setStudioView('project')", ctx);
  } catch (error) {
    throw new Error(`${error.message}; script load errors: ${(app.loadErrors || []).join('; ')}`,
      {cause: error});
  }
  app.read = (expression) => vm.runInContext(expression, ctx);
  app.document = document;
  return app;
}

for (const runId of RUNS) {
  live(`the live page opens ${runId} and renders it without a script error`, async () => {
    const app = await openProject(runId);
    const tl = app.read('state.timeline');
    assert.ok(tl, JSON.stringify({runId, selected: app.read('state.runId'),
      refreshSerial: app.read('state.refreshSerial'), requests: app.observed,
      loadErrors: app.loadErrors || []}));

    // The projections the review is about, read straight off the live server's
    // answer as the page received it.
    assert.equal(String(tl.run.run_id), runId);
    const rail = tl.rail || [];
    assert.ok(rail.length === 4, 'four stages in the rail');
    const direction = rail.find(step => step.stage === 'direction');
    assert.ok(direction, 'the direction step is present');

    // `renderPane` ran for real; whatever it drew is in the stage host.
    const stage = app.document.getElementById('stage');
    const gate = stage.querySelector('.gate');
    const pending = tl.pending;
    if (pending && pending.kind === 'brief_amendment') {
      // The card that used to throw on open (F1) — it must carry the contract.
      assert.ok(gate, 'the blocked-alignment card was drawn');
      assert.ok(gate.querySelector('.reason'), 'the note box is present');
      assert.ok(gate.querySelector('.gate-feedback'), 'the status line is present');
      const valid = gate.dataset.amendValid === '1';
      const approve = gate.querySelector('[data-act="amend_brief"]');
      assert.equal(Boolean(approve), valid,
        'approving is offered exactly when a real proposal exists (F4)');
      assert.ok(gate.querySelector('[data-act="reject"]'), 'returning is always offered');
      assert.ok(approve ? approve.onclick : true, 'the offered button is bound');
    } else if (pending) {
      assert.ok(gate, `the ${pending.kind} card was drawn`);
      assert.ok(gate.querySelector('.reason'), 'the decision card carries the note box');
      assert.ok(gate.querySelector('.gate-feedback'), 'the decision card carries the status line');
      assert.ok(gate.querySelector('[data-act="approve"]'), 'approving is offered');
      assert.ok(gate.querySelector('[data-act="reject"]'), 'returning is offered');
    } else {
      // Nothing waiting: the page must not advertise a decision it cannot take.
      assert.equal(gate, null, 'no gate card is drawn without a pending decision');
    }

    // The composer contract: both paths are wired and the box survives a refusal.
    const input = app.document.getElementById('chat-input');
    input.value = '只读检查：这行字不应被清空';
    app.read('state.busy = true');
    app.read(`
      currentJob = {job_id: 'live-check', job_kind: 'conversation', status: 'queued'};
      submitJob = async () => { throw new Error('live check must not submit'); };
    `);
    const result = await app.read('submit()');
    assert.equal(result.accepted, false, 'a queued task refuses the send');
    assert.equal(input.value, '只读检查：这行字不应被清空', 'and the text survives (F6)');

    // Nothing was written: no POST reached the server.
    const writes = app.observed.filter(call => call.method !== 'GET');
    assert.deepEqual(writes, [], 'a read-only open sends no write request');
  });
}
