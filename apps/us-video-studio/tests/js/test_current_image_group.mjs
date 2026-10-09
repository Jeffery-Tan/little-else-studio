/* Synthetic server projections for a four-shot group after shot 2 is revised.
 * No providers, real project records or generation requests are used.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {createRequire} from 'node:module';
import {loadApp,makeDom} from './dom-fixture.mjs';
const require=createRequire(import.meta.url),ui=require('../../studio/web/production.js');
const SHA='a'.repeat(64),RUN='demo';

function scene() {
  const output=(name)=>({name,sha256:SHA,url:`/api/runs/${RUN}/materials/${name}`});
  const drafts=Array.from({length:4},(_,index)=>({kind:'image',draft_id:`input-${index+1}`,
    shot_id:`shot-${index+1}`,draft_sha256:SHA,source_version:4,source_sha256:SHA,
    is_current:true,source_ready:true,can_generate:true,prompt:`Current frame ${index+1}`,
    references:[],attempts:index===1?[]:[{attempt_id:`current-${index+1}`,status:'succeeded',
      output_available:true,output:output(`frame-${index+1}.png`)}]}));
  drafts[1].supersedes='old-shot-2';
  const old={...drafts[1],draft_id:'old-shot-2',is_current:false,superseded_by:drafts[1].draft_id,
    prompt:'Retained old shot 2',attempts:[{attempt_id:'old-success',status:'succeeded',
      output_available:true,output:output('old-frame-2.png')}]};
  const shots=drafts.map((draft,index)=>({ordinal:index+1,shot_id:draft.shot_id,draft_id:draft.draft_id,
    source_ready:true,stage:index===1?'input_ready':'completed',stage_label:index===1?'输入已准备':'已完成'}));
  return {drafts:[drafts[0],old,...drafts.slice(1)],sources:[{ready:true,version:4,artifact_sha256:SHA}],
    active:false,unresolved_attempts:[],capabilities:{image:{available:true}},
    materials:[...drafts.flatMap(d=>d.attempts.map(a=>({...a.output,kind:'image',approved:false}))),
      {...old.attempts[0].output,kind:'image',approved:false}],
    targets:{image:{complete_list:true,source_version:4,source_sha256:SHA,
      shots:shots.map(({ordinal,shot_id})=>({ordinal,shot_id}))}},
    shots:{image:{complete_list:true,total:4,shots}}};
}

function page(data,format='video') {
  const ids=['production-action','chat-input','send','toast','conversation-body','conversation-count',
    'conversation','conversation-toggle','conversation-caret','draft','job-status','rail','stage',
    'journey-decision','run-select'];
  const p=loadApp({runtime:'real',dom:makeDom({ids,body:''}),globals:{StudioProduction:ui,
    setTimeout:()=>0,clearTimeout:()=>{},setInterval:()=>0,clearInterval:()=>{}}});
  p.ctx.data=data;p.ctx.format=format;
  p.read=(code)=>vm.runInContext(code,p.ctx);
  p.read('state.runId="demo";state.busy=false;state.currentJob=null;state.productionData=data;'+
    'state.canvasTab="board";state.creatorView={format,tab:"storyboard"};state.timeline={pending:null,interaction:null};renderProductionAction()');
  return p;
}

test('a revised input has four current plans and three current results while its predecessor stays readable history',()=>{
  const data=scene(),before=JSON.stringify(data),group=ui.imageGroupSummary(data,RUN);
  assert.equal(group.total,4);
  assert.deepEqual(group.drafts.map(d=>d.draft_id),['input-1','input-2','input-3','input-4']);
  assert.equal(group.generated,3);
  assert.equal(ui.batchGenerationDrafts(data,RUN).length,1);
  const html=ui.canvasHtml(data,'image',RUN,[]);
  assert.match(html,/分镜首帧（4）/);
  assert.match(html,/已生成 3 张/);
  assert.match(html,/已被替换的历史输入 · 1/);
  assert.match(html,/Retained old shot 2/);
  assert.equal(JSON.stringify(data),before);
});

for(const format of ['video','photo'])test(`real ${format} board conversation counts only the current group without changing confirmation scope`,()=>{
  const data=scene(),p=page(data,format),host=p.document.getElementById('production-action');
  assert.match(host.textContent,new RegExp(`当前 4 张${format==='photo'?'图片':'首帧'}方案`));
  assert.match(host.textContent,/当前已生成 3 张，待生成 1 张/);
  assert.doesNotMatch(host.textContent,/5 张|已生成 4 张/);
  assert.match(host.textContent,/批注这张生成图/);
  assert.match(host.textContent,/核对并一次生成 1 张/);
  assert.equal(p.observed.filter(row=>row.method!=='GET').length,0);
});

test('server target membership prevents unrelated current records from adding plans or results',()=>{
  const data=scene(),other={...data.drafts[0],draft_id:'not-this-script',shot_id:'foreign-shot'};
  data.drafts.push(other);
  const group=ui.imageGroupSummary(data,RUN);
  assert.equal(group.total,4);assert.equal(group.drafts.length,4);assert.equal(group.generated,3);
});

test('the target count is supplied by the server even when more target shots lack inputs',()=>{
  const data=scene();
  data.shots.image.total=6;
  data.shots.image.shots.push({ordinal:5,shot_id:'shot-5',source_ready:false},
    {ordinal:6,shot_id:'shot-6',source_ready:false});
  data.targets.image.shots.push({ordinal:5,shot_id:'shot-5'},{ordinal:6,shot_id:'shot-6'});
  const group=ui.imageGroupSummary(data,RUN);
  assert.equal(group.total,6);assert.equal(group.drafts.length,4);
  assert.match(ui.canvasHtml(data,'image',RUN,[]),/分镜首帧（6）/);
});

test('without a readable target list history never invents a target count',()=>{
  const data=scene();delete data.targets;delete data.shots;
  const group=ui.imageGroupSummary(data,RUN);
  assert.equal(group.total,null);assert.equal(group.drafts.length,4);assert.equal(group.generated,3);
  const html=ui.canvasHtml(data,'image',RUN,[]);
  assert.match(html,/分镜首帧输入（4）/);assert.doesNotMatch(html,/分镜首帧输入（5）|目标 4/);
  assert.match(html,/已被替换的历史输入 · 1/);
});

test('explicitly unavailable output is not included in current generated counts',()=>{
  const data=scene();data.drafts[0].attempts[0].output_available=false;
  assert.equal(ui.imageGroupSummary(data,RUN).generated,2);
});

test('an unresolved original attempt continues to outrank the current-group summary after replacement',()=>{
  const data=scene(),old=data.drafts.find(d=>d.is_current===false);
  old.attempts.push({attempt_id:'old-unknown',status:'unknown'});
  data.unresolved_attempts=[{kind:'image',draft_id:old.draft_id,shot_id:old.shot_id,
    ordinal:2,attempt_id:'old-unknown',status:'unknown'}];
  const p=page(data),host=p.document.getElementById('production-action');
  assert.match(host.textContent,/结果待确认|核实这次图片任务/);
  assert.doesNotMatch(host.textContent,/当前 4 张|核对并一次生成/);
  assert.equal(p.observed.filter(row=>row.method!=='GET').length,0);
});


function intentFor(data,ids) {
  return {kind:'generate',drafts:ids.map(id=>{
    const draft=data.drafts.find(row=>row.draft_id===id);
    return {draft_id:id,expected_sha256:draft.draft_sha256};
  })};
}

test('the exact one-image confirmation retains shot 2 and never calls it the whole set or shot 1',()=>{
  const data=scene(),intent=intentFor(data,['input-2']),before=JSON.stringify({data,intent});
  const html=ui.batchConfirmationHtml(intent,data,RUN);
  assert.match(html,/<strong>生成这张首帧<\/strong>/);
  assert.match(html,/<strong>首帧 02<\/strong>/);
  assert.match(html,/确认并生成 1 张/);
  assert.match(html,/输入：本次所选 1 面准确分镜/);
  assert.doesNotMatch(html,/整组|首帧 01/);
  assert.equal(JSON.stringify({data,intent}),before);
  const p=page(data);p.ctx.intent={...intent,runId:RUN,request_id:'same-confirmation'};
  p.read('saveBatchIntent(intent);renderProductionAction()');
  const host=p.document.getElementById('production-action');
  assert.match(host.textContent,/生成这张首帧.*首帧 02/);
  assert.equal(p.observed.filter(row=>row.method!=='GET').length,0);
});

test('a selected subset keeps server shot ordinals even when the selection order differs',()=>{
  const data=scene();
  const html=ui.batchConfirmationHtml(intentFor(data,['input-4','input-2']),data,RUN);
  assert.match(html,/<strong>一次生成所选 2 张首帧<\/strong>/);
  assert.ok(html.indexOf('<strong>首帧 04</strong>')<html.indexOf('<strong>首帧 02</strong>'));
  assert.doesNotMatch(html,/整组|首帧 01/);
});

test('the older targets-only projection supplies its unique accurate ordinal without parsing draft IDs',()=>{
  const data=scene();delete data.shots;
  data.drafts.find(d=>d.draft_id==='input-2').draft_id='opaque-named-99';
  const html=ui.batchConfirmationHtml(intentFor(data,['opaque-named-99']),data,RUN);
  assert.match(html,/<strong>首帧 02<\/strong>/);
  assert.doesNotMatch(html,/首帧 99|首帧 01|镜头号未提供/);
});

for(const problem of ['no_projection','duplicate_coverage','coverage_omission','duplicate_targets','source_mismatch'])
  test(`a ${problem} ordinal is left unknown instead of numbered from the selected array or ID`,()=>{
    const data=scene();
    if(problem==='no_projection'){delete data.shots;delete data.targets;}
    if(problem==='duplicate_coverage')data.shots.image.shots.push({...data.shots.image.shots[1],ordinal:7});
    if(problem==='coverage_omission')data.shots.image.shots.splice(1,1);
    if(problem==='duplicate_targets'){delete data.shots;data.targets.image.shots.push({...data.targets.image.shots[1],ordinal:7});}
    if(problem==='source_mismatch'){delete data.shots;data.targets.image.source_sha256='b'.repeat(64);}
    const html=ui.batchConfirmationHtml(intentFor(data,['input-2']),data,RUN);
    assert.match(html,/所选首帧（镜头号未提供）/);
    assert.doesNotMatch(html,/首帧 01|首帧 02|首帧 07/);
    assert.match(html,/确认并生成 1 张/);
  });


test('a current source-unready shot remains one of four plans but cannot join generation',()=>{
  const data=scene(),changed=data.drafts.find(d=>d.draft_id==='input-2');
  changed.source_ready=false;
  changed.design_issue='本镜连续性输入须按当前规则重新保存。';
  const row=data.shots.image.shots[1];
  row.source_ready=false;row.stage='stale';row.stage_label='输入需更新';
  const group=ui.imageGroupSummary(data,RUN);
  assert.equal(group.total,4);assert.equal(group.drafts.length,4);
  assert.equal(group.readyDrafts.length,3);assert.equal(group.needsUpdate,1);
  assert.equal(group.generated,3);assert.equal(group.pending,1);
  assert.equal(ui.batchGenerationDrafts(data,RUN).length,0);
  const p=page(data),host=p.document.getElementById('production-action');
  assert.match(host.textContent,/目标 4 镜.*当前 4 张首帧方案/);
  assert.match(host.textContent,/已准备输入 3 张，需更新 1 张；当前已生成 3 张，待生成 1 张/);
  assert.doesNotMatch(host.textContent,/当前 3 张首帧方案|核对并一次生成/);
  const html=ui.canvasHtml(data,'image',RUN,[]);
  assert.match(html,/分镜首帧（4）/);
  assert.match(html,/需更新输入 1 面/);
  assert.match(html,/data-draft-id="input-2"/);
  assert.match(html,/本镜连续性输入须按当前规则重新保存/);
  assert.match(html,/已被替换的历史输入 · 1/);
  assert.equal(p.observed.filter(row=>row.method!=='GET').length,0);
  // The input is still current even if an older service omitted a detail string.
  delete changed.design_issue;
  assert.equal(ui.imageGroupSummary(data,RUN).drafts.length,4);
});


for(const format of ['photo','video'])test(`zero generated ${format} pages direct feedback to prepared inputs, not nonexistent images`,()=>{
  const data=scene();
  for(const draft of data.drafts)draft.attempts=[];
  for(const shot of data.shots.image.shots){shot.stage='input_ready';shot.stage_label='输入已准备';}
  const before=JSON.stringify(data),p=page(data,format);
  const host=p.document.getElementById('production-action');
  assert.match(host.textContent,/当前已生成 0 张，待生成 4 张/);
  assert.match(host.textContent,/尚未生成图片。要修改制作输入，直接在这里说明页码和修改要求/);
  assert.doesNotMatch(host.textContent,/批注这张生成图|打开画布图片/);
  assert.equal(JSON.stringify(data),before);
  assert.equal(p.observed.filter(row=>row.method!=='GET').length,0);
});


for(const ids of [['input-2'],['input-4','input-2']])test(`photo confirmation reads exact page copy and server ordinals for ${ids.join(',')}`,()=>{
  const data=scene();
  for(const draft of data.drafts){
    draft.shot_binding={line:`Exact page copy ${draft.shot_id}\n<keep & check>`};
    draft.design_binding={character_ids:[],scene_id:'kitchen',material_plan:{version:2,sha256:SHA},writing:{version:4,sha256:SHA}};
  }
  const intent=intentFor(data,ids),before=JSON.stringify({data,intent});
  const html=ui.batchConfirmationHtml(intent,data,RUN,'photo');
  assert.match(html,/来源：图文方案第 4 版/);
  assert.match(html,new RegExp(`输入：本次所选 ${ids.length} 页准确页面`));
  assert.match(html,/<strong>本页图内文案<\/strong>/);
  assert.match(html,/Exact page copy shot-2\n&lt;keep &amp; check&gt;/);
  assert.match(html,/<strong>第 02 页<\/strong>/);
  assert.doesNotMatch(html,/首帧|分镜|本镜|逐镜|脚本版本|第 01 页/);
  assert.match(html,/本页采用的共享设计/);
  assert.match(html,/图文方案版本 4/);
  assert.equal(JSON.stringify({data,intent}),before);
});

test('photo confirmation cannot invent missing exact adopted copy',()=>{
  const data=scene(),intent=intentFor(data,['input-2']);
  assert.equal(ui.batchConfirmationHtml(intent,data,RUN,'photo'),'');
  const uncertain={...intent,uncertain:true};
  assert.match(ui.batchConfirmationHtml(uncertain,data,RUN,'photo'),/原请求的图内文案尚未读取到；请核实同一请求/);
  data.drafts.find(d=>d.draft_id==='input-2').shot_binding={line:''};
  assert.match(ui.batchConfirmationHtml(intent,data,RUN,'photo'),/本页无图内文案/);
});
