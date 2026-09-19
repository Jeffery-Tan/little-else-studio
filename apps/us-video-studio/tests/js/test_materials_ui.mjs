import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
const require = createRequire(import.meta.url);
const ui = require('../../studio/web/materials.js');
const item = {name:'sample.jpg',kind:'image',url:'/api/runs/project-a/materials/sample.jpg',sha256:'abc',status_label:'已上传 · 待补充观察',observations:[]};

test('uploaded materials never claim automatic understanding',()=>{
  const html=ui.render({materials:[item],notice:'当前模型未直接读取图片或视频'});
  assert.match(html,/已上传 · 待补充观察/);
  assert.match(html,/查看并记录观察/);
  assert.match(html,/当前模型未直接读取/);
  assert.doesNotMatch(html,/分析完成/);
});
test('external media and injected fields cannot create active content',()=>{
  const html=ui.render({materials:[{...item,name:'<script>x</script>',url:'https://evil.test/a.jpg',observations:[{coverage:'<img onerror=x>',findings:['<script>bad</script>'],limitations:[]}]}]});
  assert.doesNotMatch(html,/<script>|<img onerror|evil.test/);
  assert.match(html,/&lt;script&gt;/);
});
test('form only offers applicable observation modes and makes source clear',()=>{
  assert.doesNotMatch(ui.observationForm(item),/value="audiovisual_review"/);
  assert.match(ui.observationForm({...item,kind:'video'}),/value="transcript_only"/);
  assert.match(ui.observationForm(item),/保存人工观察/);
  assert.match(ui.observationForm(item),/不作为商品事实/);
});
test('submission binds the rendered file and manifest revision',()=>{
  const values={purpose:'reference',level:'image_observed',coverage:' whole image ',findings:'one\n\ntwo',limitations:' Unknown back '};
  const result=ui.formPayload({elements:{namedItem:name=>({value:values[name]})}},item,{expected_manifest_sha256:'revision'});
  assert.equal(result.expected_sha256,'abc');
  assert.equal(result.expected_manifest_sha256,'revision');
  assert.deepEqual(result.findings,['one','two']);
  assert.deepEqual(result.limitations,['Unknown back']);
});
