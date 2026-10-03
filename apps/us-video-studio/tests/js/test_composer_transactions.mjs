import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {File} from 'node:buffer';
import {webcrypto} from 'node:crypto';
import {loadApp, webSource, parseHtml} from './dom-fixture.mjs';

function fixture({runtime='shim', result={accepted:true}, storage}={}) {
  const jobs=[], steers=[];
  const app=loadApp({runtime, ...(storage ? {storage} : {}), globals:{
    setTimeout:()=>0, clearTimeout:()=>{}, crypto:webcrypto, FormData, File,
    __submitJob:async(runId,payload)=>{jobs.push({runId,payload});return result;},
    __submitSteer:async(runId,jobId,payload)=>{steers.push({runId,jobId,payload});return {ok:true};},
  }});
  vm.runInContext(webSource('conversation-input.js'),app.ctx);
  vm.runInContext(`
    state.runId='one'; state.busy=false; currentJob=null;
    render=()=>{}; setConversationOpen=()=>{}; setProjectFocus=()=>{};
    toggleMaterials=()=>{}; loadDraft=()=>{}; refreshTimeline=async()=>{};
    restoreJob=async()=>{currentJob=null;state.busy=false;};
    state.rail=[{stage:'writing',version:3,artifact_sha256:'a'.repeat(64)}];
    state.timeline={display_context_id:'display-one'};
    $('toast').textContent='';
  `,app.ctx);
  const run=code=>vm.runInContext(code,app.ctx);
  const writing=()=>run(`state.feedbackTarget={kind:'writing',run_id:'one',stage:'writing',version:3,
    artifact_sha256:'a'.repeat(64),quote:'Old line',path:['shots',0,'voiceover']};
    $('chat-input').value='改成用户的原话';`);
  return {...app,jobs,steers,run,writing};
}

test('busy direct revision stays a direct revision when retried with Enter',async()=>{
  const f=fixture(); f.writing();
  f.run(`state.busy=true;currentJob={job_id:'active',job_kind:'conversation',status:'running'};`);
  const refused=await f.run(`submit('writing_revision')`);
  assert.equal(refused.accepted,false);
  assert.equal(f.steers.length,0);
  assert.equal(f.run(`$('chat-input').value`),'改成用户的原话');
  assert.match(f.run(`$('toast').textContent`),/直接修改/);
  f.run(`state.busy=false;currentJob=null;`);
  await f.run('submit()');
  assert.equal(f.jobs[0].payload.request_kind,'writing_revision');
  assert.deepEqual([...f.jobs[0].payload.feedback_target.path],['shots',0,'voiceover']);
});

test('project switches restore target, direct mode and clarification with the words',async()=>{
  const f=fixture({result:{accepted:false,reason:'refused'}}); f.writing();
  f.run(`state.clarificationChoices.set('one',{id:'question-one',text:'改成用户的原话'});
    state.busy=true;currentJob={job_id:'active',job_kind:'conversation',status:'running'};`);
  await f.run(`submit('writing_revision')`);
  await f.run(`selectRun('two')`);
  assert.equal(f.run(`state.feedbackTarget`),null);
  f.run(`$('chat-input').value='Second project';`);
  await f.run(`selectRun('one')`);
  assert.equal(f.run(`$('chat-input').value`),'改成用户的原话');
  assert.equal(f.run(`state.feedbackTarget.artifact_sha256`),'a'.repeat(64));
  assert.equal(f.run(`$('feedback-target').hidden`),false);
  f.run(`state.rail=[{stage:'writing',version:3,artifact_sha256:'a'.repeat(64)}];`);
  await f.run('submit()');
  assert.equal(f.jobs[0].payload.request_kind,'writing_revision');
  assert.equal(f.jobs[0].payload.question_id,'question-one');
  await f.run(`selectRun('two')`);
  assert.equal(f.run(`$('chat-input').value`),'Second project');
});

test('accepted upload plus rejected conversation keeps attachment use and skips re-upload',async()=>{
  const f=fixture({result:{accepted:false,reason:'request refused'}});
  let uploads=0;
  f.ctx.fetch=async()=>{uploads++;return {ok:true};};
  f.ctx.fixtureFile=new File(['bytes'],'reference.mp4',{type:'video/mp4'});
  f.run(`StudioComposer.setFiles('one',[fixtureFile],'reference');$('chat-input').value='分析这个视频';`);
  await f.run('submit()');
  assert.equal(f.run(`StudioComposer.files('one').length`),1);
  assert.equal(f.run(`StudioComposer.missingVideoUse('one')`),false);
  assert.equal(f.run(`$('chat-input').value`),'分析这个视频');
  await f.run('submit()');
  assert.equal(uploads,1);
  assert.equal(f.jobs.length,2);
  assert.equal(f.jobs[0].payload.text,f.jobs[1].payload.text);
  assert.match(f.jobs[1].payload.text,/reference.mp4（参考视频，用于拆解表达）/);
});

test('partial upload retry only retries the file without a confirmed receipt',async()=>{
  const f=fixture(); let calls=0;
  f.ctx.first=new File(['one'],'one.png',{type:'image/png'});
  f.ctx.second=new File(['two'],'two.png',{type:'image/png'});
  f.ctx.request=async()=>{calls++;if(calls===2)throw Error('lost response');return {ok:true};};
  f.run(`StudioComposer.setFiles('one',[first,second]);`);
  await assert.rejects(f.run(`StudioComposer.upload('one',request)`),/lost response/);
  assert.deepEqual([...await f.run(`StudioComposer.upload('one',request)`)],['one.png','two.png']);
  assert.equal(calls,3);
});

for(const status of ['queued','cancelling'])test(`${status} visibly refuses Enter and disables adjustment`,async()=>{
  const f=fixture({runtime:'real'});
  f.run(`state.busy=true;currentJob={job_id:'active',job_kind:'conversation',status:'${status}'};
    $('chat-input').value='请等一下';syncComposerAction();`);
  assert.equal(f.run(`$('job-stop').disabled`),true);
  const result=await f.run('submit()');
  assert.equal(result.accepted,false);
  assert.equal(f.run(`$('chat-input').value`),'请等一下');
  assert.match(f.run(`$('toast').textContent`),status==='queued'?/排队/:/停止/);
});

test('running adjustment includes the visible context and clarification identity',async()=>{
  const f=fixture();
  f.run(`state.busy=true;currentJob={job_id:'active',job_kind:'conversation',status:'running'};
    state.clarificationChoices.set('one',{id:'question-one',text:'改成英文'});
    $('chat-input').value='改成英文';`);
  await f.run('submit()');
  assert.equal(f.steers[0].payload.display_context_id,'display-one');
  assert.equal(f.steers[0].payload.question_id,'question-one');
});

test('unknown conversation acceptance recovers its exact payload without re-uploading or offering a resend',async()=>{
  const f=fixture({runtime:'real'}); const posts=[]; let uploads=0;
  f.run(`watchJob=async()=>{};`);
  f.ctx.fetch=async(url,options)=>{
    if(url.includes('/materials?')){uploads++;return {ok:true};}
    posts.push(JSON.parse(options.body));
    if(posts.length===1)throw new TypeError('response lost');
    return {ok:true,json:async()=>({job_id:'same-job',status:'succeeded',events:[]})};
  };
  f.ctx.fixtureFile=new File(['bytes'],'reference.mp4',{type:'video/mp4'});
  f.run(`StudioComposer.setFiles('one',[fixtureFile],'reference');$('chat-input').value='分析这个视频';`);
  const unknown=await f.run('submit()');
  assert.equal(unknown.uncertain,true);
  assert.equal(f.run(`$('chat-input').value`),'');
  assert.equal(f.run(`StudioComposer.files('one').length`),0);
  const pending=f.run(`pendingSubmission('one')`);
  assert.equal(pending.request_id,posts[0].request_id);
  assert.match(pending.text,/reference.mp4（参考视频，用于拆解表达）/);
  f.run(`$('chat-input').value='新的草稿';`);
  assert.equal((await f.run('submit()')).uncertain,true);
  assert.equal(posts.length,1);
  await f.run(`submitJob('one',pendingSubmission('one'))`);
  assert.equal(uploads,1);
  assert.deepEqual(posts[1],posts[0]);
  assert.equal(f.run(`$('chat-input').value`),'新的草稿');
});

test('media feedback restores its exact attempt and time across project switches',async()=>{
  const f=fixture();
  f.run(`setMediaFeedbackTarget('one',{attempt_id:'attempt-one',output:{sha256:'b'.repeat(64)}},'video',2.3);
    $('chat-input').value='这里肩带太长';`);
  await f.run(`selectRun('two')`);
  await f.run(`selectRun('one')`);
  assert.match(f.run(`$('feedback-target').textContent`),/2.3 秒/);
  await f.run('submit()');
  assert.equal(f.jobs[0].payload.feedback_target.attempt_id,'attempt-one');
  assert.equal(f.jobs[0].payload.feedback_target.output_sha256,'b'.repeat(64));
  assert.equal(f.jobs[0].payload.feedback_target.at_seconds,2.3);
});

test('a late rejection restores the full owning project draft after switching away',async()=>{
  let finish; const result=new Promise(resolve=>{finish=resolve;});
  const f=fixture({result}); f.writing();
  const sending=f.run(`submit('writing_revision')`);
  await f.run(`selectRun('two')`);
  f.run(`$('chat-input').value='Other project draft';`);
  finish({accepted:false,reason:'refused'});
  await sending;
  assert.equal(f.run(`$('chat-input').value`),'Other project draft');
  await f.run(`selectRun('one')`);
  assert.equal(f.run(`$('chat-input').value`),'改成用户的原话');
  assert.equal(f.run(`state.composerRequestKind`),'writing_revision');
  assert.equal(f.run(`state.feedbackTarget.artifact_sha256`),'a'.repeat(64));
});

test('switching back before a rejection restores only an untouched composer',async()=>{
  for(const changed of [false,true]){
    let finish; const result=new Promise(resolve=>{finish=resolve;});
    const f=fixture({result}); f.writing();
    const sending=f.run(`submit('writing_revision')`);
    await f.run(`selectRun('two')`);
    await f.run(`selectRun('one')`);
    if(changed)f.run(`$('chat-input').value='Newer words';updatesComposerVersion();`);
    finish({accepted:false,reason:'refused'});
    await sending;
    assert.equal(f.run(`$('chat-input').value`),changed?'Newer words':'改成用户的原话');
  }
});

for(const uncertain of [false,true])test(`late ${uncertain?'unknown':'rejected'} receipt retains its classification and original project ownership`,async()=>{
  let finish; const reply=new Promise(resolve=>{finish=resolve;});
  const f=fixture({runtime:'real'});
  f.ctx.fetch=async()=>{
    await reply;
    if(uncertain)throw new TypeError('response lost');
    return {ok:false,status:400,json:async()=>({detail:'request refused'})};
  };
  f.run(`$('chat-input').value='原项目的请求';`);
  const sending=f.run('submit()');
  await f.run(`selectRun('two')`);
  f.run(`$('chat-input').value='Other project draft';`);
  finish();
  const result=await sending;
  assert.equal(result.uncertain,uncertain);
  assert.equal(f.run(`$('chat-input').value`),'Other project draft');
  assert.equal(Boolean(f.run(`pendingSubmission('one')`)),uncertain);
  await f.run(`selectRun('one')`);
  assert.equal(f.run(`$('chat-input').value`),uncertain?'':'原项目的请求');
});

test('reload restores the complete direct-edit draft and still rejects a stale target',async()=>{
  const f=fixture(); f.writing();
  f.run(`state.clarificationChoices.set('one',{id:'question-one',text:'改成用户的原话'});
    state.busy=true;currentJob={job_id:'busy',status:'running',job_kind:'conversation'};`);
  await f.run(`submit('writing_revision')`);
  const fresh=fixture({storage:f.storage});
  await fresh.run(`selectRun('one')`);
  assert.equal(fresh.run(`$('chat-input').value`),'改成用户的原话');
  assert.equal(fresh.run(`state.composerRequestKind`),'writing_revision');
  assert.equal(fresh.run(`state.clarificationChoices.get('one').id`),'question-one');
  assert.equal(fresh.run(`state.feedbackTarget.artifact_sha256`),'a'.repeat(64));
  fresh.run(`state.rail=[{stage:'writing',version:4,artifact_sha256:'b'.repeat(64)}];`);
  assert.equal((await fresh.run('submit()')).accepted,false);
  assert.equal(fresh.jobs.length,0);
  assert.match(fresh.run(`$('toast').textContent`),/版本已变化/);
});

test('uploaded attachment names and purposes survive reload without uploading their bytes again',async()=>{
  const f=fixture({result:{accepted:false,reason:'refused'}});
  f.ctx.fixtureFile=new File(['bytes'],'reference.mp4',{type:'video/mp4'});
  f.ctx.fetch=async()=>({ok:true});
  f.run(`StudioComposer.setFiles('one',[fixtureFile],'reference');$('chat-input').value='保留参考用途';`);
  await f.run('submit()');
  const fresh=fixture({storage:f.storage});let uploads=0;
  fresh.ctx.fetch=async()=>{uploads++;throw Error('must not upload');};
  await fresh.run(`selectRun('one');`);
  fresh.run(`StudioComposer.render();`);
  assert.match(fresh.run(`$('chat-attachments').textContent`),/reference.mp4.*已上传/);
  assert.equal(fresh.run(`StudioComposer.missingVideoUse('one')`),false);
  assert.equal((await fresh.run('submit()')).accepted,true);
  assert.equal(uploads,0);
  assert.match(fresh.jobs[0].payload.text,/参考视频，用于拆解表达/);
});

test('unuploaded files survive reload only as explicit re-selection placeholders with their purposes',async()=>{
  const f=fixture();
  f.ctx.fixtureFile=new File(['bytes'],'product.mp4',{type:'video/mp4'});
  f.run(`StudioComposer.setFiles('one',[fixtureFile],'product');$('chat-input').value='补全资料';updatesComposerVersion();`);
  const fresh=fixture({storage:f.storage});let uploads=0;
  fresh.ctx.fetch=async()=>{uploads++;return {ok:true};};
  await fresh.run(`selectRun('one')`);
  fresh.run(`StudioComposer.render();`);
  assert.match(fresh.run(`$('chat-attachments').textContent`),/需重新选择/);
  assert.doesNotMatch(fresh.run(`$('chat-attachments').textContent`),/已上传/);
  assert.equal((await fresh.run('submit()')).accepted,false);
  assert.equal(fresh.jobs.length,0);assert.equal(uploads,0);
  fresh.ctx.reselected=new File(['bytes'],'product.mp4',{type:'video/mp4'});
  assert.equal(fresh.run(`StudioComposer.replaceMissingFile('one',0,reselected)`),true);
  await fresh.run('submit()');
  assert.equal(uploads,1);
  assert.match(fresh.jobs[0].payload.text,/商品视频，用于补全商品信息/);
});

test('reload keeps an unknown request frozen while preserving a newer draft and project isolation',async()=>{
  const f=fixture({runtime:'real'});
  f.ctx.fixtureFile=new File(['bytes'],'reference.mp4',{type:'video/mp4'});
  f.ctx.fetch=async(url)=>{if(url.includes('/materials?'))return {ok:true};throw new TypeError('lost response');};
  f.run(`StudioComposer.setFiles('one',[fixtureFile],'reference');$('chat-input').value='此前请求';`);
  assert.equal((await f.run('submit()')).uncertain,true);
  const identity=f.run(`pendingSubmission('one').request_id`);
  f.run(`$('chat-input').value='新的未发送草稿';updatesComposerVersion();`);
  await f.run(`selectRun('two')`);
  f.run(`$('chat-input').value='第二个项目';updatesComposerVersion();`);
  const fresh=fixture({runtime:'real',storage:f.storage});const posts=[];
  fresh.ctx.fetch=async(url,options)=>{posts.push(JSON.parse(options.body));return {ok:true,json:async()=>({job_id:'known',status:'succeeded',events:[]})};};
  fresh.run(`watchJob=async()=>{};`);
  await fresh.run(`selectRun('two')`);
  assert.equal(fresh.run(`$('chat-input').value`),'第二个项目');
  assert.equal(fresh.run(`StudioComposer.files('two').length`),0);
  await fresh.run(`selectRun('one')`);
  assert.equal(fresh.run(`$('chat-input').value`),'新的未发送草稿');
  assert.equal(fresh.run(`StudioComposer.files('one').length`),0);
  assert.equal((await fresh.run('submit()')).uncertain,true);
  assert.equal(posts.length,0);
  await fresh.run(`submitJob('one',pendingSubmission('one'))`);
  assert.equal(posts[0].request_id,identity);
  assert.equal(fresh.run(`$('chat-input').value`),'新的未发送草稿');
  const again=fixture({storage:fresh.storage});await again.run(`selectRun('one')`);
  assert.equal(again.run(`$('chat-input').value`),'新的未发送草稿');
  assert.equal(again.run(`StudioComposer.files('one').length`),0);
});

test('definite rejection after reload restores the frozen original text, target and uploaded purpose',async()=>{
  const f=fixture({runtime:'real'});
  f.ctx.fixtureFile=new File(['bytes'],'reference.mp4',{type:'video/mp4'});
  f.ctx.fetch=async(url)=>{if(url.includes('/materials?'))return {ok:true};throw new TypeError('lost response');};
  f.run(`setMediaFeedbackTarget('one',{attempt_id:'attempt-one',output:{sha256:'b'.repeat(64)}},'video',2.3);
    StudioComposer.setFiles('one',[fixtureFile],'reference');$('chat-input').value='原始修改意见';`);
  assert.equal((await f.run('submit()')).uncertain,true);
  const fresh=fixture({runtime:'real',storage:f.storage});
  fresh.ctx.fetch=async()=>({ok:false,status:400,json:async()=>({detail:'request refused'})});
  await fresh.run(`selectRun('one')`);
  assert.equal(fresh.run(`$('chat-input').value`),'');
  const rejected=await fresh.run(`submitJob('one',pendingSubmission('one'))`);
  assert.equal(rejected.uncertain,false);
  assert.equal(fresh.run(`$('chat-input').value`),'原始修改意见');
  assert.equal(fresh.run(`state.feedbackTarget.attempt_id`),'attempt-one');
  assert.equal(fresh.run(`state.feedbackTarget.at_seconds`),2.3);
  assert.match(fresh.run(`StudioComposer.describe('one',['reference.mp4'])`),/参考视频/);
  assert.equal(fresh.run(`StudioComposer.files('one')[0].needsReselection`),false);
  assert.equal(fresh.run(`pendingSubmission('one')`),null);
});

test('a clicked clarification choice survives an immediate reload before typing or sending',async()=>{
  const f=fixture({runtime:'real'});
  const question={id:'question-one',question:'采用哪种表达？',options:[{label:'直接演示'}]};
  const card=parseHtml(f.ctx.questionCardHtml(question))[0];
  f.ctx.bindQuestionChoices(card,question);
  await card.querySelector('[data-question-choice="0"]').click();
  const fresh=fixture({storage:f.storage});await fresh.run(`selectRun('one')`);
  assert.match(fresh.run(`$('chat-input').value`),/直接演示/);
  assert.equal(fresh.run(`state.clarificationChoices.get('one').id`),'question-one');
  await fresh.run('submit()');
  assert.equal(fresh.jobs[0].payload.question_id,'question-one');
});
