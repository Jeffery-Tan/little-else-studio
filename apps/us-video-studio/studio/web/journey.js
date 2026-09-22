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
  const options=pending.options || [];
  const cards=(step?.blocks || []).find(b=>b.type==='cards')?.cards || [];
  return `<section class="gate conversation-question" data-journey="1" data-stage="${escapeHtml(pending.stage)}" data-version="${pending.version}" data-sha="${escapeHtml(pending.artifact_sha256)}" data-passed="${pending.passed?'1':'0'}">` +
    `<h3>${pending.stage==='direction'?'你更想往哪个方向做？':'这份方案可以开始准备分镜了吗？'}</h3>` +
    `<p>${pending.passed?'方案已整理在左侧，你可以查看后确认，也可以提出修改。':'内部检查发现需要调整的地方。建议先说说你的意见，由团队完善。'}</p><button class="ghost small" data-view-plan>查看完整方案</button>` +
    (options.length?`<fieldset class="gate-options"><legend>选择方向</legend>${options.map(o=>{
      const card=cards.find(c=>c.head===o.key || String(c.head).startsWith(o.key));
      const claim=card?.fields?.find(f=>f.label==='主张')?.value || `方向 ${o.key}`;
      return `<label class="conversation-choice"><input type="radio" name="direction-choice" value="${escapeHtml(o.key)}"><span><strong>${escapeHtml(o.key)}${o.recommended?'<small>推荐</small>':''}</strong><span>${escapeHtml(claim)}</span></span></label>`;
    }).join('')}</fieldset>`:'') +
    '<textarea class="reason" aria-label="决定意见" rows="2" maxlength="6000" placeholder="有其他想法？在这里补充…"></textarea><p class="gate-feedback" role="status"></p>' +
    `<div class="gate-actions">${pending.passed?`<button class="primary" data-act="approve">${pending.stage==='direction'?'按此方向完善方案':'确认方案，准备分镜'}</button>`:''}<button class="ghost" data-act="reject">按补充意见调整</button></div>` +
    (!pending.passed?`<details><summary>查看检查意见与其他决定</summary>${(pending.findings||[]).map(f=>`<p>${escapeHtml(f.issue)}</p>`).join('')}<button class="ghost" data-act="approve">填写理由，仍按此方案继续</button></details>`:'') +
    '<small>图片和视频生成前，会另行展示内容与费用供你确认。</small></section>';
}

function renderJourneyStatus() {
  const tl=state.timeline, brief=state.projectBrief || tl?.brief;
  $('project-progress').innerHTML=`<div class="journey-status"><span class="small-dot"></span><span>${escapeHtml(journeyStatus(brief,tl?.pending,tl?.interaction))}</span><span class="journey-path">想法 → 方案 → 分镜 → 成片</span></div>`;
  const mode=tl?.creation_mode;
  if(mode){const label=box('span','attachment-chip',({autonomous:'自主创作',recreate:'按参考复刻',inspiration:'局部借鉴'})[mode.mode]||'');label.title=mode.scope||'可以直接在对话中调整';$('project-progress').append(label);}
  if(typeof StudioComposer!=='undefined')StudioComposer.render();
}

function renderJourneyDecision() {
  const host=$('journey-decision'),tl=state.timeline;
  if (!host || !tl) return;
  const q=tl.interaction, pending=tl.pending;
  const key=JSON.stringify([state.runId,q?.id,pending?.artifact_sha256,Boolean(state.busy)]);
  if(host.dataset.key===key)return;
  host.dataset.key=key;
  host.innerHTML='';
  if (state.busy) return;
  if (q) {
    host.innerHTML=questionCardHtml(q);
    const runId=state.runId;
    for(const button of host.querySelectorAll('[data-question-choice]'))button.onclick=async()=>{
      if(state.busy||state.runId!==runId)return;
      const choice=q.options[Number(button.dataset.questionChoice)];if(!choice)return;
      await submitJob(runId,{text:`关于「${q.question}」：${choice.label}`,question_id:q.id,request_id:crypto.randomUUID()});
    };
  } else if (pending) {
    host.innerHTML=journeyGateHtml(pending,tl.rail.find(s=>s.stage===pending.stage));
    wireGate(host);
    host.querySelector('[data-view-plan]').onclick=()=>{showJourneyTab('plan');selectStage(pending.stage);setProjectFocus('canvas');};
  } else if (['review_failed','interrupted'].includes((state.projectBrief||tl.brief)?.phase)) {
    host.innerHTML='<section class="conversation-question"><h3>方案已经保留</h3><p>本轮内部检查没能完成。你可以先看方案、直接说修改意见，或让团队接着处理。</p><button class="primary" data-resume>让团队继续处理</button></section>';
    host.querySelector('[data-resume]').onclick=()=>submitJob(state.runId,{text:'请继续处理当前方案未完成的内部检查，保留当前内容，不重复改写。完成后展示我需要决定的内容。',request_id:crypto.randomUUID()});
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
  closeEmbeddedProduction();
  state.canvasTab=tab;
  for(const button of document.querySelectorAll('[data-journey-tab]'))button.setAttribute('aria-selected',String(button.dataset.journeyTab===tab));
  $('stage').hidden=tab!=='plan';$('journey-media').hidden=tab==='plan';
  if(tab==='plan'){
    const candidate=state.timeline?.pending?.stage || (state.rail||[]).find(s=>s.stage==='writing'&&s.has_doc)?.stage || (state.rail||[]).find(s=>s.stage==='direction'&&s.has_doc)?.stage || 'intake';
    selectStage(candidate);
  } else openProduction({mount:$('journey-media'),kind:tab==='storyboard'?'image':'video'});
}

function startFromConversation(){
  if(state.decisionBusy)return;
  setStudioView('home');$('start-prompt').focus();$('start-prompt').scrollIntoView({block:'center'});
}

function initJourney() {
  for(const button of document.querySelectorAll('[data-journey-tab]'))button.onclick=()=>showJourneyTab(button.dataset.journeyTab);
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
      await loadRuns();await selectRun(run.run_id);setStudioView('project');setProjectFocus('chat');
      const request={text:frozen.text+(manifest.length?'\n本次附上的素材：'+manifest.map(f=>f.name).join('、'):''),request_id:frozen.request_id};
      await submitJob(run.run_id,request);
      // submitJob durably retains uncertain requests under this run's own key.
      sessionStorage.removeItem('studio-first-message');frozen=null;selected=[];input.value='';drawFiles();status.textContent='';
    }catch(error){status.textContent=error.message||'发送未完成，内容已保留，请重试。';}
    finally{sending=false;for(const control of form.querySelectorAll('button,input,textarea'))control.disabled=false;}
  };
}

if(typeof module!=='undefined')module.exports={journeyStatus,questionCardHtml,journeyGateHtml};
