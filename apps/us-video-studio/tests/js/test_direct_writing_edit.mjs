import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {createRequire} from 'node:module';
import {loadApp} from './dom-fixture.mjs';

const require = createRequire(import.meta.url);
globalThis.escapeHtml = require('../../studio/web/md.js').escapeHtml;
globalThis.renderMarkdown = require('../../studio/web/md.js').renderMarkdown;
globalThis.blocksHtml = require('../../studio/web/blocks.js').blocksHtml;
const {stageHtml} = require('../../studio/web/stage.js');
const SHA = 'a'.repeat(64);

function writing() {
  return {stage:'writing', label:'脚本与分镜', state:'active', has_doc:true,
    version:1, artifact_sha256:SHA, integrity_ok:true, stale:false,
    blank:{state:'ready',text:''}, blocks:[
      {type:'cards',title:'逐段',cards:[{head:'0:00–0:03',fields:[
        {label:'画面',value:'Open bag'}, {label:'台词',value:'Original line'},
      ]}]},
      {type:'text',title:'发布文案',text:'A better lunch.'},
    ]};
}

async function page(routes) {
  const app=loadApp({runtime:'real',routes,globals:{setProjectFocus:()=>{}}});
  for (let i=0;i<4;i++) await new Promise(resolve=>setImmediate(resolve));
  const step=writing();
  app.ctx.__step=step;
  vm.runInContext(`
    state.runId='run-1'; state.rail=[globalThis.__step];
    state.timeline={questions:[]}; state.busy=false;
    refreshTimeline=async()=>{}; watchJob=async()=>{}; restoreJob=async()=>{};
    document.getElementById('stage').innerHTML=globalThis.__html;
    wireCreatorFeedback(document.getElementById('stage'),globalThis.__step);
  `, Object.assign(app.ctx,{__html:stageHtml(step,{contentFormat:'video'})}));
  return app;
}

function selectLine(app) {
  const node=app.document.getElementById('stage').querySelectorAll('[data-creator-path]')
    .find(item=>item.dataset.creatorTarget==='第 1 段台词');
  assert.ok(node);
  node.listeners.click();
  return app.document.getElementById('feedback-target');
}

test('an original remote video query releases the composer for a new queued conversation without steering it',async()=>{
  const app=await page({'/jobs?enqueue=true':{job_id:'new-text',job_kind:'conversation',status:'queued',events:[]}});
  vm.runInContext(`showJob({job_id:'original-video',job_kind:'production_video_generation',status:'running',lane:'query',phase:'remote_waiting',events:[]});`,app.ctx);
  assert.equal(vm.runInContext('state.busy',app.ctx),false);
  assert.equal(app.document.getElementById('send').dataset.mode,'send');
  assert.equal(app.document.getElementById('job-stop').dataset.mode,'stop-query');
  app.document.getElementById('chat-input').value='先讨论下一页，保留原视频任务';
  await vm.runInContext('submit()',app.ctx);
  const posts=app.observed.filter(row=>row.method==='POST');assert.equal(posts.length,1);
  assert.equal(posts[0].path,'/api/runs/run-1/jobs?enqueue=true');assert.equal(posts[0].body.text,'先讨论下一页，保留原视频任务');
});

function retainedLine(app) {
  const host=app.document.getElementById('stage');
  const node=host.querySelectorAll('[data-creator-path]')
    .find(item=>item.dataset.creatorTarget==='第 1 段台词');
  assert.ok(node);
  // The shared minimal fixture keeps one listener per event. This retained DOM
  // node needs browser semantics: multiple distinct listeners all fire until
  // explicitly removed, so duplicates cannot hide behind fixture overwrites.
  const listeners=new Map(Object.entries(node.listeners).map(([type,handler])=>[type,new Set([handler])]));
  node.addEventListener=(type,handler)=>{
    if(!listeners.has(type))listeners.set(type,new Set());
    listeners.get(type).add(handler);
  };
  node.removeEventListener=(type,handler)=>listeners.get(type)?.delete(handler);
  const counts={targets:0,chips:0,focus:0,toasts:[]};
  const state=vm.runInContext('state',app.ctx);
  let target=state.feedbackTarget;
  Object.defineProperty(state,'feedbackTarget',{
    configurable:true,get:()=>target,set:value=>{target=value;counts.targets++;},
  });
  const chip=app.document.getElementById('feedback-target');
  const replace=chip.replaceChildren.bind(chip);
  chip.replaceChildren=(...children)=>{counts.chips++;replace(...children);};
  app.ctx.setProjectFocus=()=>{counts.focus++;};
  const toast=app.ctx.toast;
  app.ctx.toast=(...args)=>{counts.toasts.push(args);toast(...args);};
  return {host,node,listeners,counts,state,chip,
    dispatch:(type,event={})=>{
      for(const handler of [...(listeners.get(type)||[])])handler({target:node,...event});
    }};
}

test('rebinding the retained script node after polling selects one target and renders one chip per input',async()=>{
  const app=await page({}), held=retainedLine(app);
  const before=app.observed.length;
  app.document.getElementById('chat-input').value='Unsent feedback stays here';
  for(let i=0;i<5;i++)app.ctx.wireCreatorFeedback(held.host,held.state.rail[0]);
  held.dispatch('click');
  assert.equal(held.listeners.get('click').size,1);
  assert.equal(held.listeners.get('keydown').size,1);
  assert.equal(held.counts.targets,1);
  assert.equal(held.counts.chips,1);
  assert.equal(held.counts.focus,1);
  assert.equal(held.counts.toasts.length,0);
  assert.equal(held.state.feedbackTarget.version,1);
  assert.equal(held.state.feedbackTarget.artifact_sha256,SHA);
  assert.equal(held.chip.children.length,3);
  let prevented=0;
  held.dispatch('keydown',{key:'Enter',preventDefault(){prevented++;}});
  assert.equal(prevented,1);
  assert.equal(held.counts.targets,2);
  assert.equal(held.counts.chips,2);
  assert.equal(held.counts.focus,2);
  assert.equal(held.chip.children.length,3);
  assert.equal(app.document.getElementById('chat-input').value,'Unsent feedback stays here');
  assert.equal(app.observed.length,before);
});

for(const change of ['version','hash'])test(`retained node replaces the old ${change} binding without a stale listener toast`,async()=>{
  const app=await page({}), held=retainedLine(app);
  const oldClick=[...held.listeners.get('click')][0];
  const oldKeydown=[...held.listeners.get('keydown')][0];
  const before=app.observed.length;
  const step=held.state.rail[0];
  if(change==='version')step.version=2;
  step.artifact_sha256='b'.repeat(64);
  held.dispatch('click'); // Until rebound, old displayed identity must refuse.
  assert.equal(held.counts.toasts.length,1);
  assert.match(held.counts.toasts[0][0],/版本已变化/);
  assert.equal(held.counts.targets,0);
  assert.equal(held.counts.chips,0);
  held.node.textContent='New exact displayed line';
  app.ctx.wireCreatorFeedback(held.host,step);
  app.ctx.wireCreatorFeedback(held.host,step);
  assert.equal(held.listeners.get('click').has(oldClick),false);
  assert.equal(held.listeners.get('keydown').has(oldKeydown),false);
  assert.equal(held.listeners.get('click').size,1);
  assert.equal(held.listeners.get('keydown').size,1);
  held.dispatch('click');
  assert.equal(held.counts.toasts.length,1); // Removed old handler cannot add a false conflict.
  assert.equal(held.counts.targets,1);
  assert.equal(held.counts.chips,1);
  assert.equal(held.state.feedbackTarget.version,step.version);
  assert.equal(held.state.feedbackTarget.artifact_sha256,step.artifact_sha256);
  assert.equal(held.state.feedbackTarget.quote,'New exact displayed line');
  assert.deepEqual(Array.from(held.state.feedbackTarget.path),['segments',0,'line']);
  held.dispatch('keydown',{key:' ',preventDefault(){}});
  assert.equal(held.counts.toasts.length,1);
  assert.equal(held.counts.targets,2);
  assert.equal(held.counts.chips,2);
  assert.equal(held.chip.children.length,3);
  assert.equal(app.observed.length,before);
});

test('selected script line offers an explicit edit action bound to its current field',async()=>{
  const accepted={job_id:'edit-1',job_kind:'writing_revision',status:'succeeded',events:[]};
  const app=await page({'actions/revise-writing':accepted});
  const chip=selectLine(app);
  const button=[...chip.children].find(node=>node.textContent==='按意见直接修改');
  assert.ok(button);
  app.document.getElementById('chat-input').value='只改第一句，让语气自然一点';
  await button.click();
  const post=app.observed.find(row=>row.path.endsWith('/actions/revise-writing?enqueue=true'));
  assert.ok(post);
  assert.equal(post.body.request_kind,'writing_revision');
  assert.equal(post.body.text,'只改第一句，让语气自然一点');
  assert.equal(post.body.feedback_target.artifact_sha256,SHA);
  assert.deepEqual(Array.from(post.body.feedback_target.path),['segments',0,'line']);
  assert.equal(post.body.feedback_target.quote,'Original line');
  assert.equal(app.observed.filter(row=>row.path.split('?')[0].endsWith('/jobs')&&row.method==='POST').length,0);
});

test('ordinary send with a selected line remains a conversation request',async()=>{
  const app=await page({'/jobs':{job_id:'chat-1',job_kind:'conversation',status:'succeeded',events:[]}});
  const chip=selectLine(app);
  assert.ok(chip.textContent.includes('Original line'));
  app.document.getElementById('chat-input').value='这句为什么这样写？';
  await app.ctx.submit();
  const post=app.observed.find(row=>row.path.split('?')[0].endsWith('/jobs')&&row.method==='POST');
  assert.ok(post);
  assert.equal(post.body.text,'这句为什么这样写？');
  assert.equal(post.body.feedback_target.quote,'Original line');
  assert.equal(post.body.request_kind,undefined);
  assert.equal(app.observed.filter(row=>row.path.endsWith('/actions/revise-writing?enqueue=true')).length,0);
});

test('changed script version preserves the edit text and sends no request',async()=>{
  const app=await page({'actions/revise-writing':{job_id:'should-not-run'}});
  const chip=selectLine(app);
  const button=[...chip.children].find(node=>node.textContent==='按意见直接修改');
  app.document.getElementById('chat-input').value='只改第一句';
  vm.runInContext('state.rail[0].version=2',app.ctx);
  const result=await button.click();
  assert.equal(result,undefined); // click handler intentionally does not expose the async result
  for(let i=0;i<4;i++) await new Promise(resolve=>setImmediate(resolve));
  assert.equal(app.document.getElementById('chat-input').value,'只改第一句');
  assert.equal(app.observed.filter(row=>row.path.endsWith('/actions/revise-writing?enqueue=true')).length,0);
});

test('server version conflict explains the rejection and leaves the text for a fresh selection',async()=>{
  const conflict=Object.assign(new Error('[version_conflict] 脚本版本已变化，请重新选择。'),{status:409});
  const app=await page({'actions/revise-writing':conflict});
  const chip=selectLine(app);
  const button=[...chip.children].find(node=>node.textContent==='按意见直接修改');
  app.document.getElementById('chat-input').value='只改第一句';
  button.click();
  for(let i=0;i<4;i++) await new Promise(resolve=>setImmediate(resolve));
  assert.equal(app.observed.filter(row=>row.path.endsWith('/actions/revise-writing?enqueue=true')).length,1);
  assert.equal(app.document.getElementById('chat-input').value,'只改第一句');
  assert.equal(chip.hidden,false);
  assert.equal(vm.runInContext('state.composerRequestKind',app.ctx),'writing_revision');
  assert.equal(vm.runInContext('state.feedbackTarget.artifact_sha256',app.ctx),SHA);
  assert.match(app.document.getElementById('toast').textContent,/版本已变化.*重新选择/);
  await app.ctx.submit();
  assert.equal(app.observed.filter(row=>row.path.endsWith('/actions/revise-writing?enqueue=true')).length,2);
  assert.equal(app.observed.filter(row=>row.path.split('?')[0].endsWith('/jobs')&&row.method==='POST').length,0);
});

test('an older approval in the conversation names its version beside a newer script',async()=>{
  const app=await page({});
  const card=app.ctx.renderApproval({stage_label:'脚本',version:1,sentence:'你批准了这一版。'});
  assert.match(card.textContent,/你的决定 · 脚本第 1 版/);
  assert.doesNotMatch(card.textContent,/第 2 版/);
});
