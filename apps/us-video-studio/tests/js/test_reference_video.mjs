import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {makeDom} from './dom-fixture.mjs';
const require=createRequire(import.meta.url);
const ui=require('../../studio/web/reference-video.js');
const images=require('../../studio/web/image-analysis.js');

test('new reference entry uses conversation instead of a classification form',()=>{
  const html=ui.form({connection:{}});
  assert.match(html,/对话/);
  assert.doesNotMatch(html,/<form|<select|<input|<textarea/);
});

test('saved video resume pins source and intent while failed download permits file fallback',()=>{
  const failed=ui.form({connection:{}},{intent:'recreate',source_url:'https://vm.tiktok.com/test/'});
  assert.match(failed,/type="file"[^>]*>/);
  assert.doesNotMatch(failed,/type="file"[^>]*disabled/);
  assert.match(failed,/继续参考复刻/);
  assert.match(failed,/不选文件会重新获取原链接/);
  assert.match(failed,/原视频（可选）/);
  assert.doesNotMatch(failed,/select|textarea|name="consent"/);
  const downloaded=ui.form({connection:{}},{sha256:'abc',name:'same.mp4'});
  assert.match(downloaded,/type="file"[^>]*disabled/);
  assert.match(downloaded,/原片已保存，声画分析尚未完成/);
  assert.match(ui.form({connection:{}},{sha256:'abc',name:'same.mp4',analysis_id:'saved'}),/已完成的分析会复用/);
});

test('reference text is escaped and completed tasks have no resume action',()=>{
  const html=ui.tasks({runId:'run',tasks:[{reference_id:'id',intent:'analyze',status:'completed',
    title:'<img src=x onerror=alert(1)>',name:'video.mp4',report:'<script>alert(1)</script>'}]});
  assert.doesNotMatch(html,/<img|<script|data-reference-resume/);
  assert.match(html,/&lt;script&gt;/);
  assert.match(html,/video controls/);
  assert.match(html,/模型观察未经人工核对/);
});

test('automatic reference observation never claims human confirmation',()=>{
  const c={status:'accepted',adoption_source_kind:'requested_model_reference',analysis_id:'id',
    findings:['scene'],selected_findings:[0],limitations:[],observed_at:'2026-09-21'};
  assert.match(images.candidateCard(c),/未经人工核对/);
  const html=images.review(c);
  assert.match(html,/勾选不表示人工确认/);
  assert.doesNotMatch(html,/你此前采用|已保存你的选择/);
});

test('reference canvas ties timed observations to playback and separates interpretation',()=>{
  const task={reference_id:'reference-1',name:'clip.mp4',status:'completed',
    candidate:{analysis_id:'analysis-1',candidate_sha256:'a'.repeat(64),
      timeline:[{start_seconds:0,end_seconds:2.5,visible:'<script>画面',audible:'spoken line',relationship:'matches'}],
      reference_methods:[{supports:[0],mechanism:'show the product first',non_transferable:'original offer',test_change:'use verified facts'}],
      limitations:['time is estimated']}};
  const html=ui.breakdownHtml(task,'run-1');
  assert.match(html,/data-reference-seek="0"/);
  assert.match(html,/片段 1 · 0.0s–2.5s/);
  assert.match(html,/模型观察未经人工核对|观察未经人工核对/);
  assert.match(html,/不能直接照搬/);
  assert.doesNotMatch(html,/<script>/);
  const {document}=makeDom();
  const host=document.createElement('div');host.innerHTML=html;
  const video=host.querySelector('video');video.play=()=>Promise.resolve();
  ui.mountBreakdowns(host,{runId:'run-1',tasks:[task]});
  host.querySelector('[data-reference-seek]').onclick();
  assert.equal(video.currentTime,0);
  assert.doesNotMatch(ui.breakdownHtml(task,'run-1',{adaptation:false}),/本品提示词/);
});

test('prompt edits save only against the displayed analysis and prompt version',async()=>{
  const task={reference_id:'reference-1',name:'clip.mp4',status:'completed',
    candidate:{analysis_id:'analysis-1',candidate_sha256:'a'.repeat(64),timeline:[]},
    prompt:{text:'old prompt',sha256:'b'.repeat(64),revision:7,actor:'model'}};
  const {document}=makeDom();
  const host=document.createElement('div');host.innerHTML=ui.breakdownHtml(task,'run-1');
  const field=host.querySelector('[data-reference-prompt]');field.value='new prompt';
  const calls=[];
  ui.mountBreakdowns(host,{runId:'run-1',tasks:[task],request:async(url,options)=>{
    calls.push({url,payload:JSON.parse(options.body)});
    return {prompt:{text:'new prompt',sha256:'c'.repeat(64),actor:'user'}};
  }});
  await host.querySelector('[data-reference-save]').onclick();
  assert.equal(calls.length,1);
  assert.equal(calls[0].payload.expected_prompt_sha256,'b'.repeat(64));
  assert.equal(calls[0].payload.expected_prompt_revision,7);
  assert.equal(calls[0].payload.analysis_id,'analysis-1');
  assert.equal(task.prompt.text,'new prompt');
});

test('an uncertain prompt save retries the same request identity',async()=>{
  const task={reference_id:'reference-1',name:'clip.mp4',status:'completed',
    candidate:{analysis_id:'analysis-1',candidate_sha256:'a'.repeat(64),timeline:[]}};
  const {document}=makeDom(),host=document.createElement('div');
  host.innerHTML=ui.breakdownHtml(task,'run-1');
  host.querySelector('[data-reference-prompt]').value='new prompt';
  const ids=[];
  ui.mountBreakdowns(host,{runId:'run-1',tasks:[task],request:async(_url,options)=>{
    ids.push(JSON.parse(options.body).request_id);
    if(ids.length===1)throw Error('response lost');
    return {prompt:{text:'new prompt',sha256:'c'.repeat(64)}};
  }});
  const save=host.querySelector('[data-reference-save]');
  await save.onclick();await save.onclick();
  assert.equal(ids.length,2);
  assert.equal(ids[0],ids[1]);
  assert.equal(task.prompt.text,'new prompt');
});

test('reference cards distinguish completed steps from the owning job and only resume safe incomplete work',()=>{
  const failed={reference_id:'failed-ref',intent:'analyze',status:'downloaded',name:'clip.mp4',
    execution_status:'failed',failure_reason:'观察失败 <script>raw error</script>'};
  let html=ui.tasks({runId:'run',tasks:[failed]});
  assert.match(html,/原片已保存，声画分析尚未完成/);
  assert.match(html,/本轮任务失败/);
  assert.match(html,/观察失败 &lt;script&gt;/);
  assert.doesNotMatch(html,/<script>/);
  assert.match(html,/data-reference-resume/);
  for(const execution_status of ['queued','running','cancelling','unknown']){
    html=ui.tasks({runId:'run',tasks:[{...failed,execution_status}]});
    assert.doesNotMatch(html,/data-reference-resume|data-reference-continue/);
    if(execution_status==='unknown')assert.match(html,/结果待核实/);
  }
  assert.doesNotMatch(ui.tasks({runId:'run',busy:true,tasks:[failed]}),/data-reference-resume/);
  assert.match(ui.tasks({runId:'run',tasks:[{reference_id:'historic',status:'started'}]}),/历史任务状态未记录/);
  for(const status of ['analyzed','completed']){
    html=ui.tasks({runId:'run',tasks:[{...failed,status,analysis_id:'saved',report:'Saved report',candidate:{}}]});
    assert.match(html,/后续任务失败/);
    assert.match(html,/data-reference-continue/);
    assert.doesNotMatch(html,/data-reference-resume/,'a saved analysis must never be re-analyzed');
  }
});

test('conversation recovery binds the original job and receipt without treating observation scope as the task',()=>{
  const task={reference_id:'reference-A',intent:'analyze',status:'downloaded',execution_status:'failed',
    instruction:'Only the opening mechanism',recovery:{reference_id:'reference-A',parent_job_id:'original-job',
      source_user_input_id:'original-receipt',interpretation_id:'frozen-contract',deliverable:'writing',
      original_text:'Write the complete product plan',reference_scope:'Only the opening mechanism',
      allowed:true,requires_observation:true}};
  const payload=ui.recoveryPayload(task,'Continue the original task; keep this real new instruction');
  assert.deepEqual(payload.reference_recovery,{reference_id:'reference-A',parent_job_id:'original-job',
    source_user_input_id:'original-receipt',interpretation_id:'frozen-contract'});
  assert.equal(payload.text,'Continue the original task; keep this real new instruction');
  assert.equal(payload.reference_consent,true);
  assert.doesNotMatch(JSON.stringify(payload),/Only the opening|deliverable|reference_scope/);
  const html=ui.form({connection:{}},task);
  assert.match(html,/继续完整方案/);
  assert.match(html,/Write the complete product plan/);
  assert.doesNotMatch(html,/继续视频分析/);
});

test('cached conversation recovery has no new observation admission and unknown original tasks have no restore controls',()=>{
  const task={reference_id:'reference-A',intent:'analyze',status:'completed',execution_status:'blocked',candidate:{},
    recovery:{allowed:true,requires_observation:false,reference_id:'reference-A',parent_job_id:'original-job',
      source_user_input_id:'original-receipt',interpretation_id:'frozen-contract',deliverable:'reference_prompt'}};
  assert.equal(ui.recoveryPayload(task,'Continue').reference_consent,false);
  assert.match(ui.tasks({runId:'run',tasks:[task]}),/本品提示词/);
  task.recovery={allowed:false,reason:'Original receipt cannot be verified'};
  assert.throws(()=>ui.recoveryPayload(task,'Continue'),/Original receipt cannot be verified/);
  assert.doesNotMatch(ui.tasks({runId:'run',tasks:[task]}),/data-reference-resume|data-reference-continue/);
});

test('only explicit original-file fallback adds a byte-bound reference attachment to recovery',()=>{
  const task={recovery:{allowed:true,requires_observation:true,reference_id:'reference-A',parent_job_id:'original-job',
    source_user_input_id:'original-receipt',interpretation_id:'frozen-contract'}};
  const payload=ui.recoveryPayload(task,'Continue original task',{name:'original.mp4',sha256:'a'.repeat(64)});
  assert.deepEqual(payload.attachments,[{name:'original.mp4',purpose:'reference'}]);
  assert.equal(payload.reference_recovery.name,'original.mp4');
  assert.equal(payload.reference_recovery.expected_sha256,'a'.repeat(64));
});
