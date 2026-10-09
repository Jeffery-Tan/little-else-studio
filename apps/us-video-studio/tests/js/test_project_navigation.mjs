import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

// Exercise the shipped effects without booting the application or calling a model.
const app = fs.readFileSync(new URL('../../studio/web/app.js', import.meta.url), 'utf8');
const section = (start, end) => {
  const from = app.indexOf(start), to = app.indexOf(end, from + start.length);
  assert.ok(from >= 0 && to > from, `Missing source section: ${start}`);
  return app.slice(from, to);
};
const effects = section('function composerDraft(', 'async function refreshTimeline(')
  + section('function toggleMaterials(', '/* Dropping files')
  + section('function newRunDialog(', 'function autoRunId(');

function harness({ post, list, loadRuns } = {}) {
  const nodes = new Map(), dialogs = [], calls = [], projects = new Map(), messages = [];
  let nextId = 0, listLoads = 0;
  const get = selector => {
    const key = selector.replace(/^#/, '');
    if (!nodes.has(key)) nodes.set(key, { value: '', disabled: false, textContent: '', style: {},
      focus() {}, setAttribute(name,value) {this[name]=value;},
      replaceChildren() {this.children=[];this.innerHTML='';this.textContent='';} });
    return nodes.get(key);
  };
  const state = { runId: 'old-run', decisionBusy: false, busy: false, chatDrafts: new Map(),
    artifactBodies: new Map(), clarificationChoices: new Map(), composerSubmissions: new Map(),
    projectReadNotices: new Map(), runs: [], view: 'home' };
  get('run-select').value = state.runId;
  const context = vm.createContext({
    state, $: get,
    document: {
      querySelectorAll: () => [],
      querySelector: () => dialogs.find(dialog => !dialog.removed) || null,
      body: { appendChild: dialog => dialogs.push(dialog) },
      createElement: () => {
        const handlers = new Map();
        return {
          handlers, querySelector: get,
          querySelectorAll: () => ['nr-title', 'nr-brief', 'nr-format'].map(get),
          addEventListener: (type, listener) => handlers.set(type, listener),
          showModal() { this.shown = true; },
          close() { this.closed = true; handlers.get('close')?.(); },
          remove() { this.removed = true; },
        };
      },
    },
    autoRunId: () => `new-${++nextId}`,
    api: async (url, options) => {
      const body = options?.body ? JSON.parse(options.body) : undefined;
      calls.push({ url, method: options?.method || 'GET', body });
      if (options?.method === 'POST') {
        if (post) return post(body, projects, calls);
        projects.set(body.run_id, body);
        return body;
      }
      return list ? list(projects) : [...projects.values()];
    },
    loadRuns: async () => {
      listLoads++;
      if (loadRuns) await loadRuns(listLoads);
      state.runs = [...projects.values()];
    },
    setStudioView: view => { state.view = view; },
    toast: message => messages.push(message),
    sessionStorage: { setItem() {} },
    clearFeedbackTarget() { state.feedbackTarget = null; },
    renderFeedbackTarget() {}, updatesComposerVersion() { state.composerVersion = (state.composerVersion || 0) + 1; },
    loadDraft() {}, refreshTimeline: async () => {}, restoreJob: async () => { state.busy = false; },
    loadMaterialWorkspace() { calls.push({materialRun:state.runId}); },
  });
  vm.runInContext(effects, context);
  function open(format = 'video') {
    context.newRunDialog(format);
    get('nr-title').value = 'Fixture project';
    get('nr-brief').value = 'Fixture brief';
    return dialogs.at(-1);
  }
  return { context, state, get, dialogs, calls, projects, messages, open,
    submit: () => get('form').onsubmit({ preventDefault() {} }),
    generatedIds: () => nextId, listLoads: () => listLoads };
}

test('accepted creation with a failed project refresh only reopens the same project', async () => {
  const h = harness({ loadRuns: count => { if (count === 1) throw new TypeError('list offline'); } });
  const dialog = h.open();
  await h.submit();
  assert.equal(h.calls.filter(call => call.method === 'POST').length, 1);
  assert.equal(h.projects.size, 1);
  assert.equal(dialog.closed, undefined);
  assert.match(h.get('.form-error').textContent, /已创建.*同一个项目/);
  assert.equal(h.get('[type="submit"]').textContent, '重新打开');
  assert.ok(['nr-title', 'nr-brief', 'nr-format'].every(key => h.get(key).disabled));
  await h.submit();
  assert.equal(h.calls.filter(call => call.method === 'POST').length, 1);
  assert.equal(h.generatedIds(), 1);
  assert.equal(h.state.runId, 'new-1');
  assert.equal(h.get('run-select').value, 'new-1');
  assert.equal(dialog.closed, true);
});

test('new project binds the explicitly selected product card without copying it into editable text',async()=>{
  const h=harness();const dialog=h.open();
  dialog.productCardBinding=()=>({source_run_id:'source',card_id:'card-1',sha256:'f'.repeat(64)});
  await h.submit();
  const payload=h.calls.find(c=>c.method==='POST').body;
  assert.equal(payload.product_card.card_id,'card-1');
  assert.equal(payload.product_card.sha256,'f'.repeat(64));
  assert.match(payload.brief,/Fixture brief/);
});

test('definitive input validation lets the user correct fields while retaining their text',async()=>{
  const h=harness({post:()=>{const error=new Error('资料卡与本次需求合计过长');error.status=400;throw error;}});
  h.open();await h.submit();
  assert.match(h.get('.form-error').textContent,/合计过长/);
  assert.equal(h.get('nr-brief').disabled,false);
  assert.equal(h.get('nr-brief').value,'Fixture brief');
  assert.equal(h.projects.size,0);
});

for (const status of [400, 409]) test(`uncertain POST acceptance retries one identity and recovers the exact project after ${status}`, async () => {
  const h = harness({ post: (body, projects) => {
    if (!projects.has(body.run_id)) {
      projects.set(body.run_id, body);
      throw new TypeError('acceptance response lost');
    }
    const error = new Error('run already exists'); error.status = status; throw error;
  } });
  const dialog = h.open('photo');
  await h.submit();
  assert.equal(dialog.closed, undefined);
  assert.ok(['nr-title', 'nr-brief', 'nr-format'].every(key => h.get(key).disabled));
  // Even a programmatic field mutation cannot change an uncertain POST's intent.
  h.get('nr-title').value = 'Different title';
  h.get('nr-brief').value = 'Different brief';
  h.get('nr-format').value = 'video';
  await h.submit();
  const posts = h.calls.filter(call => call.method === 'POST');
  assert.deepEqual(posts.map(call => call.body.run_id), ['new-1', 'new-1']);
  assert.deepEqual(posts[1].body, posts[0].body);
  assert.match(posts[0].body.brief, /带货图片与文案/);
  assert.equal(h.projects.size, 1);
  assert.equal(h.state.runId, 'new-1');
  assert.equal(dialog.closed, true);
});

test('same-ID conflict with different content is not mistaken for recovered creation', async () => {
  const h = harness({
    post: () => { const error = new Error('run already exists'); error.status = 400; throw error; },
    list: () => [{ run_id: 'new-1', title: 'Different owner intent', brief: 'Different brief' }],
  });
  const dialog = h.open();
  await h.submit();
  assert.equal(dialog.closed, undefined);
  assert.equal(h.state.runId, 'old-run');
  assert.match(h.get('.form-error').textContent, /无法确认/);
});

test('conflict recovery never substitutes another existing project', async () => {
  const h = harness({
    post: () => { const error = new Error('conflict'); error.status = 409; throw error; },
    list: () => [{ run_id: 'unrelated-run', title: 'Other' }],
  });
  const dialog = h.open();
  await h.submit();
  assert.equal(dialog.closed, undefined);
  assert.equal(h.state.runId, 'old-run');
  assert.equal(h.get('run-select').value, 'old-run');
  assert.match(h.get('.form-error').textContent, /无法确认/);
});

test('decision save prevents opening or submitting a new-project dialog', async () => {
  const h = harness();
  h.state.decisionBusy = true;
  h.context.newRunDialog();
  assert.equal(h.dialogs.length, 0);
  assert.equal(h.generatedIds(), 0);
  h.state.decisionBusy = false;
  h.open();
  h.state.decisionBusy = true;
  await h.submit();
  assert.equal(h.calls.length, 0);
  assert.equal(h.state.runId, 'old-run');
});

test('decision starting after creation keeps the accepted project available for reopening', async () => {
  const h = harness({ loadRuns: count => { if (count === 1) h.state.decisionBusy = true; } });
  const dialog = h.open();
  await h.submit();
  assert.equal(h.state.runId, 'old-run');
  assert.equal(h.get('run-select').value, 'old-run');
  assert.equal(dialog.closed, undefined);
  assert.match(h.get('.form-error').textContent, /已创建.*同一个项目/);
  h.state.decisionBusy = false;
  await h.submit();
  assert.equal(h.calls.filter(call => call.method === 'POST').length, 1);
  assert.equal(h.state.runId, 'new-1');
  assert.equal(dialog.closed, true);
});

test('a rejected project switch restores the selector and preserves the original chat draft', async () => {
  const h = harness();
  h.state.decisionBusy = true;
  h.get('chat-input').value = 'Unsent original draft';
  h.get('run-select').value = 'other-run';
  assert.equal(await h.context.selectRun('other-run'), false);
  assert.equal(h.get('run-select').value, 'old-run');
  assert.equal(h.state.runId, 'old-run');
  assert.equal(h.get('chat-input').value, 'Unsent original draft');
  assert.equal(h.calls.length, 0);
});

test('successful project switches keep chat drafts separate', async () => {
  const h = harness();
  h.state.projectReadIntegrity = {available:false};
  h.state.projectReadNotices.set('recovery',{runId:'old-run',integrity:h.state.projectReadIntegrity});
  h.get('project-read-status').hidden = false;
  h.get('chat-input').value = 'Draft for old run';
  assert.equal(await h.context.selectRun('other-run'), true);
  assert.equal(h.state.projectReadIntegrity,null);
  assert.equal(h.state.projectReadNotices.size,0);
  assert.equal(h.get('project-read-status').hidden,true);
  assert.equal(h.get('chat-input').value, '');
  h.get('chat-input').value = 'Draft for other run';
  assert.equal(await h.context.selectRun('old-run'), true);
  assert.equal(h.get('chat-input').value, 'Draft for old run');
  assert.equal(h.state.chatDrafts.get('other-run').text, 'Draft for other run');
});

test('home cannot open remembered project materials; project panel names its owner', () => {
  const h = harness();
  h.state.runs = [{run_id:'old-run',title:'项目 A'}];
  h.context.toggleMaterials(true);
  assert.equal(h.calls.length,0);
  assert.ok(!h.state.materialsOpen);
  h.state.view='project';
  h.context.toggleMaterials(true);
  assert.equal(h.state.materialsOpen,true);
  assert.equal(h.get('materials-project-name').textContent,'项目 A');
  assert.equal(h.calls[0].materialRun,'old-run');
});

test('switching project clears old material content before the new request finishes', async () => {
  const h=harness();
  h.state.view='project';h.state.materialsOpen=true;
  h.get('materials-grid').innerHTML='Old project assets';
  h.get('reference-video-results').innerHTML='Old project references';
  let finish;
  h.context.refreshTimeline=()=>new Promise(resolve=>{finish=resolve;});
  const switching=h.context.selectRun('other-run');
  assert.equal(h.state.materialsOpen,false);
  assert.equal(h.get('materials').hidden,true);
  assert.equal(h.get('materials-grid').innerHTML,'');
  assert.equal(h.get('reference-video-results').innerHTML,'');
  finish();await switching;
});

test('duplicate submit and Escape cannot abandon an in-flight creation', async () => {
  let release;
  const h = harness({ post: body => new Promise(resolve => { release = () => resolve(body); }) });
  const dialog = h.open();
  h.context.newRunDialog();
  assert.equal(h.dialogs.length, 1);
  const pending = h.submit();
  await h.submit();
  assert.equal(h.calls.length, 1);
  let prevented = false;
  dialog.handlers.get('cancel')({ preventDefault() { prevented = true; } });
  assert.equal(prevented, true);
  assert.equal(h.get('[data-cancel]').disabled, true);
  release(); await pending;
  assert.equal(dialog.closed, true);
});

test('a batch upload stays bound to its initial project after a mid-upload switch', async () => {
  const state = { runId: 'initial-run' }, uploads = [];
  let refreshed = 0;
  class Multipart {
    constructor() { this.parts = []; }
    append(name, file, filename) { this.parts.push({ name, file, filename }); }
  }
  const context = vm.createContext({
    state, FormData: Multipart, toast() {},
    refreshTimeline: async () => { refreshed++; },
    fetch: async (url, options) => {
      uploads.push({ url, method: options.method, parts: options.body.parts });
      if (uploads.length === 1) state.runId = 'newly-selected-run';
      return { ok: true };
    },
  });
  vm.runInContext(section('async function uploadFiles(', 'const shelf ='), context);
  const files = [{ name: 'product front.png' }, { name: 'product back.png' }, { name: 'reference.mp4' }];
  await context.uploadFiles(files);
  assert.deepEqual(uploads.map(upload => upload.url), Array(3).fill('/api/runs/initial-run/materials'));
  assert.ok(uploads.every(upload => upload.method === 'POST'));
  assert.deepEqual(uploads.map(upload => upload.parts[0].filename), files.map(file => file.name));
  assert.equal(state.runId, 'newly-selected-run');
  assert.equal(refreshed, 0, 'finishing an old-project upload must not refresh the new project');
});
