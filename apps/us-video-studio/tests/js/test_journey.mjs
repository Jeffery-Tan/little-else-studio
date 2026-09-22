import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
const require=createRequire(import.meta.url);
globalThis.escapeHtml=require('../../studio/web/md.js').escapeHtml;
const {questionCardHtml,journeyGateHtml,journeyStatus}=require('../../studio/web/journey.js');

test('clarification supports recommendation and free text without implicit selection',()=>{
  const html=questionCardHtml({id:'abc',question:'<script>?</script>',options:[{label:'A',description:'one'},{label:'B'}],recommended:0});
  assert.ok(html.includes('推荐'));
  assert.ok(html.includes('data-question-choice'));
  assert.ok(!html.includes('<form'));
  assert.ok(html.includes('下方对话'));
  assert.ok(!html.includes(' checked'));
  assert.ok(!html.includes('<script>'));
  assert.ok(!html.includes('data-act="approve"'));
});
test('formal decision retains the displayed fingerprint and states continuation',()=>{
  const html=journeyGateHtml({stage:'writing',version:3,artifact_sha256:'a'.repeat(64),passed:true},{});
  assert.ok(html.includes('data-version="3"'));
  assert.ok(html.includes('data-sha="'+'a'.repeat(64)+'"'));
  assert.ok(html.includes('确认方案，准备分镜'));
  assert.ok(!html.includes('第 3 版'));
});
test('execution completion cannot replace an unresolved user decision',()=>{
  assert.equal(journeyStatus({execution:{active:false}},{stage:'direction'},null),'选一个你想继续的方向');
  assert.equal(journeyStatus({execution:{active:true}},null,null),'团队正在处理');
});
