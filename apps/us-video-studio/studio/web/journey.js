/* User journey: conversation → plan → storyboard images → video.
 * Version-bound storage and authorization stay in the existing domain APIs.
 */
"use strict";

function journeyStatus(brief, pending, interaction) {
  if (brief?.execution?.active) return '团队正在处理';
  if (interaction) return '有一个问题想和你确认';
  if (pending) return pending.stage === 'direction' ? '选一个你想继续的方向' : '完整方案等你确认';
  if (brief?.questions?.length) return '有一个细节想和你确认';
  if (brief?.phase === 'review_failed') return '方案已保存，内部检查未完成';
  if (brief?.stage === 'writing' || brief?.phase === 'ready') return '完善方案与画面';
  return '一起把想法变成画面';
}

function resumeDirection(action) {
  const current=state.timeline?.next_action;
  if (state.busy || !current || current.kind!=='resume_direction' ||
      current.expected_context_sha256!==action?.expected_context_sha256) {
    toast('方向所依据的资料可能已变化，请刷新项目后重试。',true);
    return;
  }
  return submitJob(state.runId, {request_kind:'direction_resume',
    request_id:crypto.randomUUID(), expected_context_sha256:current.expected_context_sha256,
    text:'按当前有效资料重做创意方向，完成后交给我点选。'});
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
  const next=state.busy||visibleTarget||view.action?.kind==='review_stage' ? null : view.action;
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
      if(action.kind==='resume_direction'){await resumeDirection(action);return;}
      if(action.kind==='question'){setProjectFocus('chat');$('journey-decision')?.scrollIntoView({block:'nearest'});return;}
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
  $('journey-tab-reference').hidden=analysis||!(state.referenceTasks||[]).some(task=>task.candidate);
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
  if (((photo || analysis) && state.canvasTab === 'video') ||
      (analysis && ['storyboard','assets','reference'].includes(state.canvasTab)) ||
      (state.canvasTab==='reference'&&$('journey-tab-reference').hidden))
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
  const briefRedo=tl.pending_brief_redo, nextAction=tl.next_action;
  const key=JSON.stringify([state.runId,q?.id,artifactQuestion?.stage,artifactQuestion?.version,artifactQuestion?.question,
    pending?.version,pending?.artifact_sha256,
    continuation?.expected_version,continuation?.expected_artifact_sha256,
    briefRedo?.amendment_id,nextAction?.expected_context_sha256,Boolean(state.busy)]);
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
  } else if (pending) {
    const step=(tl.rail || []).find(row => row.stage === pending.stage && row.version === pending.version);
    host.innerHTML=`<div class="conversation-decision"><p>当前作品已放在左侧工作区。看完后，在这里选择继续或说明修改意见。</p>` +
      `<button type="button" class="ghost small" data-view-plan>查看当前作品 ↗</button>${gateHtml(pending, step)}</div>` +
      (artifactQuestion ? artifactQuestionHtml(artifactQuestion) : '');
    host.querySelector('[data-view-plan]').onclick=()=>{showJourneyTab('plan',{stage:pending.stage});setProjectFocus('canvas');};
    wireGate(host);
    if (artifactQuestion) bindArtifactQuestion(host, artifactQuestion);
  } else if (artifactQuestion) {
    host.innerHTML=artifactQuestionHtml(artifactQuestion);
    bindArtifactQuestion(host, artifactQuestion);
  } else if (nextAction?.kind==='resume_direction') {
    host.innerHTML='<section class="conversation-question"><h3>创意方向等待更新</h3>'+
      '<p>将按当前有效需求和已选参考直接派导演，完成后把 A/B/C 交给你点选。会发起一次文字模型任务。</p>'+
      '<button type="button" class="primary" data-resume-direction>重做创意方向</button></section>';
    host.querySelector('[data-resume-direction]').onclick=()=>resumeDirection(nextAction);
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
let productionBatchIntent=null;
function batchStorageKey(runId){return `studio-production-batch:${runId}`;}
function currentBatchIntent(){
  if(!state.runId)return null;
  if(productionBatchIntent?.runId===state.runId)return productionBatchIntent;
  try{productionBatchIntent=JSON.parse(sessionStorage.getItem(batchStorageKey(state.runId))||'null');}
  catch(_){productionBatchIntent=null;}
  return productionBatchIntent?.runId===state.runId?productionBatchIntent:null;
}
function saveBatchIntent(intent){
  productionBatchIntent=intent;
  try{if(intent)sessionStorage.setItem(batchStorageKey(intent.runId),JSON.stringify(intent));
    else if(state.runId)sessionStorage.removeItem(batchStorageKey(state.runId));}catch(_){/* Retry identity remains in memory. */}
}
function openProductionBatch(kind){
  const data=state.productionData,runId=state.runId;
  if(!runId||!data||data.active||state.busy||state.timeline?.pending||state.timeline?.interaction){
    toast('当前任务结束后再核对整组画面。',true);return false;
  }
  if(kind==='generate'&&StudioProduction.batchBlocker(data,runId)){
    toast('整组中仍有首帧任务待核实，请先处理原任务。',true);return false;
  }
  const target=['video_prepare','video_generate','assemble'].includes(kind)?'video':'storyboard';
  const existing=currentBatchIntent();
  if(existing){
    if(existing.kind!==kind&&(existing.uncertain||existing.submitting)){
      toast('上一次整组请求仍待核实，请先处理原请求。',true);return false;
    }
    if(existing.kind===kind){
      if(state.canvasTab!==target)showJourneyTab(target);
      setProjectFocus('chat');renderProductionAction();return true;
    }
  }
  if(kind==='choose'){
    saveBatchIntent({kind,runId});
    if(state.canvasTab!=='storyboard')showJourneyTab('storyboard');
    setProjectFocus('chat');renderProductionAction();return true;
  }
  const selected=kind==='approve'?StudioProduction.batchApprovalImages(data,runId)
    :kind==='generate'?StudioProduction.batchGenerationDrafts(data,runId)
    :kind==='video_generate'?StudioProduction.videoBatchGenerationDrafts(data,runId)
    :kind==='assemble'?StudioProduction.assemblyClips(data,runId)
    :StudioProduction.videoSequenceState(data,runId).filter(shot=>shot.approved&&shot.reference);
  if(kind==='assemble'&&!data.edit_plan){
    toast('当前脚本还没有准确剪辑计划，请先完成计划再拼接。',true);return false;
  }
  if(!selected.length){toast(kind==='approve'?'当前没有待确认的生成图。':'当前没有可提交的首帧方案。',true);return false;}
  const intent={kind,runId,request_id:crypto.randomUUID(),
    ...(kind==='generate'?{connection_revision:data.capabilities?.image?.connection_revision}:{}),
    ...(kind==='approve'?{images:selected.map(item=>({name:item.output.name,sha256:item.output.sha256}))}
      :kind==='video_prepare'?{images:selected.map(item=>shotReference(item))}
      :kind==='assemble'?{clips:selected,edit_plan_id:data.edit_plan.request_id,
          edit_plan_sha256:data.edit_plan.plan_sha256}
      :{drafts:selected.map(item=>({draft_id:item.draft_id,expected_sha256:item.draft_sha256}))})};
  saveBatchIntent(intent);
  if(state.canvasTab!==target)showJourneyTab(target);
  setProjectFocus('chat');renderProductionAction();
  $('production-action')?.scrollIntoView({block:'nearest'});
  return true;
}
function shotReference(shot){return {name:shot.reference.name,sha256:shot.reference.sha256};}
async function submitProductionBatch(){
  const intent=currentBatchIntent();if(!intent||intent.submitting||state.runId!==intent.runId)return;
  intent.submitting=true;intent.error='';renderProductionAction();
  const path=intent.kind==='approve'?'/material-approvals/batch'
    :intent.kind==='video_prepare'?'/material-approvals/batch/continue'
    :intent.kind==='assemble'?'/assemble':'/batch-generate';
  const body={request_id:intent.request_id,
    ...(intent.kind==='approve'||intent.kind==='video_prepare'?{images:intent.images}:{}),
    ...(intent.kind==='generate'||intent.kind==='video_generate'?{drafts:intent.drafts,consent:true}:{}),
    ...(intent.kind==='generate'?{expected_connection_revision:intent.connection_revision}:{}),
    ...(intent.kind==='assemble'?{clips:intent.clips,edit_plan_id:intent.edit_plan_id,
      edit_plan_sha256:intent.edit_plan_sha256}:{}),
    ...(intent.kind==='video_prepare'||intent.kind==='assemble'?{}:{nonce:state.productionData?.nonce})};
  try{
    const result=await api(`/api/runs/${encodeURIComponent(intent.runId)}/production${path}`,
      {method:'POST',body:JSON.stringify(body)});
    if(state.runId!==intent.runId)return;
    saveBatchIntent(null);
    if(result.job_id||result.continuation?.job_id)void restoreJob(intent.runId);
    await refreshTimeline();
    toast(intent.kind==='approve'?`已一次确认 ${result.approved} 张生成图。${result.continuation?.status==='started'?'团队正在准备视频输入。':''}`
      :intent.kind==='video_prepare'?'团队正在准备逐段视频输入。'
      :intent.kind==='assemble'?'本地拼接已启动。'
      :`整组生成已启动；将按顺序处理 ${intent.drafts.length} ${intent.kind==='video_generate'?'段':'张'}。`);
  }catch(error){
    if(state.runId!==intent.runId)return;
    intent.submitting=false;
    if(StudioProduction.uncertain(error)){
      intent.uncertain=true;
      intent.error='提交结果待确认。请用下方按钮核实同一请求，不要重新发起。';
      saveBatchIntent(intent);
    }else{
      saveBatchIntent(null);
      toast(error.message||'整组操作未被接受，请刷新后核对当前版本。',true);
      try{await refreshTimeline();}catch(_){/* Error already shown. */}
    }
    renderProductionAction();
  }
}
function productionBatchCommand(text,available={}){
  const value=String(text||'').trim().replace(/^(?:好|好的|可以|行)[，,\s]*/,'').trim();
  if(value.length>64||/[，。；;\n]/.test(value)||/(不要|别|暂不|先不|修改|调整|问题|重写)/.test(value))return null;
  if(/(全部|所有|整组|这[一二三四五六七八九十\d]+段)/.test(value)&&/(视频|片段|视频段)/.test(value)){
    if(/准备/.test(value)&&available.video_prepare)return 'video_prepare';
    if(/(生成|制作|开始)/.test(value)&&available.video_generate)return 'video_generate';
  }
  if(/(拼接|合成).*(成片|视频|片段)/.test(value)&&available.assemble)return 'assemble';
  if(/^(?:批准|确认)(?:全部|所有|这批|这些)$/.test(value)||/^(?:按这版做|现在执行)$/.test(value)){
    const options=[['generate',available.generate],['approve',available.approve],
      ['video_prepare',available.video_prepare],['video_generate',available.video_generate],['assemble',available.assemble]]
      .filter(([,count])=>count);
    return options.length===1?options[0][0]:options.length>1?'choose':null;
  }
  // A whole-set request about shots. The position marker is optional: people say
  // 「按这六镜生成」as readily as 「生成全部首帧」, and the unit word is what says
  // a set is meant. A request that names one shot still carries a revision word
  // and is rejected above.
  if(!/(全部|所有|整组|这批|(?:这|那|按)?[一二三四五六七八九十百两\d]+个?(?:张|镜|段))/.test(value)
     ||!/(首帧|分镜|镜头|镜|图|画面)/.test(value))return null;
  if(/(确认|批准|通过|采用)/.test(value)){
    if(available.generate&&!available.approve&&!/(生成图|成图|结果|已出图)/.test(value))return 'generate';
    return 'approve';
  }
  if(/(生成|出图|开始制作)/.test(value))return 'generate';
  return null;
}
function renderProductionAction(){
  const host=$('production-action');
  if(!host)return;
  host.replaceChildren();
  if(!['storyboard','video'].includes(state.canvasTab)||!state.productionData||
      state.timeline?.pending||state.timeline?.interaction)return;
  const intent=currentBatchIntent();
  const blocker=state.canvasTab==='storyboard'
    ?StudioProduction.batchBlocker(state.productionData,state.runId):null;
  if(blocker&&intent?.kind==='generate'&&!intent.uncertain&&!intent.submitting)saveBatchIntent(null);
  if(blocker&&(!intent||intent.kind==='generate'&&!intent.uncertain&&!intent.submitting)){
    const drafts=(state.productionData.drafts||[]).filter(d=>d.kind==='image'&&d.source_ready!==false);
    const after=drafts.slice(blocker.index+1).filter(d=>!(d.attempts||[]).length).length;
    const position=String(blocker.index+1).padStart(2,'0');
    const retryAvailable=(state.productionData.recovery_modes||[]).includes('retry_with_unknown_cost');
    host.append(box('strong',null,`整组暂停在首帧 ${position}`),
      box('p',null,blocker.work.phase==='unknown'
        ?`这张图的提交结果还未确认；${after} 张后续首帧没有提交。先核实原任务，不能跳过这张直接生成其余图片。`
        :`这张图仍在处理中；${after} 张后续首帧尚未提交。`));
    if(blocker.work.phase==='unknown'&&!retryAvailable)host.append(
      box('p',null,'当前服务仍是旧版；接受未知费用后继续的入口，需服务更新后才能使用。'));
    const button=box('button','primary',blocker.work.phase==='unknown'?'核实这次图片任务':'查看这次图片任务');
    button.type='button';button.onclick=()=>openProduction({kind:'image',focusDraft:blocker.draft.draft_id});
    host.append(button);
    return;
  }
  if(intent){
    if(intent.kind==='choose'){
      host.append(box('strong',null,'“批准全部”需要明确范围'),
        box('p',null,'当前有多种操作可以继续。请选择这次要核对的范围。'));
      const unmade=StudioProduction.batchGenerationDrafts(state.productionData,state.runId).length;
      const unapproved=StudioProduction.batchApprovalImages(state.productionData,state.runId).length;
      if(unmade){const button=box('button','primary',`核对并生成 ${unmade} 张首帧`);
        button.onclick=()=>openProductionBatch('generate');host.append(button);}
      if(unapproved){const button=box('button','ghost',`查看并确认 ${unapproved} 张生成图`);
        button.onclick=()=>openProductionBatch('approve');host.append(button);}
      const shots=StudioProduction.videoSequenceState(state.productionData,state.runId);
      if(shots.length>1&&shots.every(shot=>shot.approved)&&shots.some(shot=>!shot.draft)){
        const button=box('button','ghost','准备整组视频输入');button.onclick=()=>openProductionBatch('video_prepare');host.append(button);}
      const videoCount=StudioProduction.videoBatchGenerationDrafts(state.productionData,state.runId).length;
      if(videoCount){const button=box('button','ghost',`核对并生成 ${videoCount} 段视频`);
        button.onclick=()=>openProductionBatch('video_generate');host.append(button);}
      if(state.productionData.edit_plan&&StudioProduction.assemblyClips(state.productionData,state.runId).length&&!StudioProduction.currentAssembly(state.productionData,state.runId)){
        const button=box('button','ghost','拼接成片');button.onclick=()=>openProductionBatch('assemble');host.append(button);}
      return;
    }
    const html=StudioProduction.batchConfirmationHtml(intent,state.productionData,state.runId);
    if(!html){host.append(box('p',null,'本次核对的版本已变化。请刷新后重新选择整组画面。'));
      const reset=box('button','ghost small','重新选择');reset.onclick=()=>{saveBatchIntent(null);renderProductionAction();};
      host.append(reset);return;}
    host.innerHTML=html;
    const confirm=host.querySelector('[data-batch-confirm]');
    const images=[...host.querySelectorAll('img')];
    const update=()=>{confirm.disabled=Boolean(intent.submitting)||images.some(img=>!img.complete||!img.naturalWidth);};
    images.forEach(img=>{img.onload=update;img.onerror=update;});update();
    if(intent.uncertain)confirm.textContent='核实同一请求';
    confirm.onclick=submitProductionBatch;
    host.querySelector('[data-batch-dismiss]').onclick=()=>{if(intent.uncertain||intent.submitting)return;
      saveBatchIntent(null);renderProductionAction();};
    host.querySelector('[data-batch-status]').textContent=intent.error||'';
    return;
  }
  if(state.canvasTab==='video'&&(state.productionData.video_sequence||[]).length>1){
    const shots=StudioProduction.videoSequenceState(state.productionData,state.runId);
    const ready=shots.every(shot=>shot.approved&&shot.reference);
    const missing=shots.filter(shot=>!shot.draft).length;
    const blocked=shots.find(shot=>shot.unresolved);
    const clips=StudioProduction.assemblyClips(state.productionData,state.runId);
    const assembly=StudioProduction.currentAssembly(state.productionData,state.runId);
    host.append(box('strong',null,`${shots.length} 段视频流程`));
    if(!ready){
      host.append(box('p',null,'先完成整组首帧出图与准确图片确认。'));
      const button=box('button','primary','查看整组首帧');button.onclick=()=>showJourneyTab('storyboard');host.append(button);return;
    }
    if(missing){
      host.append(box('p',null,`仍有 ${missing} 段缺少视频输入；团队将按已确认脚本和五张首帧准备逐段描述。`));
      const button=box('button','primary','准备整组视频输入');button.onclick=()=>openProductionBatch('video_prepare');host.append(button);return;
    }
    if(blocked){
      host.append(box('p',null,`第 ${blocked.index} 段任务待核实；后续视频不会跳过提交。`));
      const button=box('button','primary','查看这段任务');button.onclick=()=>openProduction({kind:'video',focusDraft:blocked.draft.draft_id});host.append(button);return;
    }
    const unmade=StudioProduction.videoBatchGenerationDrafts(state.productionData,state.runId);
    if(unmade.length){
      const total=unmade.reduce((sum,draft)=>sum+(draft.estimate?.amount||0),0);
      host.append(box('p',null,`已取回 ${shots.length-unmade.length}/${shots.length} 段。待生成 ${unmade.length} 段，公开原价估算合计 CNY ${total.toFixed(2)}；请核对逐段输入与费用。`));
      const button=box('button','primary',`核对并顺序生成 ${unmade.length} 段`);
      button.onclick=()=>openProductionBatch('video_generate');host.append(button);return;
    }
    if(assembly){
      host.append(box('p',null,'成片已保存。请播放核对画面、口播和衔接。'));
      const link=box('a','primary','下载成片');link.href=assembly.output.url;
      link.download=assembly.output.name;host.append(link);return;
    }
    if(clips.length===shots.length){
      const plan=state.productionData.edit_plan;
      host.append(box('p',null,plan?`${clips.length} 段已取回，可按当前脚本的 ${plan.cuts.length} 个画面切点拼接 ${plan.duration_seconds} 秒成片。`:
        '视频段已取回，当前脚本的准确剪辑计划尚未保存。'));
      if(plan){const button=box('button','primary','核对剪辑计划并拼接成片');button.onclick=()=>openProductionBatch('assemble');host.append(button);}
      return;
    }
  }
  const kind=state.canvasTab==='storyboard'?'image':'video';
  const imageDrafts=kind==='image'?(state.productionData.drafts||[]).filter(d=>d.kind==='image'&&d.source_ready!==false):[];
  if(imageDrafts.length>1){
    const generated=imageDrafts.filter(d=>StudioProduction.currentProductionWork({...state.productionData,drafts:[d]},'image',state.runId).outputUrl).length;
    const unmade=StudioProduction.batchGenerationDrafts(state.productionData,state.runId).length;
    const unapproved=StudioProduction.batchApprovalImages(state.productionData,state.runId).length;
    host.append(box('strong',null,`${imageDrafts.length} 张首帧方案`),
      box('p',null,`已生成 ${generated} 张。要修改哪条，点画面区的“提意见”后直接在这里说。`));
    if(unmade){const button=box('button','primary',`核对并一次生成 ${unmade} 张`);
      button.type='button';button.onclick=()=>openProductionBatch('generate');host.append(button);}
    if(unapproved){const button=box('button','ghost',`一次确认 ${unapproved} 张生成图`);
      button.type='button';button.onclick=()=>openProductionBatch('approve');host.append(button);}
    return;
  }
  const work=StudioProduction.currentProductionWork(state.productionData,kind,state.runId);
  const heading=box('strong',null,work.label);
  const detail=box('p',null,work.detail);
  host.append(heading,detail);
  if(work.primaryAction==='none')return;
  if(work.primaryAction==='download'&&work.outputUrl){
    const link=box('a','primary','下载当前作品');
    link.href=work.outputUrl;link.download=work.outputName||'作品';host.append(link);
  }else{
    const button=box('button','primary',kind==='image'&&work.phase==='ready'?'查看出图输入与费用':'核对并继续');
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
  $('journey-reference').hidden=tab!=='reference';
  $('journey-storyboard').hidden=tab!=='storyboard';
  $('journey-video').hidden=tab!=='video';
  if(tab==='plan'){
    const candidate=(options.stage && (state.rail||[]).some(s=>s.stage===options.stage) ? options.stage : null)
      || state.timeline?.pending?.stage || (state.rail||[]).find(s=>s.stage==='writing'&&s.has_doc)?.stage
      || (state.rail||[]).find(s=>s.stage==='direction'&&s.has_doc)?.stage || 'intake';
    selectStage(candidate);
  } else if(tab==='assets') renderCanvasMaterials();
  else if(tab==='reference') renderProjectReference();
  else {
    const kind=tab==='storyboard'?'image':'video';
    const host=$(tab==='storyboard'?'journey-storyboard':'journey-video');
    host.innerHTML=state.productionData
      ? StudioProduction.canvasHtml(state.productionData,kind,state.runId,state.rail)
      : '<div class="creator-production-empty">正在读取当前作品…</div>';
    (host.querySelectorAll?.('[data-feedback-image-draft]')||[]).forEach(button=>{
      button.onclick=()=>{
        const draft=(state.productionData?.drafts||[]).find(row=>row.draft_id===button.dataset.feedbackImageDraft);
        if(draft)setProductionFeedbackTarget(state.runId,draft,null);
      };
    });
    (host.querySelectorAll?.('[data-feedback-image-attempt]')||[]).forEach(button=>{
      button.onclick=()=>{
        const attempt=(state.productionData?.drafts||[]).flatMap(row=>row.attempts||[])
          .find(row=>row.attempt_id===button.dataset.feedbackImageAttempt);
        if(attempt)setMediaFeedbackTarget(state.runId,attempt,'image',null);
      };
    });
  }
  renderProductionAction();
  syncJourneyTabs();
  if(state.creatorView)renderJourneyStatus();
}

function referenceAnalysisHtml(task,materials) {
  return StudioReferenceVideo.breakdownHtml(task,state.runId);
}
function wireReferenceAnalysis(host) {
  host.querySelector('[data-reference-open-materials]')?.addEventListener('click',()=>toggleMaterials(true));
  StudioReferenceVideo.mountBreakdowns(host,{runId:state.runId,tasks:state.referenceTasks,
    request:api,reload:refreshTimeline,notify:toast,compose:composeReferencePrompt});
}
function renderReferenceAnalysis() {
  const host=$('stage');
  const selected=(state.referenceTasks||[]).find(task=>task.reference_id===state.referenceSelectedId)
    ||state.creatorView?.reference;
  host.innerHTML=referenceAnalysisHtml(selected,state.materials);
  wireReferenceAnalysis(host);
}

function composeReferencePrompt(task){
  const source=task.source_url||task.name;
  const input=$('chat-input');
  if(!input||!source)return;
  input.value=`请把这条参考视频的可借鉴表达适配到当前商品，只交付可编辑提示词。按原片观察到的时间段写镜头、动作、台词或字卡及商品证据；全片风格只写有依据的部分。核对本款商品事实，不照搬原片价格、承诺或市场语言；缺失事实标为待确认。参考：${source}`;
  setProjectFocus('chat');input.focus();
}

function renderProjectReference(){
  const host=$('journey-reference'),tasks=(state.referenceTasks||[]).filter(task=>task.candidate);
  if(!tasks.length){host.innerHTML='<p>当前项目还没有已分析的参考视频。</p>';return;}
  const selected=tasks.find(task=>task.reference_id===state.referenceSelectedId)||tasks.at(-1);
  state.referenceSelectedId=selected.reference_id;
  host.innerHTML=(tasks.length>1?`<label class="reference-picker">选择参考视频 <select data-reference-select>${tasks.map(task=>
    `<option value="${escapeHtml(task.reference_id)}" ${task.reference_id===selected.reference_id?'selected':''}>${escapeHtml(task.title||task.name||task.source_url||'参考视频')}</option>`).join('')}</select></label>`:'')+
    StudioReferenceVideo.breakdownHtml(selected,state.runId);
  host.querySelector('[data-reference-select]')?.addEventListener('change',event=>{
    state.referenceSelectedId=event.target.value;renderProjectReference();
  });
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
  $('chat-reference-attach').onclick=()=>{if(!state.busy)$('chat-reference-files').click();};
  const isVideoFile=file=>file.type.startsWith('video/')||/\.(mp4|mov|m4v|webm)$/i.test(file.name);
  for(const [pickerId,use] of [['chat-files','product'],['chat-reference-files','reference']]){
    const picker=$(pickerId);
    picker.onchange=()=>{
      const selected=[...picker.files];picker.value='';
      if(state.busy||!selected.length)return;
      if(use==='reference'&&selected.some(file=>!isVideoFile(file))){toast('参考视频入口只能添加视频。',true);return;}
      StudioComposer.setFiles(state.runId,selected,use);StudioComposer.render();
    };
  }
  const form=$('start-conversation'),input=$('start-prompt'),files=$('start-files'),referenceFiles=$('start-reference-files'),tray=$('start-attachments'),status=$('start-status');
  let selected=[],videoUses={},frozen=null,sending=false;
  try {frozen=JSON.parse(sessionStorage.getItem('studio-first-message')||'null');}catch(_){}
  if(frozen){input.value=frozen.text;status.textContent='上次发送尚未完成。再次发送会接续同一次请求；如有附件，请重新选择原文件。';}
  function drawFiles(){
    tray.replaceChildren();
    selected.forEach((file,i)=>{
      const chip=box('span','attachment-chip',file.name),remove=box('button','ghost small','移除');
      if(isVideoFile(file)&&(!frozen||frozen.files?.[i]?.use)){
        const select=document.createElement('select');select.setAttribute('aria-label',`${file.name} 的用途`);
        for(const [value,label] of [['','这个视频用于…'],['product','补全商品信息'],['reference','拆解参考表达']]){
          const option=document.createElement('option');option.value=value;option.textContent=label;select.append(option);
        }
        select.value=videoUses[i]||'';select.disabled=!!frozen;
        select.onchange=()=>{if(select.value)videoUses[i]=select.value;else delete videoUses[i];};
        chip.append(select);
      }
      remove.type='button';remove.disabled=!!frozen;
      remove.onclick=()=>{
        selected.splice(i,1);
        videoUses=Object.fromEntries(Object.entries(videoUses).filter(([key])=>Number(key)!==i).map(([key,use])=>[Number(key)>i?Number(key)-1:Number(key),use]));
        drawFiles();
      };
      chip.append(remove);tray.append(chip);
    });
  }
  for(const [picker,use] of [[files,'product'],[referenceFiles,'reference']])picker.onchange=()=>{
    const chosen=[...picker.files];picker.value='';
    if(sending||!chosen.length)return;
    if(use==='reference'&&chosen.some(file=>!isVideoFile(file))){status.textContent='参考视频入口只能添加视频。';return;}
    selected=chosen;
    videoUses=frozen
      ?Object.fromEntries((frozen.files||[]).map((item,i)=>[i,item.use]).filter(([,value])=>value))
      :Object.fromEntries(selected.flatMap((file,i)=>isVideoFile(file)?[[i,use]]:[]));
    status.textContent='';drawFiles();
  };
  $('start-attach').onclick=()=>files.click();
  $('start-reference-attach').onclick=()=>referenceFiles.click();
  input.onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();form.requestSubmit();}};
  form.onsubmit=async e=>{
    e.preventDefault();if(sending||state.decisionBusy)return;
    if(frozen&&!sessionStorage.getItem('studio-first-message'))frozen=null;
    const text=input.value.trim() || (selected.length?'我附上了素材，请按标注用途处理。':'');if(!text){status.textContent='说一句想法，或附上素材就可以。';input.focus();return;}
    if(!frozen&&selected.some((file,i)=>isVideoFile(file)&&!videoUses[i])){status.textContent='请先为每段视频选择用途：补全商品信息或拆解参考表达。';return;}
    if(frozen&&frozen.text!==text){status.textContent='上次发送还未确认，请保留原文字重试，避免重复创建。';return;}
    sending=true;for(const control of form.querySelectorAll('button,input,textarea'))control.disabled=true;
    try{
      const manifest=await Promise.all(selected.map(async (f,i)=>({name:f.name,sha256:[...new Uint8Array(await crypto.subtle.digest('SHA-256',await f.arrayBuffer()))].map(x=>x.toString(16).padStart(2,'0')).join(''),use:frozen&&!(frozen.files?.[i]&&'use' in frozen.files[i])?undefined:(videoUses[i]||null)})));
      if(!frozen){frozen={request_id:crypto.randomUUID(),text,files:manifest};sessionStorage.setItem('studio-first-message',JSON.stringify(frozen));}
      if(JSON.stringify(frozen.files)!==JSON.stringify(manifest))throw new Error('请重新选择上次发送的原附件，文件与顺序保持一致。');
      status.textContent='正在整理你的想法与素材…';
      const run=await api('/api/conversations',{method:'POST',body:JSON.stringify({request_id:frozen.request_id,text:frozen.text})});
      for(let i=0;i<selected.length;i++){
        const body=new FormData();body.append('file',selected[i],selected[i].name);
        const response=await fetch(`/api/runs/${run.run_id}/materials?retry_sha256=${manifest[i].sha256}`,{method:'POST',body});
        if(!response.ok){const failure=await response.json();throw new Error(failure.detail||'附件尚未上传完成，请重试。');}
      }
      await loadRuns();await selectRun(run.run_id);setStudioView('project');setProjectFocus('canvas');
      const request={text:frozen.text+(manifest.length?'\n本次附上的素材：'+manifest.map(f=>f.name+(f.use==='product'?'（商品视频，用于补全商品信息）':f.use==='reference'?'（参考视频，用于拆解表达）':'')).join('、'):''),request_id:frozen.request_id};
      await submitJob(run.run_id,request);
      // submitJob durably retains uncertain requests under this run's own key.
      sessionStorage.removeItem('studio-first-message');frozen=null;selected=[];videoUses={};input.value='';drawFiles();status.textContent='';
    }catch(error){status.textContent=error.message||'发送未完成，内容已保留，请重试。';}
    finally{sending=false;for(const control of form.querySelectorAll('button,input,textarea'))control.disabled=false;}
  };
}

if(typeof module!=='undefined')module.exports={journeyStatus,questionCardHtml,artifactQuestionHtml,showJourneyTab,bindJourneyTabs,shouldShowQuestionInTurn,bindQuestionChoices,productionBatchCommand};
