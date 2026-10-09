/* Real shipped media controllers and watcher; offline HTTP, clock and DOM.
 * The adapter has no layout or provider connection. A pending projection is
 * deliberately left unresolved to exercise independent task observation. */
import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {El, makeDom, makeStorage, webSource} from './dom-fixture.mjs';

const RUN='media-project', SHA='a'.repeat(64);
const flush=()=>new Promise(resolve=>setImmediate(resolve));
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};};
const progress=(i,total=4)=>({seq:10+i,type:'status',message:`正在生成第 ${i}/${total} 个首帧。`,
  args:{private_reasoning:'not-for-the-interface'}});
const job=(extra={})=>({run_id:RUN,job_id:'media-job',job_kind:'production_batch_generation',
  status:'running',cursor:11,events:[progress(1)],...extra});
function data(){
  const drafts=Array.from({length:4},(_,index)=>({draft_id:`frame-${index+1}`,draft_sha256:SHA,
    shot_id:`shot-${index+1}`,kind:'image',model:'codex-image',prompt:`Offline frame ${index+1}`,
    source_version:1,source_sha256:SHA,source_ready:true,is_current:true,references:[],attempts:[],
    estimate:{amount:null,currency:'CNY',notice:'Offline rate unknown'}}));
  return {nonce:'offline-nonce',drafts,materials:[],active:false,unresolved_attempts:[],
    sources:[{version:1,artifact_sha256:SHA,ready:true}],
    capabilities:{image:{available:true,connection_revision:3},video:{available:false,models:[]}},
    shots:{image:{total:4,complete_list:true,shots:drafts.map((d,i)=>({ordinal:i+1,
      shot_id:d.shot_id,draft_id:d.draft_id,draft_sha256:SHA,source_ready:true,stage:'input_ready'}))}}};
}

if(!Object.getOwnPropertyDescriptor(El.prototype,'isConnected'))
  Object.defineProperty(El.prototype,'isConnected',{get(){return Boolean(this.parentNode);}});
if(!Object.getOwnPropertyDescriptor(El.prototype,'elements'))
  Object.defineProperty(El.prototype,'elements',{get(){return {namedItem:name=>this.querySelector(`[name="${name}"]`)};}});

function page(handler){
  const dom=makeDom({body:webSource('index.html').match(/<body[^>]*>([\s\S]*?)<\/body>/)[1]});
  const {document}=dom;
  for(const node of document.querySelectorAll('[id]'))document.__ids.set(node.getAttribute('id'),node);
  const requests=[],timers=new Map(),streams=[],notices=[];let nextTimer=0;
  class Source {constructor(url){this.url=url;streams.push(this);}close(){this.closed=true;}}
  const state={runId:RUN,currentJob:null,busy:false,rail:[],timeline:{},productionData:data(),
    canvasTab:'storyboard',creatorView:{format:'photo',tab:'storyboard'},runs:[{run_id:RUN,kind:'creative'}]};
  const ctx={document,state,console,EventSource:Source,crypto:{randomUUID:()=>`offline-${requests.length}`},
    sessionStorage:makeStorage(),setTimeout:fn=>{const id=++nextTimer;timers.set(id,fn);return id;},
    clearTimeout:id=>timers.delete(id),$:(id)=>document.getElementById(id),
    box:(tag='div',className='',text='')=>{const node=document.createElement(tag);node.className=className||'';node.textContent=text||'';return node;},
    toast:(text,error)=>notices.push({text,error}),renderProjectProgress(){},configureJourneyTabs(){},
    projectReadStatus(){},showJourneyTab(){},setProjectFocus(){},
    api:async(path,options)=>{const request={path,method:options?.method||'GET',body:options?.body?JSON.parse(options.body):null};
      requests.push(request);return handler(request);},
  };
  ctx.refreshTimeline=()=>ctx.api(`/api/runs/${state.runId}/timeline`);
  ctx.window=ctx;ctx.globalThis=ctx;vm.createContext(ctx);
  for(const name of ['production.js','team-progress.js','runtime.js','journey.js'])
    vm.runInContext(webSource(name),ctx,{filename:name});
  // The stage decision itself is outside these media task tests.
  ctx.renderJourneyDecision=()=>{};
  return {ctx,state,document,requests,timers,streams,notices,storage:ctx.sessionStorage};
}
function intent(p){
  p.ctx.saveBatchIntent({runId:RUN,kind:'generate',request_id:'approved-batch',connection_revision:3,
    drafts:p.state.productionData.drafts.map(d=>({draft_id:d.draft_id,expected_sha256:d.draft_sha256}))});
}
function visible(p,id){return p.document.getElementById(id).textContent;}

test('inline POST installs the exact receipt and stop without latest or a slow work projection',async()=>{
  const submit=deferred(),projection=deferred();
  const p=page(r=>r.method==='POST'?submit.promise:projection.promise);intent(p);
  const pending=p.ctx.submitProductionBatch();
  assert.equal(p.state.currentJob,null);
  assert.equal(p.streams.length,0);
  assert.doesNotMatch(visible(p,'production-action'),/正在生成第|停止本次制作/);
  submit.resolve(job());await pending;await flush();
  assert.equal(p.state.currentJob.job_id,'media-job');
  assert.equal(p.document.getElementById('send').dataset.mode,'stop');
  assert.equal(p.document.getElementById('send').disabled,false);
  assert.match(visible(p,'production-action'),/正在生成第 1\/4 个首帧/);
  assert.match(visible(p,'job-status'),/正在生成第 1\/4 个首帧/);
  assert.doesNotMatch(visible(p,'production-action'),/not-for-the-interface/);
  assert.equal(p.ctx.currentBatchIntent(),null);
  assert.equal(p.streams.length,1);assert.equal(p.timers.size,1);
  assert.equal(p.requests.filter(r=>r.method==='POST').length,1);
  assert.equal(p.requests.filter(r=>r.path.endsWith('/jobs/latest')).length,0);
});

test('one durable receipt opens one observer and cannot roll back an advanced cursor',async()=>{
  const p=page(()=>new Promise(()=>{}));
  assert.equal(p.ctx.trackAcceptedJob(RUN,job({cursor:12,events:[progress(2)]})),true);await flush();
  p.ctx.trackAcceptedJob(RUN,job());await flush();
  assert.equal(p.streams.length,1);assert.equal(p.timers.size,1);assert.equal(p.requests.length,0);
  assert.equal(p.state.currentJob.cursor,12);
  assert.match(visible(p,'production-action'),/第 2\/4/);
});

test('equal and cursorless bare receipts cannot discard an existing batch event history',async()=>{
  const p=page(()=>new Promise(()=>{}));
  p.ctx.trackAcceptedJob(RUN,job({cursor:12,events:[progress(1),progress(2)]}));await flush();
  for(const receipt of [{job_id:'media-job',status:'running'},
    {job_id:'media-job',status:'running',cursor:12,events:[]}])p.ctx.trackAcceptedJob(RUN,receipt);
  await flush();
  assert.equal(p.state.currentJob.cursor,12);assert.equal(p.state.currentJob.events.length,2);
  assert.equal(p.state.currentJob.job_kind,'production_batch_generation');
  assert.match(visible(p,'production-action'),/第 2\/4/);
  assert.equal(p.streams.length,1);assert.equal(p.timers.size,1);assert.equal(p.requests.length,0);
});

test('an old running receipt cannot reopen a terminal Job even when both receipts are bare',async()=>{
  const p=page(()=>new Promise(()=>{}));
  p.ctx.trackAcceptedJob(RUN,{job_id:'media-job',job_kind:'production_batch_generation',status:'succeeded'});
  await flush();
  for(const receipt of [job({cursor:0}),job({cursor:999})])p.ctx.trackAcceptedJob(RUN,receipt);
  assert.equal(p.state.currentJob.status,'succeeded');assert.equal(p.streams.length,0);
  assert.equal(p.document.getElementById('send').dataset.mode,'send');
  assert.doesNotMatch(visible(p,'production-action'),/正在生成第|停止本次制作/);
});

test('a newer complete receipt preserves known history and closes the original observer once',async()=>{
  const p=page(r=>r.path.endsWith('/recovery')?{revision:'done',can_continue:true}:new Promise(()=>{}));
  p.ctx.trackAcceptedJob(RUN,job({cursor:12,events:[progress(1),progress(2)]}));await flush();
  const completed={job_id:'media-job',status:'succeeded',cursor:20,events:[]};
  p.ctx.trackAcceptedJob(RUN,completed);await flush();
  const reads=p.requests.length;p.ctx.trackAcceptedJob(RUN,completed);await flush();
  assert.equal(p.state.currentJob.status,'succeeded');assert.equal(p.state.currentJob.events.length,2);
  assert.equal(p.streams.length,1);assert.equal(p.streams[0].closed,true);assert.equal(p.timers.size,0);
  assert.equal(p.requests.length,reads);assert.equal(p.document.getElementById('send').dataset.mode,'send');
});

test('real SSE marks update media progress while the projection remains pending, then terminal clears it',async()=>{
  let current=job();const projection=deferred();
  const p=page(r=>r.path.endsWith('/recovery')?{revision:'done',can_continue:true}:
    r.path.includes('/jobs/media-job')?current:projection.promise);
  p.ctx.trackAcceptedJob(RUN,current);await flush();
  current=job({cursor:12,events:[progress(1),progress(2)]});
  await p.streams[0].onmessage();
  assert.match(visible(p,'production-action'),/第 2\/4/);
  assert.equal(p.streams.length,1);
  current=job({cursor:20,status:'succeeded',events:[progress(1),progress(2),progress(4)]});
  const ending=p.streams[0].onmessage();await flush();
  assert.equal(p.state.currentJob.status,'succeeded');assert.equal(p.streams[0].closed,true);
  assert.equal(p.document.getElementById('send').dataset.mode,'send');
  assert.doesNotMatch(visible(p,'production-action'),/正在生成第|停止本次制作/);
  projection.resolve(true);await ending;
});

test('SSE loss and a failed exact read retain Stop and retry only the original media identity',async()=>{
  const p=page(()=>{throw new TypeError('Offline read interruption');});
  p.ctx.trackAcceptedJob(RUN,job());await flush();
  p.streams[0].onerror();const read=[...p.timers.values()].at(-1);p.timers.clear();await read();
  assert.equal(p.state.currentJob.job_id,'media-job');
  assert.equal(p.document.getElementById('send').disabled,false);
  assert.equal(p.document.getElementById('send').dataset.mode,'stop');
  assert.match(visible(p,'production-action'),/第 1\/4/);
  assert.deepEqual(p.requests.map(r=>r.path),[`/api/runs/${RUN}/jobs/media-job`]);
  const retry=[...p.timers.values()].at(-1);p.timers.clear();await retry();
  assert.equal(p.state.currentJob.job_id,'media-job');
  assert.equal(p.streams.length,2);assert.equal(p.streams[0].closed,true);
  assert.equal(p.requests.some(r=>r.path.endsWith('/jobs/latest')||r.method==='POST'),false);
});

test('a queued receipt offers stopping the accepted task without inventing generation progress',async()=>{
  const p=page(()=>new Promise(()=>{}));
  p.ctx.trackAcceptedJob(RUN,job({status:'queued',events:[]}));await flush();
  assert.match(visible(p,'production-action'),/等待执行/);
  assert.doesNotMatch(visible(p,'production-action'),/正在生成第/);
  assert.equal(p.document.getElementById('send').dataset.mode,'stop');
});

test('stop uses only the accepted media Job, once, while a slow read remains pending',async()=>{
  const cancel=deferred(),projection=deferred();
  const p=page(r=>r.path.endsWith('/cancel')?cancel.promise:projection.promise);
  p.ctx.trackAcceptedJob(RUN,job());await flush();
  const stop=p.document.getElementById('production-action').querySelector('button');
  stop.onclick();stop.onclick();await flush();
  assert.deepEqual(p.requests.filter(r=>r.method==='POST').map(r=>r.path),[`/api/runs/${RUN}/jobs/media-job/cancel`]);
  cancel.resolve(job({status:'cancelling',cursor:15,reason:'原提供方任务可能仍在处理与计费。'}));await flush();
  assert.equal(p.document.getElementById('send').disabled,true);
  assert.match(visible(p,'production-action'),/正在停止/);
  assert.match(visible(p,'production-action'),/原提供方任务可能仍在处理与计费/);
  assert.equal(p.state.currentJob.reason,'原提供方任务可能仍在处理与计费。');
  assert.equal(p.requests.some(r=>r.path.endsWith('/batch-generate')),false);
});

test('late receipt after project switch cannot register or render an old media task',async()=>{
  const submit=deferred();const p=page(()=>submit.promise);intent(p);
  const pending=p.ctx.submitProductionBatch();
  p.state.runId='new-project';p.ctx.detachProjectJob();
  submit.resolve(job());await pending;await flush();
  assert.equal(p.state.currentJob,null);assert.equal(p.streams.length,0);
  assert.equal(p.ctx.trackAcceptedJob(RUN,job()),false);
  assert.equal(p.ctx.trackAcceptedJob('new-project',job()),false);
  assert.equal(p.notices.length,0);
});

test('uncertain POST retains the original authorization and does not start another task or watcher',async()=>{
  const p=page(()=>{throw new TypeError('Offline uncertain submission');});intent(p);
  await p.ctx.submitProductionBatch();await flush();
  assert.equal(p.requests.length,1);assert.equal(p.state.currentJob,null);assert.equal(p.streams.length,0);
  const saved=p.ctx.currentBatchIntent();assert.equal(saved.request_id,'approved-batch');assert.equal(saved.uncertain,true);
  assert.equal(saved.drafts.length,4);assert.equal(saved.submitting,false);
  assert.match(visible(p,'production-action'),/核实同一请求/);
});

for(const reply of [{},{job_id:'media-job'},{...job(),run_id:'another-project'}])
  test(`a successful HTTP reply without a verifiable Job (${JSON.stringify(reply)}) stays uncertain`,async()=>{
    const p=page(()=>reply);intent(p);
    await p.ctx.submitProductionBatch();await flush();
    assert.equal(p.state.currentJob,null);assert.equal(p.streams.length,0);
    assert.equal(p.requests.filter(r=>r.method==='POST').length,1);
    assert.equal(p.ctx.currentBatchIntent().request_id,'approved-batch');
    assert.equal(p.ctx.currentBatchIntent().uncertain,true);
    assert.doesNotMatch(visible(p,'production-action'),/正在生成第|停止本次制作/);
    assert.equal(p.notices.some(n=>/整组生成已启动/.test(n.text)),false);
  });

test('reopening a production panel reads its saved exact Job independently of a slow workspace',async()=>{
  const workspace=deferred();
  const p=page(r=>r.path.includes('/jobs/media-job')?job():workspace.promise);
  p.storage.setItem(`studio-production:${RUN}`,JSON.stringify({lastJob:{job_id:'media-job',status:'running',action:'generate'}}));
  const mount=new El('section');p.document.body.append(mount);
  const opening=p.ctx.openProduction({mount,kind:'image'});await flush();
  assert.equal(p.state.currentJob.job_id,'media-job');assert.equal(p.streams.length,1);
  assert.equal(p.document.getElementById('send').disabled,false);
  assert.match(mount.querySelector('[data-production-job]').textContent,/第 1\/4/);
  assert.equal(mount.querySelector('[data-production-stop]').disabled,false);
  assert.equal(p.requests.filter(r=>r.path.includes('/jobs/')).length,1);
  workspace.resolve(data());await opening;
});

test('production panel POST tracks its real Job and no later full read replaces its active identity',async()=>{
  const submit=deferred(),workspace=deferred();let reads=0;
  const p=page(r=>r.method==='POST'?submit.promise:++reads===1?data():workspace.promise);
  const mount=new El('section');p.document.body.append(mount);
  await p.ctx.openProduction({mount,kind:'image',focusDraft:'frame-1'});
  const body=mount.querySelector('[data-body]');body.listeners.click({target:body.querySelector('[data-generate]')});
  const confirm=mount.querySelector('[data-confirm]');const consent=confirm.querySelector('[data-consent]');
  consent.checked=true;consent.onchange();confirm.querySelector('[data-authorize]').onclick();await flush();
  assert.equal(p.state.currentJob,null);
  submit.resolve(job());await flush();
  assert.equal(p.state.currentJob.job_id,'media-job');assert.equal(p.streams.length,1);
  assert.equal(p.document.getElementById('send').dataset.mode,'stop');
  assert.match(mount.querySelector('[data-production-job]').textContent,/第 1\/4/);
  assert.equal(mount.querySelector('[data-production-stop]').disabled,false);
  assert.equal(p.requests.some(r=>r.path.endsWith('/jobs/latest')),false);
  assert.equal(p.requests.filter(r=>r.method==='POST').length,1);
  workspace.resolve(data());await flush();
  assert.equal(p.state.currentJob.job_id,'media-job');assert.equal(p.document.getElementById('send').disabled,false);
});

test('a verified generation ACK retains its exact watcher and Stop after the workspace GET rejects',async()=>{
  let reads=0,allowRead=false,current=job();
  const p=page(request=>{
    if(request.method==='POST')return request.path.endsWith('/cancel')
      ?(current={...current,status:'cancelling',cursor:13}):current;
    if(request.path.includes('/jobs/'))return current;
    if(++reads===1||allowRead)return data();
    throw new Error('Failed to fetch');
  });
  const mount=new El('section');p.document.body.append(mount);
  await p.ctx.openProduction({mount,kind:'image',focusDraft:'frame-1'});
  const body=mount.querySelector('[data-body]');body.listeners.click({target:body.querySelector('[data-generate]')});
  const confirm=mount.querySelector('[data-confirm]');confirm.querySelector('[data-consent]').checked=true;
  confirm.querySelector('[data-consent]').onchange();confirm.querySelector('[data-authorize]').onclick();await flush();
  const saved=JSON.parse(p.storage.getItem(`studio-production:${RUN}`));
  assert.equal(saved.pending,null);assert.equal(saved.lastJob.job_id,'media-job');
  assert.equal(p.state.currentJob.job_id,'media-job');assert.equal(p.streams.length,1);
  assert.match(mount.querySelector('[data-error]').textContent,/制作请求已接收，作品记录暂未读取/);
  assert.doesNotMatch(mount.textContent,/Failed to fetch|上次请求结果待确认/);
  assert.equal(mount.querySelector('[data-retry]'),null);assert.equal(body.querySelector('[data-generate]'),null);
  assert.equal(mount.querySelector('[data-production-stop]').disabled,false);
  current=job({cursor:12,events:[progress(1),progress(2)]});
  await p.streams[0].onmessage();await flush();
  assert.match(mount.querySelector('[data-production-job]').textContent,/第 2\/4/);
  assert.equal(p.requests.filter(row=>row.method==='POST').length,1);
  allowRead=true;body.querySelector('[data-load]').onclick();await flush();
  assert.equal(body.querySelector('[data-draft-id]').dataset.draftId,'frame-1');
  assert.equal(mount.querySelector('[data-error]').textContent,'');assert.equal(p.streams.length,1);
  assert.equal(p.requests.filter(row=>row.method==='POST').length,1);
  await mount.querySelector('[data-production-stop]').onclick();await flush();
  assert.equal(p.requests.filter(row=>row.path.endsWith('/jobs/media-job/cancel')).length,1);
  assert.equal(p.requests.filter(row=>row.path.includes('/generate')).length,1);
});

test('batch progress only uses declared server status events and never infers private tool content',()=>{
  const p=page(()=>{throw Error('Rendering cannot call HTTP');});
  for(const malformed of [
    {type:'agent_narration',message:progress(2).message},
    {type:'status',message:'正在生成第 5/4 个首帧。'},
    {type:'status',message:'正在生成第 1/13 个首帧。'},
    {type:'status',message:'正在生成第 1/4 个首帧。 private-cli-reasoning'},
    {type:'status',args:{message:progress(2).message}},
  ])assert.equal(p.ctx.mediaJobProgress(job({events:[malformed]})), '');
  assert.equal(p.ctx.mediaJobProgress(job({job_kind:'image_analysis'})), '');
  assert.equal(p.ctx.mediaJobProgress(job({status:'cancelled'})), '');
  assert.equal(p.ctx.trackAcceptedJob(RUN,{job_id:'unknown-status'}),false);
  assert.equal(p.ctx.trackAcceptedJob(RUN,job({status:['running']})),false);
  assert.equal(p.requests.length,0);
});


test('reopening a confirmed active Job with a failed first workspace read retains read only recovery and Stop',async()=>{
  let current=job();
  const p=page(request=>{
    if(request.path.endsWith('/cancel'))return current={...current,status:'cancelling',cursor:12};
    if(request.path.includes('/jobs/media-job'))return current;
    throw new Error('Failed to fetch');
  });
  p.storage.setItem(`studio-production:${RUN}`,JSON.stringify({pending:null,
    lastJob:{job_id:'media-job',status:'running',action:'generate'}}));
  const mount=new El('section');p.document.body.append(mount);
  await p.ctx.openProduction({mount,kind:'image',focusDraft:'frame-1'});await flush();
  assert.equal(p.state.currentJob.job_id,'media-job');assert.equal(p.streams.length,1);
  assert.match(mount.querySelector('[data-error]').textContent,/制作请求已接收，作品记录暂未读取/);
  assert.doesNotMatch(mount.textContent,/Failed to fetch|上次请求结果待确认/);
  assert.equal(mount.querySelector('[data-retry]'),null);assert.equal(mount.querySelector('[data-save]'),null);
  assert.ok(mount.querySelector('[data-load]'));
  assert.equal(mount.querySelector('[data-production-stop]').disabled,false);
  assert.equal(p.requests.filter(row=>row.method==='POST').length,0);
  await mount.querySelector('[data-production-stop]').onclick();await flush();
  assert.deepEqual(p.requests.filter(row=>row.method==='POST').map(row=>row.path),
    [`/api/runs/${RUN}/jobs/media-job/cancel`]);
});
