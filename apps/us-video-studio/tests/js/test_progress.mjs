import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import fs from 'node:fs';
import vm from 'node:vm';
const require = createRequire(import.meta.url);
global.escapeHtml = require('../../studio/web/md.js').escapeHtml;
const {progressHtml, deliveryHtml, projectBriefHtml} = require('../../studio/web/progress.js');

test('shared brief exposes the bound next step without claiming execution means approval',()=>{
  const html=projectBriefHtml({phase:'review_failed',title:'创意方向第 3 版 · 审核未完成',
    detail:'方案已保留',stage:'direction',action:{kind:'review_stage',label:'重试本版审核'}});
  assert.match(html,/审核未完成/);assert.match(html,/重试本版审核/);
  assert.doesNotMatch(html,/本轮完成|data-act="approve"|token/);
  assert.doesNotMatch(projectBriefHtml({title:'<img onerror=x>',detail:'<script>',action:{label:'<svg>'}}),/<img|<script|<svg/);
});

test('review action submits only the displayed version and ignores detached stale actions',async()=>{
  const button={}, view={}, calls=[];
  const host={querySelector:s=>s==='[data-brief-action]'?button:view};
  const brief={stage:'direction',title:'Saved',detail:'Waiting',action:{kind:'review_stage',label:'Review',stage:'direction',version:3,sha256:'a'.repeat(64)}};
  const state={runId:'fixture',projectBrief:brief,rail:[],timeline:{brief}};
  const ctx=vm.createContext({state,$:()=>host,escapeHtml:global.escapeHtml,crypto:{randomUUID:()=> 'review-id'},
    submitJob:async(run,payload)=>calls.push({run,payload})});
  vm.runInContext(fs.readFileSync(new URL('../../studio/web/progress.js',import.meta.url),'utf8'),ctx);
  ctx.renderProjectProgress();assert.equal(calls.length,0);
  const click=button.onclick;
  await click();assert.equal(calls[0].payload.expected_version,3);
  assert.equal(calls[0].payload.expected_artifact_sha256,'a'.repeat(64));
  assert.equal(calls[0].payload.request_kind,'stage_review');
  state.projectBrief={...brief};await click();assert.equal(calls.length,1);
});

test('waiting downstream points to actual prerequisite without submitting a model request', () => {
  const html = progressHtml({completed:1,total:4,stage:'direction',title:'等你决定',detail:'Read',action:'review',label:'阅读并决定'}, 'writing', [{stage:'direction',label:'创意方向'}]);
  assert.match(html,/先处理「创意方向」/);
  assert.match(html,/data-progress-action="review"/);
  assert.doesNotMatch(html,/data-act="approve"/);
});
test('progress and delivery escape model-controlled content', () => {
  assert.doesNotMatch(progressHtml({completed:0,total:4,title:'<img onerror=bad>',detail:'<script>',action:'draft',label:'Next'}),/<img|<script>/);
  const html = deliveryHtml({run:{run_id:'fixture',title:'<img>'},ready:false,notice:'Plan',missing:['<x>'],documents:[{label:'<img>',stage:'intake',version:1,status:'待审阅'}],packages:[]});
  assert.doesNotMatch(html,/<img>|<x>/);
  assert.match(html,/进行中/);
});
test('preparing a next action keeps existing draft and never calls a model', () => {
  const input = {value:'User unfinished thought',focus(){}};
  const ctx = vm.createContext({state:{runId:'fixture'},$(){return input;},toast(){},document:{getElementById(){return {dataset:{}};},querySelectorAll(){return [];}}});
  vm.runInContext(fs.readFileSync(new URL('../../studio/web/progress.js',import.meta.url),'utf8'),ctx);
  ctx.prepareProjectDraft('Next requirement');ctx.prepareProjectDraft('Next requirement');
  assert.equal(input.value,'User unfinished thought\n\nNext requirement');
});

test('a stale next-action button cannot put another project or version into the draft', () => {
  const button = {}, host = {querySelector(){return button;}};
  const state = {runId:'first-run',activeStage:'direction',rail:[],timeline:{progress:{completed:0,total:4,stage:'direction',action:'draft',title:'Continue',label:'Next',prompt:'First project version'}}};
  const ctx = vm.createContext({state,$(){return host;},escapeHtml:global.escapeHtml,
    selectStage(){throw new Error('stale action navigated');}});
  vm.runInContext(fs.readFileSync(new URL('../../studio/web/progress.js',import.meta.url),'utf8'),ctx);
  ctx.renderProjectProgress();
  state.runId = 'second-run'; button.onclick();
  state.runId = 'first-run'; state.timeline = {progress:state.timeline.progress}; button.onclick();
});

test('recovery arriving after a drafted-answer render preserves unsent answers', () => {
  // The drafted answers are cleared only once the request was accepted. This
  // drives the real handler body out of `app.js` with a refusing `send`, which
  // is the state a refresh mid-typing can leave behind. The end-to-end version
  // of this (including the accepted side) is in `test_gate_and_input.mjs`.
  const app = fs.readFileSync(new URL('../../studio/web/app.js', import.meta.url), 'utf8');
  const start = app.indexOf('  sendBtn.onclick = async () => {');
  const end = app.indexOf('\n  };', start);
  const button = {}, state = {runId:'fixture',busy:false,recoveryBlocked:true,draft:{answer:'Keep me'}};
  const ctx = vm.createContext({sendBtn:button,state,composeDraft:()=> 'Keep me',
    send:async()=>({accepted:false}),saveDraft(){throw new Error('draft cleared');},
    renderDraft(){},renderPane(){}});
  vm.runInContext(app.slice(start,end+5),ctx);
  return button.onclick().then(()=>assert.equal(state.draft.answer,'Keep me'));
});
