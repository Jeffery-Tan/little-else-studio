import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
const costs=createRequire(import.meta.url)('../../studio/web/project-costs.js');
const row=(extra={})=>({at:'2026-09-19T01:00:00Z',role:'writer',stage:'writing',provider:'deepseek',model:'deepseek-chat',actual_model:'',input_tokens:100,output_tokens:20,usage_status:'known',call_status:'settled',ok:true,estimated_usd:0.002,...extra});

test('mixed known and unknown costs remain a known subtotal, never a complete total',()=>{
  const s=costs.summarize([row(),row({estimated_usd:null,provider:'codex_cli'}),row({input_tokens:null,output_tokens:5,estimated_usd:null,usage_status:'partial',ok:false})]);
  assert.deepEqual(s,{calls:3,input:200,output:45,knownCost:0.002,knownCostCalls:1,unknownCost:2,unknownUsage:1,pending:0,incomplete:1});
  assert.equal(costs.amount(s),'US$ 0.002 + 待计金额');
  assert.equal(costs.amount(costs.summarize([row({estimated_usd:null})])),'金额未知');
});
test('zero is valid measured usage and price, missing values are unknown',()=>{
  const s=costs.summarize([row({input_tokens:0,output_tokens:0,estimated_usd:0})]);
  assert.equal(s.unknownUsage,0);assert.equal(s.unknownCost,0);assert.equal(costs.amount(s),'US$ 0.00');
  const missing=costs.summarize([row({input_tokens:undefined,estimated_usd:NaN}),row({output_tokens:-1,estimated_usd:'0'})]);
  assert.equal(missing.unknownUsage,2);assert.equal(missing.unknownCost,2);
});
test('USD contract never sums prices expressed in other currencies',()=>{
  const s=costs.summarize([row({estimated_usd:null,estimated_cny:9,amount:5,currency:'CNY'}),row()]);
  assert.equal(s.knownCost,0.002);assert.equal(s.unknownCost,1);
});
test('media receipts retain seconds and RMB without fake tokens or USD conversion',()=>{
  const rows=[row({stage:'video_generation',input_tokens:null,output_tokens:null,estimated_usd:null,
    usage_unit:'video_seconds',usage_quantity:5.5,estimated_cny:3.3})];
  assert.equal(costs.summarize(rows).unknownUsage,0);
  const html=costs.render({rows});
  assert.ok(html.includes('CNY 3.30'));
  assert.ok(html.includes('不与美元相加'));
  assert.ok(!html.includes('data-cost-recover'));
});
test('role and model grouping separate providers and use actual model when returned',()=>{
  const rows=[row(),row({role:'director'}),row({provider:'openai_compatible',actual_model:'actual-model'})];
  assert.deepEqual(costs.groups(rows,'role').map(g=>[g.name,g.calls]),[['编剧',2],['创意导演',1]]);
  assert.equal(costs.groups(rows,'model').length,2);
  assert.match(costs.groups(rows,'model')[1].name,/actual-model/);
});
test('pending and incomplete executions retain their measured usage and explain billing',()=>{
  const rows=[row({call_status:'pending',usage_status:'unknown',estimated_usd:null,ok:false}),row({ok:false,stop_reason:'cancelled'}),row({ok:false,call_status:'reconciled'})];
  const s=costs.summarize(rows);assert.equal(s.pending,1);assert.equal(s.incomplete,2);
  assert.equal(s.unknownUsage,1);assert.equal(s.knownCost,0.004);
  const html=costs.render({rows});
  for(const phrase of ['等待用量确认','已停止','已核账 · 执行未完成','仍可能产生费用','data-cost-recover']) assert.ok(html.includes(phrase));
});
test('Codex subscription price is explicitly unknown and distinct from remaining quota',()=>{
  const html=costs.render({rows:[row({provider:'codex_cli',estimated_usd:null})]});
  for(const phrase of ['金额未知','不代表免费','不分摊订阅费用','不是提供方剩余额度']) assert.ok(html.includes(phrase));
  assert.ok(!html.includes('data-cost-recover'));
});
test('UI escapes user-controlled model labels and never renders raw errors endpoints or credentials',()=>{
  const html=costs.render({rows:[row({model:'<script>alert(1)</script>',error:'secret-raw-error',connection_id:'private-connection',request_id:'private-request',pricing_source:'https://key@private',resolution_note:'private-note'}),row({model:'https://token@private.example/v1'}),row({model:'sk-secretsecretsecret'})]});
  assert.ok(html.includes('&lt;script&gt;'));
  for(const forbidden of ['<script>','secret-raw-error','private-connection','private-request','private-note','token@','sk-secretsecret']) assert.ok(!html.includes(forbidden));
  assert.ok(html.includes('<details class="cost-details"><summary>查看'));
});
test('CSV quotes commas newlines quotes and neutralizes formula injection including leading whitespace',()=>{
  assert.equal(costs.csvCell('a,"b"\nc'),'"a,""b""\nc"');
  for(const value of ['=1+1','+SUM(1)','-2+3','@SUM(1)',' \t=1']) assert.ok(costs.csvCell(value).startsWith('"\''));
  const text=costs.csv([row({model:'=HYPERLINK("evil")',estimated_usd:null,input_tokens:null,error:'private-error'})]);
  assert.ok(text.startsWith('\ufeff'));assert.ok(text.includes('"\'=HYPERLINK(""evil"")"'));
  assert.ok(text.includes('"未知","USD"'));assert.ok(!text.includes('private-error'));
});
test('empty project shows no calls without claiming provider free credits',()=>{
  const html=costs.render({rows:[]});
  assert.ok(html.includes('还没有模型调用'));assert.ok(!html.includes('data-cost-export'));
  assert.equal(costs.amount(costs.summarize([])),'暂无调用');
});
test('late ledger response cannot overwrite the panel for another project',async()=>{
  const oldFetch=globalThis.fetch;let resolve;
  globalThis.fetch=()=>new Promise(r=>resolve=r);
  const panel={innerHTML:'',classList:{add(){}},querySelector(){return {};}};
  let current=true;
  try {
    const pending=costs.mount(panel,{runId:'run-a',isCurrent:()=>current});
    current=false;panel.innerHTML='new project';
    resolve({ok:true,json:async()=>({rows:[row()]})});await pending;
    assert.equal(panel.innerHTML,'new project');
  } finally {globalThis.fetch=oldFetch;}
});
test('failed ledger reads preserve an explicit retry without displaying server error bodies',async()=>{
  const oldFetch=globalThis.fetch;
  globalThis.fetch=async()=>({ok:false,text:async()=>'private server error'});
  const elements={};const panel={innerHTML:'',classList:{add(){}},querySelector(s){return elements[s]||=( {});}};
  try {await costs.mount(panel,{runId:'run-a'});assert.ok(panel.innerHTML.includes('data-cost-retry'));assert.ok(!panel.innerHTML.includes('private'));}
  finally {globalThis.fetch=oldFetch;}
});
