import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const context=vm.createContext({URL});
for(const file of ['learning.js','inline-feedback.js'])vm.runInContext(fs.readFileSync(new URL(`../../studio/web/${file}`,import.meta.url),'utf8'),context);
const F=context.StudioInlineFeedback;
const plain=v=>JSON.parse(JSON.stringify(v));
const step={stage:'writing',version:2,has_doc:true,artifact_sha256:'a'.repeat(64)};
const bound=F.binding('project-one',step);
test('binding freezes shown version and refuses missing identity or corrupt artifact',()=>{
  const input={...step};const result=F.binding('project-one',input);input.version=8;input.artifact_sha256='b'.repeat(64);
  assert.equal(result.version,2);assert.equal(result.artifact_sha256,'a'.repeat(64));assert.equal(Object.isFrozen(result),true);
  for(const changed of [{artifact_sha256:''},{version:0},{integrity_ok:false},{has_doc:false}])assert.equal(F.binding('project-one',{...step,...changed}),null);
  assert.equal(F.same(bound,F.binding('project-two',step)),false);
});
test('feedback retains exact quote and records location without changing source wording',()=>{
  const quote='  开头太慢\n只改镜头 2  ';
  const p=F.payload(bound,{quote,scope:'pace',location:'  第 2 个镜头  '});
  assert.equal(p.quote,quote);assert.equal(p.observation,`具体位置：第 2 个镜头\n用户原话：\n${quote}`);
  assert.equal(p.version,2);assert.equal(p.action,'diagnose');assert.equal(p.hypothesis,null);
});
test('form, history and proposed diff escape all user text',()=>{
  const bad='<img src=x onerror="boom()">';
  const proposal={id:bad,type:'feedback',status:'pending',run_id:bound.runId,payload:{...F.payload(bound,{quote:bad,scope:'pace'}),observation:bad},diff:`+${bad}`};
  const html=F.formHtml(bound,{location:bad,quote:bad})+F.historyHtml([proposal])+F.previewHtml(proposal);
  assert.doesNotMatch(html,/<img|onerror="/);assert.match(html,/&lt;img/);assert.match(html,/data-feedback-reviewed/);
  assert.match(html,/不会自动改稿或调用模型/);
});
test('request uses frozen run/version/hash and apply uses reviewed library hash',async()=>{
  const calls=[];const proposal={id:'proposal-123',type:'feedback',status:'pending',run_id:bound.runId,base_library_sha256:'base',payload:F.payload(bound,{quote:'只改开场',scope:'direction'})};
  const actions=F.createActions(async(path,options)=>{calls.push({path,options});return proposal;},bound,()=>true);
  await actions.preview({quote:'只改开场',scope:'direction'});await actions.apply(proposal);
  assert.equal(calls[0].path,'/api/runs/project-one/learning/proposals');assert.deepEqual(JSON.parse(calls[0].options.body),{type:'feedback',payload:plain(proposal.payload)});
  assert.equal(calls[1].path,'/api/runs/project-one/learning/proposals/proposal-123/apply');assert.deepEqual(JSON.parse(calls[1].options.body),{expected_library_sha256:'base'});
});
test('project/version change, mismatched proposal and duplicate in-flight submission never write',async()=>{
  let calls=0,finish;const current=F.createActions(async()=>{calls++;},bound,()=>false);
  await assert.rejects(current.preview({quote:'保留',scope:'pace'}),/项目或稿件版本已变化/);assert.equal(calls,0);
  const actions=F.createActions(()=>{calls++;return new Promise(resolve=>{finish=resolve;});},bound,()=>true);
  await assert.rejects(actions.apply({type:'feedback',run_id:'other',payload:{...bound}}),/不属于/);
  const pending=actions.preview({quote:'保留',scope:'pace'});await assert.rejects(actions.preview({quote:'保留',scope:'pace'}),/正在保存/);assert.equal(calls,1);finish({});await pending;
});
test('history contains only feedback bound to the shown project and artifact',()=>{
  const p={type:'feedback',run_id:bound.runId,payload:F.payload(bound,{quote:'原话',scope:'pace'})};
  const rows=[p,{...p,type:'method'},{...p,run_id:'other'},{...p,payload:{...p.payload,version:3}},{...p,payload:{...p.payload,artifact_sha256:'b'.repeat(64)}}];
  assert.equal(F.relevant(rows,bound).length,1);
});
test('a failed preview releases submission lock and does not mutate the draft',async()=>{
  let count=0;const draft={quote:'  保留原话  ',location:'镜头 2',scope:'pace'};const before={...draft};
  const actions=F.createActions(async()=>{if(++count===1)throw new Error('offline');return {id:'retried'};},bound,()=>true);
  await assert.rejects(actions.preview(draft),/offline/);assert.deepEqual(draft,before);
  assert.equal((await actions.preview(draft)).id,'retried');assert.deepEqual(draft,before);
});
test('feedback preview leads with readable scope and location; machine diff stays collapsed',()=>{
  const p={id:'proposal-123',type:'feedback',status:'pending',run_id:bound.runId,payload:F.payload(bound,{quote:'  开头太慢\n只改镜头 2  ',scope:'pace',location:'第 2 个镜头'}),diff:'+ {"machine_field":true}'};
  const html=F.previewHtml(p);
  const visible=html.slice(0,html.indexOf('<details'));
  assert.match(visible,/新增 1 条反馈/);assert.match(visible,/脚本与文案 · 第 2 版/);assert.match(visible,/第 2 个镜头/);assert.match(visible,/节奏/);assert.match(visible,/先诊断问题/);assert.match(visible,/  开头太慢\n只改镜头 2  /);
  assert.match(visible,/不会修改原稿、改变批准状态或启动制作/);assert.doesNotMatch(visible,/machine_field/);
  assert.doesNotMatch(html,/<details[^>]*\bopen\b/);assert.match(html,/machine_field/);
  assert.match(html,/我已核对以上稿件、具体位置和反馈内容/);
  assert.ok(html.indexOf('data-feedback-reviewed')<html.indexOf('data-learning-action="apply"'));
  const applied=F.previewHtml({...p,status:'applied'});assert.match(applied,/已保存 1 条反馈/);assert.doesNotMatch(applied,/data-feedback-reviewed|data-learning-action="apply"/);
});
test('other feedback observations remain visible without inventing a location',()=>{
  const p={status:'pending',payload:{...F.payload(bound,{quote:'保留开场',scope:'pace'}),observation:'人工备注：只听过前 3 秒'}};
  const html=F.previewHtml(p);assert.match(html,/未限定具体位置/);assert.match(html,/人工备注：只听过前 3 秒/);
});
