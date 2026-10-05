import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
const {teamProgress, teamAction, modelWaiting} = createRequire(import.meta.url)('../../studio/web/team-progress.js');

test('current team has four active roles and preserves only an evidenced historical reviewer',()=>{
  const current=teamProgress({job_id:'current',status:'running',events:[],role_sessions:[]});
  assert.deepEqual(current.participants.map(row=>row.role),['orchestrator','director','material','writer']);
  const historical=teamProgress({job_id:'old',status:'succeeded',events:[{type:'agent_tool',role:'reviewer',tool:'read_artifact',ok:true}]});
  assert.equal(historical.participants.find(row=>row.role==='reviewer').status,'本轮已参与');
  const session=teamProgress({job_id:'old',status:'interrupted',events:[],
    role_sessions:[{session_id:'old-review',parent_job_id:'old',role:'reviewer',status:'failed'}]});
  assert.equal(session.participants.find(row=>row.role==='reviewer').status,'本轮已参与');
  const other=teamProgress({job_id:'current',status:'running',events:[],
    role_sessions:[{session_id:'old-review',parent_job_id:'old',role:'reviewer',status:'failed'}]});
  assert.equal(other.participants.some(row=>row.role==='reviewer'),false);
});

test('question answer operation shows the real outcome without exposing submitted arguments',()=>{
  const event={type:'agent_tool',role:'orchestrator',tool:'record_question_answer',
    args:{answer:'private answer',user_input_id:'private identity'},result:'private output',ok:true};
  assert.equal(teamAction(event),'确认本条回答');
  assert.equal(teamAction(event,true),'回答已记录');
  assert.equal(teamAction({...event,ok:false},true),'本条回答未能记录');
  const history=teamProgress({status:'succeeded',events:[event]}).history;
  assert.equal(history[0].text,'回答已记录');
  assert.doesNotMatch(JSON.stringify(history),/private/);
});

test('role handoff follows the newest start event, including return to producer',()=>{
  const events = [{type:'agent_activity',role:'director',phase:'thinking',stage:'direction'}];
  let view = teamProgress({status:'running',events});
  assert.match(view.current,/创意导演 · 构思创意方向/);
  events.push({type:'agent_activity',role:'reviewer',phase:'thinking',stage:'direction'});
  view = teamProgress({status:'running',events});
  assert.equal(view.currentRole,'reviewer');
  assert.equal(view.participants.find(r=>r.role==='director').status,'本轮已参与');
  assert.equal(view.participants.find(r=>r.role==='writer').status,'本轮未参与');
  events.push({type:'agent_activity',role:'orchestrator',phase:'thinking'});
  assert.equal(teamProgress({status:'running',events}).currentRole,'orchestrator');
});

test('finished and interrupted jobs never claim an agent is still working',()=>{
  const events = [{type:'agent_activity',role:'writer',phase:'thinking'}];
  for (const status of ['succeeded','failed','blocked','interrupted','cancelled']) {
    const view = teamProgress({status,events});
    assert.equal(view.currentRole,null);
    assert.ok(view.participants.every(r=>r.status!=='处理中'));
  }
});

test('team record uses the shared project outcome even when execution succeeded',()=>{
  const view = teamProgress({status:'succeeded',events:[],brief:{
    title:'创意方向第 3 版 · 审核未完成',detail:'方案已保留，可只重试这一版审核。'}});
  assert.equal(view.statusTitle,'创意方向第 3 版 · 审核未完成');
  assert.equal(view.summary,'方案已保留，可只重试这一版审核。');
  assert.equal(view.currentRole,null);
});

test('history describes actual work and titles without raw arguments, paths or results',()=>{
  const events = [{type:'agent_tool',role:'orchestrator',tool:'read_project_history',ok:true,
    args:{document_id:'internal-id'},result:JSON.stringify({title:'为什么原片未通过',content:'raw-internal-dump'})},
    {type:'agent_tool',role:'writer',tool:'submit_stage',stage:'writing',ok:true,args:{summary:'完成对镜口播与演示分镜',content:'raw-script'}},
    {type:'agent_tool',role:'writer',tool:'submit_stage',stage:'writing',ok:false,args:{summary:'not saved'}},
    {type:'agent_tool',role:'orchestrator',tool:'unknown_internal_tool',ok:true,result:'raw-secret'}];
  const view = teamProgress({status:'succeeded',events});
  assert.equal(view.outputs.length,1);
  assert.match(view.history[0].text,/已查阅历史资料「为什么原片未通过」/);
  assert.match(view.history[1].text,/脚本与分镜：完成对镜口播与演示分镜/);
  assert.match(view.history[2].text,/未完成/);
  assert.doesNotMatch(JSON.stringify(view),/internal-id|raw-internal|raw-script|not saved|unknown_internal|raw-secret/);
});

test('thinking and reading are not counted as saved creative outputs',()=>{
  const view = teamProgress({status:'succeeded',events:[
    {type:'agent_tool',role:'orchestrator',tool:'read_artifact',args:{stage:'intake'},ok:true},
    {type:'done',text:'建议先比较两个方向'}]});
  assert.equal(view.outputs.length,0);
  assert.match(view.summary,/回复已保存/);
  assert.match(teamAction({tool:'read_artifact',args:{stage:'intake'}}),/阅读方案 · 创作需求/);
});

test('queued jobs and records without role evidence do not invent a working role',()=>{
  const view = teamProgress({status:'queued',events:[]});
  assert.equal(view.currentRole,null);
  assert.ok(view.participants.every(r=>r.status==='本轮未参与'));
  assert.match(teamProgress({status:'running',events:[]}).current,/等待角色进展/);
});

test('public preview updates the live line and settled narration enters history once',()=>{
  const events = [
    {type:'agent_activity',role:'orchestrator',phase:'thinking'},
    {type:'agent_narration_preview',role:'orchestrator',text:'先核对当前方案和你的调整。'},
  ];
  const live = teamProgress({status:'running',events});
  assert.match(live.current,/先核对当前方案/);
  assert.equal(live.history.length,0);
  events.push({type:'agent_narration',role:'orchestrator',text:'先核对当前方案和你的调整。'});
  const settled = teamProgress({status:'running',events});
  assert.equal(settled.history.length,1);
  assert.match(settled.history[0].text,/先核对当前方案/);
});

const waitEvent = (extra = {}) => ({type:'agent_activity',role:'director',phase:'waiting_model',
  elapsed_seconds:90,remaining_seconds:510,transport_state:'waiting',...extra});

test('transport waiting and receiving use only reported statistics without saved outputs',()=>{
  const event = waitEvent({text:'private-reasoning',args:{content:'private-tool-arguments'}});
  const waiting = teamProgress({status:'running',events:[event]});
  assert.match(waiting.current,/创意导演 · 已等待 1分30秒；等待模型回复；单次最多等待 10分钟；本次最多还能等待 8分30秒/);
  assert.equal(waiting.history.length,0);
  assert.equal(waiting.outputs.length,0);
  assert.doesNotMatch(JSON.stringify(waiting),/private-|已保存 \d|已完成|%/);
  const receiving = teamProgress({status:'running',events:[waitEvent({transport_state:'receiving'})]});
  assert.match(receiving.current,/正在接收模型回复/);
  assert.doesNotMatch(receiving.current,/回复已完成|已保存/);
  assert.equal(receiving.history.length,0);
});

test('new thinking, tool, reply and role events clear the previous transport wait',()=>{
  for (const next of [
    {type:'agent_activity',role:'orchestrator',phase:'thinking'},
    {type:'agent_activity',role:'director',phase:'tool',tool:'read_artifact',args:{stage:'direction'}},
    {type:'agent_tool',role:'director',tool:'read_artifact',ok:true},
    {type:'agent_narration_preview',role:'director',text:'接下来核对商品事实。'},
    {type:'agent_narration',role:'director',text:'接下来核对商品事实。'},
    {type:'agent_turn',role:'director'},
  ]) {
    const job = {status:'running',events:[waitEvent(),next]};
    assert.equal(modelWaiting(job),null);
    assert.doesNotMatch(teamProgress(job).current,/已等待|单次最多等待/);
  }
});

test('queued and finished jobs never display a lingering transport wait',()=>{
  for (const status of ['queued','succeeded','failed','blocked','interrupted','cancelled']) {
    const job = {status,events:[waitEvent()]};
    assert.equal(modelWaiting(job),null);
    assert.doesNotMatch(teamProgress(job).current,/已等待|接收模型回复|单次最多等待/);
  }
  const stopping = teamProgress({status:'cancelling',events:[waitEvent()]});
  assert.match(stopping.current,/随后停止；已等待 1分30秒/);
  assert.equal(stopping.currentRole,null);
  assert.ok(stopping.participants.every(row=>row.status!=='处理中'));
});

test('missing and malformed timing cannot invent a wait duration or expose payloads',()=>{
  for (const elapsed of [undefined,null,-1,NaN,Infinity,'90',{secret:'private'}]) {
    const view = modelWaiting({status:'running',events:[waitEvent({elapsed_seconds:elapsed,remaining_seconds:null})]});
    assert.equal(view.text,'等待模型回复');
  }
  const remainingOnly = modelWaiting({status:'running',events:[waitEvent({elapsed_seconds:null,remaining_seconds:60})]});
  assert.equal(remainingOnly.text,'等待模型回复；本次最多还能等待 1分钟');
  const explicitLimit = modelWaiting({status:'running',events:[waitEvent({timeout_seconds:900})]});
  assert.match(explicitLimit.text,/单次最多等待 15分钟/);
  const overflow = modelWaiting({status:'running',events:[waitEvent({elapsed_seconds:Number.MAX_VALUE,remaining_seconds:Number.MAX_VALUE})]});
  assert.doesNotMatch(overflow.text,/Infinity|NaN|单次最多等待/);
});
