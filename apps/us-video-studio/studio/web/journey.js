/* User journey: conversation → plan → storyboard images → video.
 * Version-bound storage and authorization stay in the existing domain APIs.
 */
"use strict";

function journeyStatus(brief, pending, interaction) {
  if (brief?.execution?.active) return '团队正在处理';
  if (interaction) return '有一个问题想和你确认';
  if (brief?.questions?.length) return '有一个细节想和你确认';
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
    '<p class="muted">点选会先填入对话框；可以补充或修改，发送后团队才会继续。</p><p role="status" data-feedback></p></section>';
}

function artifactQuestionHtml(q) {
  return `<section class="conversation-question artifact-question" data-artifact-question="${escapeHtml(q.stage)}:${escapeHtml(q.version)}">` +
    `<h3>有个细节想和你确认</h3><p class="artifact-question-text">${escapeHtml(q.question)}</p>` +
    (q.reason ? `<p>${escapeHtml(q.reason)}</p>` : '') +
    `<textarea rows="3" data-artifact-answer aria-label="回答：${escapeHtml(q.question)}" placeholder="说说现有条件；没有或不确定也可以，团队会调整拍法。"></textarea>` +
    `<button type="button" class="primary" data-send-artifact-answer>发送回答</button>` +
    `<p role="status" data-feedback></p></section>`;
}

function bindArtifactQuestion(host, q) {
  const button = host.querySelector('[data-send-artifact-answer]');
  const input = host.querySelector('[data-artifact-answer]');
  const feedback = host.querySelector('[data-feedback]');
  if (!button || !input) return;
  const runId = state.runId;
  button.onclick = async () => {
    if (state.busy || state.decisionBusy || state.runId !== runId) return;
    const answer = input.value.trim();
    if (!answer) { feedback.textContent = '说说目前知道的情况；不确定也可以直接说明。'; input.focus(); return; }
    const current = (state.timeline?.questions || []).some(item =>
      item.stage === q.stage && item.version === q.version && item.question === q.question);
    if (!current) { feedback.textContent = '这个问题已更新，请刷新当前项目再回答。'; return; }
    button.disabled = true;
    // The prose answer format is accepted by both the current and earlier
    // service process while a local restart is pending.
    const result = await send(`关于「${q.question}」：${answer}`);
    if (state.runId !== runId) return;
    if (result?.accepted === false) {
      feedback.textContent = result.reason || '发送未完成，回答仍保留在这里。';
      button.disabled = false;
    }
  };
}

/** One structural rule: only server-tagged turns get the interactive clarification.
 *  Copy equality between reply text and interaction.message is not used here.
 */
function shouldShowQuestionInTurn(turn, interaction) {
  if (!interaction || !turn?.pending_clarification_id) return false;
  return turn.pending_clarification_id === interaction.id;
}

function bindQuestionChoices(root, q) {
  if (!root || !q?.options) return;
  const runId = state.runId;
  for (const button of root.querySelectorAll('[data-question-choice]')) {
    button.onclick = () => {
      if (state.busy || state.runId !== runId) return;
      const choice = q.options[Number(button.dataset.questionChoice)];
      if (!choice) return;
      const input = $('chat-input');
      if (!input) return;
      if (state.feedbackTarget && typeof clearFeedbackTarget === 'function') clearFeedbackTarget();
      const selected = `关于「${q.question}」：${choice.label}`;
      const prior = state.clarificationChoices?.get(runId);
      const draft = input.value.trim();
      const rest = prior?.id === q.id && draft.startsWith(prior.text)
        ? draft.slice(prior.text.length).trimStart() : draft;
      input.value = [selected, rest].filter(Boolean).join('\n');
      if (!state.clarificationChoices) state.clarificationChoices = new Map();
      state.clarificationChoices.set(runId, {id: q.id, text: selected});
      state.composerVersion = (state.composerVersion || 0) + 1;
      input.style.height = 'auto';
      input.style.height = `${Math.min(input.scrollHeight, 200)}px`;
      root.querySelector('[data-feedback]').textContent = '已填入对话框；补充后再发送。';
      setProjectFocus('chat');
      input.focus();
    };
  }
}

function renderJourneyStatus() {
  const view=state.creatorView;
  if (!view) { $('project-progress').replaceChildren(); $('project-progress').hidden=true; return; }
  const visibleTarget=view.action?.kind==='navigate' && view.action.tab===state.canvasTab &&
    (view.action.tab!=='plan'||!view.action.stage||view.action.stage===state.activeStage);
  const next=state.busy||visibleTarget ? null : view.action;
  // The conversation already shows execution progress. Keep only an actionable
  // shortcut in the top bar when its destination is not on screen.
  const host=$('project-progress');
  host.replaceChildren();
  host.hidden=!next;
  if(next){
    const button=box('button','ghost small creator-next',next.label);
    button.type='button';
    button.setAttribute('data-creator-next','');
    button.onclick=async()=>{
      const action=view.action;
      if(action.kind==='continue'){$('journey-decision')?.querySelector('[data-resume-decision]')?.click();return;}
      if(action.kind==='brief-redo'){$('journey-decision')?.querySelector('[data-resume-brief-redo]')?.click();return;}
      if(action.kind==='question'){setProjectFocus('chat');$('journey-decision')?.scrollIntoView({block:'nearest'});return;}
      if(action.kind==='review_stage'){
        button.disabled=true;
        submitJob(state.runId,{request_kind:'stage_review',request_id:crypto.randomUUID(),stage:action.stage,
          expected_version:action.version,expected_artifact_sha256:action.sha256});return;
      }
      if(action.kind==='reference'){toggleMaterials(true);return;}
      if(action.kind==='settings'){$('connections-open')?.click();return;}
      if(action.kind==='job'){setProjectFocus('chat');openTeamProgress();return;}
      if(action.kind==='chat'){setProjectFocus('chat');$('chat-input').focus();return;}
      if(action.kind==='prepare'){
        await showJourneyTab(action.tab);
        setProjectFocus('chat');
        $('production-action')?.scrollIntoView({block:'nearest'});
        return;
      }
      if(action.kind==='navigate'){
        showJourneyTab(action.tab);
        if(action.tab==='plan'&&action.stage)selectStage(action.stage);
        setProjectFocus('canvas');
        const target=action.tab==='plan' ? document.getElementById('stage-decision')||$('stage')
          : document.querySelector('.production-embedded .production-card')||$(action.tab==='video'?'journey-video':'journey-storyboard');
        target?.scrollIntoView({block:'nearest'});
      }
    };
    host.append(button);
  }
  if(typeof StudioComposer!=='undefined')StudioComposer.render();
}

function configureJourneyTabs() {
  const run=(state.runs || []).find(item=>item.run_id===state.runId);
  const photo=(state.creatorView?.format||run?.content_format)==='photo';
  const analysis=Boolean(state.creatorView?.analysisOnly);
  $('journey-tab-plan').textContent=analysis?'分析与参考':'创意方向';
  $('journey-tab-assets').hidden=analysis;
  $('journey-tab-script').hidden=analysis;
  $('journey-tab-script').textContent=photo?'图文方案':'脚本';
  const hasWriting=(state.timeline?.rail||[]).some(step=>step.stage==='writing'&&step.has_doc);
  const hasMedia=Boolean(state.productionData?.drafts?.length);
  $('journey-tab-script').disabled=!hasWriting;
  $('journey-tab-storyboard').textContent=photo?'图片':'画面';
  $('journey-tab-storyboard').hidden=analysis;
  $('journey-tab-storyboard').disabled=!hasWriting&&!hasMedia;
  $('journey-tab-video').hidden=photo||analysis;
  $('journey-tab-video').disabled=!hasWriting&&!hasMedia;
  if (((photo || analysis) && state.canvasTab === 'video') || (analysis && ['storyboard','assets'].includes(state.canvasTab)))
    showJourneyTab('plan');
  syncJourneyTabs();
}

function syncJourneyTabs() {
  const active=state.canvasTab==='plan'
    ? state.activeStage==='writing' && !$('journey-tab-script').disabled ? 'script' : 'plan'
    : state.canvasTab;
  for(const button of document.querySelectorAll('[data-journey-tab]')){
    const selected=button.id===`journey-tab-${active}`;
    button.setAttribute('aria-selected',String(selected));
    button.tabIndex=selected?0:-1;
  }
  $('stage').setAttribute('aria-labelledby',`journey-tab-${active==='script'?'script':'plan'}`);
}

function renderJourneyDecision() {
  const host=$('journey-decision'),tl=state.timeline;
  if (!host || !tl) return;
  const q=tl.interaction, artifactQuestion=tl.questions?.[0], pending=tl.pending, continuation=tl.pending_continuation;
  const briefRedo=tl.pending_brief_redo;
  const key=JSON.stringify([state.runId,q?.id,artifactQuestion?.stage,artifactQuestion?.version,artifactQuestion?.question,
    pending?.version,pending?.artifact_sha256,
    continuation?.expected_version,continuation?.expected_artifact_sha256,
    briefRedo?.amendment_id,Boolean(state.busy)]);
  if(host.dataset.key===key)return;
  host.dataset.key=key;
  host.innerHTML='';
  if (state.busy) return;
  if (briefRedo) {
    host.innerHTML='<section class="conversation-question"><h3>简报修订已保存</h3><p>原方向已不适用，但团队重做还没有启动。请检查模型连接，然后继续重做方向。</p><button type="button" class="primary" data-resume-brief-redo>继续重做方向</button><p role="status" data-feedback></p></section>';
    const redoButton=host.querySelector('[data-resume-brief-redo]'), runId=state.runId;
    redoButton.onclick=async()=>{
      redoButton.disabled=true;
      try {
        await api(`/api/runs/${encodeURIComponent(runId)}/brief-amendment/${encodeURIComponent(briefRedo.amendment_id)}/redo`,{method:'POST',body:'{}'});
        if(runId!==state.runId)return;
        state.canvasTouched=false;
        try { await refreshTimeline(); }
        catch { const feedback=host.querySelector('[data-feedback]');if(feedback)feedback.textContent='任务已接受，页面暂时无法刷新；正在恢复运行状态。'; }
        void restoreJob(runId);
      } catch(err) { const feedback=host.querySelector('[data-feedback]');if(feedback)feedback.textContent=err.message;redoButton.disabled=false; }
    };
  } else if (continuation) {
    host.innerHTML='<section class="conversation-question"><h3>决定已保存</h3><p>下一步尚未启动。请检查模型连接，然后继续准备。</p><button type="button" class="primary" data-resume-decision>继续准备</button><p role="status" data-feedback></p></section>';
    const button=host.querySelector('[data-resume-decision]'), runId=state.runId;
    button.onclick=async()=>{
      button.disabled=true;
      try {
        await api(`/api/runs/${encodeURIComponent(runId)}/decision-continuation`,{method:'POST',body:JSON.stringify(continuation)});
        if(runId!==state.runId)return;
        state.canvasTouched=false;
        try { await refreshTimeline(); }
        catch { const feedback=host.querySelector('[data-feedback]');if(feedback)feedback.textContent='任务已接受，页面暂时无法刷新；正在恢复运行状态。'; }
        void restoreJob(runId);
      } catch(err) { const feedback=host.querySelector('[data-feedback]');if(feedback)feedback.textContent=err.message;button.disabled=false; }
    };
  } else if (q) {
    // Footer only hosts clarifications no conversation turn already owns.
    const owned = Array.isArray(tl.turns) && tl.turns.some(t => t.pending_clarification_id === q.id);
    if (owned) { host.innerHTML=''; return; }
    host.innerHTML=questionCardHtml(q);
    bindQuestionChoices(host, q);
  } else if (artifactQuestion) {
    host.innerHTML=artifactQuestionHtml(artifactQuestion);
    bindArtifactQuestion(host, artifactQuestion);
  } else if (pending) {
    const step=(tl.rail || []).find(row => row.stage === pending.stage && row.version === pending.version);
    host.innerHTML=`<div class="conversation-decision"><p>当前作品已放在左侧工作区。看完后，在这里选择继续或说明修改意见。</p>` +
      `<button type="button" class="ghost small" data-view-plan>查看当前作品 ↗</button>${gateHtml(pending, step)}</div>`;
    host.querySelector('[data-view-plan]').onclick=()=>{showJourneyTab('plan',{stage:pending.stage});setProjectFocus('canvas');};
    wireGate(host);
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
function renderProductionAction(){
  const host=$('production-action');
  if(!host)return;
  host.replaceChildren();
  if(!['storyboard','video'].includes(state.canvasTab)||!state.productionData||
      state.timeline?.pending||state.timeline?.interaction)return;
  const kind=state.canvasTab==='storyboard'?'image':'video';
  const work=StudioProduction.currentProductionWork(state.productionData,kind,state.runId);
  const heading=box('strong',null,work.label);
  const detail=box('p',null,work.detail);
  host.append(heading,detail);
  if(work.primaryAction==='none')return;
  if(work.primaryAction==='download'&&work.outputUrl){
    const link=box('a','primary','下载当前作品');
    link.href=work.outputUrl;link.download=work.outputName||'作品';host.append(link);
  }else{
    const button=box('button','primary','核对并继续');
    button.type='button';
    button.onclick=()=>openProduction({kind});
    host.append(button);
  }
}
function showJourneyTab(tab, options={}) {
  const button=tab==='plan' && options.stage==='writing' ? $('journey-tab-script') : $(`journey-tab-${tab}`);
  if(button?.hidden||button?.disabled)return;
  if(!options.automatic)state.canvasTouched=true;
  closeEmbeddedProduction();
  state.canvasTab=tab;
  $('stage').hidden=tab!=='plan';
  $('journey-assets').hidden=tab!=='assets';
  $('journey-storyboard').hidden=tab!=='storyboard';
  $('journey-video').hidden=tab!=='video';
  if(tab==='plan'){
    const candidate=(options.stage && (state.rail||[]).some(s=>s.stage===options.stage) ? options.stage : null)
      || state.timeline?.pending?.stage || (state.rail||[]).find(s=>s.stage==='writing'&&s.has_doc)?.stage
      || (state.rail||[]).find(s=>s.stage==='direction'&&s.has_doc)?.stage || 'intake';
    selectStage(candidate);
  } else if(tab==='assets') renderCanvasMaterials();
  else {
    const kind=tab==='storyboard'?'image':'video';
    $(tab==='storyboard'?'journey-storyboard':'journey-video').innerHTML=state.productionData
      ? StudioProduction.canvasHtml(state.productionData,kind,state.runId,state.rail)
      : '<div class="creator-production-empty">正在读取当前作品…</div>';
  }
  renderProductionAction();
  syncJourneyTabs();
  if(state.creatorView)renderJourneyStatus();
}

function referenceAnalysisHtml(task,materials) {
  if(!task)return '<section class="creator-analysis"><h2>参考分析</h2><p>分析尚未完成，可在素材与参考中查看任务状态。</p><button type="button" class="ghost" data-show-analysis>查看参考任务</button></section>';
  const material=(materials||[]).find(item=>item.name===task.name&&item.kind==='video'&&item.usable);
  const video=material?`<video controls preload="metadata" src="${escapeHtml(materialUrl(material.name))}" aria-label="本次分析的参考视频"></video>`:'';
  return `<section class="creator-analysis"><span class="creator-kicker">本轮任务 · 参考分析</span>`+
    `<h2>${escapeHtml(task.title||'参考视频拆解报告')}</h2>`+
    (task.name?`<p class="muted">指定参考：${escapeHtml(task.name)}</p>`:'')+video+
    (task.report?`<div class="creator-analysis-report"><h3>观察与可借鉴方法</h3><div>${escapeHtml(task.report).replace(/\n/g,'<br>')}</div></div>`:'<p>报告尚未保存，请查看参考任务状态。</p>')+
    `<p class="muted">模型观察未经人工核对；不会自动进入制作。</p>`+
    `<button type="button" class="ghost" data-show-analysis>查看参考来源与记录</button></section>`;
}
function wireReferenceAnalysis(host) {
  host.querySelector('[data-show-analysis]')?.addEventListener('click',()=>toggleMaterials(true));
}
function renderReferenceAnalysis() {
  const host=$('stage');
  host.innerHTML=referenceAnalysisHtml(state.creatorView?.reference,state.materials);
  wireReferenceAnalysis(host);
}

function bindJourneyTabs(){
  const tabs=[...document.querySelectorAll('[data-journey-tab]')];
  for(const button of tabs){
    button.onclick=()=>showJourneyTab(button.dataset.journeyTab,{stage:button.dataset.planStage});
    button.onkeydown=event=>{
      const visible=tabs.filter(tab=>!tab.hidden&&!tab.disabled),index=visible.indexOf(button);
      const next=event.key==='ArrowRight'?visible[(index+1)%visible.length]
        :event.key==='ArrowLeft'?visible[(index+visible.length-1)%visible.length]
        :event.key==='Home'?visible[0]:event.key==='End'?visible[visible.length-1]:null;
      if(!next)return;
      event.preventDefault();next.focus();showJourneyTab(next.dataset.journeyTab,{stage:next.dataset.planStage});
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

if(typeof module!=='undefined')module.exports={journeyStatus,questionCardHtml,artifactQuestionHtml,showJourneyTab,bindJourneyTabs,shouldShowQuestionInTurn,bindQuestionChoices};
