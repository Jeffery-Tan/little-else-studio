import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {webcrypto} from 'node:crypto';
import {loadApp, makeDom, webSource} from './dom-fixture.mjs';

const RESUME={resume_action:'continue',text:'继续这项未完成的创作任务，保留已保存内容并先核对当前依据。'};
const role = (extra={}) => {
  const row={session_id:'writer-one',role:'writer',stage:'writing',
    parent_job_id:'job-one',task_id:'attempt-one',status:'running',...extra};
  if(['waiting','interrupted','failed'].includes(row.status)&&!row.input_closed&&!row.artifact_receipt)
    row.resume_command={...RESUME};
  return row;
};
const job = (extra={}) => ({job_id:'job-one',job_kind:'conversation',status:'running',
  goal_revision:3,cursor:9,events:[],role_sessions:[role()],...extra});
const saved={stage:'writing',version:2,sha256:'a'.repeat(64)};
const pendingNotice='脚本第 2 版已保存，补充未投递。请阅读当前保存版本，在新稿上重新选择具体位置并发送修改意见；旧任务不会重放。';

// Runs the shipped controllers with an offline HTTP boundary. This DOM adapter
// models modal focus and queued close events, but does not verify browser layout.
async function page(initial=job()) {
  const dom=makeDom({body:webSource('index.html').match(/<body[^>]*>([\s\S]*?)<\/body>/)[1]});
  const {document}=dom, closes=[];
  const create=document.createElement;
  document.createElement=tag=>{
    const el=create(tag);
    el.focus=()=>{
      const modal=document.querySelectorAll('dialog').find(node=>node.open);
      if(!modal||modal.contains(el))document.activeElement=el;
    };
    if(tag==='dialog'){
      el.showModal=()=>{el.previousFocus=document.activeElement;el.open=true;el.focus();};
      el.close=()=>{if(!el.open)return;el.open=false;el.previousFocus?.focus();closes.push(()=>el.listeners.close?.());};
    }
    return el;
  };
  for(const id of ['chat-input','send','job-stop']){
    const el=document.getElementById(id);
    el.focus=()=>{
      const modal=document.querySelectorAll('dialog').find(node=>node.open);
      if(!modal||modal.contains(el))document.activeElement=el;
    };
  }
  const calls=[];
  const app=loadApp({dom,runtime:'real',globals:{crypto:webcrypto,setTimeout:()=>0,clearTimeout:()=>{},
    setProjectFocus:()=>{}}});
  for(let i=0;i<3;i++)await new Promise(resolve=>setImmediate(resolve));
  const run=code=>vm.runInContext(code,app.ctx);
  const restoreOriginal=run('restoreJob');
  app.ctx.__job=initial;
  app.ctx.__api=async(path,options)=>{
    if(options)calls.push({path,body:JSON.parse(options.body)});
    return initial;
  };
  run(`state.runId='one';state.timeline={display_context_id:'shown-one'};state.runs=[];
    render=()=>{};renderProjectProgress=()=>{};renderJourneyDecision=()=>{};
    configureJourneyTabs=()=>{};refreshTimeline=async()=>{};loadDraft=()=>{};
    restoreJob=async()=>{};watchJob=async()=>{};api=(...args)=>__api(...args);
    showJob(__job);`);
  const buttons=(text,root=document.body)=>root.querySelectorAll('button').filter(el=>el.textContent===text);
  return {...app,calls,run,buttons,restoreOriginal,
    flushClose:()=>{for(const close of closes.splice(0))close();}};
}

test('targeted role feedback closes its modal and keeps exact composer scope and focus',async()=>{
  const app=await page();
  app.run(`$('chat-input').value='保留原来的开场，只把这句说得更自然';
    state.feedbackTarget={kind:'writing',run_id:'one',version:3};
    state.clarificationChoices.set('one',{id:'old-question'});openTeamProgress();`);
  app.buttons('补充要求')[0].click();
  app.flushClose();
  assert.equal(app.run('state.roleTarget.session_id'),'writer-one');
  assert.equal(app.run('state.feedbackTarget'),null);
  assert.equal(app.run("state.clarificationChoices.has('one')"),false);
  assert.equal(app.document.activeElement,app.document.getElementById('chat-input'));
  assert.equal(app.document.getElementById('chat-input').value,'保留原来的开场，只把这句说得更自然');
  assert.match(app.document.getElementById('composer-key-hint').textContent,/原话只送给编剧/);
  assert.equal(app.document.getElementById('send').dataset.mode,'stop');
  assert.equal(app.calls.length,0);
  await app.run('submit()');
  assert.equal(app.calls.length,1);
  assert.equal(app.calls[0].path,'/api/runs/one/jobs/job-one/steer');
  assert.equal(app.calls[0].body.target_session_id,'writer-one');
  assert.equal(app.calls[0].body.text,'保留原来的开场，只把这句说得更自然');
  assert.ok(!('feedback_target' in app.calls[0].body));
  assert.ok(!('question_id' in app.calls[0].body));
});

test('role target and exact draft survive project changes without becoming ordinary feedback',async()=>{
  const app=await page();
  app.run(`setRoleFeedbackTarget('one',currentJob.role_sessions[0]);$('chat-input').value='只改编剧这项';`);
  await app.run("selectRun('two')");
  assert.equal(app.run('state.roleTarget'),null);
  app.run("$('chat-input').value='另一个项目的草稿';");
  await app.run("selectRun('one')");
  assert.equal(app.run('state.roleTarget.session_id'),'writer-one');
  assert.equal(app.document.getElementById('chat-input').value,'只改编剧这项');
  app.run('showJob(__job);');
  await app.run('submit()');
  assert.equal(app.calls[0].body.target_session_id,'writer-one');
});

test('a role that finishes before posting keeps the draft and cannot fall back to a new job',async()=>{
  const app=await page();
  app.run(`setRoleFeedbackTarget('one',currentJob.role_sessions[0]);$('chat-input').value='只改这项任务';`);
  app.ctx.__api=async(path,options)=>{
    app.calls.push({path,body:JSON.parse(options.body)});
    const error=new Error('原任务已经结束，请刷新后发送新的指令。');error.status=409;throw error;
  };
  const result=await app.run('submit()');
  assert.equal(result.accepted,false);
  assert.equal(result.draft_restored,true);
  assert.equal(app.document.getElementById('chat-input').value,'只改这项任务');
  assert.equal(app.run('state.roleTarget.session_id'),'writer-one');
  assert.equal(app.calls.length,1);
  assert.match(app.calls[0].path,/\/steer$/);
});

test('delivered role messages describe delivery without promising global cancellation or replay',async()=>{
  const app=await page();
  app.ctx.__job=job({steer_status:'delivered',steer_class:'role_message',
    steer_reason:'原话已送到编剧，其他任务继续。'});
  app.run('showJob(__job);');
  const text=app.document.getElementById('job-status').textContent;
  assert.match(text,/角色补充已收到/);
  assert.match(text,/其他任务继续/);
  assert.doesNotMatch(text,/当前模型调用结束|按你的调整接续|已处理补充/);
  app.ctx.__job=job({steer_status:'delivered',steer_class:'role_stop',steer_reason:'只停止编剧任务，其他任务继续。'});
  app.run('showJob(__job);');
  assert.match(app.document.getElementById('job-status').textContent,/此项停止要求已收到/);
});

test('independent session changes refresh team controls at the same root cursor',async()=>{
  const app=await page();app.run('openTeamProgress();');
  assert.equal(app.buttons('补充要求').length,1);
  app.ctx.__job=job({role_sessions:[role({status:'failed',reason:'保存未完成'})]});
  app.run('showJob(__job);');
  assert.equal(app.buttons('补充要求').length,0);
  assert.match(app.document.querySelector('.team-dialog-body').textContent,/保存未完成/);
  app.ctx.__job=job({status:'failed',role_sessions:[role({status:'failed'})]});
  app.run('showJob(__job);');
  assert.equal(app.buttons('继续此项').length,1);
  for(const session of [role({status:'cancelled'}),role({status:'failed',stop_requested:true})]){
    app.ctx.__job=job({status:'failed',role_sessions:[session]});app.run('showJob(__job);');
    assert.equal(app.buttons('继续此项').length,0);
  }
});

test('saved running role shows closure and blocks a retained supplement control while preserving stop',async()=>{
  const app=await page();app.run('openTeamProgress();');
  const staleAmend=app.buttons('补充要求')[0];
  app.ctx.__job=job({role_sessions:[role({input_closed:true,artifact_receipt:saved})]});
  app.run('showJob(__job);');
  assert.equal(app.buttons('补充要求').length,0);
  assert.equal(app.buttons('停止此项').length,1);
  assert.equal(app.buttons('查看已保存版本').length,1);
  assert.match(app.document.querySelector('.team-dialog-body').textContent,/产物已保存，正在收尾/);
  staleAmend.click();
  assert.equal(app.run('state.roleTarget'),undefined);
  assert.match(app.document.getElementById('toast').textContent,/第 2 版已保存/);
  assert.equal(app.calls.length,0);
});

test('role feedback drafted before closure is retained without posting or converting to an ordinary job',async()=>{
  const app=await page();
  app.run("setRoleFeedbackTarget('one',currentJob.role_sessions[0]);$('chat-input').value='只改这份已保存脚本的结尾';");
  app.ctx.__job=job({role_sessions:[role({input_closed:true,artifact_receipt:saved,pending_input_notice:pendingNotice})]});
  app.run('showJob(__job);');
  const result=await app.run('submit()');
  assert.equal(result.accepted,false);
  assert.equal(result.draft_restored,true);
  assert.equal(app.document.getElementById('chat-input').value,'只改这份已保存脚本的结尾');
  assert.equal(app.run('state.roleTarget.session_id'),'writer-one');
  assert.match(app.document.getElementById('toast').textContent,/补充未投递/);
  assert.equal(app.calls.length,0);
});

test('a save racing a real steer refusal keeps the precise reason and original scoped draft',async()=>{
  const app=await page();
  app.run("setRoleFeedbackTarget('one',currentJob.role_sessions[0]);$('chat-input').value='保存过程中发出的原话';openTeamProgress();");
  app.ctx.__api=async(path,options)=>{
    app.calls.push({path,body:JSON.parse(options.body)});
    app.ctx.__job=job({role_sessions:[role({input_closed:true,artifact_receipt:saved})]});
    app.run('showJob(__job);');
    const error=new Error('脚本第 2 版已保存，旧任务不再接收补充。请阅读当前版本，在新稿上重新选择具体位置并发送修改意见。');
    error.status=409;throw error;
  };
  const result=await app.run('submit()');
  assert.equal(result.accepted,false);
  assert.equal(result.draft_restored,true);
  assert.equal(app.calls.length,1);assert.match(app.calls[0].path,/\/steer$/);
  assert.equal(app.calls[0].body.target_session_id,'writer-one');
  assert.equal(app.document.getElementById('chat-input').value,'保存过程中发出的原话');
  assert.equal(app.run('state.roleTarget.session_id'),'writer-one');
  assert.equal(app.run('currentJob.job_id'),'job-one');
  assert.equal(app.run("readPendingSteer('one')"),null);
  assert.match(app.document.getElementById('toast').textContent,/第 2 版已保存/);
  assert.equal(app.buttons('补充要求').length,0);
});

test('historical saved role keeps undelivered input visible and reads its receipt without replaying',async()=>{
  const app=await page(job({status:'failed',role_sessions:[role({status:'failed',
    artifact_receipt:saved,pending_message_count:1,pending_input_notice:pendingNotice})]}));
  const gets=[];
  app.ctx.__api=async(path,options)=>{
    assert.equal(options,undefined,'Reading saved work cannot submit a task');gets.push(path);
    return {stage:'writing',version:2,sha256:saved.sha256,integrity_ok:true,
      numbered:'脚本第 2 版',author:'writer',created_at:'2026-10-04',content:'Saved exact version'};
  };
  app.run('openTeamProgress();');
  assert.equal(app.buttons('继续此项').length,0);
  assert.match(app.document.querySelector('.team-dialog-body').textContent,/补充未投递/);
  app.buttons('查看已保存版本')[0].click();app.flushClose();
  for(let i=0;i<3;i++)await new Promise(resolve=>setImmediate(resolve));
  assert.deepEqual(gets,['/api/runs/one/artifacts/writing/2']);
  assert.equal(app.run('readerState.version'),2);
  assert.equal(app.document.getElementById('reader-title').textContent,'脚本第 2 版');
  assert.equal(app.run('currentJob.role_sessions[0].pending_message_count'),1);
  assert.equal(app.calls.length,0);
});

test('new-baseline feedback opens the current script and binds the newly selected exact field',async()=>{
  const app=await page(job({status:'failed',role_sessions:[role({status:'failed',
    artifact_receipt:saved,pending_message_count:1,pending_input_notice:pendingNotice})]}));
  for(const file of ['md.js','blocks.js','stage.js','workspace.js'])vm.runInContext(webSource(file),app.ctx,{filename:file});
  app.ctx.__current={stage:'writing',label:'脚本与分镜',has_doc:true,version:3,
    artifact_sha256:'b'.repeat(64),integrity_ok:true,stale:false,state:'done',blank:{text:''},
    questions:[],team_issues:[],blocks:[{type:'cards',title:'逐段',cards:[{head:'0:00–0:03',
      fields:[{label:'画面',value:'Current scene'},{label:'台词',value:'Accurate new line'}]}]}]};
  app.run(`state.rail=[__current];state.timeline.rail=state.rail;state.activeStage='writing';
    state.canvasTab='board';$('journey-tab-script').disabled=false;
    $('chat-input').value='完整意见原话';state.roleTarget={run_id:'one',session_id:'writer-one'};
    openTeamProgress();`);
  app.buttons('在当前稿重新选择反馈')[0].click();app.flushClose();
  assert.equal(app.run('state.canvasTab'),'plan');
  assert.equal(app.run('state.activeStage'),'writing');
  const line=app.document.getElementById('stage').querySelector('.creator-script-line');
  assert.equal(line.textContent,'Accurate new line');line.listeners.click();
  assert.equal(app.run('state.roleTarget'),null);
  assert.equal(app.run('state.feedbackTarget.version'),3);
  assert.equal(app.run('state.feedbackTarget.artifact_sha256'),'b'.repeat(64));
  assert.equal(app.document.getElementById('chat-input').value,'完整意见原话');
  assert.equal(app.calls.length,0);
});

test('a retained resume button cannot reopen a newly saved or unverified session',async()=>{
  for(const extra of [{input_closed:true,artifact_receipt:saved},{resume_command:null}]){
    const app=await page(job({status:'failed',role_sessions:[role({status:'failed'})]}));
    app.run('openTeamProgress();');const stale=app.buttons('继续此项')[0];
    app.ctx.__job=job({status:'failed',role_sessions:[{...role({status:'failed'}),...extra}]});
    app.run('showJob(__job);');
    assert.equal(app.buttons('继续此项').length,0);
    await stale.click();
    assert.equal(app.calls.length,0);
    assert.equal(app.run("readPendingJob('one')"),null);
  }
});

test('stop targets one session using current goal revision and never invokes root cancel',async()=>{
  const app=await page();app.run('openTeamProgress();');
  app.run('currentJob.goal_revision=4;');
  await app.buttons('停止此项')[0].click();
  assert.equal(app.calls.length,1);
  assert.equal(app.calls[0].path,'/api/runs/one/jobs/job-one/steer');
  assert.deepEqual({...app.calls[0].body,request_id:'normalized'},
    {text:'停止',request_id:'normalized',expected_goal_revision:4,target_session_id:'writer-one',reference_consent:true});
});

test('stale role controls never send to a newly selected project',async()=>{
  const app=await page();app.run('openTeamProgress();');
  const stop=app.buttons('停止此项')[0],amend=app.buttons('补充要求')[0];
  app.run("state.runId='two';");
  await stop.click();amend.click();
  assert.equal(app.calls.length,0);
  assert.equal(app.run('state.roleTarget'),undefined);
});

test('explicit role resume uses the session route and retries only its original identity',async()=>{
  const app=await page(job({status:'interrupted',role_sessions:[role({status:'interrupted'})]}));
  let posts=0;
  app.ctx.__api=async(path,options)=>{
    if(options){app.calls.push({path,body:JSON.parse(options.body)});if(++posts===1)throw new TypeError('lost reply');}
    return job({status:'succeeded'});
  };
  app.run('openTeamProgress();');
  await app.buttons('继续此项')[0].click();
  const first=app.calls[0];
  assert.equal(first.path,'/api/runs/one/role-sessions/writer-one/resume');
  assert.equal(first.body.expected_task_id,'attempt-one');
  assert.equal(first.body.resume_action,'continue');
  assert.equal(first.body.text,RESUME.text);
  assert.equal(app.run("readPendingJob('one').request_id"),first.body.request_id);
  assert.equal(app.run("readPendingJob('one').session_id"),'writer-one');
  await app.run("submitJob('one',readPendingJob('one'))");
  assert.equal(app.calls.length,2);
  assert.deepEqual(app.calls[1],first);
  assert.equal(app.run("readPendingJob('one')"),null);
});

test('parallel roles are both shown as working only while their owning root is active',async()=>{
  const app=await page(job({role_sessions:[role(),role({session_id:'director-one',role:'director'})]}));
  const active=app.run('teamProgress(currentJob)');
  assert.equal(active.participants.find(row=>row.role==='director').status,'处理中');
  assert.equal(active.participants.find(row=>row.role==='writer').status,'处理中');
  app.run("currentJob.status='interrupted';");
  assert.ok(app.run('teamProgress(currentJob)').participants.every(row=>row.status!=='处理中'));
});

test('a rejected frozen receipt cannot overwrite a newer role target with an empty draft',async()=>{
  const app=await page(job({role_sessions:[role(),role({session_id:'director-one',role:'director'})]}));
  app.run(`storeComposerDraft('one',{text:'',roleTarget:null},
    {requestId:'old-message',draft:{text:'旧的定向补充',roleTarget:{run_id:'one',session_id:'writer-one',label:'编剧'}}});
    setRoleFeedbackTarget('one',currentJob.role_sessions[1]);
    settleComposerRequest('one','old-message',false);`);
  assert.equal(app.run('state.roleTarget.session_id'),'director-one');
  assert.equal(app.run("readComposerRecord('one').draft.roleTarget.session_id"),'director-one');
  assert.equal(app.document.getElementById('chat-input').value,'');
});

for(const kind of ['media','production_draft','writing']){
  test(`selecting ${kind} feedback replaces the role target before the real send boundary`,async()=>{
    const app=await page();
    app.run(`setRoleFeedbackTarget('one',currentJob.role_sessions[0]);
      $('chat-input').value='  保留这份反馈原话\\n只改选中的内容  ';
      state.clarificationChoices.set('one',{id:'old-question'});`);
    const sha='a'.repeat(64);
    if(kind==='media')app.run(`setMediaFeedbackTarget('one',
      {attempt_id:'image-one',output:{sha256:'${sha}'}},'image');`);
    if(kind==='production_draft')app.run(`setProductionFeedbackTarget('one',
      {draft_id:'draft-one',kind:'image',draft_sha256:'${sha}',attempts:[]});`);
    if(kind==='writing')app.run(`{
      const step={stage:'writing',has_doc:true,version:3,artifact_sha256:'${sha}'};
      state.rail=[step];const host=document.createElement('section'),line=document.createElement('p');
      line.dataset.creatorTarget='这句';line.dataset.creatorPath='["segments",0,"line"]';
      line.textContent='准确原句';host.append(line);document.body.append(host);
      wireCreatorFeedback(host,step);line.listeners.click();
    }`);
    assert.equal(app.run('state.roleTarget'),null);
    assert.equal(app.run('state.feedbackTarget.kind'),kind);
    assert.equal(app.run("state.clarificationChoices.has('one')"),false);
    assert.equal(app.document.getElementById('chat-input').value,'  保留这份反馈原话\n只改选中的内容  ');
    assert.doesNotMatch(app.document.getElementById('feedback-target').textContent,/补充给编剧/);
    await app.run('submit()');
    assert.equal(app.calls.length,1);
    assert.equal(app.calls[0].body.feedback_target.kind,kind);
    assert.ok(!('target_session_id' in app.calls[0].body));
    assert.ok(!('question_id' in app.calls[0].body));
  });
}

test('work feedback persists as the sole target across project navigation and a frozen request',async()=>{
  const app=await page(),sha='a'.repeat(64);
  app.run(`setRoleFeedbackTarget('one',currentJob.role_sessions[0]);
    $('chat-input').value='看这张图的商品形状';
    storeComposerDraft('one',composerDraft(),{requestId:'older-frozen',draft:{text:'旧请求'}});
    setMediaFeedbackTarget('one',{attempt_id:'image-one',output:{sha256:'${sha}'}},'image');`);
  assert.equal(app.run("readComposerRecord('one').frozen.requestId"),'older-frozen');
  await app.run("selectRun('two')");await app.run("selectRun('one')");
  assert.equal(app.run('state.roleTarget'),null);
  assert.equal(app.run('state.feedbackTarget.attempt_id'),'image-one');
  assert.equal(app.document.getElementById('chat-input').value,'看这张图的商品形状');
  assert.equal(app.run("readComposerRecord('one').frozen.requestId"),'older-frozen');
});

test('confirmed role delivery rejection restores its frozen draft and keeps watching the original job',async()=>{
  const app=await page();
  app.run(`setRoleFeedbackTarget('one',currentJob.role_sessions[0]);
    const draft=composerDraft();draft.text='完整原话未投递';
    storeComposerDraft('one',{text:''},{requestId:'pending-role',draft});
    clearFeedbackTarget(false);$('chat-input').value='';
    sessionStorage.setItem(pendingSteerKey('one'),JSON.stringify({job_id:'job-one',
      request_id:'pending-role',text:draft.text,target_session_id:'writer-one'}));
    watchJob=async(runId,observed)=>{globalThis.__watched=observed.job_id;showJob(observed);};`);
  app.ctx.__api=async(path,options)=>{
    if(options)throw new Error('Receipt recovery must not POST again');
    if(path.includes('/request-receipts/'))return {received:false,rejected:true,kind:'steer',
      request_id:'pending-role',job_id:'job-one',code:'role_delivery_failed',reason:'这条补充未能投递，请重新选择当前任务。'};
    return job();
  };
  await app.restoreOriginal('one');
  assert.equal(app.run("readPendingSteer('one')"),null);
  assert.equal(app.document.getElementById('chat-input').value,'完整原话未投递');
  assert.equal(app.run('state.roleTarget.session_id'),'writer-one');
  assert.equal(app.ctx.__watched,'job-one');
  assert.match(app.document.getElementById('toast').textContent,/未能投递/);
});

test('a mismatched delivery rejection cannot clear another pending identity or restore its draft',async()=>{
  const app=await page();
  app.run(`sessionStorage.setItem(pendingSteerKey('one'),JSON.stringify({job_id:'job-one',
    request_id:'pending-role',text:'准确的未决原话'}));`);
  app.ctx.__api=async path=>path.includes('/request-receipts/')?
    {received:false,rejected:true,kind:'steer',request_id:'different',job_id:'job-one'}:job();
  await app.restoreOriginal('one');
  assert.equal(app.run("readPendingSteer('one').request_id"),'pending-role');
  assert.equal(app.document.getElementById('chat-input').value,'');
});
