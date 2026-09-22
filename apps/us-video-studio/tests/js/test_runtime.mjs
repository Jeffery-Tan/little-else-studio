import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../../studio/web/team-progress.js', import.meta.url), 'utf8') + '\n' + fs.readFileSync(new URL('../../studio/web/runtime.js', import.meta.url), 'utf8');
test('uncertain reference submission preserves source and intent on the dedicated route',async()=>{
  const calls=[];
  const h=harness(async(url,options)=>{
    if(!options)return {can_continue:true,revision:'r'};
    calls.push({url,body:JSON.parse(options.body)});
    if(calls.length===1)throw new TypeError('offline');
    return {job_id:'ref',request_id:'same-ref',job_kind:'reference_video',status:'succeeded',events:[]};
  });
  const payload={request_kind:'reference_video',request_id:'same-ref',reference_id:'source-ref',intent:'recreate',source_url:'https://vm.tiktok.com/ABC/'};
  await h.context.submitJob('run-a',payload);
  await h.nodes.get('job-status').children.at(-1).onclick();
  assert.equal(calls.length,2);
  assert.ok(calls.every(c=>c.url==='/api/runs/run-a/reference-videos'));
  assert.deepEqual(calls[0].body,calls[1].body);
});
test('project brief outranks successful job status and historical missing usage',()=>{
  const h=harness(async()=>{throw new Error('no request on render');});
  const brief={title:'创意方向第 3 版 · 审核未完成',detail:'原方案已保留',phase:'review_failed'};
  h.context.showJob({job_id:'j',status:'succeeded',job_kind:'conversation',brief,events:[],unresolved_calls:[{usage_status:'unknown'}]});
  assert.equal(h.context.state.projectBrief,brief);
  const copy=visibleText(h.nodes.get('job-status'));
  assert.match(copy,/团队工作记录/);assert.doesNotMatch(copy,/本轮完成|核账|未返回用量/);
  assert.equal(h.context.state.busy,false);
});

test('uncertain review retries preserve the exact version and dedicated endpoint',async()=>{
  const calls=[];
  const h=harness(async(url,options)=>{
    if(!options)return {can_continue:true,revision:'r'};
    calls.push({url,body:JSON.parse(options.body)});
    if(calls.length===1)throw new TypeError('offline');
    return {job_id:'review',request_id:'same-review',job_kind:'stage_review',status:'succeeded',events:[]};
  });
  const payload={request_kind:'stage_review',request_id:'same-review',stage:'direction',expected_version:3,expected_artifact_sha256:'f'.repeat(64)};
  await h.context.submitJob('run-a',payload);
  await h.nodes.get('job-status').children.at(-1).onclick();
  assert.equal(calls.length,2);
  assert.ok(calls.every(c=>c.url==='/api/runs/run-a/actions/review'));
  assert.deepEqual(calls[0].body,calls[1].body);
});
test('resolved accounting uses current recovery state without hiding actual budget caps', () => {
  const h = harness(async () => ({}));
  const job = {status: 'blocked', reason: '1 model call(s) have unknown, partial or pending usage', events: []};
  assert.equal(h.context.jobPresentation(job, {can_continue: true}).label, '可以继续');
  assert.notEqual(h.context.jobPresentation(job, {can_continue: false}).label, '可以继续');
  assert.notEqual(h.context.jobPresentation({...job, reason: 'tokens reached the soft cap'}, {can_continue: true}).label, '可以继续');
});
test('missing Codex executable explains the startup problem without exposing paths', () => {
  const h = harness(async () => ({}));
  const guidance = h.context.jobGuidance({status:'blocked', reason:'连接准备未通过：Codex CLI is not installed or not on PATH'});
  assert.match(guidance, /服务没有找到 Codex CLI/);
  assert.match(guidance, /重启工作台服务/);
  assert.doesNotMatch(guidance, /API Key|制作团队|C:\\/);
});
test('uncertain revision submission retries the same draft on its dedicated route',async()=>{
  const calls=[];
  const h=harness(async(url,options)=>{
    if(!options)return {can_continue:true,revision:'r'};
    calls.push({url,body:JSON.parse(options.body)});
    if(calls.length===1)throw new TypeError('offline');
    return {job_id:'edit-1',request_id:'same-edit',job_kind:'artifact_revision',job_payload:{stage:'direction'},status:'succeeded',events:[]};
  });
  const payload={request_kind:'artifact_revision',request_id:'same-edit',stage:'direction',draft_id:'draft-1',expected_draft_sha256:'f'.repeat(64),consent:true};
  await h.context.submitJob('run-a',payload);
  await h.nodes.get('job-status').children.at(-1).onclick();
  assert.equal(calls.length,2);
  assert.ok(calls.every(c=>c.url==='/api/runs/run-a/edits/direction/draft-1/generate'));
  assert.deepEqual(calls[0].body,calls[1].body);
  assert.equal(h.storage.size,0);
  assert.equal(h.context.state.busy,false);
});
test('uncertain image submission retains the same payload and image route',async()=>{
  const calls=[];
  const h=harness(async(url,options)=>{
    if(!options) return {can_continue:true,revision:'r'};
    calls.push({url,body:JSON.parse(options.body)});
    if(calls.length===1) throw new TypeError('offline');
    return {job_id:'image-1',request_id:'same-image',job_kind:'image_analysis',status:'succeeded',events:[]};
  });
  const payload={request_kind:'image_analysis',request_id:'same-image',name:'picture.png',consent:true};
  await h.context.submitJob('run-a',payload);
  await h.nodes.get('job-status').children.at(-1).onclick();
  assert.equal(calls.length,2);
  assert.ok(calls.every(c=>c.url==='/api/runs/run-a/image-analysis'));
  assert.deepEqual(calls[0].body,calls[1].body);
  assert.equal(h.storage.size,0);
  assert.equal(h.context.state.busy,false);
});

test('uncertain video observation retries acceptance identity without becoming a conversation job',async()=>{
  const calls=[];
  const h=harness(async(url,options)=>{
    if(!options) return {can_continue:true,revision:'r'};
    calls.push({url,body:JSON.parse(options.body)});
    if(calls.length===1) throw new TypeError('offline');
    return {job_id:'video-1',request_id:'same-video',job_kind:'video_analysis',status:'succeeded',events:[]};
  });
  const payload={request_kind:'video_analysis',request_id:'same-video',name:'reference.mp4',consent:true};
  await h.context.submitJob('run-a',payload);
  await h.nodes.get('job-status').children.at(-1).onclick();
  assert.equal(calls.length,2);
  assert.ok(calls.every(c=>c.url==='/api/runs/run-a/video-analysis'));
  assert.deepEqual(calls[0].body,calls[1].body);
  assert.equal(h.storage.size,0);
});
function harness(api) {
  const nodes = new Map();
  const node = () => ({ children: [], disabled: false, hidden: false, value: '',
    append(...items) { this.children.push(...items); }, replaceChildren() { this.children = []; },
    setAttribute() {}, addEventListener() {}, showModal() { this.open = true; },
    querySelector(selector) { return descendants(this, n=>n.className?.split(' ').includes(selector.slice(1)))[0]; } });
  const body = node();
  const storage = new Map();
  const timers = [];
  const context = vm.createContext({
    state: { runId: 'run-a', busy: false, decisionBusy: false },
    document: {body},
    $: (id) => { if (!nodes.has(id)) nodes.set(id, node()); return nodes.get(id); },
    box: (tag, className, text) => Object.assign(node(), { tag, className, text }),
    sessionStorage: { getItem: (key) => storage.get(key), setItem: (key, value) => storage.set(key, value), removeItem: (key) => storage.delete(key) },
    setTimeout: (fn) => { timers.push(fn); return timers.length; }, clearTimeout() {},
    refreshTimeline: async () => {}, toast() {}, api,
  });
  vm.runInContext(source, context);
  return { context, nodes, storage, timers, body };
}

test('uncertain acceptance retries the same request id and preserves the user message', async () => {
  const calls = [];
  const h = harness(async (url, options) => {
    calls.push({ url, body: JSON.parse(options.body) });
    if (calls.length === 1) throw new TypeError('offline');
    return { job_id: 'j1', request_id: 'same-request', status: 'succeeded', events: [] };
  });
  const payload = { request_id: 'same-request', text: '继续该版本' };
  await h.context.submitJob('run-a', payload);
  assert.equal(h.context.state.busy, true);
  assert.equal(h.storage.size, 1);
  const retry = h.nodes.get('job-status').children.at(-1);
  await retry.onclick();
  assert.deepEqual(calls.map((c) => c.body), [{...payload,reference_consent:true}, {...payload,reference_consent:true}]);
  assert.equal(h.storage.size, 0);
  assert.equal(h.context.state.busy, false);
});

test('refresh reattaches to accepted running job without posting a second request', async () => {
  const calls = [];
  const h = harness(async (url, options) => {
    calls.push({ url, options });
    return { job_id: 'j1', request_id: 'accepted', status: 'running', events: [{ say: '审核中' }] };
  });
  h.storage.set('studio-job-pending:run-a', JSON.stringify({ request_id: 'accepted', text: 'go' }));
  await h.context.restoreJob('run-a');
  assert.equal(calls.length, 1);
  assert.equal(calls[0].options, undefined);
  assert.equal(h.storage.size, 0);
  assert.equal(h.context.state.busy, true);
  assert.equal(h.timers.length, 1);
});

test('interrupted job allows an explicit follow-up and does not retry itself', async () => {
  const h = harness(async () => ({ job_id: 'j1', status: 'interrupted', events: [] }));
  await h.context.restoreJob('run-a');
  assert.equal(h.context.state.busy, false);
  assert.equal(h.timers.length, 0);
  assert.match(h.nodes.get('job-status').children[0].children[0].text, /未自动重试/);
});

test('late reply from a previous project cannot replace current project status', async () => {
  let resolve;
  const h = harness(() => new Promise((r) => { resolve = r; }));
  const pending = h.context.restoreJob('run-a');
  h.context.state.runId = 'run-b';
  resolve({ job_id: 'old', status: 'running', events: [] });
  await pending;
  assert.equal(h.nodes.has('job-status'), false);
  assert.equal(h.timers.length, 0);
});

test('a definitive validation failure restores text and clears uncertain acceptance', async () => {
  const h = harness(async (_url, options) => {
    if (options) { const error = new Error('busy'); error.status = 409; throw error; }
    return null;
  });
  await h.context.submitJob('run-a', { request_id: 'r1', text: '保留草稿' });
  assert.equal(h.nodes.get('chat-input').value, '保留草稿');
  assert.equal(h.storage.size, 0);
  assert.equal(h.context.state.busy, false);
});

test('unchanged polling preserves the stop button and keyboard focus target', () => {
  const h = harness(async () => {});
  const job = { job_id: 'j1', status: 'running', events: [] };
  h.context.showJob(job);
  const stopButton = h.nodes.get('job-status').children.at(-1);
  h.context.showJob({ ...job, cursor: 12 });
  assert.equal(h.nodes.get('job-status').children.at(-1), stopButton);
});

test('timeline failure after acceptance does not lose the running job or enable a second submission', async () => {
  const h = harness(async () => ({ job_id: 'accepted', status: 'running', events: [] }));
  h.context.refreshTimeline = async () => { throw new TypeError('timeline offline'); };
  await h.context.submitJob('run-a', { request_id: 'r1', text: 'test' });
  assert.equal(h.context.state.busy, true);
  assert.equal(h.nodes.get('send').disabled, true);
  assert.equal(h.storage.size, 0);
  assert.equal(h.timers.length, 1);
  assert.equal(h.nodes.get('job-status').children[0].children[0].text, '正在执行');
});

function visibleText(node) {
  if (node.hidden) return '';
  if (node.tag === 'details' && !node.open) return visibleText(node.children[0]);
  return [node.text || '', ...node.children.map(visibleText)].join(' ');
}

test('raw errors stay collapsed and unfinished stages remain actionable without accounting forms', () => {
  const h = harness(async () => { throw new Error('must not call'); });
  const job = { job_id: 'raw-job-id', status: 'blocked', reason: 'RawTransportError: diagnostic-secret',
    unresolved_calls: [{ call_id: 'raw-call-id', usage_status: 'unknown' }],
    open_attempts: [{ stage: 'material_plan', attempt: 'raw-attempt-id' }],
    recovery: 'Use recovery abandon after reconcile provider evidence', events: [] };
  h.context.showJob(job);
  const host = h.nodes.get('job-status');
  const visible = visibleText(host);
  assert.match(visible, /尚未结束的制作阶段/);
  assert.doesNotMatch(visible, /核账|待核实/);
  assert.doesNotMatch(visible, /输入 Tokens|确认并保存核账结果/);
  assert.match(visible, /不会自动重试/);
  assert.doesNotMatch(visible, /diagnostic-secret|raw-job-id|raw-call-id|raw-attempt-id|material_plan/);
  const activity = host.children[0].children.find(node => node.text === '团队工作记录 ↗');
  activity.onclick();
  const dialog = h.body.children[0];
  assert.doesNotMatch(visibleText(dialog), /diagnostic-secret|raw-job-id|raw-call-id/);
  const details = dialog.querySelector('.team-diagnostics');
  details.open = true;
  assert.match(visibleText(dialog), /diagnostic-secret/);
  assert.match(visibleText(dialog), /recovery abandon/);
  assert.equal(h.timers.length, 0);
});

test('current production role replaces raw model progress on the collapsed running status', () => {
  const h = harness(async () => {});
  h.context.showJob({ job_id: 'j1', status: 'running', events: [{ role: 'writer', say: 'internal-schema-dump' }] });
  assert.match(visibleText(h.nodes.get('job-status')), /编剧 · 继续处理本轮内容/);
  assert.doesNotMatch(visibleText(h.nodes.get('job-status')), /internal-schema-dump/);
});

test('budget and connection failures remain actionable in plain language', () => {
  const h = harness(async () => {});
  h.context.showJob({ job_id: 'j1', status: 'blocked', reason: '20000 tokens reached the soft cap of 20000; refusing a new call.' });
  assert.match(visibleText(h.nodes.get('job-status')), /达到本轮预算上限/);
  h.context.showJob({ job_id: 'j2', status: 'blocked', reason: '连接准备未通过：ConnectionUnavailable' });
  assert.match(visibleText(h.nodes.get('job-status')), /检查制作团队/);
});

function descendants(node, predicate) {
  return [...(predicate(node) ? [node] : []), ...node.children.flatMap((child) => descendants(child, predicate))];
}
const recoverySnapshot = {
  run_id: 'run-a', revision: 'a'.repeat(64), can_recover: true, can_continue: false,
  saved_artifacts: [{ stage: 'intake', version: 1 }], open_attempts: [],
  unresolved_calls: [{ run_id: 'run-a', call_id: 'call-exact', role: 'director', provider: 'synthetic', model: 'test', call_status: 'pending' }],
};

test('unknown usage alone never disables sending or creates an accounting form', async () => {
  const writes = [];
  const h = harness(async (url, options) => {
    if (options) writes.push(options);
    return url.endsWith('/recovery') ? {...recoverySnapshot, can_continue: true} : null;
  });
  await h.context.restoreJob('run-a');
  assert.equal(h.nodes.get('send').disabled, false);
  assert.equal(descendants(h.nodes.get('job-status'), n => n.tag === 'form').length, 0);
  assert.equal(writes.length, 0);
});

test('abandon binds the viewed attempt, retains typed reason on conflict, and cannot target another selected project', async () => {
  const writes = [];
  const snap = { ...recoverySnapshot, unresolved_calls: [], open_attempts: [{ stage: 'direction', token: null }] };
  const h = harness(async (url, options) => {
    if (options) { writes.push(JSON.parse(options.body)); throw new Error('state changed'); }
    return url.endsWith('/recovery') ? snap : null;
  });
  await h.context.restoreJob('run-a');
  const form = descendants(h.nodes.get('job-status'), (node) => node.tag === 'form')[0];
  const reason = descendants(form, (node) => node.tag === 'textarea')[0];
  const confirm = descendants(form, (node) => node.type === 'checkbox')[0];
  const button = descendants(form, (node) => node.type === 'submit')[0];
  reason.value = '服务中断，保留已有版本'; confirm.checked = true;
  await form.onsubmit({ preventDefault() {} });
  assert.equal(writes[0].stage, 'direction');
  assert.equal(writes[0].attempt_token, undefined);
  assert.equal(reason.value, '服务中断，保留已有版本');
  assert.equal(button.disabled, false);
  h.context.state.runId = 'run-b';
  await form.onsubmit({ preventDefault() {} });
  assert.equal(writes.length, 1);
});

test('unknown usage in another project does not block this project or request settlement', async () => {
  const h = harness(async url => url.endsWith('/recovery')
    ? {...recoverySnapshot, can_continue: true, unresolved_calls: [{...recoverySnapshot.unresolved_calls[0], run_id:'other-run'}]} : null);
  await h.context.restoreJob('run-a');
  assert.equal(h.nodes.get('send').disabled, false);
  assert.equal(descendants(h.nodes.get('job-status'), node => node.tag === 'form').length, 0);
});

test('continue after recovery only prepares a draft and never sends it', async () => {
  const calls = [];
  const h = harness(async (url, options) => {
    calls.push({ url, options });
    return url.endsWith('/recovery') ? { ...recoverySnapshot, can_continue: true, unresolved_calls: [] }
      : { job_id: 'j1', status: 'interrupted', events: [] };
  });
  let draft;
  h.context.prepareProjectDraft = (text) => { draft = text; };
  await h.context.restoreJob('run-a');
  descendants(h.nodes.get('job-status'), (node) => node.text === '准备继续创作')[0].onclick();
  assert.match(draft, /检查已保存的内容/);
  assert.ok(calls.every((call) => !call.options));
});
