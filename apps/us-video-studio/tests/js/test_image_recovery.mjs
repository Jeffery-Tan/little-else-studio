/* Shipped recovery controller, with explicit offline DOM/HTTP adapters. */
import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {createRequire} from 'node:module';
import {El, loadApp, makeDom, webSource} from './dom-fixture.mjs';

const ui=createRequire(import.meta.url)('../../studio/web/production.js');
const RUN='image-recovery-fixture',SHA='a'.repeat(64),RECOVERY='e'.repeat(64);
const flush=async()=>{for(let i=0;i<5;i++)await new Promise(resolve=>setImmediate(resolve));};
if(!Object.getOwnPropertyDescriptor(El.prototype,'elements'))
  Object.defineProperty(El.prototype,'elements',{get(){return {namedItem:name=>this.querySelector(`[name="${name}"]`)};}});
class LocalFormData {
  constructor(form){this.rows=form.querySelectorAll('[name]').filter(node=>
    node.getAttribute('type')!=='radio'||node.checked).map(node=>[node.getAttribute('name'),node.value]);}
  [Symbol.iterator](){return this.rows[Symbol.iterator]();}
  get(name){return this.rows.find(row=>row[0]===name)?.[1]??null;}
  has(name){return this.rows.some(row=>row[0]===name);}
}
function scene({retained=true,projected=true,modes}={}){
  const output={name:'retained.png',sha256:SHA,url:`/api/runs/${RUN}/materials/retained.png`};
  const attempt={attempt_id:'original-image-attempt',draft_id:'exact-image-input',media_kind:'image',
    status:'unknown',provider_outcome:'unknown',failure_code:'cancelled',observed_image_calls:1,
    usage_status:'unknown',...(retained?{output,output_available:true}:{})};
  if(projected)attempt.recovery={modes:modes??(retained?['retry_with_unknown_cost']:['not_submitted','retry_with_unknown_cost']),
    attempt_sha256:RECOVERY,has_retained_output:retained,reason:''};
  const draft={draft_id:attempt.draft_id,draft_sha256:'b'.repeat(64),kind:'image',shot_id:'shot-second',
    model:'codex-image',prompt:'Synthetic exact image input.',shot_prompt:'Synthetic exact image input.',
    source_version:4,source_sha256:SHA,source_ready:true,is_current:true,references:[],attempts:[attempt]};
  return {nonce:'original-page-nonce',drafts:[draft],active:false,recovery_modes:['retry_with_unknown_cost'],
    unresolved_attempts:[{kind:'image',shot_id:draft.shot_id,ordinal:2,draft_id:draft.draft_id,
      attempt_id:attempt.attempt_id,status:'unknown'}],
    materials:retained?[{...output,kind:'image',approved:false}]:[],
    sources:[{version:4,artifact_sha256:SHA,ready:true}],
    capabilities:{image:{available:true},video:{available:false,models:[]}},
    shots:{image:{complete_list:true,total:1,shots:[{draft_id:draft.draft_id,shot_id:draft.shot_id,
      ordinal:2,draft_sha256:draft.draft_sha256,source_ready:true,stage:'unverified'}]}}};
}
async function page({data=scene(),reply}={}){
  const dom=makeDom({body:webSource('index.html').match(/<body[^>]*>([\s\S]*?)<\/body>/)[1]});
  for(const node of dom.document.querySelectorAll('[id]'))dom.document.__ids.set(node.getAttribute('id'),node);
  const create=dom.document.createElement;
  dom.document.createElement=tag=>{const node=create(tag);if(tag==='dialog'){
    node.showModal=()=>node.open=true;node.close=()=>node.open=false;}return node;};
  const app=loadApp({dom,routes:{'/production/attempts/original-image-attempt/recover':request=>{
    if(reply)return reply(request,data);
    data.drafts[0].attempts[0].status='retry_authorized';data.unresolved_attempts=[];
    data.shots.image.shots[0].stage='failed';
    return {recovered:true,new_generation:false,usage_still_unknown:true};
  },'/production':()=>data,'/api/learning':{cases:[]}},globals:{
    FormData:LocalFormData,initStudioNavigation(){},setStudioView(){},renderProjects(){},loadInspiration(){},
    setTimeout:()=>0,clearTimeout(){},requestAnimationFrame:fn=>fn(),scrollDown(){},
    __submitJob:()=>{throw Error('Recording a recovery decision cannot call a model');},
  }});
  vm.runInContext(webSource('production.js'),app.ctx,{filename:'production.js'});await flush();
  app.ctx.fixtureRun=RUN;vm.runInContext('state.runId=fixtureRun;state.currentJob=null;state.rail=[];',app.ctx);
  await app.ctx.openProduction({kind:'image',focusDraft:'exact-image-input'});
  const dialog=app.document.querySelector('.production-dialog'),body=dialog.querySelector('[data-body]');
  return {...app,data,dialog,body,open:()=>body.listeners.click({target:body.querySelector('[data-recover]')}),
    store:()=>JSON.parse(app.storage.getItem(`studio-production:${RUN}`))};
}

test('only the exact projected retained-image recovery is offered, without a zero-cost claim',async()=>{
  const p=await page();p.open();
  const confirm=p.dialog.querySelector('[data-confirm]');
  assert.equal(confirm.querySelector('[value="not_submitted"]'),null);
  assert.ok(confirm.querySelector('[value="retry_with_unknown_cost"]'));
  assert.match(confirm.textContent,/已保留一张图片|没有完整结束回执|不会立刻再次生成|未知费用继续保留/);
  assert.equal(p.observed.filter(row=>row.method==='POST').length,0);
  const form=confirm.querySelector('form');form.querySelector('[value="retry_with_unknown_cost"]').checked=true;
  form.querySelector('textarea').value='Synthetic explicit user accepts old uncertainty.';
  form.onsubmit({preventDefault(){}});await flush();
  const posts=p.observed.filter(row=>row.method==='POST');assert.equal(posts.length,1);
  assert.match(posts[0].path,/\/attempts\/original-image-attempt\/recover$/);
  assert.equal(posts[0].body.expected_attempt_sha256,RECOVERY);assert.ok(posts[0].body.request_id);
  assert.equal(posts[0].body.retry_with_unknown_cost,true);assert.equal(posts[0].body.not_submitted,undefined);
  assert.equal(posts[0].body.quantity,undefined);assert.equal(p.store().pending,null);
  assert.equal(p.data.drafts[0].attempts[0].provider_outcome,'unknown');
  assert.equal(p.data.drafts[0].attempts[0].usage_status,'unknown');
  assert.ok(p.observed.every(row=>!row.path.includes('/generate')&&!row.path.includes('/jobs')));
});

test('missing or unreadable retained-output affordance offers only rereading the original record',async()=>{
  for(const data of [scene({projected:false}),scene({modes:[]})]){
    const p=await page({data});p.open();const confirm=p.dialog.querySelector('[data-confirm]');
    assert.equal(confirm.querySelector('form'),null);assert.equal(confirm.querySelector('[value="not_submitted"]'),null);
    assert.equal(confirm.querySelector('[value="retry_with_unknown_cost"]'),null);
    const before=p.observed.length;confirm.querySelector('[data-read-recovery]').onclick();await flush();
    assert.ok(p.observed.length>before);assert.ok(p.observed.slice(before).every(row=>row.method==='GET'));
  }
});

test('no-output older-service recovery stays compatible without inventing a retained-file authority',async()=>{
  const p=await page({data:scene({retained:false,projected:false})});p.open();
  const confirm=p.dialog.querySelector('[data-confirm]');
  assert.ok(confirm.querySelector('[value="not_submitted"]'));
  assert.ok(confirm.querySelector('[value="retry_with_unknown_cost"]'));
  assert.doesNotMatch(confirm.textContent,/已保留一张图片/);
});

test('an unknown recovery POST keeps exactly the same decision request for explicit verification',async()=>{
  const p=await page({reply:()=>{throw new Error('Failed to fetch');}});p.open();
  const form=p.dialog.querySelector('[data-confirm]').querySelector('form');
  form.querySelector('[value="retry_with_unknown_cost"]').checked=true;
  form.querySelector('textarea').value='Synthetic frozen recovery decision.';
  form.onsubmit({preventDefault(){}});await flush();
  const first=p.observed.find(row=>row.method==='POST');
  assert.deepEqual(p.store().pending.body,first.body);
  p.dialog.querySelector('[data-retry]').onclick();await flush();
  const posts=p.observed.filter(row=>row.method==='POST');assert.equal(posts.length,2);
  assert.deepEqual(posts[1],first);
  assert.ok(posts.every(row=>!row.path.includes('/generate')));
});

test('a stale project cannot submit the original recovery form',async()=>{
  const p=await page();p.open();const form=p.dialog.querySelector('[data-confirm]').querySelector('form');
  form.querySelector('[value="retry_with_unknown_cost"]').checked=true;
  form.querySelector('textarea').value='Synthetic old-project decision.';
  vm.runInContext('state.runId="different-project";',p.ctx);
  form.onsubmit({preventDefault(){}});await flush();
  assert.equal(p.observed.filter(row=>row.method==='POST').length,0);
});

test('a retained file or invalid projection never offers the unsubmitted decision',()=>{
  for(const mutate of [
    a=>a.recovery.modes=['not_submitted','retry_with_unknown_cost'],
    a=>a.recovery.attempt_sha256='invalid',
    a=>a.status='succeeded',
    a=>a.task_id='unexpected-video-task',
  ]){
    const data=scene(),attempt=data.drafts[0].attempts[0];mutate(attempt);
    assert.ok(!ui.imageRecoveryOptions(attempt,data).modes.includes('not_submitted'));
  }
});

test('a recorded unknown-result recovery keeps its retained file read only in current and historical records',()=>{
  const data=scene(),draft=data.drafts[0],attempt=draft.attempts[0];
  attempt.status='retry_authorized';attempt.recovery.modes=[];data.unresolved_attempts=[];
  for(const html of [ui.draftHtml(draft,data,RUN),ui.historyDraftHtml(draft,data,RUN)]){
    assert.match(html,/保留文件 · 原结果未确认；仅供查看，恢复决定不代表图审通过/);
    assert.match(html,/alt="保留文件/);
    assert.doesNotMatch(html,/data-image="retained.png"|data-approve|这张图片已取回，等待你核对并确认/);
  }
});


test('the recovered retained-image card separates the original unknown result from a new unsubmitted generation',()=>{
  const data=scene(),draft=data.drafts[0],attempt=draft.attempts[0];
  attempt.status='retry_authorized';data.unresolved_attempts=[];
  const html=ui.draftHtml(draft,data,RUN);
  assert.match(html,/<h3>原任务图片已保留<\/h3>/);
  assert.match(html,/完整结果与费用仍未确认/);
  assert.match(html,/下一次生成尚未提交/);
  assert.match(html,/须另行核对输入与费用并由你独立确认/);
  assert.match(html,/data-generate="exact-image-input">核对并生成新图片/);
  assert.doesNotMatch(html,/首帧待生成|本次画面尚未生成|data-image="retained.png"|data-approve/);
  assert.equal(attempt.status,'retry_authorized');assert.equal(attempt.provider_outcome,'unknown');
  for(const mutate of [a=>a.status='not_submitted',a=>a.provider_outcome='completed',a=>delete a.output,a=>a.output_available=false]){
    const normal=structuredClone(data);mutate(normal.drafts[0].attempts[0]);
    const ordinary=ui.draftHtml(normal.drafts[0],normal,RUN);
    assert.match(ordinary,/<h3>首帧待生成<\/h3>/);
    assert.match(ordinary,/data-generate="exact-image-input">核对并生成图片/);
    assert.doesNotMatch(ordinary,/核对并生成新图片/);
  }
});
