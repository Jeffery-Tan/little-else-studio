/* The shipped dialog and runtime with a fake HTTP boundary. No server/model,
 * real settings, approvals, media generation or browser storage is used. */
import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {createRequire} from 'node:module';
import {loadApp,makeDom,makeStorage,webSource} from './dom-fixture.mjs';
const require=createRequire(import.meta.url);
const U=require('../../studio/web/design-upgrade.js');
const RUN='offline-design-upgrade',SHA='a'.repeat(64),PROPOSAL_SHA='b'.repeat(64);

function proposal(){
  const design={schema_version:1,market:'US',audience:'Parents',everyday_context:'Park picnic',
    localization:'Natural US speech',style:'Handheld',product_appearance:'Use verified bag appearance',
    characters:[{id:'parent',label:'Parent',relationship:'Parent and child',appearance:'Dark hair',
      wardrobe:'Blue shirt',performance:'Casual packing'}],scenes:[{id:'park',label:'Park table',
      location:'Local park',layout:'Table under tree',props:'Fruit and bag',lighting:'Daylight',
      local_details:'Ordinary picnic'}],continuity:['Same wardrobe'],assumptions:['Casting is a creative choice']};
  const bindings=[{shot_id:'shot-08',character_ids:'parent',scene_id:'park'}];
  return {id:'design-proposal-1',sha256:PROPOSAL_SHA,current:true,basis:{shot_ids:['shot-08'],artifacts:{
    material_plan:{version:2,sha256:SHA},writing:{version:3,sha256:SHA}}},supplement:{design,bindings,
    material_plan:{visual_design:design},writing:{segments:[{tc:'0:00–0:03',picture:'Original packing action',
      line:'Original line',sound:'Original sound',duration:'3 seconds',character_ids:'parent',scene_id:'park'}]}}};
}
async function settle(){for(let i=0;i<8;i++)await new Promise(resolve=>setImmediate(resolve));}
async function page({preview=null,losePropose=false,loseSave=false,storage=makeStorage(),missingKey=false}={}){
  let modelStarts=0,proposeCalls=0,saveCalls=0,refreshes=0,job=null,currentProposal=preview;
  const timers=new Map(),navigation=[];let nextTimer=0;
  const production={active:false,nonce:'original-save-nonce',design_upgrade_action:{
    label:'补齐制作设计，保留原脚本',expected_context_sha256:SHA},drafts:[]};
  const config={revision:7,roles:{material:{connection:'ds',model:'deepseek-chat'}},connections:[{
    id:'ds',provider:'deepseek',name:'DeepSeek',base_url:'https://example.invalid',has_key:!missingKey}]};
  const result={saved:true,already_saved:false,artifacts:{
    material_plan:{version:3,sha256:'c'.repeat(64)},writing:{version:4,sha256:'d'.repeat(64)}}};
  const dom=makeDom({body:webSource('index.html').match(/<body[^>]*>([\s\S]*?)<\/body>/)[1]});
  const create=dom.document.createElement;
  dom.document.createElement=tag=>{
    const element=create(tag);if(tag==='dialog'){
      element.showModal=()=>{element.open=true;};element.close=()=>{element.open=false;};
    }return element;
  };
  const app=loadApp({dom,storage,runtime:'real',routes:{
    '/production/design-upgrade/propose':()=>{
      proposeCalls++;
      if(!job){modelStarts++;job={run_id:RUN,job_id:'upgrade-job',job_kind:'design_upgrade',
        status:'running',cursor:0,events:[]};}
      if(losePropose&&proposeCalls===1)return Object.assign(new Error('Lost acceptance response'),{status:503});
      return job;
    },
    '/production/design-upgrade/save':()=>{
      saveCalls++;currentProposal=null;production.nonce='fresh-server-nonce';
      if(loseSave&&saveCalls===1)return Object.assign(new Error('Lost save response'),{status:502});
      return {...result,already_saved:saveCalls>1};
    },
    '/production/design-upgrade/proposal':()=>currentProposal,
    '/request-receipts/':({path})=>({received:Boolean(job),kind:'job',
      request_id:decodeURIComponent(path.split('/request-receipts/')[1].split('?')[0]),job_id:job?.job_id}),
    '/jobs/latest':()=>job,'/jobs/upgrade-job':()=>job,
    '/production':()=>production,'/api/connections':()=>config,'/recovery':{},
  },globals:{initStudioNavigation(){},setStudioView(){},renderProjects(){},loadInspiration(){},
    renderProjectProgress(){},renderJobProgress(){},
    setTimeout:fn=>{timers.set(++nextTimer,fn);return nextTimer;},clearTimeout:id=>timers.delete(id)}});
  vm.runInContext(webSource('production.js'),app.ctx);
  vm.runInContext(webSource('design-upgrade.js'),app.ctx);
  await settle();
  // The shared adapter treats null as an error body. These two real endpoints
  // deliberately return JSON null when no proposal or job exists.
  const fetch=app.ctx.fetch;
  app.ctx.fetch=async(url,options)=>{
    const response=await fetch(url,options);
    if(String(url).endsWith('/design-upgrade/proposal'))return {...response,json:async()=>currentProposal};
    if(String(url).endsWith('/jobs/latest'))return {...response,json:async()=>job};
    return response;
  };
  app.ctx.__production=production;app.ctx.__onRefresh=()=>refreshes++;
  app.ctx.__navigate=(...args)=>navigation.push(JSON.parse(JSON.stringify(args)));
  vm.runInContext(`state.runId='${RUN}';state.runs=[{run_id:'${RUN}',kind:'creative'}];
    state.busy=false;state.productionData=__production;state.rail=[];state.canvasTab='board';
    renderProductionAction=()=>{};
    refreshTimeline=async()=>{__onRefresh();state.rail=[{stage:'writing',version:4,artifact_sha256:'${'d'.repeat(64)}'}];};
    showJourneyTab=(...args)=>__navigate(...args);`,app.ctx);
  const dialog=await app.ctx.openDesignUpgrade(RUN,production);
  const body=dialog.dialog.querySelector('[data-upgrade-body]');
  return {...app,production,config,dialog,body,navigation,
    read:expression=>vm.runInContext(expression,app.ctx),
    get modelStarts(){return modelStarts;},get refreshes(){return refreshes;},
    complete:()=>{currentProposal=proposal();job.status='succeeded';},
    setProposal:value=>{currentProposal=value;},
    click:async selector=>{const button=body.querySelector(selector);assert.ok(button,selector);
      button.click();await settle();},
    consent:selector=>{const input=body.querySelector(selector);assert.ok(input,selector);input.checked=true;input.onchange();}};
}

test('proposal preview retains original sound/story/timing and escapes all model text',()=>{
  const preview=proposal();preview.supplement.design.style='<script>hostile()</script>';
  preview.supplement.writing.segments[0].line='<img src=x onerror=hostile()> original';
  const html=U.proposalHtml(preview);
  assert.match(html,/shot-08|0:00–0:03|Original packing action|Original sound|3 seconds/);
  assert.match(html,/&lt;script&gt;hostile\(\)&lt;\/script&gt;/);
  assert.doesNotMatch(html,/<script>|<img src=x|data-generate|data-approve/);
  assert.equal(U.proposalIssue(preview),'');
  preview.supplement.bindings[0].shot_id='other-shot';
  assert.notEqual(U.proposalIssue(preview),'');
  assert.throws(()=>U.savePayload(preview,'nonce'));
  const incomplete=proposal();incomplete.supplement.design.scenes[0]=null;
  assert.notEqual(U.proposalIssue(incomplete),'');
});

test('opening the legacy design dialog is read-only and requires a separate model-call confirmation',async()=>{
  const app=await page();
  assert.match(app.body.textContent,/最多调用素材角色模型一次|实际费用以提供方为准|DeepSeek/);
  assert.equal(app.body.querySelector('[data-upgrade-propose]').disabled,true);
  assert.equal(app.read('state.canvasTab'),'board');
  assert.equal(app.observed.filter(call=>call.method!=='GET').length,0);
  await app.click('[data-upgrade-propose]');
  assert.equal(app.modelStarts,0);
  app.consent('[data-upgrade-model-consent]');await app.click('[data-upgrade-propose]');
  const writes=app.observed.filter(call=>call.method!=='GET');
  assert.equal(writes.length,1);assert.equal(writes[0].path,`/api/runs/${RUN}/production/design-upgrade/propose`);
  assert.deepEqual(Object.keys(writes[0].body).sort(),['consent','expected_connection_revision','expected_context_sha256','request_id']);
  assert.equal(writes[0].body.expected_context_sha256,SHA);assert.equal(writes[0].body.expected_connection_revision,7);
  assert.equal(app.modelStarts,1);assert.match(app.body.textContent,/设计补充正在准备/);
  app.complete();await app.dialog.refresh();
  assert.match(app.body.textContent,/原脚本与逐镜绑定|Original line|Original sound/);
  assert.equal(app.body.querySelector('[data-upgrade-save]').disabled,true);
  assert.equal(app.modelStarts,1);app.dialog.close();assert.equal(app.read('state.canvasTab'),'board');
});

test('lost model acceptance retains the bounded original request and reuses the runtime route on retry',async()=>{
  const app=await page({losePropose:true});
  app.consent('[data-upgrade-model-consent]');await app.click('[data-upgrade-propose]');
  const original=app.observed.find(call=>call.method==='POST');
  assert.match(app.body.textContent,/核实同一准备请求/);
  const pending=JSON.parse(app.storage.getItem(`studio-job-pending:${RUN}`));
  assert.equal(pending.request_id,original.body.request_id);assert.equal(pending.request_kind,'design_upgrade');
  await app.click('[data-upgrade-retry-propose]');
  const writes=app.observed.filter(call=>call.method==='POST');
  assert.equal(writes.length,2);assert.equal(writes[1].path,writes[0].path);
  assert.deepEqual(writes[1].body,writes[0].body);assert.equal(app.modelStarts,1);
  assert.equal(app.storage.getItem(`studio-job-pending:${RUN}`),null);
});

test('preview saving is explicit, version-bound and opens the existing adoption surface without approving',async()=>{
  const app=await page({preview:proposal()});
  assert.equal(app.modelStarts,0);assert.match(app.body.textContent,/新版仍须从原脚本采用入口确认/);
  assert.equal(app.body.querySelector('[data-upgrade-save]').disabled,true);
  app.consent('[data-upgrade-save-consent]');await app.click('[data-upgrade-save]');
  const writes=app.observed.filter(call=>call.method!=='GET');
  assert.equal(writes.length,1);assert.deepEqual(writes[0].body,{proposal_id:'design-proposal-1',
    expected_proposal_sha256:PROPOSAL_SHA,nonce:'original-save-nonce',consent:true});
  assert.match(app.body.textContent,/脚本第 4 版|旧批准没有转移|没有生成媒体/);
  assert.equal(app.refreshes,1);assert.equal(app.navigation.length,0);
  await app.click('[data-upgrade-open-writing]');
  assert.deepEqual(app.navigation,[['plan',{stage:'writing'}]]);
  assert.equal(app.observed.filter(call=>call.method!=='GET').length,1);
});

test('a lost save response survives reopening and replays the exact proposal and original nonce',async()=>{
  const app=await page({preview:proposal(),loseSave:true});
  app.consent('[data-upgrade-save-consent]');await app.click('[data-upgrade-save]');
  const first=app.observed.find(call=>call.method==='POST');
  assert.match(app.body.textContent,/核实同一次保存/);
  app.dialog.close();
  const reopened=await app.ctx.openDesignUpgrade(RUN,app.production);
  const body=reopened.dialog.querySelector('[data-upgrade-body]');
  assert.match(body.textContent,/核实同一次保存/);
  assert.ok(!body.querySelector('[data-upgrade-propose]'));
  body.querySelector('[data-upgrade-retry-save]').click();await settle();
  const writes=app.observed.filter(call=>call.method==='POST');
  assert.equal(writes.length,2);assert.deepEqual(writes[1].body,first.body);
  assert.equal(app.modelStarts,0);assert.match(body.textContent,/设计补充已保存为新版本/);
  assert.equal(app.storage.getItem(`studio-design-upgrade-save:${RUN}`),null);
});

test('partial saving keeps the same reviewed proposal and never offers a new model call',async()=>{
  const preview=proposal();preview.partial_save=true;
  const app=await page({preview});
  assert.match(app.body.textContent,/已有部分版本保存|继续保存这份补充方案/);
  assert.ok(!app.body.querySelector('[data-upgrade-propose]'));
  app.consent('[data-upgrade-save-consent]');await app.click('[data-upgrade-save]');
  assert.equal(app.modelStarts,0);
  assert.equal(app.observed.find(call=>call.method==='POST').body.proposal_id,preview.id);
});

test('stale or incomplete proposals and missing model credentials never offer an executable action',async()=>{
  const stale=proposal();stale.current=false;
  const app=await page({preview:stale});
  assert.match(app.body.textContent,/仅供查看|不能沿用保存/);
  assert.ok(!app.body.querySelector('[data-upgrade-save]'));app.dialog.close();
  const missing=await page({missingKey:true});
  assert.match(missing.body.textContent,/补齐素材角色的模型连接与密钥/);
  assert.ok(!missing.body.querySelector('[data-upgrade-propose]'));
  assert.equal(missing.observed.filter(call=>call.method!=='GET').length,0);
});

test('leaving the project prevents old dialog actions and never changes the canvas layout',async()=>{
  const app=await page({preview:proposal()});
  app.consent('[data-upgrade-save-consent]');
  app.read("state.runId='other-project'");
  await app.click('[data-upgrade-save]');
  assert.equal(app.observed.filter(call=>call.method!=='GET').length,0);
  app.dialog.close();assert.equal(app.read('state.canvasTab'),'board');
});

test('a completed preparation with an unreadable proposal offers retrieval rather than another model call',async()=>{
  const app=await page();
  app.consent('[data-upgrade-model-consent]');await app.click('[data-upgrade-propose]');
  app.complete();app.setProposal(null);await app.dialog.refresh();
  assert.match(app.body.textContent,/准备任务已完成|重新读取已有补充方案/);
  assert.ok(!app.body.querySelector('[data-upgrade-propose]'));
  await app.click('[data-upgrade-reload]');assert.equal(app.modelStarts,1);
});
