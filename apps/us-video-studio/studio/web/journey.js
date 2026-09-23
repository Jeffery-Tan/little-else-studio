/* User journey: conversation → plan → storyboard images → video.
 * Version-bound storage and authorization stay in the existing domain APIs.
 */
"use strict";

function journeyStatus(brief, pending, interaction) {
  if (brief?.execution?.active) return '团队正在处理';
  if (interaction) return '有一个问题想和你确认';
  if (pending) return pending.stage === 'direction' ? '选一个你想继续的方向' : '完整方案等你确认';
  if (brief?.phase === 'review_failed') return '方案已保存，内部检查未完成';
  if (brief?.stage === 'writing' || brief?.phase === 'ready') return '完善方案与画面';
  return '一起把想法变成画面';
}

function questionCardHtml(q) {
  if (!q) return '';
  return `<section class="conversation-question" data-question="${escapeHtml(q.id)}"><h3>${escapeHtml(q.question)}</h3>` +
    (q.context ? `<p>${escapeHtml(q.context)}</p>` : '') +
    `<div class="reference-quick-choices">${q.options.map((o,i)=>`<button type="button" class="ghost" data-question-choice="${i}"><strong>${escapeHtml(o.label)}${i===q.recommended?' · 推荐':''}</strong>${o.description?`<span> ${escapeHtml(o.description)}</span>`:''}</button>`).join('')}</div>` +
    '<p class="muted">点一个选项，或直接在下方对话中回答。</p><p role="status" data-feedback></p></section>';
}

function journeyGateHtml(pending, step) {
  return `<section class="conversation-question"><h3>${pending.stage==='direction'?'方向等你决定':'完整方案等你确认'}</h3>` +
    '<p>请在创作板阅读当前版本并提交决定。选择、意见和确认只保留在这一处。</p>' +
    '<button type="button" class="primary" data-view-plan>阅读并决定</button></section>';
}

function renderJourneyStatus() {
  const tl=state.timeline, brief=state.projectBrief || tl?.brief;
  const run=(state.runs || []).find(item=>item.run_id===state.runId) || tl?.run || {};
  const format=run.content_format;
  const status=tl?.pending_continuation?'决定已保存，下一步尚未启动':journeyStatus(brief,tl?.pending,tl?.interaction);
  const kind=format==='photo'?'图文作品':format==='video'?'视频作品':'创作目标待确认';
  const next=state.busy?'':tl?.pending_continuation?'继续保存的决定':tl?.pending?'查看并决定':tl?.interaction?'回答当前问题'
    :brief?.action?.kind==='activity'?'':brief?.action?.label||'';
  $('project-progress').innerHTML=`<section class="creator-goal" aria-label="当前作品与下一步">`+
    `<div class="creator-goal-main"><div class="creator-goal-heading"><span class="creator-kicker">当前作品 · ${escapeHtml(kind)}</span><h1>${escapeHtml(run.title||'未命名作品')}</h1></div>`+
    `<p class="creator-goal-request"><strong>你的要求</strong> ${escapeHtml(run.brief||'创作目标仍待确认')}</p>`+
    `<p class="creator-goal-current"><span class="small-dot"></span><strong>${escapeHtml(status)}</strong>`+
    (brief?.detail?`<span>${escapeHtml(brief.detail)}</span>`:'')+`</p></div>`+
    (next?`<button type="button" class="primary creator-next" data-creator-next>${escapeHtml(next)}</button>`:'')+
    `</section>`;
  const button=$('project-progress').querySelector('[data-creator-next]');
  if(button)button.onclick=()=>{
    if(tl?.pending_continuation){$('journey-decision')?.querySelector('[data-resume-decision]')?.click();return;}
    if(tl?.pending){showJourneyTab('plan');selectStage(tl.pending.stage);setProjectFocus('canvas');
      document.getElementById('stage-decision')?.scrollIntoView({block:'nearest'});return;}
    if(tl?.interaction){setProjectFocus('chat');$('journey-decision')?.scrollIntoView({block:'nearest'});return;}
    const action=brief?.action||{};
    if(action.kind==='review_stage'){
      button.disabled=true;
      submitJob(state.runId,{request_kind:'stage_review',request_id:crypto.randomUUID(),stage:action.stage,
        expected_version:action.version,expected_artifact_sha256:action.sha256});return;
    }
    if(action.kind==='delivery'){openDelivery();return;}
    if(action.kind==='recovery'){setProjectFocus('chat');$('job-status')?.scrollIntoView({block:'nearest'});return;}
    if(action.stage){showJourneyTab('plan');selectStage(action.stage);}
    if(action.kind==='draft')prepareProjectDraft(action.prompt);
    else setProjectFocus('canvas');
  };
  if(typeof StudioComposer!=='undefined')StudioComposer.render();
}

function configureJourneyTabs() {
  const run=(state.runs || []).find(item=>item.run_id===state.runId);
  const photo=run?.content_format==='photo';
  const analysis=!run?.content_format && state.currentJob?.job_kind==='reference_video';
  $('journey-tab-plan').textContent=analysis?'分析与参考':photo?'创意与文案':'创意与脚本';
  $('journey-tab-storyboard').textContent=photo?'图片':'画面';
  $('journey-tab-storyboard').hidden=analysis;
  $('journey-tab-video').hidden=photo||analysis;
  if (((photo || analysis) && state.canvasTab === 'video') || (analysis && state.canvasTab === 'storyboard'))
    showJourneyTab('plan');
}

function renderJourneyDecision() {
  const host=$('journey-decision'),tl=state.timeline;
  if (!host || !tl) return;
  const q=tl.interaction, pending=tl.pending, continuation=tl.pending_continuation;
  const key=JSON.stringify([state.runId,q?.id,pending?.version,pending?.artifact_sha256,
    continuation?.expected_version,continuation?.expected_artifact_sha256,Boolean(state.busy)]);
  if(host.dataset.key===key)return;
  host.dataset.key=key;
  host.innerHTML='';
  if (state.busy) return;
  if (continuation) {
    host.innerHTML='<section class="conversation-question"><h3>决定已保存</h3><p>下一步尚未启动。请检查模型连接，然后继续准备。</p><button type="button" class="primary" data-resume-decision>继续准备</button><p role="status" data-feedback></p></section>';
    const button=host.querySelector('[data-resume-decision]'), runId=state.runId;
    button.onclick=async()=>{
      button.disabled=true;
      try {
        await api(`/api/runs/${encodeURIComponent(runId)}/decision-continuation`,{method:'POST',body:JSON.stringify(continuation)});
        if(runId!==state.runId)return;
        try { await refreshTimeline(); }
        catch { const feedback=host.querySelector('[data-feedback]');if(feedback)feedback.textContent='任务已接受，页面暂时无法刷新；正在恢复运行状态。'; }
        void restoreJob(runId);
      } catch(err) { const feedback=host.querySelector('[data-feedback]');if(feedback)feedback.textContent=err.message;button.disabled=false; }
    };
  } else if (q) {
    host.innerHTML=questionCardHtml(q);
    const runId=state.runId;
    for(const button of host.querySelectorAll('[data-question-choice]'))button.onclick=async()=>{
      if(state.busy||state.runId!==runId)return;
      const choice=q.options[Number(button.dataset.questionChoice)];if(!choice)return;
      await submitJob(runId,{text:`关于「${q.question}」：${choice.label}`,question_id:q.id,request_id:crypto.randomUUID()});
    };
  } else if (pending) {
    host.innerHTML=journeyGateHtml(pending,tl.rail.find(s=>s.stage===pending.stage));
    host.querySelector('[data-view-plan]').onclick=()=>{showJourneyTab('plan');selectStage(pending.stage);setProjectFocus('canvas');};
  }
}

function scrollToJourneyDecision(host) {
  const card=host.querySelector('#journey-decision .conversation-question');
  if(!card)return false;
  // A long decision must open at its question, not at its last button.
  host.scrollTop+=card.getBoundingClientRect().top-host.getBoundingClientRect().top-12;
  return true;
}
function closeEmbeddedProduction(){document.querySelector('.production-embedded')?.studioClose?.();}
function showJourneyTab(tab) {
  if($(`journey-tab-${tab}`)?.hidden)return;
  closeEmbeddedProduction();
  state.canvasTab=tab;
  for(const button of document.querySelectorAll('[data-journey-tab]')){
    const selected=button.dataset.journeyTab===tab;
    button.setAttribute('aria-selected',String(selected));button.tabIndex=selected?0:-1;
  }
  $('stage').hidden=tab!=='plan';
  $('journey-storyboard').hidden=tab!=='storyboard';
  $('journey-video').hidden=tab!=='video';
  if(tab==='plan'){
    const candidate=state.timeline?.pending?.stage || (state.rail||[]).find(s=>s.stage==='writing'&&s.has_doc)?.stage || (state.rail||[]).find(s=>s.stage==='direction'&&s.has_doc)?.stage || 'intake';
    selectStage(candidate);
  } else openProduction({mount:$(tab==='storyboard'?'journey-storyboard':'journey-video'),kind:tab==='storyboard'?'image':'video'});
}

function bindJourneyTabs(){
  const tabs=[...document.querySelectorAll('[data-journey-tab]')];
  for(const button of tabs){
    button.onclick=()=>showJourneyTab(button.dataset.journeyTab);
    button.onkeydown=event=>{
      const visible=tabs.filter(tab=>!tab.hidden),index=visible.indexOf(button);
      const next=event.key==='ArrowRight'?visible[(index+1)%visible.length]
        :event.key==='ArrowLeft'?visible[(index+visible.length-1)%visible.length]
        :event.key==='Home'?visible[0]:event.key==='End'?visible[visible.length-1]:null;
      if(!next)return;
      event.preventDefault();next.focus();showJourneyTab(next.dataset.journeyTab);
    };
  }
}

function startFromConversation(){
  if(state.decisionBusy)return;
  setStudioView('home');$('start-prompt').focus();$('start-prompt').scrollIntoView({block:'center'});
}

function initJourney() {
  bindJourneyTabs();
  $('chat-attach').onclick=()=>{if(!state.busy)$('chat-files').click();};
  $('chat-files').onchange=()=>{if(!state.busy){StudioComposer.setFiles(state.runId,$('chat-files').files);StudioComposer.render();}$('chat-files').value='';};
  const form=$('start-conversation'),input=$('start-prompt'),files=$('start-files'),tray=$('start-attachments'),status=$('start-status');
  let selected=[],frozen=null,sending=false;
  try {frozen=JSON.parse(sessionStorage.getItem('studio-first-message')||'null');}catch(_){}
  if(frozen){input.value=frozen.text;status.textContent='上次发送尚未完成。再次发送会接续同一次请求；如有附件，请重新选择原文件。';}
  function drawFiles(){
    tray.replaceChildren();
    selected.forEach((file,i)=>{const chip=box('span','attachment-chip',file.name);const remove=box('button','ghost small','移除');remove.type='button';remove.disabled=!!frozen;remove.onclick=()=>{selected.splice(i,1);drawFiles();};chip.append(remove);tray.append(chip);});
  }
  files.onchange=()=>{if(!sending){selected=[...files.files];drawFiles();}files.value='';};
  $('start-attach').onclick=()=>files.click();
  input.onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();form.requestSubmit();}};
  form.onsubmit=async e=>{
    e.preventDefault();if(sending||state.decisionBusy)return;
    if(frozen&&!sessionStorage.getItem('studio-first-message'))frozen=null;
    const text=input.value.trim() || (selected.length?'我附上了素材，请先确认我想如何使用。':'');if(!text){status.textContent='说一句想法，或附上素材就可以。';input.focus();return;}
    if(frozen&&frozen.text!==text){status.textContent='上次发送还未确认，请保留原文字重试，避免重复创建。';return;}
    sending=true;for(const control of form.querySelectorAll('button,input,textarea'))control.disabled=true;
    try{
      const manifest=await Promise.all(selected.map(async f=>({name:f.name,sha256:[...new Uint8Array(await crypto.subtle.digest('SHA-256',await f.arrayBuffer()))].map(x=>x.toString(16).padStart(2,'0')).join('')})));
      if(!frozen){frozen={request_id:crypto.randomUUID(),text,files:manifest};sessionStorage.setItem('studio-first-message',JSON.stringify(frozen));}
      if(JSON.stringify(frozen.files)!==JSON.stringify(manifest))throw new Error('请重新选择上次发送的原附件，文件与顺序保持一致。');
      status.textContent='正在整理你的想法与参考素材…';
      const run=await api('/api/conversations',{method:'POST',body:JSON.stringify({request_id:frozen.request_id,text:frozen.text})});
      for(let i=0;i<selected.length;i++){
        const body=new FormData();body.append('file',selected[i],selected[i].name);
        const response=await fetch(`/api/runs/${run.run_id}/materials?retry_sha256=${manifest[i].sha256}`,{method:'POST',body});
        if(!response.ok){const failure=await response.json();throw new Error(failure.detail||'附件尚未上传完成，请重试。');}
      }
      await loadRuns();await selectRun(run.run_id);setStudioView('project');setProjectFocus('canvas');
      const request={text:frozen.text+(manifest.length?'\n本次附上的素材：'+manifest.map(f=>f.name).join('、'):''),request_id:frozen.request_id};
      await submitJob(run.run_id,request);
      // submitJob durably retains uncertain requests under this run's own key.
      sessionStorage.removeItem('studio-first-message');frozen=null;selected=[];input.value='';drawFiles();status.textContent='';
    }catch(error){status.textContent=error.message||'发送未完成，内容已保留，请重试。';}
    finally{sending=false;for(const control of form.querySelectorAll('button,input,textarea'))control.disabled=false;}
  };
}

if(typeof module!=='undefined')module.exports={journeyStatus,questionCardHtml,journeyGateHtml,showJourneyTab,bindJourneyTabs};
