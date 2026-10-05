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

test('reference adaptation preserves words and replaces old destinations through the persistent composer transaction',async()=>{
  const task={reference_id:'reference-one',name:'source.mp4',status:'completed',
    candidate:{analysis_id:'analysis-one',candidate_sha256:'b'.repeat(64)}};
  for(const previous of ['ordinary','writing','role']){
    const f=fixture({result:{accepted:false,reason:'definite refusal'}});
    f.ctx.__task=task;
    f.run(webSource('journey.js'));
    f.run(`state.referenceTasks=[__task];$('chat-input').value='用户原来的未发送要求';`);
    if(previous==='writing'){
      f.writing();
      f.run(`state.composerRequestKind='writing_revision';$('chat-input').value='用户原来的未发送要求';`);
    }
    if(previous==='role')f.run(`state.roleTarget={run_id:'one',session_id:'old-role'};`);
    f.run(`composeReferencePrompt(__task);`);
    assert.equal(f.jobs.length,0,'selecting a reference must not send a task');
    assert.match(f.run(`$('chat-input').value`),/^用户原来的未发送要求\n\n请把这条参考视频/);
    assert.equal(f.run('state.feedbackTarget'),null);
    assert.equal(f.run('state.roleTarget'),null);
    assert.equal(f.run('state.composerRequestKind'),null);
    const draft=JSON.parse(f.run(`JSON.stringify(readComposerRecord('one').draft)`));
    assert.equal(draft.referenceTarget.reference_id,'reference-one');
    assert.equal(draft.text,f.run(`$('chat-input').value`));
    await f.run('submit()');
    assert.equal(f.jobs[0].payload.reference_target.analysis_id,'analysis-one');
    assert.equal(f.jobs[0].payload.request_kind,undefined);
    assert.equal(f.jobs[0].payload.feedback_target,undefined);
    assert.equal(f.run('state.referenceTarget.reference_id'),'reference-one');
    const reloaded=fixture({storage:f.storage});
    reloaded.ctx.__task=task;
    reloaded.run(`state.referenceTasks=[__task];restoreComposerDraft(readComposerRecord('one').draft);`);
    assert.equal(reloaded.run('state.referenceTarget.candidate_sha256'),'b'.repeat(64));
    assert.equal(reloaded.run(`$('chat-input').value`),draft.text);
    reloaded.run(`state.referenceTasks[0]={...__task,status:'stale'};`);
    const blocked=await reloaded.run('submit()');
    assert.equal(blocked.accepted,false);
    assert.match(blocked.reason,/参考分析已变化/);
    assert.equal(reloaded.jobs.length,0);
    assert.equal(reloaded.run(`$('chat-input').value`),draft.text);
  }
});

test('reference continuation keeps the original owner and true new words through rejection and refresh',async()=>{
  const task={reference_id:'reference-one',name:'source.mp4',status:'analyzed',
    instruction:'只拆解开场的动作和声音',
    candidate:{analysis_id:'analysis-one',candidate_sha256:'b'.repeat(64)},
    recovery:{allowed:true,reference_id:'reference-one',parent_job_id:'failed-parent',
      source_user_input_id:'original-user-message',interpretation_id:'original-interpretation',
      deliverable:'writing',original_text:'借用参考完成整套脚本',requires_observation:false}};
  const f=fixture({result:{accepted:false,reason:'definite refusal'}});
  f.ctx.__task=task;
  f.run(webSource('reference-video.js'));f.run(webSource('journey.js'));
  f.run(`state.referenceTasks=[__task];$('chat-input').value='这次补充：语气更像朋友';
    composeReferenceContinuation(__task);`);
  const original=f.run(`$('chat-input').value`);
  assert.match(original,/^这次补充：语气更像朋友/);
  assert.doesNotMatch(original,/原任务目标：只拆解|只拆解开场/);
  assert.equal(f.run(`state.referenceRecovery.source_user_input_id`),'original-user-message');
  await f.run('submit()');
  assert.equal(f.jobs[0].payload.reference_recovery.parent_job_id,'failed-parent');
  assert.match(f.jobs[0].payload.text,/这次补充：语气更像朋友/);
  assert.equal(f.run(`$('chat-input').value`),original);
  const fresh=fixture({storage:f.storage});fresh.ctx.__task=task;
  fresh.run(`state.referenceTasks=[__task];restoreComposerDraft(readComposerRecord('one').draft);`);
  assert.equal(fresh.run(`state.referenceRecovery.interpretation_id`),'original-interpretation');
  fresh.run(`state.referenceTasks[0]={...__task,recovery:{...__task.recovery,parent_job_id:'different-parent'}};`);
  const refused=await fresh.run('submit()');
  assert.equal(refused.accepted,false);assert.match(refused.reason,/原任务已变化/);
  assert.equal(fresh.jobs.length,0);assert.equal(fresh.run(`$('chat-input').value`),original);
});

test('a reference recovery draft cannot steer a different active job',async()=>{
  const f=fixture();
  f.run(`state.referenceRecovery={reference_id:'ref',parent_job_id:'failed',source_user_input_id:'old',interpretation_id:'task'};
    state.referenceTasks=[{reference_id:'ref',recovery:{...state.referenceRecovery,allowed:true}}];
    state.busy=true;currentJob={job_id:'other',job_kind:'conversation',status:'running'};
    $('chat-input').value='继续原任务';`);
  const refused=await f.run('submit()');
  assert.equal(refused.accepted,false);assert.match(refused.reason,/等本轮结束/);
  assert.equal(f.steers.length,0);assert.equal(f.jobs.length,0);
  assert.equal(f.run(`$('chat-input').value`),'继续原任务');
});

test('both attachment pickers append and submit per-file uses instead of replacing the earlier material',async()=>{
  const f=fixture();
  f.run(webSource('journey.js'));
  f.run(`initJourney();`);
  const product=new File(['product'],'product.mp4',{type:'video/mp4'});
  const image=new File(['image'],'product.jpg',{type:'image/jpeg'});
  const reference=new File(['reference'],'reference.mp4',{type:'video/mp4'});
  const select=(id,value)=>{const picker=f.document.getElementById(id);picker.files=value;picker.onchange();};
  select('chat-files',[product,image]);select('chat-reference-files',[reference]);
  assert.deepEqual(Array.from(f.run('StudioComposer.files("one").map(file=>file.name)')),
    ['product.mp4','product.jpg','reference.mp4']);
  assert.equal(f.document.getElementById('chat-attachments').querySelectorAll('.attachment-chip').length,3);
  f.ctx.fetch=async()=>({ok:true});
  f.run(`$('chat-input').value='按商品信息适配参考';`);
  await f.run('submit()');
  assert.deepEqual(JSON.parse(JSON.stringify(f.jobs[0].payload.attachments)),[
    {name:'product.mp4',purpose:'product'},{name:'product.jpg',purpose:null},{name:'reference.mp4',purpose:'reference'},
  ]);
  select('start-reference-files',[reference]);select('start-files',[product,image]);
  assert.equal(f.document.getElementById('start-attachments').querySelectorAll('.attachment-chip').length,3);
  const uses=f.document.getElementById('start-attachments').querySelectorAll('select').map(select=>select.value);
  assert.deepEqual(uses,['reference','product']);
});

test('home sends both purposes and restores confirmed uploads after a rejected first request without retransmitting',async()=>{
  const f=fixture({result:{accepted:false,reason:'connection unavailable'}});
  f.run(webSource('journey.js'));
  f.run(`loadRuns=async()=>{};selectRun=async()=>{};setStudioView=()=>{};initJourney();`);
  const uploads=[];
  f.ctx.fetch=async(url,options)=>{
    if(String(url).includes('/materials?'))uploads.push(options.body.get('file').name);
    return {ok:true,json:async()=>({run_id:'one'})};
  };
  const select=(page,id,files)=>{const picker=page.document.getElementById(id);picker.files=files;picker.onchange();};
  select(f,'start-files',[new File(['product'],'product.mp4',{type:'video/mp4'})]);
  select(f,'start-reference-files',[new File(['reference'],'reference.mp4',{type:'video/mp4'})]);
  f.run(`$('start-prompt').value='按这些素材拆解参考';`);
  await f.document.getElementById('start-conversation').onsubmit({preventDefault(){}});
  assert.deepEqual(uploads,['product.mp4','reference.mp4']);
  assert.equal(f.jobs.length,1);
  assert.deepEqual(JSON.parse(JSON.stringify(f.jobs[0].payload.attachments)),[
    {name:'product.mp4',purpose:'product'},{name:'reference.mp4',purpose:'reference'},
  ]);
  assert.equal(f.run(`$('start-prompt').value`),'按这些素材拆解参考');
  assert.deepEqual(JSON.parse(f.storage.getItem('studio-first-message')).uploaded,[0,1]);
  const restored=fixture({storage:f.storage});
  restored.run(webSource('journey.js'));
  restored.run(`loadRuns=async()=>{};selectRun=async()=>{};setStudioView=()=>{};initJourney();`);
  restored.ctx.fetch=async(url)=>{
    assert.doesNotMatch(String(url),/\/materials\?/,'confirmed uploads are referenced after reload');
    return {ok:true,json:async()=>({run_id:'one'})};
  };
  assert.equal(restored.document.getElementById('start-attachments').querySelectorAll('.attachment-chip').length,2);
  await restored.document.getElementById('start-conversation').onsubmit({preventDefault(){}});
  assert.equal(restored.jobs.length,1);
  assert.notEqual(restored.jobs[0].payload.request_id,f.jobs[0].payload.request_id,
    'a confirmed rejection is corrected under a new input identity');
  assert.deepEqual(JSON.parse(JSON.stringify(restored.jobs[0].payload.attachments)),JSON.parse(JSON.stringify(f.jobs[0].payload.attachments)));
  assert.equal(restored.storage.getItem('studio-first-message'),null);
});

test('home partially uploaded first request restores exact uses and order across separate reselection pickers',async()=>{
  const f=fixture();
  f.run(webSource('journey.js'));
  f.run(`loadRuns=async()=>{};selectRun=async()=>{};setStudioView=()=>{};initJourney();`);
  const product=new File(['product'],'product.mp4',{type:'video/mp4'});
  const reference=new File(['reference'],'reference.mp4',{type:'video/mp4'});
  const image=new File(['image'],'product.jpg',{type:'image/jpeg'});
  const select=(page,id,files)=>{const picker=page.document.getElementById(id);picker.files=files;picker.onchange();};
  select(f,'start-files',[product,image]);select(f,'start-reference-files',[reference]);
  f.run(`$('start-prompt').value='同一条首次请求';`);
  f.ctx.fetch=async(url,options)=>{
    const failed=String(url).includes('/materials?')&&options.body.get('file').name==='product.jpg';
    return {ok:!failed,json:async()=>failed?{detail:'upload refused'}:{run_id:'one'}};
  };
  await f.document.getElementById('start-conversation').onsubmit({preventDefault(){}});
  assert.equal(f.jobs.length,0);
  const saved=JSON.parse(f.storage.getItem('studio-first-message'));
  assert.deepEqual(saved.uploaded,[0]);
  const restored=fixture({storage:f.storage});
  restored.run(webSource('journey.js'));
  restored.run(`loadRuns=async()=>{};selectRun=async()=>{};setStudioView=()=>{};initJourney();`);
  const uploads=[];
  restored.ctx.fetch=async(url,options)=>{
    if(String(url).includes('/materials?'))uploads.push(options.body.get('file').name);
    return {ok:true,json:async()=>({run_id:'one'})};
  };
  select(restored,'start-reference-files',[reference]);
  select(restored,'start-files',[image]);
  await restored.document.getElementById('start-conversation').onsubmit({preventDefault(){}});
  assert.deepEqual(uploads,['product.jpg','reference.mp4']);
  assert.deepEqual(JSON.parse(JSON.stringify(restored.jobs[0].payload.attachments)),[
    {name:'product.mp4',purpose:'product'},{name:'product.jpg',purpose:null},{name:'reference.mp4',purpose:'reference'},
  ]);
  assert.equal(restored.jobs[0].payload.request_id,saved.request_id);
});

test('running task allows removing its unsent draft attachment and posting only progress words without stopping',async()=>{
  const f=fixture({runtime:'real'}); const requests=[];
  f.ctx.source=new File(['uploaded bytes'],'reference.mp4',{type:'video/mp4'});
  f.ctx.fetch=async(url,options)=>{
    requests.push({url,body:options.body instanceof FormData?'upload':JSON.parse(options.body)});
    return {ok:true,json:async()=>({job_id:'active',job_kind:'conversation',status:'running',events:[]})};
  };
  f.run(`StudioComposer.setFiles('one',[source],'reference');`);
  await f.run(`StudioComposer.upload('one',fetch)`);
  f.run(`state.busy=true;currentJob=null;StudioComposer.render();`);
  const remove=f.document.getElementById('chat-attachments').querySelector('[data-attachment-remove]');
  assert.equal(remove.disabled,true,'busy upload/restoration without an active job remains locked');
  f.run(`showJob({job_id:'active',job_kind:'conversation',status:'running',events:[]});`);
  assert.equal(remove.disabled,false,'the actual job-state projection unlocks draft removal after reload');
  remove.onclick();
  assert.equal(f.run(`StudioComposer.files('one').length`),0);
  f.run(`$('chat-input').value='现在进度怎么样？';`);
  const result=await f.run('submit()');
  assert.equal(result.accepted,true);
  assert.equal(requests.length,2);
  assert.match(requests[1].url,/\/jobs\/active\/steer$/);
  assert.equal(requests[1].body.text,'现在进度怎么样？');
  assert.equal(requests[1].body.attachments,undefined);
  assert.ok(requests.every(request=>!request.url.includes('/cancel')&&request.body!=='delete'));
  assert.equal(f.run('currentJob.status'),'running');
});

test('uploading or unresolved input keeps draft removal locked even when an older job is visible',()=>{
  const f=fixture({runtime:'real'});
  f.ctx.source=new File(['bytes'],'product.png',{type:'image/png'});
  f.run(`StudioComposer.setFiles('one',[source]);state.busy=true;
    currentJob={job_id:'active',status:'running',job_kind:'conversation'};
    state.composerSubmissions.set('one',{stamp:1});StudioComposer.render();`);
  const remove=f.document.getElementById('chat-attachments').querySelector('[data-attachment-remove]');
  assert.equal(remove.disabled,true);
  remove.onclick();assert.equal(f.run(`StudioComposer.files('one').length`),1);
  f.run(`state.composerSubmissions.delete('one');
    sessionStorage.setItem('studio-job-pending:one',JSON.stringify({request_id:'unknown',text:'Old input'}));syncComposerAction();`);
  assert.equal(remove.disabled,true);
  remove.onclick();assert.equal(f.run(`StudioComposer.files('one').length`),1);
});
