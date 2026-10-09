import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../../studio/web/team-progress.js', import.meta.url), 'utf8') + '\n' + fs.readFileSync(new URL('../../studio/web/runtime.js', import.meta.url), 'utf8');
const appSource = fs.readFileSync(new URL('../../studio/web/app.js', import.meta.url), 'utf8');
const boundedReadSource = appSource.slice(appSource.indexOf('async function boundedProjectRead('),appSource.indexOf('async function loadProjectView('));
test('production prerequisite shows its exact next action without generic retry copy',()=>{
  const h=harness(async()=>{throw Error('render must not execute');});
  const note='当前方案缺共同设计。请打开「补齐制作设计，保留原脚本」确认。';
  const job={job_id:'blocked-production',job_kind:'conversation',status:'blocked',events:[],
    reason:note,deliverable:{goal:'production_input',status:'incomplete',missing:[note],blocker:{note}},
    brief:{title:'旧采用方案已恢复',detail:'保留原输入',phase:'ready',
      execution:{active:false,status:'blocked',project_restored:true}}};
  assert.equal(h.context.jobGuidance(job),note);
  h.context.showJob(job);
  const copy=visibleText(h.nodes.get('job-status'));
  assert.match(copy,/补齐制作设计/);
  assert.doesNotMatch(copy,/交付还缺|请查看准确版本后继续|。。|旧采用方案已恢复/);
  assert.equal(descendants(h.nodes.get('job-status'),node=>node.attributes?.role==='alert').length,1);
});
test('unresolved original media guidance keeps the shot-specific action and identity',()=>{
  const h=harness(async()=>{throw Error('render must not execute');});
  const note='第 3 镜的原图片任务结果未核实。请打开该镜头原任务核实，不要重新提交。';
  const job={status:'blocked',deliverable:{goal:'production_media',status:'unknown',
    blocker:{note,next_action:{action:'check_original_task',attempt_id:'original-3'}}}};
  assert.equal(h.context.jobGuidance(job),note);
  assert.equal(job.deliverable.blocker.next_action.attempt_id,'original-3');
});
test('cancelled interrupted and failed outcomes outrank a retained production blocker',()=>{
  const h=harness(async()=>{throw Error('render must not execute');});
  for(const deliveryState of ['unknown','incomplete']){
    const deliverable={status:deliveryState,missing:['共同设计'],blocker:{note:'请打开设计补充确认。'}};
    for(const status of ['cancelled','interrupted','failed']){
      const copy=h.context.jobGuidance({status,job_kind:'conversation',deliverable});
      assert.doesNotMatch(copy,/设计补充|查询原任务|交付还缺/);
      assert.match(copy,status==='cancelled'?/已停止/:status==='interrupted'?/中断/:/未完成/);
    }
  }
});
test('design upgrade uses one durable identity and replays its exact dedicated request after uncertainty',async()=>{
  const calls=[];
  const h=harness(async(url,options)=>{
    if(!options)return {can_continue:true,revision:'r'};
    calls.push({url,body:JSON.parse(options.body)});
    if(calls.length===1)throw new TypeError('offline');
    return {job_id:'design',request_id:'same-design',job_kind:'design_upgrade',status:'succeeded',events:[]};
  });
  const payload={request_kind:'design_upgrade',request_id:'same-design',expected_context_sha256:'a'.repeat(64),expected_connection_revision:0,consent:true};
  await h.context.submitJob('run-a',payload);
  await h.nodes.get('job-status').children.at(-1).onclick();
  assert.equal(calls.length,2);
  assert.ok(calls.every(c=>c.url==='/api/runs/run-a/production/design-upgrade/propose'));
  assert.deepEqual(calls[0].body,calls[1].body);
  assert.equal(calls[0].body.request_kind,undefined);
  assert.match(visibleText(h.nodes.get('job-status')),/补充已准备好/);
});
test('received discussion keeps the current task running and describes a read-only successor',()=>{
  const h=harness(async()=>{throw Error('render must not execute');});
  h.context.showJob({job_id:'discussion',job_kind:'conversation',status:'running',
    steer_status:'received',steer_class:'discussion',events:[]});
  const strip=visibleText(h.nodes.get('job-status'));
  assert.match(strip,/讨论已收到/);
  assert.match(strip,/原任务继续执行/);
  assert.match(strip,/不会自动改稿/);
});
test('unanswered recovered status question is visible and offers no adjustment replay',()=>{
  const h=harness(async()=>{throw Error('render must not execute');});
  h.context.showJob({job_id:'unanswered',job_kind:'conversation',status:'interrupted',
    steer_status:'unanswered',steer_reason:'之前的问题未保存回答，请重新查询状态。',events:[]});
  const strip=visibleText(h.nodes.get('job-status'));
  assert.match(strip,/未保存回答/);
  assert.doesNotMatch(strip,/继续保存的调整/);
});
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
  assert.match(h.nodes.get('send').textContent,/停止/);
  // The composer action rides the secondary button while the job runs, so 停止
  // keeps one identity instead of swapping with 发送 as the user types.
  assert.match(h.nodes.get('job-stop').textContent,/插入调整/);
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
  assert.match(h.nodes.get('send').textContent,/停止/);
  assert.match(h.nodes.get('job-stop').textContent,/插入调整/);
  assert.equal(h.nodes.get('job-stop').hidden,false);
});

const waitingModelEvent = (extra = {}) => ({type:'agent_activity',role:'director',phase:'waiting_model',
  elapsed_seconds:90,remaining_seconds:510,transport_state:'waiting',at:'2026-10-02T02:00:00Z',...extra});

test('the original conversation line shows real wait, transport and single call limit',()=>{
  const h=harness(async()=>{throw new Error('render must not submit work');});
  const event=waitingModelEvent({text:'private-reasoning',args:{content:'private-tool-arguments'}});
  h.context.showJob({job_id:'waiting',job_kind:'conversation',status:'running',events:[event]});
  const host=h.nodes.get('job-status'),strip=visibleText(host);
  assert.match(strip,/创意导演：已等待 1分30秒；等待模型回复；单次最多等待 10分钟；本次最多还能等待 8分30秒/);
  assert.match(strip,/本轮结果尚未提交/);
  assert.doesNotMatch(strip,/private-|已完成|%/);
  assert.equal(descendants(host,node=>node.className==='job-live-summary').length,1);
  assert.equal(descendants(host,node=>node.tag==='dialog'||node.tag==='form'||node.attributes?.role==='alert').length,0);
  assert.equal(h.timers.length,0);
  assert.equal(h.nodes.get('send').dataset.mode,'stop');
});

test('wait heartbeats refresh at the same event count and ignore older snapshots',()=>{
  const h=harness(async()=>{});
  const job={job_id:'waiting',job_kind:'conversation',status:'running'};
  h.context.showJob({...job,cursor:1,events:[waitingModelEvent()]});
  h.context.showJob({...job,cursor:2,events:[waitingModelEvent({elapsed_seconds:120,remaining_seconds:480,transport_state:'receiving'})]});
  const strip=visibleText(h.nodes.get('job-status'));
  assert.match(strip,/已等待 2分钟；正在接收模型回复；单次最多等待 10分钟；本次最多还能等待 8分钟/);
  assert.doesNotMatch(strip,/已等待 1分30秒|本轮完成/);
  h.context.showJob({...job,cursor:1,events:[waitingModelEvent()]});
  assert.equal(visibleText(h.nodes.get('job-status')),strip);
  h.context.showJob({...job,cursor:3,events:[{type:'agent_activity',role:'writer',phase:'tool',tool:'read_artifact',args:{stage:'writing'}}]});
  assert.match(visibleText(h.nodes.get('job-status')),/编剧正在阅读方案/);
  assert.doesNotMatch(visibleText(h.nodes.get('job-status')),/已等待|单次最多等待/);
});

test('direction redo preserves real milestones while its model is still receiving',()=>{
  const h=harness(async()=>{});
  h.context.showJob({job_id:'redo-waiting',job_kind:'direction_resume',status:'running',events:[
    {type:'agent_tool',role:'director',tool:'read_artifact',args:{stage:'direction'},ok:true},
    waitingModelEvent({transport_state:'receiving'}),
  ]});
  const strip=visibleText(h.nodes.get('job-status'));
  assert.match(strip,/已核对旧方向/);
  assert.match(strip,/新版尚未保存/);
  assert.match(strip,/已等待 1分30秒；正在接收模型回复；单次最多等待 10分钟/);
  assert.doesNotMatch(strip,/新版 A\/B\/C 已保存|本轮完成/);
});

test('stopping keeps actual call wait after a saved adjustment has been discarded',()=>{
  const h=harness(async()=>{});
  h.context.showJob({job_id:'stopping',job_kind:'conversation',status:'cancelling',steer_status:'discarded',events:[waitingModelEvent()]});
  const strip=visibleText(h.nodes.get('job-status'));
  assert.match(strip,/正在停止/);
  assert.match(strip,/不会启动下一步/);
  assert.match(strip,/已等待 1分30秒/);
  assert.doesNotMatch(strip,/调整已收到|按你的调整接续/);
  assert.equal(h.nodes.get('send').dataset.mode,'stop');
  assert.equal(h.nodes.get('send').disabled,true);
  h.context.showJob({job_id:'stopping',job_kind:'conversation',status:'cancelled',events:[waitingModelEvent()]});
  assert.doesNotMatch(visibleText(h.nodes.get('job-status')),/已等待|单次最多等待|正在接收/);
  assert.equal(h.nodes.get('send').dataset.mode,'send');
  assert.equal(h.context.state.busy,false);
});

test('adjustment and analysis jobs share the same reported transport statistics',()=>{
  const h=harness(async()=>{});
  for (const status of ['running','cancelling']) {
    h.context.showJob({job_id:'adjusting',job_kind:'conversation',status,steer_status:'received',events:[waitingModelEvent()]});
    assert.match(visibleText(h.nodes.get('job-status')),/调整已收到.*按你的调整接续.*已等待 1分30秒/s);
    assert.equal(h.nodes.get('send').disabled,false);
  }
  h.context.showJob({job_id:'analysis',job_kind:'image_analysis',status:'running',events:[waitingModelEvent({role:'material'})]});
  const strip=visibleText(h.nodes.get('job-status'));
  assert.match(strip,/视觉策划正在分析素材/);
  assert.match(strip,/已等待 1分30秒；等待模型回复；单次最多等待 10分钟/);
  assert.equal((strip.match(/已等待/g)||[]).length,1);
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
    reason:'NonRetryableStageError: 团队模型返回的结果读不出来，本轮已停止自动重试'});
  // The user-facing sentence names the team, not an internal "role model"; the
  // old wording leaked the architecture into a failure message.
  assert.match(message,/团队模型的回复格式不完整/);
  assert.match(message,/没有保存.*重新发起/);
  assert.doesNotMatch(message,/角色/);
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
  h.context.state.timeline = {run:{run_id:'run-a',content_format:'video'}};
  const job = {run_id:'run-a',status:'blocked',reason:'交付未完成',deliverable:{goal:'writing',status:'incomplete',missing:['writing 当前准确版本']}};
  assert.match(h.context.jobGuidance(job), /脚本当前准确版本/);
  assert.doesNotMatch(h.context.jobGuidance(job), /writing/);
  assert.doesNotMatch(h.context.jobGuidance(job), /已完成本轮/);
});

test('photo job guidance uses the trusted matching project format and keeps original outcomes', () => {
  const h = harness(async () => { throw Error('presentation must not submit'); });
  h.context.state.timeline = {run:{run_id:'run-a',content_format:'photo'}};
  const missing = {job_id:'photo-writing-missing',run_id:'run-a',cursor:1,status:'blocked',events:[],reason:'交付未完成',
    deliverable:{goal:'writing',status:'incomplete',missing:['writing 当前准确版本']}};
  const original = JSON.stringify(missing);
  h.context.showJob(missing);
  assert.match(visibleText(h.nodes.get('job-status')), /图文方案当前准确版本/);
  assert.doesNotMatch(visibleText(h.nodes.get('job-status')), /脚本|writing/);
  assert.equal(JSON.stringify(missing), original);
  assert.match(h.context.jobGuidance({...missing,job_kind:'writing_revision',
    revision_result:{change_status:'saved'}}), /图文方案新版已保存/);
  assert.match(h.context.jobGuidance({...missing,status:'succeeded',job_kind:'design_upgrade'}), /原图文方案与图片保留/);
  assert.match(h.context.jobGuidance({...missing,deliverable:{status:'input_ready'}}), /逐页图片输入已齐备/);
});

test('unknown or mismatched projects cannot supply a job format from titles or raw requests', () => {
  const h = harness(async () => { throw Error('presentation must not submit'); });
  const job = {run_id:'run-a',status:'blocked',reason:'做视频',
    deliverable:{goal:'writing',status:'incomplete',missing:['writing 当前准确版本']}};
  for (const project of [undefined,{run_id:'run-a',content_format:null},
    {run_id:'other-run',content_format:'photo'},{run_id:'run-a',content_format:'unexpected'}]) {
    h.context.state.timeline = {run:project};
    assert.match(h.context.jobGuidance(job), /创作方案当前准确版本/);
    assert.doesNotMatch(h.context.jobGuidance(job), /脚本|图文方案/);
  }
  h.context.state.timeline = {run:{run_id:'run-a',content_format:'photo'}};
  assert.match(h.context.jobGuidance({...job,run_id:'other-run'}), /创作方案当前准确版本/);
  assert.match(h.context.jobGuidance({...job,run_id:undefined}), /创作方案当前准确版本/);
});

test('verified project recovery replaces the old missing-script banner without changing job or media status', () => {
  const h = harness(async () => { throw new Error('presentation must not start work'); });
  const job = {job_id:'restored',job_kind:'conversation',status:'blocked',events:[],
    deliverable:{status:'incomplete',missing:['已确认的当前完整方案']},
    brief:{title:'原采用方案已恢复',detail:'已保留分镜制作输入与修改意见',phase:'ready',
      execution:{active:false,status:'blocked',project_restored:true}}};
  h.context.showJob(job);
  const copy = visibleText(h.nodes.get('job-status'));
  assert.match(copy, /原采用方案已恢复/);
  assert.doesNotMatch(copy, /交付还缺|本轮完成/);
  assert.equal(job.status, 'blocked');
  assert.equal(h.context.state.busy, false);
  assert.match(h.context.jobGuidance({...job,brief:{...job.brief,
    execution:{...job.brief.execution,project_restored:false}}}), /交付还缺/);
  assert.match(h.context.jobGuidance({...job,job_kind:'production_group',reason:'uncertain submission',
    deliverable:null}), /暂停/);
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
  assert.match(status, /创作方案新版已保存/);
  assert.doesNotMatch(status, /审核退回|审核未完成/);
  assert.match(status, /请阅读并决定是否采用/);
  const nav=[];
  h.context.showJourneyTab = tab => nav.push(`tab:${tab}`);
  h.context.selectStage = stage => nav.push(`stage:${stage}`);
  h.context.setProjectFocus = focus => nav.push(`focus:${focus}`);
  descendants(h.nodes.get('job-status'), node => node.text === '查看创作方案并决定')[0].onclick();
  assert.deepEqual(nav, ['tab:plan','stage:writing','focus:canvas']);
  h.context.showJob({...job,job_id:'edit-2',revision_result:{...job.revision_result,review_status:'pending'},
    status:'failed',deliverable:null,reason:'独立审核调用失败'});
  const pending = visibleText(h.nodes.get('job-status'));
  assert.match(pending, /创作方案新版已保存/);
  assert.doesNotMatch(pending, /审核退回/);
  assert.equal(descendants(h.nodes.get('job-status'), node => node.text === '查看创作方案并决定').length, 1);
  h.context.showJob({...job,job_id:'edit-3',revision_result:null});
  assert.doesNotMatch(visibleText(h.nodes.get('job-status')), /创作方案新版已保存/);
  h.context.showJob({...job,job_id:'edit-4',revision_result:null,deliverable:null,reason:'模型调用失败'});
  assert.doesNotMatch(visibleText(h.nodes.get('job-status')), /创作方案新版已保存/);
  assert.equal(descendants(h.nodes.get('job-status'), node => node.text === '查看创作方案并决定').length, 0);
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
  const node = () => ({ children: [], disabled: false, hidden: false, value: '', dataset: {},
    append(...items) { this.children.push(...items); }, replaceChildren() { this.children = []; },
    setAttribute(name, value) {
      // Mirror the real DOM: data-* lands in `dataset` under its camelCase key,
      // so a test can read back exactly what the renderer set.
      const key = String(name).startsWith('data-')
        ? String(name).slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())
        : name;
      if (key === name) this.attributes = {...(this.attributes || {}), [name]: value};
      else this.dataset[key] = value;
    },
 addEventListener() {}, showModal() { this.open = true; },
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
    refreshTimeline: async () => {}, projectReadStatus() {}, toast() {}, api,
  });
  vm.runInContext(boundedReadSource,context);
  vm.runInContext(source, context);
  context.syncComposerActionForTest = () => vm.runInContext('syncComposerAction()', context);
  return { context, nodes, storage, timers, body };
}

const savedStageEvent = (seq = 12) => ({seq, type:'agent_tool', role:'material',
  stage:'material_plan', tool:'submit_stage', ok:true, args:{stage:'material_plan'}});

test('running job refreshes saved stage content once across repeated progress polls', async () => {
  const h = harness(async () => {});
  let refreshes = 0;
  h.context.refreshTimeline = async () => { refreshes++; return true; };
  const job = {job_id:'save-1',status:'running',cursor:12,events:[savedStageEvent()]};
  await h.context.watchJob('run-a',job,0);
  assert.equal(refreshes,1);
  assert.equal(h.context.state.busy,true);
  await h.context.watchJob('run-a',{...job,cursor:30,events:[...job.events,
    {seq:30,type:'agent_narration',text:'正在继续写稿',role:'writer'}]},0);
  assert.equal(refreshes,1);
  await h.context.watchJob('run-a',{...job,cursor:40,events:[...job.events,savedStageEvent(40)]},0);
  assert.equal(refreshes,2);
});

test('tokens and failed or skipped persistence calls do not refresh the timeline', async () => {
  const h = harness(async () => {});
  let refreshes = 0;
  h.context.refreshTimeline = async () => { refreshes++; };
  const events = [
    {seq:1,type:'agent_narration_preview',text:'partial'},
    {seq:2,type:'agent_narration',text:'saved according to model'},
    {seq:3,type:'agent_activity',tool:'submit_stage'},
    {...savedStageEvent(4),ok:false}, {...savedStageEvent(5),skipped:true},
    {...savedStageEvent(6),tool:'read_artifact'}, {...savedStageEvent(7),seq:null},
  ];
  await h.context.refreshSavedArtifacts('run-a',{job_id:'no-save',events},0);
  assert.equal(refreshes,0);
});

test('a failed or superseded refresh retries the same saved receipt on a later poll', async () => {
  const h = harness(async () => {});
  let refreshes = 0;
  h.context.refreshTimeline = async () => {
    refreshes++;
    if (refreshes === 1) throw Error('offline');
    return refreshes > 2;
  };
  const job = {job_id:'save-retry',events:[savedStageEvent()]};
  for(let i=0;i<4;i++) await h.context.refreshSavedArtifacts('run-a',job,0);
  assert.equal(refreshes,3);
});

test('concurrent SSE and polling refresh share the same saved receipt', async () => {
  const h = harness(async () => {});
  let release, refreshes = 0;
  h.context.refreshTimeline = () => { refreshes++; return new Promise(resolve => {release=resolve;}); };
  const job = {job_id:'concurrent-save',events:[savedStageEvent()]};
  const first = h.context.refreshSavedArtifacts('run-a',job,0);
  const second = h.context.refreshSavedArtifacts('run-a',job,0);
  assert.equal(refreshes,1);
  release(true);
  await Promise.all([first,second]);
  assert.equal(refreshes,1);
});

test('project switch during artifact refresh cannot acknowledge a stale receipt or attach old polling', async () => {
  const h = harness(async () => {});
  let release, refreshes = 0;
  h.context.refreshTimeline = () => {refreshes++; return new Promise(resolve => {release=resolve;});};
  const job = {job_id:'old-save',status:'running',events:[savedStageEvent()]};
  const pending = h.context.watchJob('run-a',job,0);
  h.context.state.runId = 'run-b';
  h.context.detachProjectJob();
  release(true);
  await pending;
  assert.equal(h.timers.length,0);
  assert.equal(vm.runInContext('savedArtifactRefreshes.size',h.context),0);
  await h.context.refreshSavedArtifacts('run-a',job,0);
  assert.equal(refreshes,1);
});

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
  assert.match(h.nodes.get('send').textContent, /停止/);
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
  assert.match(h.nodes.get('job-stop').textContent, /插入调整/);
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
  assert.deepEqual(calls, [['/api/runs/run-a/jobs/j1/steer', {...payload,reference_consent:true}]]);
  assert.equal(h.storage.size, 0);
  assert.equal(vm.runInContext('currentJob.steer_status', h.context), 'received');
});

test('busy input carries its own media purpose and exact reference target', async () => {
  const calls=[];
  const h=harness(async(url,options)=>{
    calls.push({url,body:JSON.parse(options.body)});
    return {job_id:'busy',job_kind:'conversation',status:'cancelling',events:[]};
  });
  const target={run_id:'run-a',reference_id:'ref',analysis_id:'analysis',candidate_sha256:'a'.repeat(64)};
  const payload={text:'用这条参考',request_id:'media-input',expected_goal_revision:0,
    attachments:[{name:'reference.mp4',purpose:'reference'}],reference_target:target};
  h.context.showJob({job_id:'busy',job_kind:'conversation',status:'running',events:[]});
  await h.context.submitSteer('run-a','busy',payload);
  assert.deepEqual(calls[0].body,{...payload,reference_consent:true});
  await h.context.submitSteer('run-a','busy',{...payload,request_id:'no-admission',reference_consent:false});
  assert.equal(calls[1].body.reference_consent,false);
});

test('legacy uncertain steer keeps frozen payload without adding observation admission', async () => {
  const calls=[];
  const h=harness(async(url,options)=>{
    calls.push({url,body:JSON.parse(options.body)});
    return {job_id:'busy',job_kind:'conversation',status:'cancelling',events:[]};
  });
  const payload={text:'旧的待核实输入',request_id:'legacy-input',expected_goal_revision:0};
  h.storage.set('studio-steer-pending:run-a',JSON.stringify({...payload,job_id:'busy'}));
  h.context.showJob({job_id:'busy',job_kind:'conversation',status:'running',events:[]});
  const result=await h.context.submitSteer('run-a','busy',payload);
  assert.equal(result.ok,true);
  assert.deepEqual(calls[0].body,payload);
  assert.equal(Object.hasOwn(calls[0].body,'reference_consent'),false);
});

test('legacy uncertain idle input retries its exact envelope without new metadata or current view defaults',async()=>{
  const calls=[];
  const h=harness(async(url,options)=>{
    if(!options)return {revision:'r',can_continue:true};
    calls.push({url,body:JSON.parse(options.body)});
    return {job_id:'old-job',job_kind:'conversation',status:'succeeded',events:[]};
  });
  const payload={text:'旧的待核实输入',request_id:'legacy-idle'};
  h.storage.set('studio-job-pending:run-a',JSON.stringify(payload));
  h.context.state.runs=[{run_id:'run-a',kind:'creative'}];
  h.context.state.timeline={display_context_id:'new-display'};
  const result=await h.context.submitJob('run-a',payload);
  assert.equal(result.accepted,true);
  assert.deepEqual(calls,[{url:'/api/runs/run-a/jobs',body:payload}]);
  assert.equal(h.storage.has('studio-job-pending:run-a'),false);
});

test('legacy idle frozen input refuses added metadata and preserves its original retry entry',async()=>{
  const h=harness(async()=>{throw Error('changed retry must not reach HTTP');});
  const payload={text:'旧的待核实输入',request_id:'legacy-idle'};
  h.storage.set('studio-job-pending:run-a',JSON.stringify(payload));
  const result=await h.context.submitJob('run-a',{...payload,reference_consent:true,attachments:[]});
  assert.equal(result.accepted,false);assert.equal(result.uncertain,true);
  assert.deepEqual(JSON.parse(h.storage.get('studio-job-pending:run-a')),payload);
  assert.equal(h.nodes.get('job-status').children.at(-1).text,'确认提交状态');
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
  assert.match(h.nodes.get('send').textContent, /停止/);
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

test('the primary button keeps one identity while a job runs, whatever is typed', () => {
  // Reported 2026-09-30: "the agent starts working, so send becomes stop — but
  // as soon as I type, stop becomes send again." The primary button used to swap
  // between 发送 / 插入调整 / (separately) 停止 depending on whether the running
  // job accepted an adjustment, so its meaning changed under the user's hands.
  //
  // ``syncComposerAction`` is the one place that decides this, so the test reads
  // its output directly: the identity must be a function of "is a job running",
  // and of nothing the user types.
  const h = harness(async () => {});
  // ``syncComposerAction()`` reads the module's ``currentJob`` when given no
  // argument — that is how every production caller invokes it — so the test
  // drives that same variable rather than passing a job the real code never does.
  const sync = (job) => {
    vm.runInContext(`currentJob = ${JSON.stringify(job)}`, h.context);
    h.context.syncComposerActionForTest();
  };
  const primary = () => h.nodes.get('send');
  const secondary = () => h.nodes.get('job-stop');

  // Idle: the primary button sends and there is nothing to stop.
  sync(null);
  assert.equal(primary().dataset.mode, 'send');
  assert.match(primary().textContent, /发送/);
  assert.equal(secondary().hidden, true);

  // A steerable job is running: 停止 takes the primary slot and stays there.
  const job = {job_id: 'j1', job_kind: 'conversation', status: 'running', events: []};
  sync(job);
  assert.equal(primary().dataset.mode, 'stop');
  assert.match(primary().textContent, /停止/);
  assert.match(secondary().textContent, /插入调整/);
  assert.equal(secondary().hidden, false);

  // Re-rendering (which is what a keystroke triggers) must not move it.
  sync({...job, cursor: 2});
  assert.equal(primary().dataset.mode, 'stop');
  assert.match(primary().textContent, /停止/);

  // A reference-video job is steerable too, so 停止 still leads and the
  // composer's action stays on the secondary button.
  sync({job_id: 'j2', job_kind: 'reference_video', status: 'running', events: []});
  assert.equal(primary().dataset.mode, 'stop');
  assert.match(primary().textContent, /停止/);
  assert.match(secondary().textContent, /插入调整/);
  assert.equal(secondary().hidden, false);

  // A job already stopping keeps 停止 in place and says so.
  sync({job_id: 'j3', job_kind: 'conversation', status: 'cancelling', events: []});
  assert.equal(primary().dataset.mode, 'stop');
  assert.match(primary().textContent, /正在停止/);
  assert.equal(primary().disabled, true);

  // Once it ends the primary button goes back to sending.
  sync({job_id: 'j2', job_kind: 'reference_video', status: 'succeeded', events: []});
  assert.equal(primary().dataset.mode, 'send');
  assert.match(primary().textContent, /发送/);
  assert.equal(secondary().hidden, true);
});

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


test('direct field edit composer names only its matching explicit project format',()=>{
  const h=harness(async()=>{throw Error('copy cannot send');});
  h.context.state.composerRequestKind='writing_revision';
  h.context.state.feedbackTarget={kind:'writing',run_id:'run-a',path:['segments',1,'line'],version:2};
  for(const [format,label] of [['photo','图文方案'],['video','脚本'],[null,'创作方案']]) {
    h.context.state.timeline={run:{run_id:'run-a',content_format:format}};
    h.context.syncComposerAction(null);
    assert.equal(h.nodes.get('send').attributes['aria-label'],`按意见直接修改${label}`);
    assert.equal(h.nodes.get('send').dataset.mode,'send');
  }
  h.context.state.timeline={run:{run_id:'another-project',content_format:'photo'}};
  h.context.syncComposerAction(null);
  assert.equal(h.nodes.get('send').attributes['aria-label'],'按意见直接修改创作方案');
  assert.deepEqual(h.context.state.feedbackTarget.path,['segments',1,'line']);
});


test('saved field revision status and review action follow only the exact project format',()=>{
  const h=harness(async()=>{throw Error('display cannot send');});
  const job={job_id:'edit',run_id:'run-a',job_kind:'writing_revision',status:'succeeded',events:[],
    revision_result:{change_status:'saved',version:2}};
  for (const [format,label] of [['photo','图文方案'],['video','脚本'],[null,'创作方案']]) {
    h.context.state.timeline={run:{run_id:'run-a',content_format:format}};
    h.context.showJob(job);
    const text=visibleText(h.nodes.get('job-status'));
    assert.match(text,new RegExp(`${label}新版已保存`));
    assert.match(text,new RegExp(`查看${label}并决定`));
  }
  h.context.state.timeline={run:{run_id:'other-project',content_format:'photo'}};
  assert.equal(h.context.jobPresentation(job).label,'创作方案新版已保存');
});


test('batch progress accepts exact photo status without inferring completion or altering records',()=>{
  const h=harness(async()=>{throw Error('read-only progress');});
  const job={job_kind:'production_batch_generation',status:'running',events:[{type:'status',message:'正在生成第 2/3页图片。'}]};
  const before=JSON.stringify(job);
  assert.equal(h.context.mediaJobProgress(job),'正在生成第 2/3页图片。');
  assert.equal(JSON.stringify(job),before);
  for(const message of ['正在生成第 4/3页图片。','正在生成第 2/99页图片。','3页图片已生成并保存。','模型说正在生成第 2/3页图片。']){
    assert.equal(h.context.mediaJobProgress({...job,events:[{type:'status',message}]}),'');
  }
  assert.equal(h.context.mediaJobProgress({...job,status:'succeeded'}),'');
  assert.equal(h.context.mediaJobProgress({...job,events:[{type:'status',message:'正在生成第 2/3 个首帧。'}]}),'正在生成第 2/3 个首帧。');
});


test('photo subset progress separates source page identity from bounded batch counts',()=>{
  const h=harness(async()=>{throw Error('read-only progress');});
  const job={job_kind:'production_batch_generation',status:'running',events:[]};
  const valid=['正在生成第 02 页图片（本次 1/1 张）。','正在生成第 18 页图片（本次 2/3 张）。',
    '正在生成图文图片（本次 1/1 张）。'];
  for(const message of valid){
    const current={...job,events:[{type:'status',message}]},before=JSON.stringify(current);
    assert.equal(h.context.mediaJobProgress(current),message);
    assert.equal(JSON.stringify(current),before);
    assert.equal(h.context.mediaJobProgress({...current,status:'succeeded'}),'');
  }
  for(const message of ['正在生成第 00 页图片（本次 1/1 张）。','正在生成第 02 页图片（本次 2/1 张）。',
    '正在生成第 02 页图片（本次 1/99 张）。','正在生成第 02 页图片（本次 0/1 张）。',
    '正在生成第 10000 页图片（本次 1/1 张）。','模型说正在生成第 02 页图片（本次 1/1 张）。',
    '本次 1 张图文图片已生成并保存。']){
    assert.equal(h.context.mediaJobProgress({...job,events:[{type:'status',message}]}),'');
  }
  assert.equal(h.context.mediaJobProgress({...job,events:[{type:'tool_call',message:valid[0]}]}),'');
});
