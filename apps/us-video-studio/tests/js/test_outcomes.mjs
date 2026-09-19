import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
const require=createRequire(import.meta.url);
const ui=require('../../studio/web/outcomes.js');
const observation={snapshot_id:'one',window_start:'2026-09-17T00:00:00Z',window_end:'2026-09-18T00:00:00Z',observed_at:'2026-09-18T01:00:00Z',traffic:'unknown',views:0,likes:null,clicks:null,orders:null,revenue_usd:null,spend_usd:0,source:'User source',notes:'No causal conclusion'};
test('unknown metrics remain distinct from zero including money',()=>{
  assert.equal(ui.numeric(''),null);assert.equal(ui.numeric('  '),null);assert.equal(ui.numeric('0'),0);
  assert.equal(ui.metric(null,true),'未提供');assert.equal(ui.metric(0,true),'$0.00');
  const html=ui.snapshotHtml(observation);assert.match(html,/未提供/);assert.match(html,/\$0\.00/);assert.match(html,/用户提供，未自动核验/);
});
test('invalid numeric and date inputs are rejected before a request',()=>{
  for(const value of ['NaN','Infinity','-1'])assert.throws(()=>ui.numeric(value));
  assert.throws(()=>ui.iso(''));assert.throws(()=>ui.iso('invalid'));
  assert.throws(()=>ui.submission('snapshot',{...observation,views:'1.5'}, {snapshots:[]},'one'));
});
test('publication submission binds the exact integrity checked historical version',()=>{
  const values={title:'A',url:'https://www.tiktok.com/@fixture/video/1',version:'2',published_at:'2026-09-18T00:00:00Z',differences:'Changed opening'};
  const body=ui.submission('publication',values,[{version:2,artifact_sha256:'a'.repeat(64),integrity_ok:true}],'request');
  assert.equal(body.version,2);assert.equal(body.artifact_sha256,'a'.repeat(64));
  assert.throws(()=>ui.submission('publication',values,[{version:2,integrity_ok:false}],'request'));
});
test('snapshot requests retain known zero and bind the latest observed record',()=>{
  const body=ui.submission('snapshot',{...observation,likes:'',views:'0'}, {snapshots:[{snapshot_id:'previous'}]},'retry');
  assert.equal(body.expected_latest_id,'previous');assert.equal(body.views,0);assert.equal(body.likes,null);
  assert.equal(body.request_id,'retry');
});
test('untrusted publication text and URLs never become active markup',()=>{
  const html=ui.publicationHtml({title:'<img src=x onerror=alert(1)>',url:'javascript:alert(1)',version:1,differences:'<script>bad</script>',snapshots:[{...observation,source:'<img src=x>',notes:'<script>x</script>'}]});
  assert.doesNotMatch(html,/<script>|<img|href="javascript:/);assert.match(html,/&lt;img/);
  assert.equal(ui.safeUrl('https://user:password@example.com'),'');
});
test('reflection prepares a bounded observation, with source and no false trend claims',()=>{
  const text=ui.reflection({title:'A',url:'https://example.com/a',version:1,differences:'Unknown execution',snapshots:[observation]});
  assert.match(text,/播放：0/);assert.match(text,/点赞：未提供/);assert.match(text,/来源：User source/);assert.match(text,/不将单次结果推广/);assert.match(text,/记录：one/);
});
test('historical snapshots stay visible without automatic growth or ranking',()=>{
  const html=ui.publicationHtml({title:'A',url:'https://example.com/a',version:1,snapshots:[observation,{...observation,snapshot_id:'two',views:200}]});
  assert.match(html,/此前的 1 次记录/);assert.match(html,/不自动计算趋势/);assert.doesNotMatch(html,/增长率|排名/);
});
test('uncertain saves restore the exact frozen request across dialog reopening',()=>{
  const draft={values:{views:'10'},payload:{request_id:'same-request',expected_latest_id:'old',views:10}};
  const stored=JSON.parse(JSON.stringify(draft));
  const result=ui.restored(stored,[],{snapshots:[{snapshot_id:'old'}]});
  assert.deepEqual(result.payload,draft.payload);assert.equal(result.exists,false);
  assert.equal(ui.restored(stored,[],{snapshots:[{snapshot_id:'same-request'}]}).exists,true);
  assert.equal(ui.restored(stored,[{publication_id:'same-request'}],null).exists,true);
});
test('broken source records remain readable but cannot be taken into creative context',()=>{
  const p={title:'A',version:1,source_integrity_ok:false,snapshots:[observation]};
  assert.match(ui.publicationHtml(p),/关联稿件暂时无法核对/);
  assert.match(ui.publicationHtml(p),/data-outcome-reflect="[^"]*" disabled/);
  assert.throws(()=>ui.reflection(p),/无法核对/);
});
