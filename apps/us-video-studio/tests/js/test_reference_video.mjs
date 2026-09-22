import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
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
