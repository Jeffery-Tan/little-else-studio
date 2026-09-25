/* Conversation as the single stream — the interaction pattern mature OSS
 * chat UIs (Open WebUI, LibreChat) converge on:
 *
 *   progress lives in one stable status row → the conclusion is always a
 *   bubble of its own → clarification options arrive as an ADDITIONAL card
 *   after the answer → a finished round with no reply still says so instead
 *   of leaving the assistant side blank.
 *
 * And the project chrome carries no persistent status band: the one
 * next-action is a small button in the workspace tab bar (Open WebUI's top
 * bar is just title + actions), not a coloured “最终交付/现在” banner.
 *
 * Loaded like the shipped page: real team-progress/runtime/journey/app.js,
 * only fetch and the clock replaced.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {loadApp, makeDom} from './dom-fixture.mjs';

const PAGE_BODY = '<nav class="journey-tabs" role="tablist" aria-label="作品内容">'
  + '<button id="journey-tab-plan" role="tab" data-journey-tab="plan" aria-selected="true">创意与脚本</button>'
  + '</nav>'
  + '<div id="project-progress"></div>';

const IDS = ['chat-input', 'send', 'toast', 'conversation-body', 'conversation-count',
  'conversation', 'conversation-toggle', 'conversation-caret', 'draft', 'job-status',
  'rail', 'stage', 'journey-decision', 'run-select'];

function page() {
  const timers = [];
  const p = loadApp({
    runtime: 'real',
    dom: makeDom({ids: IDS, body: PAGE_BODY}),
    globals: {
      setTimeout: (fn, ms) => { timers.push({fn, ms}); return timers.length; },
      clearTimeout: (id) => { if (id && timers[id - 1]) timers[id - 1] = null; },
    },
  });
  vm.runInContext('state.runId = "run-1"; state.rail = []; null;', p.ctx);
  p.read = (expr) => vm.runInContext(expr, p.ctx);
  p.el = (id) => p.document.getElementById(id);
  return p;
}

function turn(overrides = {}) {
  return {
    user: {text: '现在的结论是什么', at: '2026-09-25T15:54:12Z'},
    reply: {text: '结论：先补一版只动达人层的方向稿。', at: '2026-09-25T15:56:38Z'},
    items: [], detail: null,
    ...overrides,
  };
}

const CLARIFICATION = {
  id: 'q-1',
  question: '要补充哪一版？',
  options: [
    {label: '在 A 基础上补', description: '其余内容不动'},
    {label: '整版重做', description: '三条一起重开'},
  ],
  recommended: 0,
};

test('a pending clarification is an extra card after the conclusion, not a replacement', () => {
  const p = page();
  p.ctx.__tl = {
    turns: [turn({pending_clarification_id: 'q-1'})],
    interaction: CLARIFICATION,
    rail: [], questions: [], progress: {}, pending: null,
  };
  p.read('state.timeline = globalThis.__tl; renderConversation(globalThis.__tl)');

  const host = p.el('conversation-body');
  const assistant = host.querySelectorAll('.msg.assistant');
  assert.equal(assistant.length, 2,
    'the conclusion bubble AND the question card bubble both render');
  const replyBubble = assistant.find(b => !b.querySelector('.question-host'));
  assert.ok(replyBubble, 'the conclusion has its own bubble');
  assert.match(replyBubble.textContent, /结论：先补一版只动达人层的方向稿/,
    'the answer is visible even while a clarification is pending');
  const questionBubble = assistant.find(b => b.querySelector('.question-host'));
  assert.ok(questionBubble, 'the clarification renders as its own card');
  assert.match(questionBubble.textContent, /要补充哪一版/,
    'and it carries the actual question');
  assert.match(questionBubble.textContent, /在 A 基础上补/,
    'with its options (rendered through the real question card)');
});

test('a finished round with no reply says so instead of leaving the assistant side blank', () => {
  const p = page();
  p.ctx.__tl = {
    turns: [turn({reply: null, items: [{kind: 'note', text: '导演这一次没能交出「创意方向」：派工失败'}]})],
    rail: [], questions: [], progress: {}, pending: null,
  };
  p.read('state.busy = false; state.timeline = globalThis.__tl; renderConversation(globalThis.__tl)');

  const host = p.el('conversation-body');
  const assistant = host.querySelectorAll('.msg.assistant');
  assert.equal(assistant.length, 1, 'the assistant side answers, even with no reply text');
  assert.match(assistant[0].textContent, /本轮没有留下结论回复/,
    'an honest fallback, not silence');
  assert.match(assistant[0].textContent, /本轮工作记录/,
    'pointing at the work journal right above it');
  assert.ok(host.querySelector('.turn-work'),
    'the work journal still holds the details');
});

test('while a round is still running the fallback stays out of the way', () => {
  const p = page();
  p.ctx.__tl = {
    turns: [turn({reply: null, items: []})],
    rail: [], questions: [], progress: {}, pending: null,
  };
  p.read('state.busy = true; state.timeline = globalThis.__tl; renderConversation(globalThis.__tl)');

  const host = p.el('conversation-body');
  assert.equal(host.querySelectorAll('.msg.assistant').length, 0,
    'the live status row owns the in-progress state; no premature “no conclusion”');
});

test('only the live turn stays quiet — an older failed round still gets its fallback', () => {
  const p = page();
  p.ctx.__tl = {
    turns: [
      turn({user: {text: '再重新递交审核一次'}, reply: null,
            items: [{kind: 'note', text: '导演这一次没能交出「创意方向」：派工失败'}]}),
      turn({user: {text: '再试一次'}, reply: null, items: []}),   // the running one
    ],
    rail: [], questions: [], progress: {}, pending: null,
  };
  p.read('state.busy = true; state.timeline = globalThis.__tl; renderConversation(globalThis.__tl)');

  const turns = p.el('conversation-body').querySelectorAll('.turn');
  assert.equal(turns.length, 2);
  assert.match(turns[0].querySelectorAll('.msg.assistant')[0]?.textContent || '',
    /本轮没有留下结论回复/, 'the historical failed round is not silenced by a new run');
  assert.equal(turns[1].querySelectorAll('.msg.assistant').length, 0,
    'the live turn defers to the status row');
});

test('the workspace chrome carries the next action, and the green status band is gone', () => {
  const p = page();
  vm.runInContext(`
    state.busy = false;
    state.runs = [];
    state.timeline = null;
    state.creatorView = {
      goal: '交付一部短片', current: '等待重试审核', detail: '',
      action: {kind: 'review_stage', label: '重试本版审核', stage: 'direction',
               version: 1, sha256: 'a'.repeat(64)},
      tab: 'plan',
    };
    renderJourneyStatus();
    null;
  `, p.ctx);

  const band = p.document.querySelector('.creator-goal');
  assert.equal(band, null, 'no persistent 最终交付/现在 status band is rendered');
  const slot = p.el('project-progress');
  assert.doesNotMatch(slot.textContent || '', /最终交付|现在：/,
    'none of the band copy stands in for the chat');
  const button = slot.querySelector('[data-creator-next]');
  assert.ok(button, 'the next action stays in the chrome as a plain slot button');
  assert.equal(button.textContent, '重试本版审核');
  assert.ok(button.onclick, 'and its handler is bound');
  assert.match(button.className, /ghost/,
    'a neutral toolbar control, not a banner call-to-action');
});

test('the chrome action disappears while the team is working', () => {
  const p = page();
  vm.runInContext(`
    state.busy = true;
    state.runs = [];
    state.timeline = null;
    state.creatorView = {
      goal: '交付一部短片', current: '团队正在处理',
      action: {kind: 'review_stage', label: '重试本版审核', stage: 'direction',
               version: 1, sha256: 'a'.repeat(64)},
      tab: 'plan',
    };
    renderJourneyStatus();
    null;
  `, p.ctx);
  assert.equal(p.el('project-progress').querySelector('[data-creator-next]'), null,
    'in-progress state belongs to the status row, not to a second action');
});
