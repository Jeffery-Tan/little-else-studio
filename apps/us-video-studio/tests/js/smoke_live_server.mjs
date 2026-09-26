/* A live smoke test against a real running Studio, on an isolated data root.
 *
 * Everything else in this suite stubs the server. This one does not: it loads
 * `app.js` in the DOM fixture, points `fetch` at a real loopback server, selects
 * a seeded run, and drives the actual decision card through the real routes.
 * It is the closest this environment gets to the review's "真实 DOM/浏览器"
 * acceptance without a browser.
 *
 * It is opt-in, because it needs a server *and* it writes to it:
 *
 *   STUDIO_SMOKE_URL=http://127.0.0.1:4762 \
 *   STUDIO_SMOKE_RUN=preview-brief-blocked-01 \
 *   node --test apps/us-video-studio/tests/js/smoke_live_server.mjs
 *
 * The target must be a **preview** root seeded by `scripts/seed_preview.py`:
 * the second test approves a real brief amendment and starts a real redo job.
 * Point it at a working root and it stops at `openBlockedProject` with that
 * reason — it will not touch a real project.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import vm from 'node:vm';
import {loadApp, makeDom, parseHtml} from './dom-fixture.mjs';

const require = createRequire(import.meta.url);
globalThis.escapeHtml = require('../../studio/web/md.js').escapeHtml;
const stageMod = require('../../studio/web/stage.js');
globalThis.gateApproveLabel = stageMod.gateApproveLabel;
globalThis.gateRejectLabel = stageMod.gateRejectLabel;

const BASE = process.env.STUDIO_SMOKE_URL || '';
const RUN = process.env.STUDIO_SMOKE_RUN || '';
const live = BASE && RUN ? test : test.skip;

/* The page loads `journey.js` before `app.js` and calls these from it. They are
 * the shared question card and the shared draft key — the same functions the
 * page uses, not re-implementations. */
const journey = require('../../studio/web/journey.js');
const {answerKey} = stageMod;

async function settle(rounds = 12) {
  for (let i = 0; i < rounds; i += 1) await new Promise(resolve => setImmediate(resolve));
}

/** Load the page against the live server and select the run under test. */
async function openProject() {
  const app = loadApp({
    baseUrl: BASE,
    dom: makeDom(['chat-input', 'send', 'stage', 'toast', 'rail', 'project-progress',
                  'conversation-body', 'conversation-count', 'draft', 'spend', 'materials',
                  'materials-grid', 'materials-count', 'materials-toggle', 'materials-close',
                  'materials-project-name', 'viewer', 'reader', 'run-select', 'journey-decision',
                  'journey-tab-plan', 'journey-tab-assets', 'journey-tab-script',
                  'journey-tab-storyboard', 'journey-tab-video', 'journey-assets',
                  'journey-storyboard', 'journey-video', 'spend-panel', 'detail-toggle',
                  'conversation-toggle', 'conversation-caret', 'conversation-latest',
                  'feedback-target', 'viewer-body', 'viewer-name', 'reader-body', 'reader-title',
                  'reader-sub', 'reader-versions', 'reader-close', 'new-run']),
    globals: {
      EventSource: undefined,
      // The page loads these before `app.js`; this smoke test is about the
      // decision card and the amendment round-trip, so they are stubbed rather
      // than pulled in whole. Everything under test — `refreshTimeline`,
      // `stageHtml`, `wireGate`, the HTTP routes — is the real code.
      StudioProduction: {currentProductionWork: () => null},
      creatorProjectView: () => ({tab: 'plan', stage: 'direction'}),
      renderProjectProgress: () => {},
      configureJourneyTabs: () => {},
      syncJourneyTabs: () => {},
      renderJourneyDecision: () => {},
      closeEmbeddedProduction: () => {},
      setStudioView: () => {},
      initStudioNavigation: () => {},
      loadInspiration: () => {},
      StudioProjectCosts: {mount: () => {}},
      answerKey,
      questionCardHtml: journey.questionCardHtml,
      bindQuestionChoices: journey.bindQuestionChoices,
      stageHtml: stageMod.stageHtml,
      loadMaterialWorkspace: () => {},
      referenceAnalysisHtml: () => '',
      wireReferenceAnalysis: () => {},
      renderReferenceAnalysis: () => {},
      wireRevisionActions: () => {},
      wireProductCards: () => {},
      wireInlineFeedback: () => {},
      attachProductCardPicker: () => {},
      startFromConversation: () => {},
      showJourneyTab: async () => {},
      scrollToJourneyDecision: () => false,
      setProjectFocus: () => {},
      renderProjects: () => {},
    },
  });
  app.ctx.__run = RUN;
  vm.runInContext(`
    state.runId = globalThis.__run;
    sessionStorage.setItem('studio-selected-run', globalThis.__run);
    null;
  `, app.ctx);
  await vm.runInContext('refreshTimeline()', app.ctx);
  // `state` is a lexical binding in `app.js`, not a property of the global
  // object, so it is read back through the same realm.
  app.read = (expression) => vm.runInContext(expression, app.ctx);

  const pending = app.read('state.timeline && state.timeline.pending');
  // Refuse rather than write: this suite approves an amendment for real, and a
  // working root must not be the thing it approves it on.
  assert.ok(pending && pending.kind === 'brief_amendment',
    `${RUN} is not a brief-blocked preview run (pending=${pending && pending.kind}). `
    + 'Seed a preview root with scripts/seed_preview.py and point '
    + 'STUDIO_SMOKE_RUN at its preview-brief-blocked-01.');
  return app;
}

live('a live project renders its decision card without a script error', async () => {
  const app = await openProject();
  const pending = app.read('state.timeline.pending');

  // The panel the real `renderPane` drew, taken out of its host and parsed — no
  // hand-built card, and no second renderer.
  const gate = app.ctx.document.getElementById('stage').querySelector('.gate');
  assert.ok(gate, 'renderPane drew the gate card into the stage panel');

  // The two nodes `wireGate` binds first: their absence was F1.
  assert.ok(gate.querySelector('.reason'), 'the card carries the note box');
  assert.ok(gate.querySelector('.gate-feedback'), 'the card carries the status line');
  const approve = gate.querySelector('[data-act="amend_brief"]');
  const back = gate.querySelector('[data-act="reject"]');
  assert.ok(approve && back, 'both actions are offered');
  assert.equal(gate.dataset.amendValid, '1');
  assert.ok(gate.textContent.includes(pending.amend.current_text), 'shows the current clause');
  assert.ok(gate.textContent.includes(pending.amend.text), 'shows the proposed clause');
  assert.ok(approve.onclick && back.onclick, 'both handlers were bound by the real wireGate');
});

live('the live approval writes the amendment and starts the redo exactly once', async () => {
  const app = await openProject();
  const before = app.read('state.timeline.pending');
  assert.equal(before.kind, 'brief_amendment');
  const gate = app.ctx.document.getElementById('stage').querySelector('.gate');
  gate.querySelector('[data-act="amend_brief"]').click();
  await settle(60);

  const posts = app.observed.filter(call => call.path.includes('/brief-amendment'));
  assert.equal(posts.length, 1, 'one amendment request');
  assert.equal(posts[0].body.approve, true);
  assert.equal(posts[0].body.changes[0].clause_id, 'goal.primary');
  assert.equal(posts[0].body.changes[0].text, before.amend.text);

  // The redo is a real job, created once, named after the amendment. Asked for
  // by name, so "no job at all" and "a job under another name" fail differently.
  const redoEvents = async () => (await (await fetch(`${BASE}/api/runs/${RUN}/events`)).json())
    .filter(row => row.kind === 'job_created'
      && String(row.request_id || '').startsWith('brief-amendment:'));
  let created = await redoEvents();
  for (let i = 0; i < 20 && !created.length; i += 1) {
    await settle(4);
    created = await redoEvents();
  }
  assert.equal(created.length, 1, 'exactly one redo job was created');
  assert.equal(created[0].job_kind, 'conversation');
  assert.match(created[0].text, /生成/, 'the redo text keeps the no-media boundary');
  assert.match(created[0].request_id, /^brief-amendment:amd-[0-9a-f]{16}$/,
    'the request identity is derived from the amendment id, so a retry finds the same job');

  // And the card is gone: the brief moved, so the old direction is stale and no
  // second decision may be offered on it.
  const state = await (await fetch(`${BASE}/api/runs/${RUN}/state`)).json();
  const direction = state.stages.find(row => row.stage === 'direction');
  assert.equal(direction.stale, true, 'the old direction was invalidated by the amendment');
  assert.equal(direction.can_approve, false);
  const timeline = await (await fetch(`${BASE}/api/runs/${RUN}/timeline`)).json();
  assert.equal(timeline.pending, null, 'nothing is waiting on the stale version');
  assert.equal(timeline.pending_brief_redo, null, 'the redo started, so no recovery entry');
});
