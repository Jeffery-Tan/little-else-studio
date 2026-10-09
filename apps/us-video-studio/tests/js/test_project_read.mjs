/* Shipped read/transport/conversation flow; fake HTTP and clock, no writes. */
import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {loadApp,makeDom,webSource} from './dom-fixture.mjs';

const RUN='read-preview';
const closing=()=>Object.assign(new Error('[closing] 此服务正在停止，请连接当前制片室服务。'),{status:409});
const refused=()=>{throw new TypeError('Failed to fetch');};
const settle=async()=>{for(let index=0;index<4;index++)await new Promise(resolve=>setImmediate(resolve));};

function timeline(reply='原已保存的图片观察结论') {
  return {run:{run_id:RUN,kind:'creative',content_format:'video'},rail:[],materials:[],
    questions:[],pending:null,progress:{},brief:{phase:'ready',execution:{active:false}},
    turns:[{user:{text:'检查图片'},reply:{text:reply},items:[],detail:null}]};
}

async function page({timelineRoute=()=>timeline(),productionRoute=()=>({}),referencesRoute=()=>({tasks:[]}),recoveryRoute=()=>({})}={}) {
  const timers=[],dom=makeDom({body:webSource('index.html').match(/<body[^>]*>([\s\S]*?)<\/body>/)[1]});
  const app=loadApp({dom,runtime:'real',routes:{
    '/timeline':timelineRoute,'/production':productionRoute,'/reference-videos':referencesRoute,
    '/recovery':recoveryRoute,'/jobs/latest':null,
  },globals:{setTimeout:(fn,ms)=>{timers.push({fn,ms});return timers.length;},clearTimeout:()=>{},
    creatorProjectView:()=>({tab:'plan',stage:null}),StudioProduction:{},renderProjects:()=>{}}});
  vm.runInContext(webSource('stage.js'),app.ctx,{filename:'stage.js'});
  vm.runInContext(webSource('production.js'),app.ctx,{filename:'production.js'});
  await settle();
  vm.runInContext(`
    state.runId='${RUN}';state.runs=[{run_id:'${RUN}',kind:'creative'}];state.view='project';state.canvasTab='board';
    renderRail=rail=>{state.rail=rail;};renderPane=()=>{};renderSpend=()=>{};renderMaterials=()=>{};
    renderDraft=()=>{};renderProjects=()=>{};syncJourneyTabs=()=>{};renderJourneyDecision=()=>{};
  `,app.ctx);
  app.el=id=>app.document.getElementById(id);app.read=code=>vm.runInContext(code,app.ctx);
  app.timers=timers;
  app.nextRead=async()=>{
    await settle();const index=timers.findIndex(timer=>timer&&[1000,2000,4000,8000].includes(timer.ms));
    assert.ok(index>=0,'expected a bounded read reconnect');const timer=timers[index];timers[index]=null;timer.fn();await settle();
    return timer.ms;
  };
  return app;
}

test('completed job closing then refused then healthy displays its saved reply using GET only',async()=>{
  let calls=0;
  const p=await page({timelineRoute:()=>[closing,refused,()=>timeline()][Math.min(calls++,2)]()});
  p.el('chat-input').value='尚未发送的新要求';
  p.el('conversation-body').scrollTop=86;p.read('state.atBottom=false');
  p.storage.setItem('studio-creative-board-layout-v1:'+RUN,'original saved layout');
  const job={job_id:'completed-original',job_kind:'conversation',status:'succeeded',cursor:7,events:[]};
  const finished=p.ctx.watchJob(RUN,job,0);
  await settle();assert.match(p.el('project-read-status').textContent,/服务正在更新/);
  assert.equal(await p.nextRead(),1000);assert.match(p.el('project-read-status').textContent,/连接暂时断开/);
  assert.equal(await p.nextRead(),2000);await finished;
  assert.equal(calls,3);assert.equal(p.el('project-read-status').hidden,true);
  assert.match(p.el('conversation-body').querySelector('.msg.assistant').textContent,/原已保存的图片观察结论/);
  assert.equal(p.read('currentJob.job_id'),'completed-original');assert.equal(p.read('currentJob.status'),'succeeded');
  assert.equal(p.el('chat-input').value,'尚未发送的新要求');assert.equal(p.el('conversation-body').scrollTop,86);
  assert.equal(p.storage.getItem('studio-creative-board-layout-v1:'+RUN),'original saved layout');
  assert.equal(p.observed.some(call=>call.method!=='GET'),false);
  assert.equal(p.observed.some(call=>/\/jobs$|\/generate|\/message/.test(call.path)),false);
});

test('bounded reads stop after four waits and leave a manual read entry which can recover',async()=>{
  let healthy=false,calls=0;
  const p=await page({timelineRoute:()=>{calls++;return healthy?timeline('稍后读取的原结论'):closing();}});
  const read=p.ctx.refreshTimeline();const checked=assert.rejects(read,/\[closing\]/);
  const waits=[];for(let index=0;index<4;index++)waits.push(await p.nextRead());
  await checked;assert.equal(calls,5);assert.deepEqual(waits,[1000,2000,4000,8000]);
  const host=p.el('project-read-status');assert.equal(host.hidden,false);assert.match(host.textContent,/暂时未同步/);
  const retry=host.querySelector('button');assert.equal(retry.textContent,'刷新已保存内容');
  healthy=true;await retry.onclick();
  assert.equal(calls,6);assert.equal(host.hidden,true);assert.match(p.el('conversation-body').textContent,/稍后读取的原结论/);
  assert.equal(p.observed.some(call=>call.method!=='GET'),false);
});

test('real project integrity 409 is shown immediately and never treated as restart',async()=>{
  const p=await page();
  const original=p.ctx.fetch;p.ctx.fetch=async(url,options)=>String(url).endsWith('/timeline')
    ? {ok:false,status:409,statusText:'Conflict',json:async()=>({detail:'[project_data] events.jsonl 第 2 行损坏',
        integrity:{available:false,issues:[{category:'invalid_json',line:2}]}})}
    :original(url,options);
  await assert.rejects(p.ctx.refreshTimeline(),/events.jsonl/);
  const host=p.el('project-read-status');assert.match(host.textContent,/项目资料损坏.*第 2 行/s);
  assert.equal(p.timers.filter(timer=>timer&&[1000,2000,4000,8000].includes(timer.ms)).length,0);
  assert.equal(p.read('state.projectReadIntegrity.available'),false);assert.equal(p.el('send').disabled,true);
  assert.ok(host.querySelector('button'));
  p.ctx.fetch=original;await host.querySelector('button').onclick();
  assert.equal(p.el('send').disabled,false);assert.equal(p.read('state.projectReadIntegrity'),null);
});

test('other business 409 on required or optional reads is visible without auto retry',async()=>{
  for(const source of ['timeline','production','references']) {
    let calls=0;const failure=()=>{calls++;return Object.assign(new Error('[conflict] 准确版本已变化'),{status:409});};
    const p=await page({[source==='references'?'referencesRoute':source+'Route']:failure});
    await assert.rejects(p.ctx.refreshTimeline(),/准确版本已变化/);
    assert.equal(calls,1);assert.match(p.el('project-read-status').textContent,/准确版本已变化/);
    assert.equal(p.timers.filter(timer=>timer&&[1000,2000,4000,8000].includes(timer.ms)).length,0);
  }
});

test('a simultaneous shutdown cannot hide a real damaged optional project record',async()=>{
  const p=await page({timelineRoute:closing}),original=p.ctx.fetch;
  p.ctx.fetch=async(url,options)=>String(url).endsWith('/production')
    ? {ok:false,status:409,statusText:'Conflict',json:async()=>({detail:'[project_data] production.json 损坏',
        integrity:{available:false,issues:[{category:'invalid_json'}]}})}
    :original(url,options);
  await assert.rejects(p.ctx.refreshTimeline(),/production.json/);
  assert.match(p.el('project-read-status').textContent,/项目资料损坏.*production.json/s);
  assert.equal(p.timers.filter(timer=>timer&&[1000,2000,4000,8000].includes(timer.ms)).length,0);
});

test('optional content read shutdown retries the same bundle instead of clearing saved content',async()=>{
  let calls=0;
  const p=await page({productionRoute:()=>calls++===0?closing():{saved:'same current production'}});
  const read=p.ctx.refreshTimeline();await p.nextRead();assert.equal(await read,true);
  assert.equal(calls,2);assert.equal(p.read('state.productionData.saved'),'same current production');
});

test('switching projects cancels pending reconnect before it reads or applies another project',async()=>{
  let calls=0;const p=await page({timelineRoute:()=>{calls++;return closing();}});
  const read=p.ctx.refreshTimeline();await settle();
  p.read('state.runId="other-project";state.timeline={run:{run_id:"other-project"}}');
  await p.nextRead();assert.equal(await read,false);assert.equal(calls,1);
  assert.equal(p.read('state.timeline.run.run_id'),'other-project');
});

test('newer refresh supersedes pending reconnect without overwriting its saved conclusion',async()=>{
  let calls=0;const p=await page({timelineRoute:()=>calls++===0?closing():timeline('新刷新读取的原结论')});
  const previous=p.ctx.refreshTimeline();await settle();
  assert.equal(await p.ctx.refreshTimeline(),true);await p.nextRead();assert.equal(await previous,false);
  assert.equal(calls,2);assert.match(p.el('conversation-body').textContent,/新刷新读取的原结论/);
});

test('network annotations never automatically resend a user write',async()=>{
  const p=await page();const observed=[];
  p.ctx.fetch=async(path,options)=>{observed.push({path,method:options.method,body:options.body});return refused();};
  await assert.rejects(p.ctx.api(`/api/runs/${RUN}/message`,{method:'POST',body:'{"text":"冻结原话"}'}),/Failed to fetch/);
  assert.equal(observed.length,1);assert.equal(observed[0].method,'POST');
  assert.equal(p.timers.filter(timer=>timer&&[1000,2000,4000,8000].includes(timer.ms)).length,0);
});

test('ended recovery read reconnects on closing and refused and clears only its own stale status',async()=>{
  let calls=0;const p=await page({recoveryRoute:()=>[closing,refused,()=>({revision:'real-recovery',can_continue:true})][Math.min(calls++,2)]()});
  const job={job_id:'ended-original',job_kind:'conversation',status:'failed',reason:'本轮模型连接中断',events:[]};
  const finished=p.ctx.watchJob(RUN,job,0);
  await settle();assert.match(p.el('project-read-status').textContent,/正在重新读取恢复状态/);
  await p.nextRead();assert.match(p.el('project-read-status').textContent,/连接暂时断开/);
  await p.nextRead();await finished;
  assert.equal(calls,3);assert.equal(p.read('currentRecovery.revision'),'real-recovery');
  assert.equal(p.el('project-read-status').hidden,true);
  assert.doesNotMatch(p.el('toast').textContent,/暂时无法读取恢复状态/);
  assert.match(p.el('conversation-body').textContent,/原已保存的图片观察结论/);
  assert.equal(p.observed.some(call=>call.method!=='GET'),false);
});

test('healthy timeline cannot clear exhausted recovery read and manual recovery uses GET only',async()=>{
  let healthy=false,calls=0;const p=await page({recoveryRoute:()=>{calls++;return healthy?{revision:'later',can_continue:true}:closing();}});
  const finished=p.ctx.watchJob(RUN,{job_id:'ended',status:'failed',job_kind:'conversation',reason:'连接中断',events:[]},0);
  for(let index=0;index<4;index++)await p.nextRead();await finished;
  assert.equal(calls,5);assert.match(p.el('conversation-body').textContent,/原已保存的图片观察结论/);
  const host=p.el('project-read-status');assert.equal(host.hidden,false);assert.match(host.textContent,/恢复状态暂时未同步/);
  const button=host.querySelector('button');assert.equal(button.textContent,'刷新恢复状态');
  healthy=true;await button.onclick();
  assert.equal(calls,6);assert.equal(host.hidden,true);assert.equal(p.read('currentRecovery.revision'),'later');
  assert.equal(p.observed.some(call=>call.method!=='GET'),false);
});

test('damaged recovery keeps writes blocked despite a healthy timeline until recovery itself is healthy',async()=>{
  const p=await page(),original=p.ctx.fetch;
  p.ctx.fetch=async(url,options)=>String(url).endsWith('/recovery')
    ? {ok:false,status:409,statusText:'Conflict',json:async()=>({detail:'[project_data] recovery.json 损坏',
        integrity:{available:false,issues:[{category:'invalid_json'}]}})}
    :original(url,options);
  await p.ctx.watchJob(RUN,{job_id:'damaged-recovery',status:'failed',job_kind:'conversation',reason:'本轮未完成',events:[]},0);
  assert.match(p.el('project-read-status').textContent,/项目资料损坏.*recovery.json/s);
  assert.equal(p.read('state.projectReadIntegrity.available'),false);assert.equal(p.el('send').disabled,true);
  assert.equal(p.timers.filter(timer=>timer&&[1000,2000,4000,8000].includes(timer.ms)).length,0);
  p.ctx.fetch=original;await p.el('project-read-status').querySelector('button').onclick();
  assert.equal(p.read('state.projectReadIntegrity'),null);assert.equal(p.el('send').disabled,false);
});

test('business recovery refusal remains visible without being classified as a restart',async()=>{
  let calls=0;const p=await page({recoveryRoute:()=>{calls++;return Object.assign(new Error('[conflict] 恢复记录版本已变化'),{status:409});}});
  await p.ctx.watchJob(RUN,{job_id:'conflict',status:'failed',events:[]},0);
  assert.equal(calls,1);assert.match(p.el('project-read-status').textContent,/恢复记录版本已变化/);
  assert.equal(p.timers.filter(timer=>timer&&[1000,2000,4000,8000].includes(timer.ms)).length,0);
});

test('superseded ended job cannot apply a delayed recovery response or refresh the new job',async()=>{
  let calls=0;const p=await page({recoveryRoute:()=>{calls++;return closing();}});
  const read=p.ctx.watchJob(RUN,{job_id:'old',status:'failed',events:[]},0);await settle();
  p.read('jobWatch++;currentJob={job_id:"new",status:"running"};state.currentJob=currentJob;state.busy=true;');
  await p.nextRead();await read;
  assert.equal(calls,1);assert.equal(p.read('currentJob.job_id'),'new');assert.equal(p.read('currentRecovery'),null);
  assert.equal(p.observed.filter(call=>call.path.endsWith('/timeline')).length,0);
});
