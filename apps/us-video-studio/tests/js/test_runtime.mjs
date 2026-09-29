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

test('running conversation says what is actually pending while progress belongs to chat',()=>{
  const h=harness(async()=>{});
  h.context.showJob({job_id:'active',job_kind:'conversation',status:'running',
    brief:{title:'方向处理中',detail:'已保存旧方案'},
    events:[{type:'agent_activity',role:'director',phase:'thinking',at:'2026-09-26T12:00:00Z'}]});
  const strip=visibleText(h.nodes.get('job-status'));
  assert.match(strip,/正在等待创意导演的模型回复/);
  assert.match(strip,/本轮结果尚未提交/);
  assert.doesNotMatch(strip,/停止本轮/);
  assert.match(h.nodes.get('send').textContent,/插入调整/);
  assert.equal(h.nodes.get('job-stop').hidden,false);
  assert.doesNotMatch(strip,/最近操作|构思创意方向/);
});

test('running conversation names checked evidence and the unsaved result',()=>{
  const h=harness(async()=>{});
  h.context.showJob({job_id:'active-research',job_kind:'conversation',status:'running',events:[
    {type:'agent_tool',role:'orchestrator',tool:'read_artifact',args:{stage:'direction'},ok:true,at:'2026-09-28T16:16:29Z'},
    {type:'agent_tool',role:'orchestrator',tool:'list_materials',ok:true,at:'2026-09-28T16:16:30Z'},
    {type:'agent_activity',role:'orchestrator',phase:'thinking',at:'2026-09-28T16:16:31Z'},
  ]});
  const strip=visibleText(h.nodes.get('job-status'));
  assert.match(strip,/已核对创意方向、素材清单/);
  assert.match(strip,/本轮结果尚未提交/);
  assert.doesNotMatch(strip,/已阅读方案|项目操作已返回/);
});

test('running direction attempt is progress, not recovery, with checked sources named',()=>{
  const h=harness(async()=>{});
  h.context.showJob({job_id:'direction-active',job_kind:'direction_resume',status:'running',
    open_attempts:[{stage:'direction',attempt:'internal-attempt'}],
    events:[
      {type:'agent_tool',role:'director',tool:'read_capabilities',ok:true,at:'2026-09-28T16:16:29Z'},
      {type:'agent_tool',role:'director',tool:'list_materials',ok:true,at:'2026-09-28T16:16:30Z'},
      {type:'agent_tool',role:'director',tool:'read_artifact',args:{stage:'direction'},ok:true,at:'2026-09-28T16:16:31Z'},
      {type:'agent_activity',role:'director',phase:'thinking',at:'2026-09-28T16:16:31Z'},
    ]});
  const strip=visibleText(h.nodes.get('job-status'));
  assert.equal(h.context.state.recoveryBlocked,false);
  assert.match(strip,/已核对旧方向、素材清单、制作能力/);
  assert.match(strip,/新版尚未保存/);
  assert.match(strip,/最近记录/);
  assert.doesNotMatch(strip,/尚未结束的制作阶段|internal-attempt/);
  assert.equal(h.nodes.get('send').disabled,false);
  assert.match(h.nodes.get('send').textContent,/插入调整/);
  assert.equal(h.nodes.get('job-stop').hidden,false);
});

test('closed reference event stream checks the job before reconnecting',async()=>{
  const calls=[];
  const failed={job_id:'reference-job',job_kind:'reference_video',status:'failed',
    reason:'ReferenceFetchError: [reference_fetch] 无法连接TikTok或视频下载中断',events:[],cursor:3};
  const h=harness(async url=>{
    calls.push(url);
    return url.endsWith('/recovery')?{can_continue:true,revision:'r'}:failed;
  });
  const streams=[];
  h.context.EventSource=class {
    constructor(url){this.url=url;streams.push(this);}
    close(){}
  };
  await h.context.watchJob('run-a',{...failed,status:'running',reason:'',cursor:1},0);
  assert.equal(h.context.state.busy,true);
  streams[0].onerror();
  await h.timers.at(-1)();
  assert.equal(calls[0],'/api/runs/run-a/jobs/reference-job');
  assert.equal(h.context.state.busy,false);
  assert.match(visibleText(h.nodes.get('job-status')),/重试链接或上传原视频/);
});

test('reference media refusal is distinct from page retrieval and model analysis',()=>{
  const h=harness(async()=>{});
  const message=h.context.jobGuidance({job_kind:'reference_video',status:'failed',
    reason:'ReferenceFetchError: [reference_fetch] TikTok视频文件获取失败（HTTP 403），可以上传原视频继续。'});
  assert.match(message,/页面已读取.*视频文件地址返回 HTTP 403/);
  assert.match(message,/不会自动重试或调用声画模型/);
});

test('blocked malformed role output explains why work stopped and how to continue',()=>{
  const h=harness(async()=>{});
  const message=h.context.jobGuidance({job_kind:'conversation',status:'blocked',
    reason:'NonRetryableStageError: 角色模型返回了无法读取的结果，本轮已停止自动重试'});
  assert.match(message,/模型回复格式不完整/);
  assert.match(message,/没有保存.*重新发起/);
});

test('unknown-usage model timeout says the role will not retry automatically',()=>{
  const h=harness(async()=>{});
  const message=h.context.jobGuidance({job_kind:'conversation',status:'blocked',
    reason:'NonRetryableStageError: 受托角色的模型连接超时或中断，本次调用用量无法确认'});
  assert.match(message,/新版未保存.*用量无法确认.*停止自动重试/);
});

test('composer stop cancels only the viewed job once and keeps the draft', async()=>{
  let release;
  const calls=[];
  const h=harness(async(url,options)=>{
    calls.push({url,options});
    return new Promise(resolve=>{release=resolve;});
  });
  const job={job_id:'active',job_kind:'conversation',status:'running',events:[]};
  h.context.showJob(job);
  h.context.$('chat-input').value='下轮再说';
  const first=h.context.stopCurrentJob();
  await h.context.stopCurrentJob();
  assert.equal(calls.length,1);
  assert.equal(calls[0].url,'/api/runs/run-a/jobs/active/cancel');
  assert.equal(h.nodes.get('send').disabled,true);
  assert.equal(h.nodes.get('job-stop').disabled,true);
  release({...job,status:'cancelling'});
  await first;
  assert.equal(h.nodes.get('chat-input').value,'下轮再说');
  assert.equal(h.nodes.get('send').disabled,true);
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
test('uncertain selected-field edit retries its exact target on the dedicated endpoint',async()=>{
  const calls=[];
  const h=harness(async(url,options)=>{
    if(!options)return {can_continue:true,revision:'r'};
    calls.push({url,body:JSON.parse(options.body)});
    if(calls.length===1)throw new TypeError('offline');
    return {job_id:'edit-1',request_id:'same-edit',job_kind:'writing_revision',status:'succeeded',events:[]};
  });
  const payload={request_kind:'writing_revision',request_id:'same-edit',text:'只改这一句',
    feedback_target:{kind:'writing',run_id:'run-a',stage:'writing',version:2,
      artifact_sha256:'a'.repeat(64),quote:'Original line',path:['segments',0,'line']}};
  await h.context.submitJob('run-a',payload);
  await h.nodes.get('job-status').children.at(-1).onclick();
  assert.equal(calls.length,2);
  assert.ok(calls.every(c=>c.url==='/api/runs/run-a/actions/revise-writing'));
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
test('an ended model turn surfaces the concrete missing deliverable', () => {
  const h = harness(async () => ({}));
  const job = {status:'blocked',reason:'交付未完成',deliverable:{goal:'writing',status:'incomplete',missing:['writing 当前准确版本']}};
  assert.match(h.context.jobGuidance(job), /脚本当前准确版本/);
  assert.doesNotMatch(h.context.jobGuidance(job), /writing/);
  assert.doesNotMatch(h.context.jobGuidance(job), /已完成本轮/);
});

test('historical job no longer tells the user to finish retired review', () => {
  const h = harness(async () => ({}));
  h.context.state.timeline = {pending:{kind:'brief_amendment',stage:'direction'}};
  const guidance = h.context.jobGuidance({job_kind:'conversation',status:'blocked',
    deliverable:{goal:'direction',status:'incomplete',missing:['direction 当前版本的独立审核通过记录']}});
  assert.match(guidance, /历史任务的审核要求已停用/);
  assert.match(guidance, /当前作品/);
  assert.doesNotMatch(guidance, /交付还缺|审核通过记录/);
});

test('historical reviewer outcome does not block a saved writing revision', () => {
  const h = harness(async () => { throw new Error('viewing a saved script must not post a job'); });
  const job = {job_id:'edit-1',job_kind:'writing_revision',status:'blocked',events:[],
    revision_result:{stage:'writing',version:2,artifact_sha256:'sha',change_status:'saved',review_status:'return'},
    deliverable:{goal:'revision',status:'incomplete',missing:['修订新版的独立审核通过记录']}};
  h.context.showJob(job);
  const status = visibleText(h.nodes.get('job-status'));
  assert.match(status, /脚本新版已保存/);
  assert.doesNotMatch(status, /审核退回|审核未完成/);
  assert.match(status, /请阅读并决定是否采用/);
  const nav=[];
  h.context.showJourneyTab = tab => nav.push(`tab:${tab}`);
  h.context.selectStage = stage => nav.push(`stage:${stage}`);
  h.context.setProjectFocus = focus => nav.push(`focus:${focus}`);
  descendants(h.nodes.get('job-status'), node => node.text === '查看脚本并决定')[0].onclick();
  assert.deepEqual(nav, ['tab:plan','stage:writing','focus:canvas']);
  h.context.showJob({...job,job_id:'edit-2',revision_result:{...job.revision_result,review_status:'pending'},
    status:'failed',deliverable:null,reason:'独立审核调用失败'});
  const pending = visibleText(h.nodes.get('job-status'));
  assert.match(pending, /脚本新版已保存/);
  assert.doesNotMatch(pending, /审核退回/);
  assert.equal(descendants(h.nodes.get('job-status'), node => node.text === '查看脚本并决定').length, 1);
  h.context.showJob({...job,job_id:'edit-3',revision_result:null});
  assert.doesNotMatch(visibleText(h.nodes.get('job-status')), /脚本新版已保存/);
  h.context.showJob({...job,job_id:'edit-4',revision_result:null,deliverable:null,reason:'模型调用失败'});
  assert.doesNotMatch(visibleText(h.nodes.get('job-status')), /脚本新版已保存/);
  assert.equal(descendants(h.nodes.get('job-status'), node => node.text === '查看脚本并决定').length, 0);
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

test("ended job does not keep the composer locked for a new prompt", () => {
  const h = harness(async () => {});
  h.context.showJob({job_id: "j1", job_kind: "conversation", status: "running", events: []});
  assert.equal(h.context.state.busy, true);
  h.context.showJob({job_id: "j1", job_kind: "conversation", status: "blocked",
    reason: "交付未完成：direction 当前版本的独立审核通过记录", events: []});
  assert.equal(h.context.state.busy, false);
  assert.equal(h.nodes.get("send").disabled, false);
});

test("adjustment against an ended job reports job_not_active for a new-prompt fallback", async () => {
  const h = harness(async () => {
    const err = new Error("原任务已经结束，请刷新后发送新的指令。");
    err.status = 409;
    throw err;
  });
  h.context.showJob({job_id: "j1", job_kind: "conversation", status: "running", events: []});
  h.context.restoreJob = async () => {
    h.context.showJob({job_id: "j1", job_kind: "conversation", status: "blocked",
      reason: "交付未完成", events: []});
  };
  const result = await h.context.submitSteer("run-a", "j1",
    {text: "下一条指令", request_id: "r1", expected_goal_revision: 0});
  assert.equal(result.ok, false);
  assert.equal(result.reason, "job_not_active");
  assert.equal(h.context.state.busy, false);
  assert.equal(h.nodes.get("send").disabled, false);
});


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
    if (url.includes('/request-receipts/accepted?kind=job'))
      return {received:true,kind:'job',request_id:'accepted',job_id:'j1'};
    return { job_id: 'j1', request_id: 'accepted', status: 'running', events: [{ say: '审核中' }] };
  });
  h.storage.set('studio-job-pending:run-a', JSON.stringify({ request_id: 'accepted', text: 'go' }));
  await h.context.restoreJob('run-a');
  assert.equal(calls.length, 2);
  assert.ok(calls.every(call => call.options === undefined));
  assert.match(calls[1].url, /request-receipts\/accepted\?kind=job/);
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

test('unchanged polling preserves the composer stop button and keyboard focus target', () => {
  const h = harness(async () => {});
  const job = { job_id: 'j1', status: 'running', events: [] };
  h.context.showJob(job);
  const stopButton = h.nodes.get('job-stop');
  h.context.showJob({ ...job, cursor: 12 });
  assert.equal(h.nodes.get('job-stop'), stopButton);
  assert.equal(stopButton.hidden,false);
});

test('bounded diagnosis failure preserves an unsent composer draft', async () => {
  const calls = [];
  const h = harness(async (url, options) => {
    if (!options) return {status:'succeeded',events:[]};
    calls.push({url,body:JSON.parse(options.body)});
    throw {status:409,message:'制作记录已变化'};
  });
  h.context.$('chat-input').value = '我还在写的反馈';
  await h.context.submitJob('run-a', {
    text:'请诊断已记录的问题',request_id:'diagnose-once',reference_consent:false,
    feedback_target:{kind:'production_draft',run_id:'run-a',draft_id:'draft-a',draft_sha256:'a'.repeat(64)},
  }, {preserveComposerOnError:true});
  assert.equal(h.nodes.get('chat-input').value, '我还在写的反馈');
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, '/api/runs/run-a/jobs');
  assert.equal(calls[0].body.reference_consent, false);
  assert.equal(calls[0].body.feedback_target.draft_id, 'draft-a');
});

test('an uncertain diagnosis retries the same request without replacing later user writing', async () => {
  const calls = [];
  const h = harness(async (url, options) => {
    if (!options) return {status:'succeeded',events:[]};
    calls.push(JSON.parse(options.body));
    if (calls.length === 1) throw new TypeError('connection lost');
    throw {status:409,message:'制作记录已变化'};
  });
  const input = h.context.$('chat-input');
  input.value = '我自己的反馈还没发';
  const payload = {text:'请诊断已记录的问题',request_id:'diagnose-same',reference_consent:false,
    feedback_target:{kind:'production_draft',run_id:'run-a',draft_id:'draft-a',draft_sha256:'a'.repeat(64)}};
  await h.context.submitJob('run-a',payload,{preserveComposerOnError:true});
  await h.nodes.get('job-status').children.at(-1).onclick();
  assert.deepEqual(calls[0],calls[1]);
  assert.equal(input.value,'我自己的反馈还没发');
});

test('refresh restores the bounded diagnosis UI option before an uncertain retry', async () => {
  const calls = [];
  const h = harness(async (_url, options) => {
    if (!options) return {job_id:'older',request_id:'older',status:'succeeded',events:[]};
    calls.push(JSON.parse(options.body));
    if (calls.length === 1) throw new TypeError('connection lost');
    throw {status:409,message:'制作记录已变化'};
  });
  const payload = {text:'请诊断已记录的问题',request_id:'diagnose-after-refresh',reference_consent:false,
    feedback_target:{kind:'production_draft',run_id:'run-a',draft_id:'draft-a',draft_sha256:'a'.repeat(64)}};
  await h.context.submitJob('run-a',payload,{preserveComposerOnError:true});
  assert.equal(JSON.parse(h.storage.get('studio-job-ui:run-a')).request_id,payload.request_id);
  await h.context.restoreJob('run-a');
  await h.nodes.get('job-status').children.at(-1).onclick();
  assert.deepEqual(calls[0],calls[1]);
  assert.equal(h.context.$('chat-input').value,'');
  assert.equal(h.storage.size,0);
});

test('conversation shows stop while running and old poll cannot undo cancellation', () => {
  const h = harness(async () => {});
  const job = {job_id:'j1', job_kind:'conversation', status:'running', cursor:10, events:[]};
  h.context.showJob(job);
  assert.equal(h.nodes.get('send').disabled, false);
  assert.match(h.nodes.get('send').textContent, /插入调整/);
  assert.equal(h.nodes.get('job-stop').hidden,false);
  h.context.showJob({...job, status:'cancelling', cursor:12, goal_revision:1, steer_status:'received'});
  h.context.showJob({...job, cursor:11});
  assert.equal(vm.runInContext('currentJob.status', h.context), 'cancelling');
  assert.equal(vm.runInContext('currentJob.goal_revision', h.context), 1);
});

test('a stopping task disables stop unless a saved adjustment is settling', () => {
  const h = harness(async () => {});
  h.context.showJob({job_id:'j1', job_kind:'conversation', status:'cancelling', cursor:12, events:[]});
  assert.equal(h.nodes.get('send').disabled, true);
  h.context.showJob({job_id:'j1', job_kind:'conversation', status:'cancelling', cursor:13,
    steer_status:'received', events:[]});
  assert.equal(h.nodes.get('send').disabled, false);
  assert.match(h.nodes.get('send').textContent, /插入调整/);
  assert.equal(h.nodes.get('job-stop').disabled,false);
});

test('steering uses one stable request receipt and does not start a concurrent job', async () => {
  const calls = [];
  const h = harness(async (url, options) => {
    calls.push([url, JSON.parse(options.body)]);
    return {job_id:'j1', job_kind:'conversation', status:'cancelling', cursor:9,
      goal_revision:1, steer_status:'received', events:[]};
  });
  h.context.showJob({job_id:'j1', job_kind:'conversation', status:'running', cursor:5, events:[]});
  const payload = {text:'只缩短口播', request_id:'same-id', expected_goal_revision:0};
  await h.context.submitSteer('run-a','j1',payload);
  assert.deepEqual(calls, [['/api/runs/run-a/jobs/j1/steer', payload]]);
  assert.equal(h.storage.size, 0);
  assert.equal(vm.runInContext('currentJob.steer_status', h.context), 'received');
});

test('saved adjustment stays visible after interruption and resumes only on click', async () => {
  const calls = [];
  const child = {job_id:'child', parent_job_id:'old', job_kind:'conversation', status:'succeeded',
    cursor:21, events:[], brief:{title:'已接续',detail:'新目标已处理'}};
  const h = harness(async (url, options) => {
    calls.push({url, options});
    if (url.endsWith('/resume-steer')) return child;
    if (url.endsWith('/recovery')) return {revision:'r1',can_continue:true,open_attempts:[]};
    return child;
  });
  h.context.showJob({job_id:'old', job_kind:'conversation', status:'interrupted', cursor:20,
    steer_status:'blocked', steer_latest_text:'改成分析', steer_reason:'服务中断，未自动接续',
    events:[], brief:{title:'服务中断',detail:'已保存'}});
  assert.equal(calls.length, 0);
  assert.match(visibleText(h.nodes.get('job-status')), /改成分析/);
  const button = descendants(h.nodes.get('job-status'), n => n.text === '继续保存的调整')[0];
  assert.ok(button);
  await button.onclick();
  assert.equal(calls.filter(call => call.options).length, 1);
  assert.equal(calls[0].url, '/api/runs/run-a/jobs/old/resume-steer');
  assert.equal(vm.runInContext('currentJob.job_id', h.context), 'child');
});

test('timeline failure after acceptance keeps the running job and composer stop', async () => {
  const h = harness(async () => ({ job_id: 'accepted', status: 'running', events: [] }));
  h.context.refreshTimeline = async () => { throw new TypeError('timeline offline'); };
  await h.context.submitJob('run-a', { request_id: 'r1', text: 'test' });
  assert.equal(h.context.state.busy, true);
  assert.equal(h.nodes.get('send').disabled, false);
  assert.match(h.nodes.get('send').textContent, /插入调整/);
  assert.equal(h.nodes.get('job-stop').hidden,false);
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
  assert.match(visibleText(h.nodes.get('job-status')), /当时的预算设置挡住/);
  h.context.showJob({ job_id: 'j2', status: 'blocked', reason: '连接准备未通过：ConnectionUnavailable' });
  assert.match(visibleText(h.nodes.get('job-status')), /检查制作团队/);
});

test('a blocked brief conversation shows an old-budget explanation outside the optional work record',()=>{
  const h=harness(async()=>{});
  h.context.state.timeline={budget:{limit_tokens:null,limit_usd:null,limit_mode:'none'}};
  h.context.showJob({job_id:'j-budget',run_id:'run-a',job_kind:'conversation',status:'blocked',
    brief:{title:'原始创作目标',detail:'旧进展'},
    reason:'[budget] 2009455 tokens reached the soft cap of 2000000; refusing a new call.',events:[]});
  const visible=visibleText(h.nodes.get('job-status'));
  assert.match(visible,/上一轮被旧额度设置中断/);
  assert.match(visible,/已保存内容仍可阅读/);
  assert.match(visible,/不会自动重试/);
  assert.doesNotMatch(visible,/2009455|refusing a new call/);
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
