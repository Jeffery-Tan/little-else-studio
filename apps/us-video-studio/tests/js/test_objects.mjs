import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import vm from 'node:vm';
import {makeDom,loadApp,webSource} from './dom-fixture.mjs';
const require=createRequire(import.meta.url),Objects=require('../../studio/web/objects.js');
const SHA='a'.repeat(64),OTHER='b'.repeat(64);
const draft={draft_id:'input-1',draft_sha256:SHA,kind:'image',source_version:2,source_sha256:OTHER,
  shot_id:'backend-page-2',is_current:true,source_ready:true,model:'codex-image',prompt:'Do not reconstruct this compiled prompt'};

test('reference edits preserve original description and exact shot while saving a replacement',()=>{
  const refs=[{name:'product.png',sha256:SHA,purpose:'product'}],before=JSON.stringify(draft);
  const payload=Objects.referencePayload(draft,refs,'request-1');
  assert.equal(payload.prompt,null);assert.deepEqual(payload.preserve_prompt_from,{draft_id:'input-1',expected_sha256:SHA});
  assert.equal(payload.supersedes,'input-1');assert.equal(payload.shot_id,'backend-page-2');
  assert.deepEqual(payload.references,refs);assert.equal(JSON.stringify(draft),before);
  assert.equal(Object.hasOwn(payload,'consent'),false);assert.equal(Object.hasOwn(payload,'nonce'),false);
});

test('reference editing rejects stale inputs and video reference removal without guessing replacements',()=>{
  for(const update of [{is_current:false},{source_ready:false},{draft_sha256:'wrong'},{shot_id:null}])
    assert.throws(()=>Objects.referencePayload({...draft,...update},[],'r1'));
  assert.throws(()=>Objects.referencePayload({...draft,kind:'video'},[],'r1'),/首帧/);
  assert.throws(()=>Objects.referencePayload({...draft,kind:'video'},[{name:'one.png',sha256:SHA,purpose:'product'}],'r1'),/首帧/);
  assert.throws(()=>Objects.referencePayload(draft,[{name:'one.png',sha256:SHA,purpose:'style'}],'r1'),/用途/);
});

test('image regions bind normalized original-image coordinates and reject clicks',()=>{
  assert.deepEqual(Objects.normalizedRegion({x:110,y:60},{x:210,y:160},{left:10,top:10,width:400,height:200}),
    {x:.25,y:.25,width:.25,height:.5});
  assert.deepEqual(Objects.normalizedRegion({x:410,y:210},{x:-10,y:-20},{left:10,top:10,width:400,height:200}),
    {x:0,y:0,width:1,height:1});
  assert.equal(Objects.normalizedRegion({x:2,y:2},{x:2,y:2},{left:0,top:0,width:100,height:100}),null);
});

test('a reference save receipt must preserve the exact source, shot and selected references',()=>{
  const payload=Objects.referencePayload(draft,[{name:'exact.png',sha256:OTHER,purpose:'product'}],'saved-input');
  const saved={...payload,draft_id:'saved-input',draft_sha256:SHA,references:[{...payload.references[0],design_id:null}]};
  assert.equal(Objects.savedReferenceMatches(saved,payload),true);
  for(const update of [{source_version:9},{shot_id:'another-shot'},{supersedes:'other-input'},
    {references:[{...payload.references[0],sha256:SHA}]},{draft_sha256:'invalid'}])
    assert.equal(Objects.savedReferenceMatches({...saved,...update},payload),false);
});

async function page(routes={}) {
  const dom=makeDom(),create=dom.document.createElement.bind(dom.document);
  dom.document.createElement=tag=>{const element=create(tag);element.ownerDocument=dom.document;
    if(tag==='dialog'){element.showModal=()=>{element.open=true;};element.close=()=>{element.open=false;};}
    Object.defineProperty(element,'isConnected',{get:()=>Boolean(element.parentNode)});return element;};
  dom.document.createElementNS=(_ns,tag)=>dom.document.createElement(tag);
  const app=loadApp({dom,routes,globals:{initStudioNavigation(){},setStudioView(){},renderProjects(){},loadInspiration(){}}});
  await new Promise(resolve=>setImmediate(resolve)); vm.runInContext('state.runId="objects-run"',app.ctx);vm.runInContext(webSource('objects.js'),app.ctx);
  return {...app,read:code=>vm.runInContext(code,app.ctx)};
}
const target={kind:'artifact',run_id:'objects-run',stage:'writing',version:2,artifact_sha256:SHA};
const settle=async()=>{for(let i=0;i<5;i++)await new Promise(resolve=>setImmediate(resolve));};

test('saving a persisted annotation makes only its own POST and never starts work',async()=>{
  const app=await page({'/annotations':({method,body})=>method==='POST'?{annotation_id:'annotation-1',...body,state:'pending'}:
    {run_id:'objects-run',snapshot_sha256:SHA,items:[]}});
  app.ctx.target=target;await app.read('StudioObjects.openAnnotations({runId:"objects-run",target})');
  const form=app.document.querySelector('.annotation-form');form.querySelector('textarea').value='第二页文字太小，请只改这一页';
  await form.onsubmit({preventDefault(){}});await settle();
  const posts=app.observed.filter(row=>row.method==='POST');assert.equal(posts.length,1);
  assert.equal(posts[0].path,'/api/runs/objects-run/annotations');assert.equal(posts[0].body.text,'第二页文字太小，请只改这一页');
  assert.deepEqual(posts[0].body.target,target);assert.match(app.document.querySelector('.object-status').textContent,/尚未启动修改/);
});

test('an ambiguous annotation save freezes its original request across manual retry',async()=>{
  let count=0;
  const app=await page({'/annotations':({method,body})=>method==='GET'?{run_id:'objects-run',snapshot_sha256:SHA,items:[]}:
    ++count===1?new Error('network unavailable'):{annotation_id:'annotation-1',...body,state:'pending'}});
  app.ctx.target=target;await app.read('StudioObjects.openAnnotations({runId:"objects-run",target})');
  const form=app.document.querySelector('.annotation-form'),input=form.querySelector('textarea');input.value='准确原话';
  await form.onsubmit({preventDefault(){}});assert.equal(input.disabled,true);
  await form.onsubmit({preventDefault(){}});
  const posts=app.observed.filter(row=>row.method==='POST');assert.equal(posts.length,2);assert.deepEqual(posts[1].body,posts[0].body);
  assert.equal(input.disabled,false);assert.equal(input.value,'');
});

test('a definite annotation refusal preserves editable text without silently retrying',async()=>{
  const refusal=new Error('target changed');refusal.status=409;
  const app=await page({'/annotations':({method})=>method==='POST'?refusal:{run_id:'objects-run',snapshot_sha256:SHA,items:[]}});
  app.ctx.target=target;await app.read('StudioObjects.openAnnotations({runId:"objects-run",target})');
  const form=app.document.querySelector('.annotation-form'),input=form.querySelector('textarea');input.value='我的原话';
  await form.onsubmit({preventDefault(){}});
  assert.equal(input.value,'我的原话');assert.equal(input.disabled,false);
  assert.equal(app.observed.filter(row=>row.method==='POST').length,1);
});

test('selected annotation execution uses one exact batch request and server job receipt',async()=>{
  const item={annotation_id:'annotation-1',target,text:'只调整第二页',state:'pending'};
  const app=await page({'/annotations/execute':{run_id:'objects-run',job_id:'annotation-job',status:'queued'},
    '/annotations':{run_id:'objects-run',snapshot_sha256:SHA,items:[item]}});
  app.ctx.__trackAcceptedJob=(runId,job)=>{app.ctx.accepted={runId,job};return true;};
  app.read('refreshTimeline=async()=>true');
  await app.read('StudioObjects.openAnnotations({runId:"objects-run"})');
  const checkbox=app.document.querySelector('.annotation-row input');checkbox.checked=true;checkbox.onchange();
  const execute=[...app.document.querySelectorAll('button')].find(node=>node.textContent==='按所选批注修改');
  await execute.onclick();await settle();
  const posts=app.observed.filter(row=>row.method==='POST');assert.equal(posts.length,1);
  assert.deepEqual(posts[0].body.annotation_ids,['annotation-1']);assert.equal(posts[0].body.expected_snapshot_sha256,SHA);
  assert.equal(app.ctx.accepted.job.job_id,'annotation-job');assert.equal(Object.hasOwn(posts[0].body,'text'),false);
});

test('real object reference editor writes one new version and never calls generation',async()=>{
  const material={name:'reference.png',kind:'image',sha256:OTHER,url:'/api/runs/objects-run/materials/reference.png'};
  const projection={run_id:'objects-run',snapshot_sha256:SHA,layout:{revision:0,view:{zoom:1,x:0,y:0},positions:{},groups:[],notes:[]},edges:[],nodes:[
    {id:'source',title:'准确参考图',kind:'material',status:'available',target:{kind:'material',run_id:'objects-run',name:material.name,sha256:OTHER},data:{material}},
    {id:'draft',title:'第二页制作输入',kind:'production_draft',status:'current',target:{kind:'production_draft',run_id:'objects-run',draft_id:draft.draft_id,draft_sha256:SHA},allowed_actions:['edit_references'],data:{draft}},
  ]};
  const app=await page({'/canvas':projection,'/production/drafts':({body})=>({...body,draft_id:body.request_id,draft_sha256:SHA})});
  app.read('refreshTimeline=async()=>true');await app.read('StudioObjects.openCanvas("objects-run")');
  const handle=[...app.document.querySelectorAll('.object-node-handle')].find(node=>node.textContent==='第二页制作输入');handle.onclick();
  const form=app.document.querySelector('.object-reference-form');form.querySelector('[data-reference-source]').value='source';form.querySelector('[aria-label="参考用途"]').value='product';
  [...form.querySelectorAll('button')].find(node=>node.textContent==='添加参考连接').onclick();
  await form.onsubmit({preventDefault(){}});await settle();
  const posts=app.observed.filter(row=>row.method==='POST');assert.equal(posts.length,1);assert.equal(posts[0].path,'/api/runs/objects-run/production/drafts');
  assert.equal(posts[0].body.supersedes,'input-1');assert.deepEqual(posts[0].body.references,[{name:'reference.png',sha256:OTHER,purpose:'product'}]);
  assert.equal(posts[0].body.prompt,null);assert.equal(posts[0].body.preserve_prompt_from.expected_sha256,SHA);
});

test('layout acknowledgement checks revision and complete placement without relying on object key order',()=>{
  const payload={expected_revision:4,view:{x:5,y:6,zoom:1},positions:{node:{x:10,y:20}},groups:[],notes:[]};
  const saved={...payload,revision:5,view:{zoom:1,x:5,y:6}};
  assert.equal(Objects.savedLayoutMatches(saved,payload),true);
  assert.equal(Objects.savedLayoutMatches({...saved,revision:4},payload),false);
  assert.equal(Objects.savedLayoutMatches({...saved,notes:[{id:'missing-note'}]},payload),false);
});

function zoomCanvas(){
  return {run_id:'objects-run',snapshot_sha256:SHA,layout:{revision:4,view:{zoom:.5,x:11,y:22},positions:{},groups:[],notes:[]},edges:[],nodes:[
    {id:'historical-writing',title:'旧方案',kind:'artifact',current:false,status:'history',target:{...target,version:1},data:{}}]};
}

test('canvas zoom controls restore and save the existing view, fit content, and show history only once',async()=>{
  const app=await page({'/canvas/layout':({body})=>({...body,revision:body.expected_revision+1}),'/canvas':zoomCanvas()});
  await app.read('StudioObjects.openCanvas("objects-run")');
  const world=app.document.querySelector('.object-graph'),viewport=app.document.querySelector('.object-graph-viewport');
  const plus=app.document.querySelector('[aria-label="放大对象画布"]'),reset=app.document.querySelector('.object-zoom-controls').children[1];
  assert.equal(world.style.zoom,'0.5');assert.equal(reset.textContent,'50%');
  assert.equal(app.document.querySelector('.object-node-status').textContent,'历史版本');
  plus.onclick();assert.equal(world.style.zoom,'0.6');reset.onclick();assert.equal(world.style.zoom,'1');
  viewport.clientWidth=700;viewport.clientHeight=500;
  [...app.document.querySelectorAll('button')].find(button=>button.textContent==='适应画布').onclick();
  assert.ok(parseFloat(world.style.width)*Number(world.style.zoom)<=700-31);
  assert.ok(parseFloat(world.style.height)*Number(world.style.zoom)<=500-31);
  const zoom=Number(world.style.zoom),save=[...app.document.querySelectorAll('button')].find(button=>button.textContent==='保存布局');
  await save.onclick();const post=app.observed.find(row=>row.method==='POST');
  assert.equal(post.path,'/api/runs/objects-run/canvas/layout');assert.deepEqual(post.body.view,{zoom,x:11,y:22});
  assert.equal(post.body.expected_revision,4);assert.match(app.document.querySelector('.object-status').textContent,/布局已保存/);
});

test('scaled pointer movement uses world coordinates, keyboard movement stays available, and zoom respects project limits',async()=>{
  const app=await page({'/canvas/layout':({body})=>({...body,revision:body.expected_revision+1}),'/canvas':zoomCanvas()});
  await app.read('StudioObjects.openCanvas("objects-run")');
  const handle=app.document.querySelector('.object-node-handle');
  handle.onpointerdown({button:0,clientX:100,clientY:100,pointerId:1});
  handle.onpointermove({clientX:120,clientY:110});handle.onpointerup({pointerId:1});
  handle.onkeydown({key:'ArrowRight',preventDefault(){}});
  const minus=app.document.querySelector('[aria-label="缩小对象画布"]');minus.onclick();minus.onclick();
  assert.equal(app.document.querySelector('.object-graph').style.zoom,'0.35');assert.equal(minus.disabled,true);
  await [...app.document.querySelectorAll('button')].find(button=>button.textContent==='保存布局').onclick();
  const post=app.observed.find(row=>row.method==='POST');assert.deepEqual(post.body.positions['historical-writing'],{x:82,y:52});
  assert.equal(post.body.view.zoom,.35);
});

const mediaTarget={kind:'media',run_id:'objects-run',attempt_id:'actual-attempt',output_sha256:SHA};
test('result inspection requires separate consent and uses the exact media and connection revision',async()=>{
  const app=await page({'/result-inspections':({method,body})=>method==='GET'?{revision:9,reports:[],image_connection:{name:'Fixture',suggested_model:'offline'},video_connection:null}:
    {run_id:'objects-run',job_id:'inspection-1',status:'queued'}});
  app.ctx.target=mediaTarget;app.ctx.__trackAcceptedJob=(_runId,job)=>{app.ctx.accepted=job;};app.read('refreshTimeline=async()=>true');
  await app.read('StudioObjects.openInspection({runId:"objects-run",target,mediaKind:"image"})');
  const action=[...app.document.querySelectorAll('button')].find(button=>button.textContent==='确认并检查结果'),consent=app.document.querySelector('.inspection-consent input');
  assert.equal(action.disabled,true);await action.onclick();assert.equal(app.observed.filter(row=>row.method==='POST').length,0);
  consent.checked=true;consent.onchange();assert.equal(action.disabled,false);await action.onclick();
  const posts=app.observed.filter(row=>row.method==='POST');assert.equal(posts.length,1);assert.deepEqual(posts[0].body.target,mediaTarget);
  assert.equal(posts[0].body.expected_revision,9);assert.equal(posts[0].body.consent,true);assert.equal(app.ctx.accepted.job_id,'inspection-1');
});

test('missing understanding connection cannot trigger an inspection and unknown findings remain visible',async()=>{
  const app=await page({'/result-inspections':{revision:1,image_connection:null,reports:[{target:mediaTarget,status:'unknown',findings:[{area:'product',status:'unknown',evidence:'对照图片未提供，无法确认商品形态'}],limitations:['仅有这一张实际结果']} ]}});
  app.ctx.target=mediaTarget;await app.read('StudioObjects.openInspection({runId:"objects-run",target})');
  const consent=app.document.querySelector('.inspection-consent input');consent.checked=true;consent.onchange();
  const action=[...app.document.querySelectorAll('button')].find(button=>button.textContent==='确认并检查结果');assert.equal(action.disabled,true);await action.onclick();
  assert.equal(app.observed.filter(row=>row.method==='POST').length,0);assert.match(app.document.querySelector('.object-inspection').textContent,/尚无法判断/);
  assert.match(app.document.querySelector('.object-dialog-body').textContent,/没有可用的理解模型/);
});

test('unknown inspection acceptance preserves the original consent and request identity on retry',async()=>{
  let count=0;const app=await page({'/result-inspections':({method})=>method==='GET'?{revision:7,reports:[],image_connection:{name:'Fixture'}}:
    ++count===1?new Error('connection interrupted'):{job_id:'original-inspection',status:'queued'}});
  app.ctx.target=mediaTarget;app.read('refreshTimeline=async()=>true');await app.read('StudioObjects.openInspection({runId:"objects-run",target})');
  const consent=app.document.querySelector('.inspection-consent input');consent.checked=true;consent.onchange();
  const action=[...app.document.querySelectorAll('button')].find(button=>button.textContent==='确认并检查结果');await action.onclick();
  assert.equal(consent.disabled,true);assert.equal(action.textContent,'核对原检查请求');await action.onclick();
  const posts=app.observed.filter(row=>row.method==='POST');assert.equal(posts.length,2);assert.deepEqual(posts[0].body,posts[1].body);
});

test('selected export sends exact targets and downloads only an application zip response',async()=>{
  const app=await page(),requests=[],downloads=[];
  app.ctx.fetch=async(path,options)=>{requests.push({path,body:JSON.parse(options.body)});return {ok:true,headers:{get:()=> 'application/zip'},blob:async()=>({size:100})};};
  app.ctx.URL={createObjectURL:blob=>{downloads.push(blob);return 'blob:exact';},revokeObjectURL:()=>{}};app.ctx.setTimeout=()=>0;app.ctx.targets=[mediaTarget,target];
  await app.read('StudioObjects.exportObjects({runId:"objects-run",targets})');assert.equal(requests.length,1);
  assert.deepEqual(requests[0].body.targets,[mediaTarget,target]);assert.equal(downloads.length,1);
  app.ctx.fetch=async()=>({ok:true,headers:{get:()=> 'application/json'},blob:async()=>({})});
  await app.read('StudioObjects.exportObjects({runId:"objects-run",targets})');assert.equal(downloads.length,1);
});

test('queue reads every waiting task but cancelling targets only the selected project job',async()=>{
  const app=await page({'/api/jobs/queue':{items:[{run_id:'other-run',job_id:'other',status:'queued',text:'另一个项目',position:1},
    {run_id:'objects-run',job_id:'owned',status:'queued',text:'当前项目文字',position:2}]},'/jobs/owned/cancel':{job_id:'owned',status:'cancelled'}});
  await app.read('StudioObjects.openQueue()');
  const cancel=[...app.document.querySelectorAll('button')].filter(button=>button.textContent==='取消这条排队任务');assert.equal(cancel.length,1);
  assert.match(app.document.querySelector('.object-dialog-body').textContent,/第 2 位/);await cancel[0].onclick();
  const posts=app.observed.filter(row=>row.method==='POST');assert.equal(posts.length,1);assert.equal(posts[0].path,'/api/runs/objects-run/jobs/owned/cancel');
});

test('task evidence keeps unknown usage and cost explicit and performs only a read',async()=>{
  const app=await page({'/tasks/task-1/usage':{calls:2,input_tokens:10,output_tokens:null,usage_complete:false,estimated_usd:null,cost_complete:false,input_bytes:{system:100},call_details:[],budget:{max_calls:5}}});
  app.read(webSource('evidence.js'));await app.read('StudioEvidence.openUsage({runId:"objects-run",jobId:"task-1"})');
  const body=app.document.querySelector('.object-dialog-body');assert.match(body.textContent,/尚有未确认项/);assert.match(body.textContent,/尚有未知项/);
  assert.match(body.textContent,/尚未记录/);assert.equal(app.observed.filter(row=>row.method==='POST').length,0);
});

test('method comparison retains insufficient evidence and cannot infer quality or costs',async()=>{
  const app=await page({'/learning/method-results':{results:[{usage_request_id:'before',method:{method_id:'method-a'},artifact:{stage:'writing',version:1,sha256:SHA},observations:[]},
    {usage_request_id:'after',method:{method_id:'method-b'},artifact:{stage:'writing',version:2,sha256:OTHER},observations:[]}]},
    '/learning/method-comparison?baseline=before&candidate=after':{same_task_inputs_verified:false,same_inputs_verified:false,limits:['旧任务缺少原始角色输入']}});
  app.read(webSource('evidence.js'));await app.read('StudioEvidence.openMethodResults("objects-run")');
  const compare=[...app.document.querySelectorAll('button')].find(button=>button.textContent==='比较两份记录');await compare.onclick();
  assert.match(app.document.querySelector('.method-comparison-output').textContent,/尚未证实相同/);
  assert.match(app.document.querySelector('.method-comparison-output').textContent,/因果改善未成立/);
  assert.equal(app.observed.filter(row=>row.method==='POST').length,0);
});
