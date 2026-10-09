/* Shipped media rendering, delegated clicks and composer handoff. The local
 * DOM adapter models modal focus blocking and the queued close event; it has
 * no layout or media decoding. All HTTP and production data are offline. */
import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {El, loadApp, makeDom, webSource} from './dom-fixture.mjs';

const RUN='feedback-project', SHA='a'.repeat(64), OUTPUT_SHA='b'.repeat(64);

function rootDocument(node) {
  while(node.parentNode)node=node.parentNode;
  return node.ownerDocument;
}
El.prototype.focus=function() {
  const document=rootDocument(this);
  if(!document)return;
  const modal=document.querySelectorAll('dialog').find(node=>node.open);
  if(modal&&!modal.contains(this))return;
  document.activeElement=this;
};
El.prototype.addEventListener=function(type, listener) {
  const list=(this.eventListeners||=new Map());
  if(!list.has(type))list.set(type,[]);
  list.get(type).push(listener);
  (this.listeners||={})[type]=event=>{
    for(const handler of list.get(type))handler(event);
  };
};
Object.defineProperty(El.prototype,'isConnected',{get() {
  let node=this;
  while(node.parentNode){
    if(!node.parentNode.children.includes(node))return false;
    node=node.parentNode;
  }
  return Boolean(node.ownerDocument);
}});
Object.defineProperty(El.prototype,'elements',{get() {
  return {namedItem:name=>this.querySelector(`[name="${name}"]`)};
}});

async function page({kind='image', embedded=false, failed=false, historyCount=0,originalTask=false,format=null,timelineRun=RUN}={}) {
  const output={name:kind==='image'?'result.png':'result.mp4',kind,sha256:OUTPUT_SHA,
    url:`/api/runs/${RUN}/materials/${kind==='image'?'result.png':'result.mp4'}`};
  const attempt={attempt_id:'shown-result',draft_id:'shown-draft',status:'succeeded',output};
  const draft={draft_id:'shown-draft',draft_sha256:SHA,kind,source_version:2,
    source_sha256:SHA,source_ready:true,shot_id:'shot-01',is_current:true,
    prompt:'Offline production input',model:kind==='image'?'codex-image':'wan3.0-video',
    review:{verdict:'ready'},references:[],attempts:[attempt]};
  if(failed)draft.attempts.push({attempt_id:'failed-new-attempt',draft_id:draft.draft_id,status:'failed',message:'Offline failure'});
  const history=Array.from({length:historyCount},(_,index)=>({...draft,
    draft_id:`historical-${index+1}`,source_version:1,source_sha256:'c'.repeat(64),
    source_ready:false,is_current:false,attempts:[]}));
  const current=historyCount?[draft,...Array.from({length:4},(_,index)=>({...draft,
    draft_id:`current-${index+2}`,shot_id:`shot-0${index+2}`,attempts:[]}))]:[draft];
  const data={drafts:[...history,...current],sources:[{version:2,artifact_sha256:SHA,ready:true}],
    shots:{[kind]:{shots:current.map((row,index)=>({ordinal:index+1,shot_id:row.shot_id,
      draft_id:row.draft_id,draft_sha256:row.draft_sha256,source_ready:true}))}},
    materials:[output],capabilities:{image:{available:true},video:{available:true,models:[]}},active:false};
  if(originalTask){
    const replacement={...draft,draft_id:'replacement-image-input',attempts:[]};
    draft.source_ready=false;draft.is_current=false;draft.design_issue='Historical input needs design';
    draft.attempts=[{attempt_id:'original-task',status:'unknown',task_id:'provider-original',output},
      {attempt_id:'later-failed',status:'failed'}];
    data.drafts=[draft,replacement];
    data.shots.image.shots=[{ordinal:8,shot_id:draft.shot_id,draft_id:replacement.draft_id,
      attempt_id:'original-task',attempt_status:'unknown'}];
    data.unresolved_attempts=[{kind:'image',draft_id:draft.draft_id,shot_id:draft.shot_id,ordinal:8,
      attempt_id:'original-task',status:'unknown',task_id:'provider-original'}];
  }
  const closeEvents=[],timers=new Map();let nextTimer=0;
  const dom=makeDom({body:webSource('index.html').match(/<body[^>]*>([\s\S]*?)<\/body>/)[1]});
  const {document}=dom;
  for(const node of document.querySelectorAll('[id]'))document.__ids.set(node.getAttribute('id'),node);
  const create=document.createElement;
  document.createElement=tag=>{
    const node=create(tag);
    if(tag==='dialog'){
      node.showModal=()=>{node.open=true;node.previousFocus=document.activeElement;node.focus();};
      node.close=()=>{
        if(!node.open)return;
        node.open=false;node.previousFocus?.focus();
        closeEvents.push(()=>node.listeners.close?.({target:node}));
      };
    }
    return node;
  };
  const app=loadApp({dom,routes:{'/attempts/original-task/refresh':()=>{
      draft.attempts[0].status='failed';data.unresolved_attempts=[];
      return {run_id:RUN,job_id:'original-refresh-job',job_kind:'production_refresh',
        status:'succeeded',events:[]};
    },'/production':()=>data,'/api/learning':{cases:[]}},globals:{
    initStudioNavigation(){},setStudioView(){},renderProjects(){},loadInspiration(){},
    setTimeout:fn=>{timers.set(++nextTimer,fn);return nextTimer;},clearTimeout:id=>timers.delete(id),
    requestAnimationFrame:fn=>fn(),scrollDown(){},
    __submitJob:()=>{throw new Error('Feedback selection cannot start a model job');},
  }});
  vm.runInContext(webSource('progress.js'),app.ctx,{filename:'progress.js'});
  vm.runInContext(webSource('production.js'),app.ctx,{filename:'production.js'});
  for(let i=0;i<3;i++)await new Promise(resolve=>setImmediate(resolve));
  app.ctx.__data=data;
  vm.runInContext(`state.runId='${RUN}';state.productionData=__data;state.rail=[];state.timeline={run:{run_id:${JSON.stringify(timelineRun)},content_format:${JSON.stringify(format)}}};`,app.ctx);
  const opener=document.createElement('button');document.body.append(opener);opener.focus();
  const input=document.getElementById('chat-input');input.value='  My unsent feedback\nkeep it  ';
  app.ctx.setConversationOpen(false);
  let mount;
  if(embedded){mount=document.createElement('section');document.body.append(mount);}
  await app.ctx.openProduction({kind,mount,focusDraft:originalTask?'replacement-image-input':draft.draft_id});
  const dialog=document.querySelector('.production-dialog');
  const body=dialog.querySelector('[data-body]');
  assert.equal(dialog.querySelector('[data-error]').textContent,'','production controller must render without fixture errors');
  return {...app,data,attempt,draft,dialog,body,opener,input,timers,
    read:expression=>vm.runInContext(expression,app.ctx),
    click:selector=>{
      const button=body.querySelector(selector);assert.ok(button,`missing rendered button ${selector}`);
      body.listeners.click({target:button,preventDefault(){}});
    },flushClose:()=>{for(const fn of closeEvents.splice(0))fn();}};
}

function assertComposer(app) {
  assert.equal(app.document.getElementById('conversation-body').hidden,false);
  assert.equal(app.document.getElementById('conversation-toggle').getAttribute('aria-expanded'),'true');
  assert.equal(app.document.getElementById('project-workspace').dataset.mobileFocus,'chat');
  assert.equal(app.document.getElementById('feedback-target').hidden,false);
  assert.ok(app.document.activeElement===app.input,'focus stays on the feedback composer');
  assert.equal(app.input.value,'  My unsent feedback\nkeep it  ');
  assert.deepEqual(app.observed.filter(call=>call.method!=='GET'),[]);
}

test('the real production controller queries the original replaced task and never opens a generation action',async()=>{
  const app=await page({originalTask:true});
  assert.match(app.body.textContent,/镜头 08|这次生成结果还未确认/);
  assert.ok(app.body.querySelector('[data-refresh="original-task"]'));
  assert.equal(app.body.querySelector('[data-generate]'),null);
  assert.equal(app.body.querySelector('[data-recover="later-failed"]'),null);
  assert.match(app.body.textContent,/保留文件 · 原结果未确认/);
  assert.equal(app.body.querySelector('[data-image="result.png"]'),null,'uncertain bytes stay read-only');
  app.click('[data-refresh="original-task"]');
  for(let i=0;i<6;i++)await new Promise(resolve=>setImmediate(resolve));
  const writes=app.observed.filter(call=>call.method!=='GET');
  assert.equal(writes.length,1);
  assert.equal(writes[0].path,`/api/runs/${RUN}/production/attempts/original-task/refresh`);
  assert.ok(!app.body.querySelector('[data-refresh="original-task"]'));
  assert.ok(app.body.querySelector('[data-generate="replacement-image-input"]'),
    'a verified settled task restores the normal generation confirmation entry');
});

test('image feedback closes the real production modal and keeps focus on the pinned composer after its close event',async()=>{
  const app=await page();
  app.click('[data-feedback-attempt]');
  assert.equal(app.dialog.open,false,'the dialog must stop blocking the composer');
  assertComposer(app);app.flushClose();assertComposer(app);
  assert.equal(app.document.querySelector('.production-dialog'),null);
  assert.equal(app.timers.size,0,'closing stops local production polling');
  assert.deepEqual(JSON.parse(app.read('JSON.stringify(state.feedbackTarget)')),
    {kind:'media',run_id:RUN,attempt_id:'shown-result',output_sha256:OUTPUT_SHA});
  assert.match(app.document.getElementById('feedback-target').textContent,/首帧 01/);
});

test('clicking the displayed image feedback replaces an earlier role destination and keeps the unsent text',async()=>{
  const app=await page();
  app.read(`state.roleTarget={run_id:'${RUN}',session_id:'writer-running',label:'编剧'};
    state.clarificationChoices.set('${RUN}',{id:'old-question'});`);
  app.click('[data-feedback-attempt]');app.flushClose();assertComposer(app);
  assert.equal(app.read('state.roleTarget'),null);
  assert.equal(app.read(`state.clarificationChoices.has('${RUN}')`),false);
  assert.equal(app.read('state.feedbackTarget.attempt_id'),'shown-result');
  assert.doesNotMatch(app.document.getElementById('feedback-target').textContent,/补充给编剧/);
});

test('image feedback uses the current first shot after fourteen historical inputs without changing its target',async()=>{
  const app=await page({historyCount:14});
  assert.equal(app.data.drafts.length,19);
  assert.equal(app.data.drafts.indexOf(app.draft),14);
  app.click('[data-feedback-attempt]');app.flushClose();assertComposer(app);
  const chip=app.document.getElementById('feedback-target').textContent;
  assert.match(chip,/正在批注首帧 01 的生成图/);assert.doesNotMatch(chip,/首帧 15/);
  assert.deepEqual(JSON.parse(app.read('JSON.stringify(state.feedbackTarget)')),
    {kind:'media',run_id:RUN,attempt_id:'shown-result',output_sha256:OUTPUT_SHA});
});

test('production input feedback uses the backend current shot ordinal with history present',async()=>{
  const app=await page({historyCount:14,failed:true});
  app.click('[data-production-feedback]');app.flushClose();assertComposer(app);
  const chip=app.document.getElementById('feedback-target').textContent;
  assert.match(chip,/正在修改首帧 01 的出图描述/);assert.doesNotMatch(chip,/首帧 15/);
  assert.equal(app.read('state.feedbackTarget.draft_id'),'shown-draft');
  assert.equal(app.read('state.feedbackTarget.attempt_id'),'failed-new-attempt');
});

for(const target of ['media','input'])test(`photo ${target} feedback shows the server page ordinal and keeps its exact identity`,async()=>{
  const app=await page({format:'photo',historyCount:14,failed:true});
  app.click(target==='media'?'[data-feedback-attempt]':'[data-production-feedback]');
  app.flushClose();assertComposer(app);
  const chip=app.document.getElementById('feedback-target').textContent;
  assert.match(chip,target==='media'?/正在批注第 01 页的生成图/:/正在修改第 01 页的出图描述/);
  assert.doesNotMatch(chip,/首帧|第 15 页/);
  assert.deepEqual(JSON.parse(app.read('JSON.stringify(state.feedbackTarget)')),target==='media'
    ?{kind:'media',run_id:RUN,attempt_id:'shown-result',output_sha256:OUTPUT_SHA}
    :{kind:'production_draft',run_id:RUN,draft_id:'shown-draft',draft_sha256:SHA,attempt_id:'failed-new-attempt'});
});

for(const format of ['video',null,'unknown'])test(`non-photo format ${format} keeps the legacy image feedback label`,async()=>{
  const app=await page({format});app.click('[data-feedback-attempt]');app.flushClose();assertComposer(app);
  assert.match(app.document.getElementById('feedback-target').textContent,/正在批注首帧 01 的生成图/);
});

test('another project photo projection cannot rename the current feedback',async()=>{
  const app=await page({format:'photo',timelineRun:'another-project'});
  app.click('[data-feedback-attempt]');app.flushClose();assertComposer(app);
  assert.match(app.document.getElementById('feedback-target').textContent,/正在批注首帧 01 的生成图/);
});

test('photo historical and unverified inputs never borrow a page ordinal',async()=>{
  const app=await page({format:'photo',historyCount:14});
  app.ctx.__old=app.data.drafts[0];
  app.read(`setProductionFeedbackTarget('${RUN}',__old);`);
  assert.match(app.document.getElementById('feedback-target').textContent,/正在说明这版图片制作的问题/);
  app.data.shots.image.shots[0].draft_sha256='f'.repeat(64);
  app.click('[data-feedback-attempt]');app.flushClose();assertComposer(app);
  assert.match(app.document.getElementById('feedback-target').textContent,/正在反馈这张图片/);
  assert.doesNotMatch(app.document.getElementById('feedback-target').textContent,/第 01 页|首帧/);
});

test('historical or unverifiable shot feedback keeps exact identity and a generic label',async()=>{
  const app=await page({historyCount:14});
  const old=app.data.drafts[0],historicalAttempt={...app.attempt,attempt_id:'historical-result',draft_id:old.draft_id};
  app.ctx.__historicalAttempt=historicalAttempt;app.ctx.__historicalDraft=old;
  app.read(`setMediaFeedbackTarget('${RUN}',__historicalAttempt,'image');`);
  assert.equal(app.document.getElementById('feedback-target').textContent,'正在反馈这张图片取消');
  assert.equal(app.read('state.feedbackTarget.attempt_id'),'historical-result');
  app.read(`setProductionFeedbackTarget('${RUN}',__historicalDraft);`);
  assert.match(app.document.getElementById('feedback-target').textContent,/正在说明这版图片制作的问题/);
  assert.equal(app.read('state.feedbackTarget.draft_id'),old.draft_id);
  app.data.shots.image.shots[0].draft_sha256='f'.repeat(64);
  app.click('[data-feedback-attempt]');app.flushClose();assertComposer(app);
  assert.match(app.document.getElementById('feedback-target').textContent,/正在反馈这张图片/);
  assert.doesNotMatch(app.document.getElementById('feedback-target').textContent,/首帧/);
  assert.equal(app.read('state.feedbackTarget.attempt_id'),'shown-result');
});

test('video feedback captures the displayed player time before closing',async()=>{
  const app=await page({kind:'video'});
  app.body.querySelector('.creator-result video').currentTime=3.24;
  app.click('[data-feedback-attempt]');app.flushClose();assertComposer(app);
  assert.equal(app.dialog.open,false);
  assert.deepEqual(JSON.parse(app.read('JSON.stringify(state.feedbackTarget)')),
    {kind:'media',run_id:RUN,attempt_id:'shown-result',output_sha256:OUTPUT_SHA,at_seconds:3.2});
  assert.match(app.document.getElementById('feedback-target').textContent,/视频 3.2 秒/);
});

test('embedded image feedback opens the collapsed conversation without removing the production panel',async()=>{
  const app=await page({embedded:true});
  app.click('[data-feedback-attempt]');app.flushClose();assertComposer(app);
  assert.ok(app.document.querySelector('.production-dialog')===app.dialog);
  assert.equal(app.timers.size,1,'embedded production continues its original polling');
});

test('a previous image stays bound to its own successful attempt when the latest attempt failed',async()=>{
  const app=await page({failed:true});
  app.click('[data-feedback-attempt]');app.flushClose();assertComposer(app);
  assert.equal(app.read('state.feedbackTarget.attempt_id'),'shown-result');
  assert.equal(app.read('state.feedbackTarget.output_sha256'),OUTPUT_SHA);
});

test('describing a failed production input uses the same modal handoff and keeps exact draft and attempt',async()=>{
  const app=await page({failed:true});
  app.click('[data-production-feedback]');app.flushClose();assertComposer(app);
  assert.deepEqual(JSON.parse(app.read('JSON.stringify(state.feedbackTarget)')),
    {kind:'production_draft',run_id:RUN,draft_id:'shown-draft',draft_sha256:SHA,attempt_id:'failed-new-attempt'});
});

test('an unverifiable media result keeps the modal open and does not bind another result',async()=>{
  const app=await page();app.attempt.output.sha256='invalid';
  app.click('[data-feedback-attempt]');app.flushClose();
  assert.equal(app.dialog.open,true);assert.equal(app.read('state.feedbackTarget'),null);
  assert.equal(app.document.getElementById('conversation-body').hidden,true);
  assert.match(app.document.getElementById('toast').textContent,/无法核对/);
  assert.deepEqual(app.observed.filter(call=>call.method!=='GET'),[]);
});

test('a stale project click cannot bind feedback and a late close cannot steal another project focus',async()=>{
  const app=await page();
  vm.runInContext("state.runId='another-project';",app.ctx);
  app.click('[data-feedback-attempt]');assert.equal(app.read('state.feedbackTarget'),null);
  vm.runInContext(`state.runId='${RUN}';`,app.ctx);
  app.click('[data-feedback-attempt]');
  vm.runInContext("state.runId='another-project';",app.ctx);
  app.opener.focus();app.flushClose();
  assert.ok(app.document.activeElement===app.opener,'the late close must not steal another project focus');
  assert.deepEqual(app.observed.filter(call=>call.method!=='GET'),[]);
});

test('ordinary closing still returns focus to the opener',async()=>{
  const app=await page();
  app.dialog.querySelector('[data-close]').click();app.flushClose();
  assert.ok(app.document.activeElement===app.opener);
  assert.equal(app.document.querySelector('.production-dialog'),null);
  assert.equal(app.read('state.feedbackTarget'),null);
});
