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

test('selected script line offers an explicit edit action bound to its current field',async()=>{
  const accepted={job_id:'edit-1',job_kind:'writing_revision',status:'succeeded',events:[]};
  const app=await page({'actions/revise-writing':accepted});
  const chip=selectLine(app);
  const button=[...chip.children].find(node=>node.textContent==='按意见直接修改');
  assert.ok(button);
  app.document.getElementById('chat-input').value='只改第一句，让语气自然一点';
  await button.click();
  const post=app.observed.find(row=>row.path.endsWith('/actions/revise-writing'));
  assert.ok(post);
  assert.equal(post.body.request_kind,'writing_revision');
  assert.equal(post.body.text,'只改第一句，让语气自然一点');
  assert.equal(post.body.feedback_target.artifact_sha256,SHA);
  assert.deepEqual(Array.from(post.body.feedback_target.path),['segments',0,'line']);
  assert.equal(post.body.feedback_target.quote,'Original line');
  assert.equal(app.observed.filter(row=>row.path.endsWith('/jobs')&&row.method==='POST').length,0);
});

test('ordinary send with a selected line remains a conversation request',async()=>{
  const app=await page({'/jobs':{job_id:'chat-1',job_kind:'conversation',status:'succeeded',events:[]}});
  const chip=selectLine(app);
  assert.ok(chip.textContent.includes('Original line'));
  app.document.getElementById('chat-input').value='这句为什么这样写？';
  await app.ctx.submit();
  const post=app.observed.find(row=>row.path.endsWith('/jobs')&&row.method==='POST');
  assert.ok(post);
  assert.equal(post.body.text,'这句为什么这样写？');
  assert.equal(post.body.feedback_target.quote,'Original line');
  assert.equal(post.body.request_kind,undefined);
  assert.equal(app.observed.filter(row=>row.path.endsWith('/actions/revise-writing')).length,0);
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
  assert.equal(app.observed.filter(row=>row.path.endsWith('/actions/revise-writing')).length,0);
});

test('server version conflict explains the rejection and leaves the text for a fresh selection',async()=>{
  const conflict=Object.assign(new Error('[version_conflict] 脚本版本已变化，请重新选择。'),{status:409});
  const app=await page({'actions/revise-writing':conflict});
  const chip=selectLine(app);
  const button=[...chip.children].find(node=>node.textContent==='按意见直接修改');
  app.document.getElementById('chat-input').value='只改第一句';
  button.click();
  for(let i=0;i<4;i++) await new Promise(resolve=>setImmediate(resolve));
  assert.equal(app.observed.filter(row=>row.path.endsWith('/actions/revise-writing')).length,1);
  assert.equal(app.document.getElementById('chat-input').value,'只改第一句');
  assert.equal(chip.hidden,true);
  assert.match(app.document.getElementById('toast').textContent,/版本已变化.*重新选择/);
});

test('an older approval in the conversation names its version beside a newer script',async()=>{
  const app=await page({});
  const card=app.ctx.renderApproval({stage_label:'脚本',version:1,sentence:'你批准了这一版。'});
  assert.match(card.textContent,/你的决定 · 脚本第 1 版/);
  assert.doesNotMatch(card.textContent,/第 2 版/);
});
