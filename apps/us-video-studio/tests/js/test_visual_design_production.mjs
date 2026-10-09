import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import vm from 'node:vm';
import {El,makeDom,makeStorage,webSource} from './dom-fixture.mjs';

const require=createRequire(import.meta.url),ui=require('../../studio/web/production.js');
const scriptHash='a'.repeat(64),planHash='b'.repeat(64),designHash='c'.repeat(64);
const materials=['product','character','scene','continuity'].map((purpose,index)=>({
  name:`${purpose} <${index+1}>.png`,sha256:String(index+1).repeat(64),kind:'image',approved:true,
  url:`/api/runs/design-demo/materials/${encodeURIComponent(`${purpose} <${index+1}>.png`)}`,
}));
const refs=materials.map((material,index)=>({name:material.name,sha256:material.sha256,purpose:['product','character','scene','continuity'][index]}));
const binding={material_plan:{version:3,sha256:planHash},writing:{version:4,sha256:scriptHash},
  design_sha256:designHash,character_ids:['nora','leo'],scene_id:'kitchen'};
const labels={characters:[{id:'nora',label:'Nora <teacher>'},{id:'leo',label:'Leo & son'}],scene:{id:'kitchen',label:'日常厨房 <US>'}};
const image={draft_id:'shot-1',draft_sha256:'d'.repeat(64),kind:'image',source_version:4,source_sha256:scriptHash,
  shot_id:'shot-01',source_ready:true,is_current:true,can_generate:true,references:refs,
  // The legacy field must not override the explicit input set.
  reference:{name:'obsolete.png',sha256:'e'.repeat(64)},design_binding:binding,design_labels:labels,
  shot_prompt:'Nora offers the bag to Leo.',prompt:'Outdated compiled text',compiled_prompt:'Exact compiled <picture>\nNora and Leo in the designed kitchen.',
  estimate:{amount:null,currency:null,notice:'费用未知'},attempts:[]};
const video={...image,draft_id:'clip-1',kind:'video',model:'wan3.0-video',duration:4,resolution:'720P',
  references:[refs[3]],reference:null,reference_approved:true};
const data={active:false,drafts:[image],materials,
  capabilities:{image:{available:true,controller_model:'fixture'},video:{available:true}}};
const confirmation=(draft=image,shown=data)=>ui.confirmationHtml(draft,shown,'design-demo');
const intent=(draft=image)=>({kind:draft.kind==='video'?'video_generate':'generate',drafts:[{draft_id:draft.draft_id,expected_sha256:draft.draft_sha256}]});

test('single confirmation displays all actual image references with their purposes and exact bytes',()=>{
  const html=confirmation();
  assert.equal(ui.gate(image,data),'');
  assert.equal((html.match(/class="production-shot-reference production-reference-input"/g)||[]).length,4);
  for(const [index,ref] of refs.entries()){
    assert.match(html,new RegExp(ui.esc(ref.name).replace(/[.*+?^${}()|[\]\\]/g,'\\$&')));
    assert.ok(html.includes(`src="${materials[index].url}"`));
    assert.ok(html.includes(ref.sha256));
  }
  for(const purpose of ['商品外观','人物外观','场景环境','画面衔接'])assert.ok(html.includes(purpose));
  assert.doesNotMatch(html,/obsolete\.png|data-approve|data-image=|跨镜锁定|保证一致/);
  assert.match(html,/参考图像素输入 · 4 张/);
  assert.match(html,/data-consent/);
  assert.equal((html.match(/data-authorize/g)||[]).length,1);
});

test('design identities, version and fingerprints are visible without an extra approval flow',()=>{
  const html=confirmation();
  assert.match(html,/素材计划第 3 版/);
  assert.match(html,/Nora &lt;teacher&gt;（nora）/);
  assert.match(html,/Leo &amp; son（leo）/);
  assert.match(html,/日常厨房 &lt;US&gt;（kitchen）/);
  assert.ok(html.includes(planHash));assert.ok(html.includes(scriptHash));assert.ok(html.includes(designHash));
  assert.doesNotMatch(html,/<teacher>|<US>|data-approve-design|data-character|data-scene|批准人物|批准场景/);
  assert.match(html,/实际结果仍需逐镜核对/);
  const noCast=confirmation({...image,design_binding:{...binding,character_ids:[]},design_labels:{characters:[],scene:labels.scene}});
  assert.match(noCast,/本镜不包含人物/);
  const unlabelled=confirmation({...image,design_labels:{characters:[],scene:null}});
  assert.match(unlabelled,/>nora、leo</);assert.match(unlabelled,/>kitchen</);
});

test('single, batch and storyboard all show the compiled provider prompt rather than uncompiled editor text',()=>{
  const batch=ui.batchConfirmationHtml(intent(),data,'design-demo');
  const canvas=ui.canvasHtml(data,'image','design-demo',[]);
  for(const html of [confirmation(),batch,canvas,ui.draftHtml(image,data,'design-demo'),ui.historyDraftHtml(image,data,'design-demo')]){
    assert.match(html,/Exact compiled &lt;picture&gt;/);
    assert.doesNotMatch(html,/Outdated compiled text|<picture>/);
    assert.match(html,/Nora &lt;teacher&gt;/);
    assert.equal((html.match(/class="production-shot-reference production-reference-input"/g)||[]).length,4);
  }
  assert.match(canvas,/data-feedback-image-draft="shot-1"/);
  assert.doesNotMatch(canvas,/data-authorize|data-batch-confirm|data-generate|data-review/);
  assert.match(batch,/data-batch-confirm/);
});

test('reference previews resolve only the exact name and sha inside the displayed run',()=>{
  for(const changed of [
    {...materials[1],sha256:'f'.repeat(64)},
    {...materials[1],kind:'video'},
    {...materials[1],url:'https://provider.test/untrusted.png'},
    {...materials[1],url:'/api/runs/other/materials/wrong.png'},
  ]){
    const shown={...data,materials:[materials[0],changed,...materials.slice(2)]};
    const html=confirmation(image,shown);
    assert.match(html,/参考图不可用/);
    assert.doesNotMatch(html,/data-consent|data-authorize|src="https:/);
    assert.equal(ui.batchConfirmationHtml(intent(),shown,'design-demo'),'');
    assert.ok(!ui.referenceInputs(image,shown,'design-demo')[1].url);
  }
});

test('video uses only one continuity first frame and cannot imply other files are sent',()=>{
  const shown={...data,drafts:[video]};
  assert.equal(ui.gate(video,shown),'');
  const html=confirmation(video,shown);
  assert.match(html,/本段实际使用的首帧 · 1 张/);
  assert.match(html,/作为本段视频的起始画面/);
  assert.equal((html.match(/class="production-shot-reference production-reference-input"/g)||[]).length,1);
  assert.doesNotMatch(html,/product &lt;1&gt;\.png|character &lt;2&gt;\.png|scene &lt;3&gt;\.png/);
  for(const references of [refs,[],[refs[0]],[refs[3],refs[3]]]){
    const invalid={...video,references};
    assert.match(ui.gate(invalid,shown),/一张已确认的首帧/);
    assert.doesNotMatch(confirmation(invalid,shown),/data-consent|data-authorize|本段实际使用的首帧/);
    assert.equal(ui.batchConfirmationHtml(intent(invalid),{...shown,drafts:[invalid]},'design-demo'),'');
  }
  assert.match(ui.gate({...video,reference_approved:false},shown),/先查看并确认/);
});

test('missing shared design leaves legacy input and result readable but blocks both generation confirmations',()=>{
  const issue='旧稿没有共同人物与场景设计，请先补齐 <设计>。';
  const output={name:'old-result.png',sha256:'f'.repeat(64),kind:'image',approved:true,url:'/api/runs/design-demo/materials/old-result.png'};
  const legacy={...image,design_binding:null,design_labels:null,references:undefined,reference:refs[0],
    compiled_prompt:undefined,shot_prompt:undefined,prompt:'Legacy per-shot prompt',source_ready:false,design_issue:issue,
    attempts:[{attempt_id:'old-image',status:'succeeded',output}]};
  const shown={...data,drafts:[legacy],materials:[...materials,output]};
  assert.equal(ui.gate(legacy,shown),issue);
  assert.equal(ui.batchConfirmationHtml(intent(legacy),shown,'design-demo'),'');
  const single=confirmation(legacy,shown);
  assert.match(single,/旧稿没有共同人物与场景设计/);
  assert.doesNotMatch(single,/data-authorize|data-consent|<设计>/);
  const canvas=ui.canvasHtml(shown,'image','design-demo',[]);
  assert.match(canvas,/Legacy per-shot prompt/);
  assert.match(canvas,/旧稿没有共同人物与场景设计/);
  assert.match(canvas,/src="\/api\/runs\/design-demo\/materials\/old-result\.png"/);
  assert.match(canvas,/这面镜头的生成结果/);
  assert.doesNotMatch(canvas,/data-generate|data-review|data-authorize/);
  const work=ui.currentProductionWork(shown,'image','design-demo');
  assert.equal(work.phase,'stale');assert.equal(work.primaryAction,'discuss');assert.equal(work.detail,issue);
  assert.equal(work.outputUrl,output.url);
});

test('legacy single reference stays readable while an explicit empty set does not restore it',()=>{
  const legacy={...image,references:undefined,design_binding:null,design_labels:null,reference:refs[0]};
  const html=confirmation(legacy);
  assert.match(html,/上传商品原图，仅供生成参考/);
  assert.doesNotMatch(html,/本镜采用的共享设计/);
  const none={...legacy,references:[]};
  assert.deepEqual(ui.draftReferences(none),[]);
  assert.match(confirmation(none),/本方案未使用参考图像素输入/);
  assert.doesNotMatch(confirmation(none),/<img|product &lt;1&gt;\.png/);
});

test('all image reference pixels must load before the existing cost consent can submit',()=>{
  const loaded={complete:true,naturalWidth:100},loading={complete:false,naturalWidth:0},broken={complete:true,naturalWidth:0};
  assert.equal(ui.generationConsentReady(false,[loaded,loaded]),false);
  assert.equal(ui.generationConsentReady(true,[loaded,loading]),false);
  assert.equal(ui.generationConsentReady(true,[loaded,broken]),false);
  assert.equal(ui.generationConsentReady(true,[loaded,loaded]),true);
  assert.equal(ui.generationConsentReady(true,[]),true);
});

test('invalid or too many image reference purposes are explicit blockers',()=>{
  for(const references of [[{...refs[0],purpose:'general'}],Array.from({length:6},()=>refs[0])]){
    const invalid={...image,references};
    assert.notEqual(ui.gate(invalid,data),'');
    assert.doesNotMatch(confirmation(invalid),/data-authorize|data-consent/);
    assert.equal(ui.batchConfirmationHtml(intent(invalid),{...data,drafts:[invalid]},'design-demo'),'');
  }
});

test('advanced edit preserves shot identity and purpose-bound references but submits only original shot instructions',()=>{
  const selectedRefs=refs.map((ref,index)=>({...ref,...(index===1?{design_id:'nora'}:index===2?{design_id:'kitchen'}:{}),
    url:'https://must-not-submit.test/image.png',label:'not an input field'}));
  const selected={...image,references:selectedRefs};
  const values=ui.editValues(selected);
  assert.equal(values.prompt,image.shot_prompt);
  assert.equal(values.shot_id,image.shot_id);assert.equal(values.supersedes,image.draft_id);
  const shown={...data,sources:[{version:4,artifact_sha256:scriptHash,ready:true}]};
  const saved=ui.submission({...values,prompt:'Edited action only'},shown,'edit-1');
  assert.equal(saved.prompt,'Edited action only');assert.equal(saved.shot_id,image.shot_id);
  assert.equal(saved.supersedes,image.draft_id);assert.equal(saved.references.length,4);
  assert.equal(saved.references[1].design_id,'nora');assert.equal(saved.references[2].design_id,'kitchen');
  assert.ok(!('reference' in saved));
  for(const key of ['compiled_prompt','design_binding','shot_binding','shot_prompt'])assert.ok(!(key in saved));
  for(const ref of saved.references){assert.ok(!('url' in ref));assert.ok(!('label' in ref));}
  assert.deepEqual(selected.references,selectedRefs);
  const invalid={...shown,materials:[materials[0],{...materials[1],sha256:'f'.repeat(64)},...materials.slice(2)]};
  assert.throws(()=>ui.submission(values,invalid,'edit-2'),/参考图文件已变化/);
  assert.match(confirmation(selected,shown),/人物外观 · Nora &lt;teacher&gt;（nora）/);
  assert.match(confirmation(selected,shown),/场景环境 · 日常厨房 &lt;US&gt;（kitchen）/);
});

test('an explicit missing shared design projection names the gap before any new preparation or generation',()=>{
  const shown={...data,sources:[{version:4,artifact_sha256:scriptHash,ready:true}],
    visual_design_status:{ready:false,issues:['素材计划尚未提供共同设计。']}};
  assert.equal(ui.gate(image,shown),'素材计划尚未提供共同设计。');
  assert.doesNotMatch(confirmation(image,shown),/data-consent|data-authorize/);
  const work=ui.currentProductionWork({...shown,drafts:[]},'image','design-demo');
  assert.equal(work.phase,'needs_design');assert.equal(work.primaryAction,'open_plan');
  assert.match(ui.emptyWorkHtml(work),/素材计划尚未提供共同设计/);
  assert.doesNotMatch(ui.emptyWorkHtml(work),/data-prepare/);
});

// The existing DOM fixture has no layout or image decoding. Add only the form
// and mounting properties needed to exercise the real production controller;
// image load/error events and the API boundary remain explicit in each test.
Object.defineProperty(El.prototype,'isConnected',{get(){return Boolean(this.parentNode);}});
Object.defineProperty(El.prototype,'elements',{get(){return {namedItem:name=>this.querySelector(`[name="${name}"]`)};}});
async function controller(shown={...data,sources:[{version:4,artifact_sha256:scriptHash,ready:true}]},timeline){
  const {document}=makeDom(),mount=new El('section'),requests=[];
  document.body.append(mount);
  const ctx={document,sessionStorage:makeStorage(),state:{runId:'design-demo',rail:[],creatorView:{format:'video'},timeline},
    crypto:{randomUUID:()=>`fixture-${requests.length+1}`},setTimeout:()=>1,clearTimeout:()=>{},
    $:()=>null,toast:()=>{},restoreJob:()=>{},showJourneyTab:()=>{},
    FormData:class{constructor(form){this.rows=form.querySelectorAll('input,select,textarea').filter(el=>el.getAttribute('name')&&!el.disabled).map(el=>[el.getAttribute('name'),el.value]);} [Symbol.iterator](){return this.rows[Symbol.iterator]();}},
    api:async(path,options)=>{requests.push({path,method:options?.method||'GET',body:options?.body?JSON.parse(options.body):null});
      if(options?.method==='POST')return new Promise(()=>{});return {...shown,nonce:'fixture-nonce'};},
  };
  ctx.window=ctx;ctx.globalThis=ctx;vm.createContext(ctx);
  vm.runInContext(webSource('production.js'),ctx,{filename:'production.js'});
  await ctx.openProduction({mount,kind:shown.drafts[0]?.kind||'image',focusDraft:shown.drafts[0]?.draft_id});
  return {ctx,mount,requests,body:mount.querySelector('[data-body]'),confirm:mount.querySelector('[data-confirm]')};
}

test('real production click path keeps authorization disabled until every reference loads and sends one pinned request',async()=>{
  const {body,confirm,requests}=await controller();
  body.listeners.click({target:body.querySelector('[data-generate]')});
  const button=confirm.querySelector('[data-authorize]'),consent=confirm.querySelector('[data-consent]');
  const images=confirm.querySelectorAll('.production-reference-input img');
  assert.equal(images.length,4);assert.equal(button.disabled,true);
  consent.checked=true;consent.onchange({target:consent});assert.equal(button.disabled,true);
  for(const img of images.slice(0,-1)){img.complete=true;img.naturalWidth=100;img.onload();}
  assert.equal(button.disabled,true);
  const last=images.at(-1);last.complete=true;last.naturalWidth=0;last.onerror();
  assert.equal(button.disabled,true);assert.match(body.parentNode.querySelector('[data-error]').textContent,/参考图未能完整加载/);
  last.naturalWidth=100;last.onload();assert.equal(button.disabled,false);
  consent.checked=false;consent.onchange({target:consent});button.onclick();
  assert.equal(requests.filter(row=>row.method==='POST').length,0);
  consent.checked=true;consent.onchange({target:consent});button.onclick();button.onclick();
  const sent=requests.filter(row=>row.method==='POST');assert.equal(sent.length,1);
  assert.equal(sent[0].path,'/api/runs/design-demo/production/shot-1/generate');
  assert.equal(sent[0].body.expected_sha256,image.draft_sha256);
  assert.equal(sent[0].body.nonce,'fixture-nonce');assert.equal(sent[0].body.consent,true);
});

test('real advanced edit flow keeps original shot text and all reference purposes through form submission',async()=>{
  const {body,requests}=await controller();
  body.listeners.click({target:body.querySelector('[data-edit-input]')});
  const form=body.querySelector('form'),prompt=form.querySelector('[name="prompt"]');
  assert.equal(prompt.value,image.shot_prompt);
  assert.equal(form.querySelectorAll('.production-reference-input').length,4);
  assert.equal(form.querySelector('[name="reference_name"]'),null);
  prompt.value='The revised action only';form.onsubmit({preventDefault(){}});
  const sent=requests.filter(row=>row.method==='POST');assert.equal(sent.length,1);
  assert.equal(sent[0].path,'/api/runs/design-demo/production/drafts');
  assert.equal(sent[0].body.prompt,'The revised action only');
  assert.equal(sent[0].body.shot_id,'shot-01');assert.equal(sent[0].body.supersedes,'shot-1');
  assert.deepEqual(sent[0].body.references,refs);assert.ok(!('reference' in sent[0].body));
  assert.ok(!('compiled_prompt' in sent[0].body));assert.ok(!('design_binding' in sent[0].body));
});

function reuseProjection({eligible=true,active=false,current=true}={}){
  const old={...image,draft_id:'old-input',source_version:4,source_ready:false,is_current:false,
    request_payload:{request_id:'old-request',kind:'image',prompt:image.shot_prompt,source_version:4,
      source_sha256:scriptHash,model:'codex-image',duration:null,resolution:null,references:refs,supersedes:'old-ancestor'}};
  const next={...image,draft_id:'current-input',source_version:5,source_sha256:'f'.repeat(64)};
  return {...data,active,drafts:current?[old,next]:[old],sources:[{version:4,artifact_sha256:scriptHash,ready:false},
    {version:5,artifact_sha256:'f'.repeat(64),ready:true}],targets:{image:{shots:[{shot_id:'shot-01',ordinal:3}]}},
    reuse_candidates:[{kind:'image',shot_id:'shot-01',source_draft_id:'old-input',source_attempt_id:'old-result',
      source_version:5,source_sha256:'f'.repeat(64),changed_dependencies:[],
      eligible,reason:eligible?'逐镜依赖与字节一致':'原结果 <missing> 尚待核实'}]};
}

test('reuse shows current shot order and only offers eligible results with no generation cost',()=>{
  const shown=reuseProjection();
  const html=ui.reuseCandidatesHtml(shown,'image');
  assert.match(html,/第 3 镜/);assert.match(html,/不调用生成、不新增生成费用/);
  assert.match(html,/图片确认和生成授权不会沿用/);
  assert.match(html,/data-reuse-attempt="old-result"/);
  const blocked=ui.reuseCandidatesHtml(reuseProjection({eligible:false}),'image');
  assert.match(blocked,/原结果 &lt;missing&gt; 尚待核实/);
  assert.doesNotMatch(blocked,/data-reuse-attempt/);
  assert.equal(ui.reuseCandidatesHtml(shown,'video'),'');
});

test('reuse click binds original instructions to the accurate current script and replaces only the current shot input',async()=>{
  const {body,requests}=await controller(reuseProjection());
  const button=body.querySelector('[data-reuse-attempt]');assert.ok(button);
  body.listeners.click({target:button});body.listeners.click({target:button});
  const sent=requests.filter(row=>row.method==='POST');assert.equal(sent.length,1);
  assert.equal(sent[0].path,'/api/runs/design-demo/production/drafts');
  assert.equal(sent[0].body.source_version,5);
  assert.equal(sent[0].body.source_sha256,'f'.repeat(64));
  assert.equal(sent[0].body.prompt,image.shot_prompt);
  assert.equal(sent[0].body.shot_id,'shot-01');
  assert.equal(sent[0].body.supersedes,'current-input');
  assert.equal(sent[0].body.reuse_attempt_id,'old-result');
  assert.deepEqual(sent[0].body.references,refs);
  assert.notEqual(sent[0].body.request_id,'old-request');
  for(const key of ['nonce','consent','authorization_id','compiled_prompt','design_binding'])assert.ok(!(key in sent[0].body));
});

test('reuse without a current draft drops the historical supersedes identity and cannot submit while another task runs',async()=>{
  const app=await controller(reuseProjection({current:false}));
  app.body.listeners.click({target:app.body.querySelector('[data-reuse-attempt]')});
  const sent=app.requests.find(row=>row.method==='POST');assert.ok(sent);
  assert.ok(!('supersedes' in sent.body));
  const active=await controller(reuseProjection({active:true}));
  const disabled=active.body.querySelector('[data-reuse-attempt]');assert.equal(disabled.disabled,true);
  active.body.listeners.click({target:disabled});
  assert.equal(active.requests.filter(row=>row.method==='POST').length,0);
});

function videoReuseProjection(){
  const oldReference={name:'original-first.png',sha256:'7'.repeat(64),purpose:'continuity'};
  const currentReference={...oldReference,name:'current-reused-first.png'};
  const old={...video,draft_id:'video-old',source_ready:false,is_current:false,references:[oldReference],
    request_payload:{request_id:'historical-request',kind:'video',references:[oldReference],
      source_version:4,source_sha256:scriptHash,model:video.model,duration:video.duration,resolution:video.resolution,
      nonce:'must-not-inherit',authorization_id:'must-not-inherit',consent:true}};
  return {...data,drafts:[old],sources:[{version:5,artifact_sha256:'f'.repeat(64),ready:true}],
    materials:[...materials,{...currentReference,kind:'image',approved:false,
      url:'/api/runs/design-demo/materials/current-reused-first.png'}],
    targets:{video:{shots:[{shot_id:'shot-01',ordinal:2}]}},
    reuse_candidates:[{kind:'video',shot_id:'shot-01',source_draft_id:'video-old',source_attempt_id:'old-clip',
      source_version:5,source_sha256:'f'.repeat(64),reuse_reference:currentReference,
      eligible:true,changed_dependencies:[]}]};
}

test('video reuse click binds the backend current first-frame identity without inheriting its approval or cost consent',async()=>{
  const shown=videoReuseProjection();
  const app=await controller(shown);
  const button=app.body.querySelector('[data-reuse-attempt]');assert.ok(button);
  app.body.listeners.click({target:button});
  const sent=app.requests.find(row=>row.method==='POST');assert.ok(sent);
  assert.equal(sent.path,'/api/runs/design-demo/production/drafts');
  assert.deepEqual(sent.body.references,[shown.reuse_candidates[0].reuse_reference]);
  assert.equal(sent.body.source_version,5);assert.equal(sent.body.source_sha256,'f'.repeat(64));
  assert.equal(sent.body.reuse_attempt_id,'old-clip');
  assert.equal(sent.body.model,video.model);assert.equal(sent.body.duration,video.duration);
  for(const key of ['reference','nonce','consent','authorization_id','supersedes'])assert.ok(!(key in sent.body));
  assert.equal(shown.materials.at(-1).approved,false);
});

for(const problem of ['missing_reference','changed_reference','missing_bytes','stale_source','changed_dependencies']){
  test(`video reuse refuses ${problem} at the real button without fallback generation`,async()=>{
    const shown=videoReuseProjection(),candidate=shown.reuse_candidates[0];
    if(problem==='missing_reference')delete candidate.reuse_reference;
    if(problem==='changed_reference'){
      candidate.reuse_reference.sha256='8'.repeat(64);shown.materials.at(-1).sha256='8'.repeat(64);
    }
    if(problem==='missing_bytes')shown.materials.pop();
    if(problem==='stale_source')candidate.source_sha256='e'.repeat(64);
    if(problem==='changed_dependencies')candidate.changed_dependencies=['words'];
    const app=await controller(shown),button=app.body.querySelector('[data-reuse-attempt]');
    app.body.listeners.click({target:button});
    assert.equal(app.requests.filter(row=>row.method==='POST').length,0);
    assert.notEqual(app.body.parentNode.querySelector('[data-error]').textContent,'');
  });
}


for(const projection of ['known','missing_ordinal','ambiguous_ordinal','other_project','unknown_format']){
  test(`single photo controller names only the verified page and keeps consent: ${projection}`,async()=>{
    const rows=[{draft_id:image.draft_id,shot_id:image.shot_id,ordinal:2}];
    if(projection==='missing_ordinal')delete rows[0].ordinal;
    if(projection==='ambiguous_ordinal')rows.push({...rows[0]});
    const shown={...data,sources:[{version:4,artifact_sha256:scriptHash,ready:true}],
      shots:{image:{complete_list:true,shots:rows}}};
    const timeline={run:{run_id:projection==='other_project'?'another-project':'design-demo',
      content_format:projection==='unknown_format'?undefined:'photo'}};
    const before=structuredClone(shown);
    const {body,confirm,requests}=await controller(shown,timeline);
    const photo=!['other_project','unknown_format'].includes(projection);
    if(photo){
      assert.match(body.innerHTML,/本页图片待生成|图文方案第 4 版/);
      assert.doesNotMatch(body.innerHTML,/首帧待生成|本镜采用的共享设计/);
      assert.match(body.innerHTML,projection==='known'?/第 02 页图片/:/图文图片（页序未提供）/);
    }else{
      assert.match(body.innerHTML,/首帧 02/);
      assert.match(body.innerHTML,/首帧待生成|脚本第 4 版/);
    }
    body.listeners.click({target:body.querySelector('[data-generate]')});
    assert.match(confirm.innerHTML,photo?projection==='known'?/生成第 02 页图片/:/生成图文图片/:/启动一个生成任务/);
    assert.match(confirm.innerHTML,photo?/本页采用的共享设计/:/本镜采用的共享设计/);
    assert.equal(confirm.querySelector('[data-authorize]').disabled,true);
    assert.equal(confirm.querySelectorAll('.production-reference-input img').length,4);
    assert.deepEqual(shown,before);
    assert.ok(requests.every(request=>request.method==='GET'));
  });
}

test('photo labels leave historical inputs, video consent and original prompts unchanged',()=>{
  const original=structuredClone(image),shown={...data,content_format:'photo',shots:{image:{shots:[{draft_id:image.draft_id,ordinal:2}]}}};
  assert.match(ui.confirmationHtml(image,shown,'design-demo'),/生成第 02 页图片/);
  assert.match(ui.draftHtml(image,shown,'design-demo'),/本页图片待生成/);
  assert.match(ui.confirmationHtml({...image,is_current:false},shown,'design-demo'),/启动一个生成任务/);
  assert.match(ui.draftHtml({...image,is_current:false},shown,'design-demo'),/首帧待生成/);
  assert.match(ui.confirmationHtml(video,{...shown,drafts:[video]},'design-demo'),/脚本第 4 版/);
  assert.doesNotMatch(ui.confirmationHtml(video,{...shown,drafts:[video]},'design-demo'),/生成图文图片|本页采用/);
  assert.deepEqual(image,original);
});


test('photo reuse shows distinct original pixels and input versions for multiple results of one page',()=>{
  const shown=reuseProjection();
  const first=shown.reuse_candidates[0];first.output={name:'old-picture.png',sha256:'a'.repeat(64)};
  shown.materials.push({kind:'image',...first.output,url:'/api/runs/design-demo/materials/old-picture.png'});
  const second={...first,source_draft_id:'newer-input',source_attempt_id:'newer-result',output:{name:'newer-picture.png',sha256:'b'.repeat(64)}};
  shown.reuse_candidates.push(second);shown.drafts.push({...shown.drafts[0],draft_id:'newer-input',source_version:3});
  shown.materials.push({kind:'image',...second.output,url:'/api/runs/design-demo/materials/newer-picture.png'});
  const html=ui.reuseCandidatesHtml(shown,'image','design-demo','photo');
  assert.match(html,/沿用未受修改影响的页面图片/);
  assert.match(html,/第 03 页图片/);assert.match(html,/原图文方案第 4 版/);assert.match(html,/原图文方案第 3 版/);
  for(const name of ['old-picture.png','newer-picture.png'])assert.ok(html.includes(`src="/api/runs/design-demo/materials/${name}"`));
  for(const id of ['old-input','newer-input'])assert.ok(html.includes(id));
  for(const id of ['old-result','newer-result'])assert.ok(html.includes(`data-reuse-attempt="${id}"`));
  assert.equal((html.match(/沿用这张图片/g)||[]).length,2);
  assert.match(html,/图片确认和生成授权不会沿用/);
});

test('reuse previews require exact output bytes and same project URLs',()=>{
  for(const problem of ['hash','other-run','duplicate']){
    const shown=reuseProjection(),row=shown.reuse_candidates[0];
    row.output={name:'old.png',sha256:'a'.repeat(64)};
    const material={kind:'image',...row.output,url:'/api/runs/design-demo/materials/old.png'};
    if(problem==='hash')material.sha256='b'.repeat(64);
    if(problem==='other-run')material.url='/api/runs/other-demo/materials/old.png';
    shown.materials.push(material);if(problem==='duplicate')shown.materials.push({...material});
    const html=ui.reuseCandidatesHtml(shown,'image','design-demo','photo');
    assert.doesNotMatch(html,/<img|<a href=/);assert.match(html,/预览暂不可核对/);
  }
});

test('reuse photo labels require unique trusted current page order and keep original values escaped',()=>{
  const shown=reuseProjection();shown.targets.image.shots.push({...shown.targets.image.shots[0]});
  shown.reuse_candidates[0].output={name:'<private>.png',sha256:'a'.repeat(64)};
  shown.drafts[0].draft_id='<old-input>';shown.reuse_candidates[0].source_draft_id='<old-input>';
  const html=ui.reuseCandidatesHtml(shown,'image','design-demo','photo');
  assert.match(html,/原图片（当前页序未核对）/);assert.doesNotMatch(html,/第 03 页/);
  assert.match(html,/&lt;private&gt;\.png/);assert.match(html,/&lt;old-input&gt;/);
  const generic=ui.reuseCandidatesHtml(reuseProjection(),'image','design-demo',undefined);
  assert.match(generic,/第 3 镜/);assert.doesNotMatch(generic,/图文方案|第 03 页/);
});
