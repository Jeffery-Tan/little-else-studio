import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
const {teamProgress, teamAction} = createRequire(import.meta.url)('../../studio/web/team-progress.js');

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
