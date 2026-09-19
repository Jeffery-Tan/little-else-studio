import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
const require=createRequire(import.meta.url),ui=require('../../studio/web/history.js');
const {renderMarkdown}=require('../../studio/web/md.js');

test('history URLs encode each identifier and reject path traversal',()=>{
  assert.equal(ui.endpoint('demo'),'\/api/runs/demo/history');
  assert.equal(ui.endpoint('demo','稿件 #1?'),'\/api/runs/demo/history/%E7%A8%BF%E4%BB%B6%20%231%3F');
  for(const value of ['','../other','..','.',String.raw`a\b`,null]){assert.throws(()=>ui.endpoint(value));assert.throws(()=>ui.endpoint('demo',value));}
});
test('document list escapes titles identifiers and unknown statuses',()=>{
  const html=ui.listHtml([{id:'x" onclick="evil',title:'<img src=x>',status:'<script>'}],null);
  assert.doesNotMatch(html,/<img|<script|data-document="x" onclick/);
  assert.match(html,/&lt;img/);assert.match(html,/&lt;script/);
});
test('historical draft feedback and approval have unambiguous status',()=>{
  assert.equal(ui.status('draft'),'未采用草稿');assert.equal(ui.status('feedback'),'用户反馈');
  assert.equal(ui.status('paused'),'暂停记录');assert.match(ui.status('approval'),/不用于新生成/);
});
test('overview never treats archive approvals or costs as new authorization',()=>{
  const html=ui.overviewHtml({available:true,summary:'<script>x</script>',historical_cost_note:'历史费用未知 <img>',materials:[{name:'<svg>',label:'旧图 <iframe>'}]});
  assert.match(html,/迁移不会启动生成/);assert.match(html,/历史批准仅供追溯/);assert.match(html,/历史费用未知/);
  assert.doesNotMatch(html,/<script|<img|<svg|<iframe|<button/);
  assert.match(ui.overviewHtml({available:false}),/还没有迁移历史/);
});
test('document markdown is readable while embedded HTML remains text',()=>{
  const html=ui.documentHtml({title:'<svg>',status:'approval',content:'# 原片反馈\n\n**需要改进**\n\n<script>unsafe</script>\n\n[click](javascript:evil)'},renderMarkdown);
  assert.match(html,/原片反馈/);assert.match(html,/<strong>需要改进<\/strong>/);assert.match(html,/不用于新生成/);
  assert.doesNotMatch(html,/<svg|<script|href="javascript:/);
});
test('new document requests invalidate older late responses',()=>{
  const scope=ui.requestScope('one',()=> 'one');const first=scope.next(),second=scope.next();
  assert.equal(scope.valid(first),false);assert.equal(scope.valid(second),true);
});
test('switching projects or closing prevents all late response updates',()=>{
  let run='one';const scope=ui.requestScope('one',()=>run),token=scope.next();
  assert.equal(scope.valid(token),true);run='two';assert.equal(scope.valid(token),false);
  run='one';scope.close();assert.equal(scope.valid(token),false);assert.equal(scope.valid(scope.next()),false);
});
