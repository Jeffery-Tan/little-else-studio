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
  assert.match(downloaded,/已完成的分析会复用/);
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
