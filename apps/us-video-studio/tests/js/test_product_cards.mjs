import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
const {preview,binding}=createRequire(import.meta.url)('../../studio/web/product-cards.js');
test('card preview separates facts sources and unknowns and escapes user strings',()=>{
  const html=preview({name:'<script>',facts:'<b>Fact</b>',evidence:'"source"',limitations:'Unknown capacity',source_limits:['No claim'],source_title:'Prior run',source_label:'v1'});
  assert.ok(!html.includes('<script>')&&!html.includes('<b>'));
  for(const phrase of ['商品信息','证据来源','未知与限制','Unknown capacity','No claim','不会复制'])assert.ok(html.includes(phrase));
});
test('new brief selection carries exact source card binding only',()=>{
  assert.deepEqual(binding({source_run_id:'one',card_id:'card',sha256:'f'.repeat(64),facts:'not in request'}),{source_run_id:'one',card_id:'card',sha256:'f'.repeat(64)});
  assert.equal(binding(null),null);
});
