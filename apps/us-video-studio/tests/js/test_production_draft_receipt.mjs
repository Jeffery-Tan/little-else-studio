/* Real shipped production dialog with synthetic immutable draft HTTP receipts.
 * The offline DOM has no layout; these checks never call providers or real data.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {createRequire} from 'node:module';
import {El,loadApp,makeDom,webSource} from './dom-fixture.mjs';

const RUN='draft-receipt-project',SHA='a'.repeat(64),NEW_SHA='b'.repeat(64);
const ui=createRequire(import.meta.url)('../../studio/web/production.js');
const flush=async()=>{for(let i=0;i<5;i++)await new Promise(resolve=>setImmediate(resolve));};
if(!Object.getOwnPropertyDescriptor(El.prototype,'elements'))
  Object.defineProperty(El.prototype,'elements',{get(){return {namedItem:name=>this.querySelector(`[name="${name}"]`)};}});
class LocalFormData {
  constructor(form){this.rows=form.querySelectorAll('[name]').map(node=>[node.getAttribute('name'),node.value]);}
  [Symbol.iterator](){return this.rows[Symbol.iterator]();}
}

function scene(){
  const originals=Array.from({length:4},(_,index)=>({draft_id:`original-${index+1}`,draft_sha256:SHA,
    kind:'image',shot_id:`shot-${String(index+1).padStart(2,'0')}`,is_current:index!==1,
    source_ready:index!==1,source_version:4,source_sha256:SHA,model:'codex-image',
    prompt:`Compiled original ${index+1}`,shot_prompt:`Original ${index+1}`,references:[],attempts:[]}));
  const replacement={...originals[1],draft_id:'replacement-at-position-5',is_current:true,
    source_ready:false,supersedes:originals[1].draft_id,prompt:'Compiled stale replacement',
    shot_prompt:'Stale replacement editable text'};
  const current=[originals[0],replacement,...originals.slice(2)];
  return {drafts:[...originals,replacement],active:false,unresolved_attempts:[],materials:[],
    sources:[{ready:true,version:4,artifact_sha256:SHA,summary:'Current exact writing'}],
    capabilities:{image:{available:true},video:{available:false,models:[]}},
    targets:{image:{complete_list:true,source_version:4,source_sha256:SHA,
      shots:current.map((row,index)=>({shot_id:row.shot_id,ordinal:index+1}))}},
    shots:{image:{complete_list:true,total:4,shots:current.map((row,index)=>({draft_id:row.draft_id,
      shot_id:row.shot_id,ordinal:index+1,draft_sha256:SHA,source_ready:row.source_ready}))}}};
}
function saved(body){
  const {request_id,...request_payload}=JSON.parse(JSON.stringify(body));
  if(!request_payload.references?.length)delete request_payload.references;
  return {...request_payload,draft_id:request_id,draft_sha256:NEW_SHA,
    shot_id:body.shot_id||'shot-02',supersedes:body.supersedes||null,
    shot_prompt:body.prompt,prompt:`Compiled saved: ${body.prompt}`,references:body.references||[],
    request_payload,attempts:[],is_current:true};
}
function install(data,receipt){
  const old=data.drafts.find(row=>row.draft_id===receipt.supersedes);if(old)old.is_current=false;
  data.drafts.push({...receipt,source_ready:true});
  const shot=data.shots.image.shots.find(row=>row.shot_id===receipt.shot_id);
  if(shot)Object.assign(shot,{draft_id:receipt.draft_id,draft_sha256:receipt.draft_sha256,source_ready:true});
}
async function page({data=scene(),focus='replacement-at-position-5',post,read,storage,failedOpening=false}={}){
  const dom=makeDom({body:webSource('index.html').match(/<body[^>]*>([\s\S]*?)<\/body>/)[1]});
  for(const node of dom.document.querySelectorAll('[id]'))dom.document.__ids.set(node.getAttribute('id'),node);
  const create=dom.document.createElement;
  dom.document.createElement=tag=>{
    const node=create(tag);
    if(tag==='dialog'){node.showModal=()=>node.open=true;node.close=()=>node.open=false;}
    return node;
  };
  const timers=new Map();let nextTimer=0,posts=0,reads=0;
  const app=loadApp({dom,storage,routes:{'/production/drafts':request=>{
    posts++;return post?post(request,posts,data):(()=>{const receipt=saved(request.body);install(data,receipt);return receipt;})();
  },'/production':request=>{reads++;return read?read(request,reads,data):data;},'/api/learning':{cases:[]}},globals:{
    FormData:LocalFormData,initStudioNavigation(){},setStudioView(){},renderProjects(){},loadInspiration(){},
    setTimeout:fn=>{timers.set(++nextTimer,fn);return nextTimer;},clearTimeout:id=>timers.delete(id),
    requestAnimationFrame:fn=>fn(),scrollDown(){},
    __submitJob:()=>{throw new Error('Draft saving cannot call a model job');},
  }});
  vm.runInContext(webSource('production.js'),app.ctx,{filename:'production.js'});
  let refreshes=0;app.ctx.refreshTimeline=async()=>{refreshes++;};
  await flush();
  vm.runInContext(`state.runId='${RUN}';state.currentJob=null;state.rail=[];`,app.ctx);
  const options={kind:'image',focusDraft:focus};await app.ctx.openProduction(options);
  const dialog=app.document.querySelector('.production-dialog'),body=dialog.querySelector('[data-body]');
  if(!failedOpening)assert.equal(dialog.querySelector('[data-error]').textContent,'');
  return {...app,data,dialog,body,timers,options,refreshes:()=>refreshes,
    store:()=>JSON.parse(app.storage.getItem(`studio-production:${RUN}`)),
    title:()=>body.querySelector('.production-record-shot h3')?.textContent,
    edit:()=>{const button=body.querySelector('[data-edit-input]');assert.ok(button);
      body.listeners.click({target:button,preventDefault(){}});},
    save:async(text='New exact manual description')=>{
      const form=body.querySelector('form');form.querySelector('textarea').value=text;
      form.oninput({target:form.querySelector('textarea')});form.onsubmit({preventDefault(){}});await flush();
    },
  };
}

test('focused stale shot 2 at the fifth historical record uses server ordinal and stays readable',async()=>{
  const data=scene(),before=JSON.stringify(data),p=await page({data});
  assert.equal(p.title(),'首帧 02');
  assert.match(p.body.textContent,/Stale replacement editable text|Compiled stale replacement/);
  assert.equal(p.body.querySelector('[data-draft-id]').dataset.draftId,'replacement-at-position-5');
  assert.equal(p.body.querySelector('[data-generate]').disabled,true);
  assert.equal(JSON.stringify(data),before);assert.equal(p.observed.filter(row=>row.method==='POST').length,0);
});
for(const fault of ['missing','duplicate','invalid'])test(`focused ${fault} ordinal cannot use the historical position or ID text`,async()=>{
  const data=scene(),row=data.shots.image.shots[1];
  if(fault==='missing')data.shots.image.shots.splice(1,1);
  if(fault==='duplicate')data.shots.image.shots.push({...row,ordinal:9});
  if(fault==='invalid')row.ordinal=0;
  const p=await page({data});assert.equal(p.title(),'首帧（镜头号未提供）');
  assert.equal(p.body.querySelector('[data-draft-id]').dataset.draftId,'replacement-at-position-5');
});

test('an absent exact focus never falls back to another latest production input',async()=>{
  const p=await page({focus:'missing-exact-input'});
  assert.match(p.body.textContent,/这份准确制作输入暂未能读取/);
  assert.equal(p.body.querySelector('[data-draft-id]'),null);assert.equal(p.body.querySelector('[data-generate]'),null);
});

for(const replacing of [true,false])test(`saving ${replacing?'a replacement':'a new input'} switches focus and editor only to its exact receipt`,async()=>{
  const data=scene();
  if(!replacing){
    data.drafts=[];
    data.targets.image.shots=[{shot_id:'shot-02',ordinal:1}];
    data.shots.image={complete_list:true,total:1,shots:[{shot_id:'shot-02',ordinal:1,source_ready:false}]};
  }
  const p=await page({data,focus:replacing?'replacement-at-position-5':null});if(replacing)p.edit();
  else {
    const form=p.body.querySelector('form');form.elements.namedItem('source_version').value='4';
    form.oninput({target:{name:'source_version'}});
  }
  await p.save();
  const posts=p.observed.filter(row=>row.method==='POST'),request=posts[0],id=request.body.request_id;
  assert.equal(posts.length,1);assert.match(request.path,/\/production\/drafts$/);
  assert.equal(request.body.supersedes,replacing?'replacement-at-position-5':undefined);
  assert.equal(p.body.querySelector('[data-draft-id]').dataset.draftId,id);
  assert.equal(p.title(),replacing?'首帧 02':'首帧 01');
  assert.equal(p.body.querySelector('textarea').value,'New exact manual description');
  assert.equal(p.store().values.supersedes,id);assert.equal(p.store().values.shot_id,'shot-02');
  assert.equal(p.store().pending,null);
  assert.match(p.body.textContent,/Compiled saved: New exact manual description/);
  assert.equal(p.body.querySelector('[data-save]').disabled,false);
  assert.equal(p.options.focusDraft,replacing?'replacement-at-position-5':null,'external caller options are not rewritten');
  await p.save('A second explicit free edit');
  assert.equal(p.observed.filter(row=>row.method==='POST')[1].body.supersedes,id);
  assert.ok(p.observed.every(row=>!row.path.includes('/generate')&&!row.path.includes('/jobs')));
});

for(const fault of ['empty','wrong-id','wrong-source','wrong-shot','wrong-parent','wrong-hash','wrong-input','wrong-reference','wrong-model'])
  test(`a ${fault} draft receipt preserves the frozen request and old edit state`,async()=>{
    const p=await page({post:request=>{
      const result=saved(request.body);
      if(fault==='empty')return {};
      if(fault==='wrong-id')result.draft_id='some-other-request';
      if(fault==='wrong-source')result.source_sha256='c'.repeat(64);
      if(fault==='wrong-shot')result.shot_id='shot-01';
      if(fault==='wrong-parent')result.supersedes='original-1';
      if(fault==='wrong-hash')result.draft_sha256='invalid';
      if(fault==='wrong-input')result.request_payload.prompt='Different saved input';
      if(fault==='wrong-reference')result.request_payload.references=[{name:'other.png',sha256:SHA,purpose:'product'}];
      if(fault==='wrong-model')result.model='other-model';
      return result;
    }});p.edit();await p.save();
    const request=p.observed.find(row=>row.method==='POST');
    assert.deepEqual(p.store().pending.body,request.body);
    assert.equal(p.store().values.supersedes,'replacement-at-position-5');
    assert.equal(p.body.querySelector('[data-draft-id]').dataset.draftId,'replacement-at-position-5');
    assert.equal(p.body.querySelector('textarea').value,'New exact manual description');
    assert.equal(p.body.querySelector('[data-save]').disabled,true);
    assert.ok(p.dialog.querySelector('[data-retry]'));
    assert.match(p.dialog.querySelector('[data-error]').textContent,/保存回执无法.*原请求/);
    assert.equal(p.observed.filter(row=>row.method==='POST').length,1);
  });

test('unknown save result retries the same request and reads its original immutable receipt',async()=>{
  let original;
  const p=await page({post:(request,count,data)=>{
    if(count===1){original=saved(request.body);install(data,original);return {};}
    assert.deepEqual(request.body,JSON.parse(JSON.stringify(p.store().pending.body)));
    return original;
  }});p.edit();await p.save();
  const first=p.observed.find(row=>row.method==='POST');
  p.dialog.querySelector('[data-retry]').onclick();await flush();
  const posts=p.observed.filter(row=>row.method==='POST');
  assert.equal(posts.length,2);assert.deepEqual(posts[1],first);
  assert.equal(p.body.querySelector('[data-draft-id]').dataset.draftId,first.body.request_id);
  assert.equal(p.store().values.supersedes,first.body.request_id);assert.equal(p.store().pending,null);
});

for(const fault of ['missing-projection','different-hash'])test(`accepted receipt with ${fault} never displays another input`,async()=>{
  const p=await page({post:(request,_count,data)=>{
    const receipt=saved(request.body);
    if(fault==='different-hash')install(data,{...receipt,draft_sha256:'c'.repeat(64)});
    return receipt;
  }});p.edit();await p.save();
  assert.match(p.body.textContent,/这份准确制作输入暂未能读取/);
  assert.equal(p.body.querySelector('[data-draft-id]'),null);
  assert.equal(p.store().pending,null);assert.equal(p.store().values.prompt,'New exact manual description');
  assert.equal(p.observed.filter(row=>row.method==='POST').length,1);
});

test('an explicit save refusal keeps the original focus and allows correcting the same editor',async()=>{
  const p=await page({post:()=>Object.assign(new Error('Synthetic source refusal'),{status:409})});
  p.edit();await p.save();
  assert.equal(p.store().pending,null);assert.equal(p.store().values.supersedes,'replacement-at-position-5');
  assert.equal(p.title(),'首帧 02');assert.equal(p.body.querySelector('[data-save]').disabled,false);
  assert.match(p.dialog.querySelector('[data-error]').textContent,/Synthetic source refusal/);
});

test('only omitted or null optional reference design IDs share the Pydantic default representation',()=>{
  const body={request_id:'exact-default',kind:'image',prompt:'Synthetic reference defaults.',
    source_version:4,source_sha256:SHA,shot_id:'shot-02',supersedes:'old',model:'codex-image',
    duration:null,resolution:null,references:[{name:'product.png',sha256:SHA,purpose:'product'},
      {name:'scene.png',sha256:NEW_SHA,purpose:'scene',design_id:'scene-table'}]};
  const receipt=saved(body);receipt.request_payload.references[0].design_id=null;
  assert.equal(ui.savedDraftForRequest(receipt,{body}),receipt);
  const omitted=structuredClone(receipt);delete omitted.request_payload.references[0].design_id;
  const explicit=structuredClone(body);explicit.references[0].design_id=null;
  assert.equal(ui.savedDraftForRequest(omitted,{body:explicit}),omitted);
  for(const mutate of [
    row=>row.references[0].design_id='unrequested-design',
    row=>row.references[0].name='other.png',
    row=>row.references[0].sha256=NEW_SHA,
    row=>row.references[0].purpose='continuity',
    row=>delete row.references[0].purpose,
    row=>row.references[0].unexpected=null,
    row=>row.references[0]=null,
    row=>row.references[1].design_id=null,
    row=>row.references[1].design_id='other-scene',
    row=>row.references.reverse(),
  ]){
    const altered=structuredClone(receipt);mutate(altered.request_payload);
    assert.throws(()=>ui.savedDraftForRequest(altered,{body}),/保存回执无法/);
  }
});

test('save ACK survives a failed subsequent GET and recovers with a read only request',async()=>{
  const p=await page({read:(_request,count,data)=>{
    if(count===2)throw new Error('Failed to fetch');return data;
  }});p.edit();const detachedForm=p.body.querySelector('form');await p.save();
  const post=p.observed.find(row=>row.method==='POST'),id=post.body.request_id;
  assert.equal(p.store().pending,null);assert.equal(p.store().values.supersedes,id);
  assert.equal(p.store().values.prompt,'New exact manual description');
  assert.match(p.dialog.querySelector('[data-error]').textContent,/制作方案已保存，作品记录暂未读取/);
  assert.doesNotMatch(p.dialog.textContent,/Failed to fetch|上次请求结果待确认|核实上次请求/);
  assert.equal(p.dialog.querySelector('[data-retry]'),null);
  assert.equal(p.body.querySelector('[data-save]'),null);assert.equal(p.body.querySelector('[data-generate]'),null);
  assert.ok(p.body.querySelector('[data-load]'));
  detachedForm.onsubmit({preventDefault(){}});await flush();
  assert.equal(p.observed.filter(row=>row.method==='POST').length,1,'Detached old editor cannot save while the read is blocked');
  p.body.querySelector('[data-load]').onclick();await flush();
  assert.equal(p.observed.filter(row=>row.method==='POST').length,1);
  assert.equal(p.store().pending,null);assert.equal(p.body.querySelector('[data-draft-id]').dataset.draftId,id);
  assert.equal(p.title(),'首帧 02');assert.equal(p.body.querySelector('textarea').value,'New exact manual description');
  assert.equal(p.dialog.querySelector('[data-error]').textContent,'');
});

for(const fault of ['missing','hash','duplicate','source-version','source-hash'])test(`read recovery rejects a ${fault} exact saved input without restoring a POST`,async()=>{
  const p=await page({read:(_request,count,data)=>{
    if(count===2)throw new Error('Failed to fetch');
    if(count!==3)return data;
    const bad=structuredClone(data),receipt=bad.drafts.at(-1);
    if(fault==='missing')bad.drafts.pop();
    if(fault==='hash')receipt.draft_sha256='c'.repeat(64);
    if(fault==='duplicate')bad.drafts.push({...receipt});
    if(fault==='source-version')receipt.source_version=5;
    if(fault==='source-hash')receipt.source_sha256='c'.repeat(64);
    return bad;
  }});p.edit();await p.save();
  const id=p.store().values.supersedes;
  p.body.querySelector('[data-load]').onclick();await flush();
  assert.match(p.body.textContent,/这份准确制作输入暂未能读取或指纹未能核对/);
  assert.equal(p.store().pending,null);assert.equal(p.store().values.supersedes,id);
  assert.equal(p.dialog.querySelector('[data-retry]'),null);assert.equal(p.body.querySelector('form'),null);
  assert.equal(p.observed.filter(row=>row.method==='POST').length,1);
  p.body.querySelector('[data-load]').onclick();await flush();
  assert.equal(p.body.querySelector('[data-draft-id]').dataset.draftId,id);
  assert.equal(p.store().pending,null);assert.equal(p.observed.filter(row=>row.method==='POST').length,1);
});

test('a late read after save ACK cannot bind or reload a different project',async()=>{
  let resolve;
  const waiting=new Promise(done=>resolve=done);
  const p=await page({read:(_request,count,data)=>count===2?waiting:data});p.edit();await p.save();
  assert.equal(p.store().pending,null);const id=p.store().values.supersedes;
  vm.runInContext('state.runId="another-project";state.productionData={marker:"another-project"};',p.ctx);
  resolve(p.data);await flush();
  assert.equal(p.dialog.open,false);
  assert.equal(vm.runInContext('state.productionData.marker',p.ctx),'another-project');
  assert.equal(p.store().values.supersedes,id);
  assert.equal(p.observed.filter(row=>row.method==='POST').length,1);
  assert.ok(p.observed.every(row=>!row.path.includes('another-project')));
});

test('confirmed save read recovery survives closing and reopening from its stale parent card',async()=>{
  const first=await page({read:(_request,count,data)=>{
    if(count===2)throw new Error('Failed to fetch');return data;
  }});first.edit();await first.save();
  const id=first.store().values.supersedes;
  assert.equal(first.store().draftReadRecovery.draft_id,id);
  assert.equal(first.store().draftReadRecovery.draft_sha256,NEW_SHA);
  first.dialog.studioClose();
  const reopened=await page({data:structuredClone(first.data),storage:first.storage,failedOpening:true,
    read:(_request,count,data)=>{if(count===1)throw new Error('Failed to fetch');return data;}});
  assert.match(reopened.dialog.textContent,/制作方案已保存|作品记录暂未读取/);
  assert.doesNotMatch(reopened.dialog.textContent,/上次请求结果待确认|Failed to fetch/);
  assert.equal(reopened.store().pending,null);assert.equal(reopened.store().values.supersedes,id);
  assert.equal(reopened.body.querySelector('form'),null);assert.equal(reopened.dialog.querySelector('[data-retry]'),null);
  reopened.body.querySelector('[data-load]').onclick();await flush();
  assert.equal(reopened.body.querySelector('[data-draft-id]').dataset.draftId,id);
  assert.equal(reopened.title(),'首帧 02');assert.equal(reopened.store().draftReadRecovery,null);
  assert.equal(reopened.body.querySelector('textarea').value,'New exact manual description');
  assert.equal(reopened.observed.filter(row=>row.method==='POST').length,0);
});

test('a saved read receipt does not replace another explicitly opened shot',async()=>{
  const first=await page({read:(_request,count,data)=>{
    if(count===2)throw new Error('Failed to fetch');return data;
  }});first.edit();await first.save();first.dialog.studioClose();
  const reopened=await page({data:structuredClone(first.data),storage:first.storage,focus:'original-3'});
  assert.equal(reopened.body.querySelector('[data-draft-id]').dataset.draftId,'original-3');
  assert.equal(reopened.title(),'首帧 03');assert.equal(reopened.store().draftReadRecovery,null);
  assert.equal(reopened.observed.filter(row=>row.method==='POST').length,0);
});


test('a later workspace read failure preserves the confirmed saved identity across another reopen',async()=>{
  const first=await page({read:(_request,count,data)=>{
    if(count===3)throw new Error('Failed to fetch');return data;
  }});first.edit();await first.save();
  const id=first.store().values.supersedes;
  assert.equal(first.store().draftReadRecovery,null,'Successful read clears the recovery marker');
  const poll=[...first.timers.values()][0];assert.ok(poll);poll();await flush();
  assert.equal(first.store().draftReadRecovery.draft_id,id);
  assert.match(first.dialog.textContent,/制作方案已保存/);first.dialog.studioClose();
  const reopened=await page({data:structuredClone(first.data),storage:first.storage});
  assert.equal(reopened.body.querySelector('[data-draft-id]').dataset.draftId,id);
  assert.equal(reopened.title(),'首帧 02');assert.equal(reopened.store().draftReadRecovery,null);
  assert.equal(reopened.observed.filter(row=>row.method==='POST').length,0);
});


test('verified local save synchronizes canonical workspace once and unchanged polling never resubmits',async()=>{
  const p=await page();assert.equal(p.refreshes(),0);p.edit();await p.save();
  assert.equal(p.refreshes(),1);
  const poll=[...p.timers.values()][0];assert.ok(poll);poll();await flush();
  assert.equal(p.refreshes(),1);assert.equal(p.observed.filter(row=>row.method==='POST').length,1);
});

test('failed receipt read synchronizes only after recovery with no repeated write',async()=>{
  const p=await page({read:(_request,count,data)=>{if(count===2)throw new Error('Failed to fetch');return data;}});
  p.edit();await p.save();assert.equal(p.refreshes(),0);
  assert.ok(p.body.querySelector('[data-load]'));p.body.querySelector('[data-load]').onclick();await flush();
  assert.equal(p.refreshes(),1);assert.equal(p.observed.filter(row=>row.method==='POST').length,1);
});

test('late local save read never synchronizes a different project',async()=>{
  let resolve;const waiting=new Promise(done=>resolve=done);
  const p=await page({read:(_request,count,data)=>count===2?waiting:data});p.edit();await p.save();
  vm.runInContext('state.runId="another-project";',p.ctx);resolve(p.data);await flush();
  assert.equal(p.refreshes(),0);assert.equal(p.observed.filter(row=>row.method==='POST').length,1);
});


test('canonical refresh failure keeps verified saved identity and reports synchronization gap',async()=>{
  const p=await page();p.ctx.refreshTimeline=async()=>{throw new Error('private network details');};
  p.edit();await p.save();
  assert.equal(p.store().pending,null);assert.equal(p.store().draftReadRecovery,null);
  assert.match(p.dialog.querySelector('[data-error]').textContent,/本次保存已核对，工作区状态暂未同步/);
  assert.doesNotMatch(p.dialog.textContent,/private network details/);
  assert.equal(p.observed.filter(row=>row.method==='POST').length,1);
});
