/* Version-bound feedback beside the artifact. No model call; preview precedes apply. */
"use strict";
(function (root) {
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const stages = {intake:'商品与需求',direction:'内容方向',material_plan:'素材计划',writing:'脚本与文案'};
  const scopes = {user_preference:'局部偏好',direction:'内容方向',persona_dialogue:'人物与说话方式',pace:'节奏',product_fact:'商品事实',input_drift:'制作输入偏移',generation_execution:'生成执行',tool_failure:'工具故障'};
  function binding(runId, step) {
    if (!runId || !step?.has_doc || !Object.hasOwn(stages, step.stage) || !Number.isInteger(step.version) || step.version < 1 || !/^[a-f0-9]{64}$/i.test(step.artifact_sha256 || '') || step.integrity_ok === false) return null;
    return Object.freeze({runId, stage:step.stage, version:step.version, artifact_sha256:step.artifact_sha256});
  }
  const key = b => `studio-inline-feedback:${b.runId}:${b.stage}:${b.version}:${b.artifact_sha256}`;
  const same = (a, b) => !!a && !!b && a.runId===b.runId && a.stage===b.stage && a.version===b.version && a.artifact_sha256===b.artifact_sha256;
  function payload(bound, draft, learning=root.StudioLearning) {
    if (!bound) throw new Error('请刷新工作台，核对这份稿件的版本后再记录反馈。');
    const result=learning.feedbackPayload(bound, draft.quote || '', draft.scope || 'user_preference');
    const location=String(draft.location || '').trim();
    return {...result, observation:location ? `具体位置：${location}\n用户原话：\n${result.quote}` : result.quote};
  }
  function relevant(proposals, bound) {
    return (Array.isArray(proposals)?proposals:[]).filter(p => p.type==='feedback' && p.run_id===bound.runId && p.payload?.stage===bound.stage && p.payload?.version===bound.version && p.payload?.artifact_sha256===bound.artifact_sha256);
  }
  function formHtml(bound, draft={}) {
    return `<div class="inline-feedback-intro"><div><h3>这份稿件哪里需要调整？</h3><p>${esc(stages[bound.stage])} · 第 ${esc(bound.version)} 版。先预览反馈，再确认保存；不会自动改稿或调用模型。</p></div><button type="button" class="ghost small" data-feedback-records>全部反馈记录 ↗</button></div><form data-feedback-form><div class="inline-feedback-fields"><label>具体位置 <span>选填</span><input name="location" maxlength="500" placeholder="例如：方向 B 的开头 / 第 2 个镜头" value="${esc(draft.location)}"></label><label>反馈范围<select name="scope">${Object.entries(scopes).map(([id,title])=>`<option value="${id}"${id===(draft.scope||'user_preference')?' selected':''}>${title}</option>`).join('')}</select></label></div><label>你的原话<textarea name="quote" rows="3" maxlength="12000" required placeholder="直接写下你的判断，可以只针对一个细节。">${esc(draft.quote)}</textarea></label><div class="inline-feedback-form-footer"><span>未提交的文字仅暂存在本窗口。</span><button type="submit" class="primary">预览反馈</button></div></form><div class="inline-feedback-preview"></div><div class="inline-feedback-history"></div>`;
  }
  function historyHtml(proposals) {
    return `<h4>这份稿件的反馈</h4>${proposals.length?proposals.map(p=>`<button type="button" class="inline-feedback-record" data-feedback-proposal="${esc(p.id)}"><span>${esc(p.payload?.quote)}</span><small>${p.status==='applied'?'已保存':'待确认'} · 查看</small></button>`).join(''):'<p class="inline-feedback-empty">还没有反馈记录。</p>'}`;
  }
  function previewHtml(proposal, learning=root.StudioLearning) {
    const data=proposal.payload||{},pending=proposal.status==='pending';
    const actions={diagnose:'先诊断问题',local_change:'局部调整',stop:'暂停',select:'选择方向',continue:'继续'};
    const observation=String(data.observation||''),quote=String(data.quote||''),suffix=`\n用户原话：\n${quote}`;
    const hasLocation=observation.startsWith('具体位置：')&&observation.endsWith(suffix);
    const location=hasLocation?observation.slice('具体位置：'.length,-suffix.length):'';
    const note=!hasLocation&&observation!==quote?observation:'';
    return `<section class="inline-feedback-review"><span class="learning-kicker">${pending?'等待确认':'已保存'}</span><h3>${pending?'新增':'已保存'} 1 条反馈</h3><p class="inline-feedback-target">${esc(stages[data.stage]||data.stage)} · 第 ${esc(data.version)} 版</p><dl class="inline-feedback-summary"><div><dt>具体位置</dt><dd>${esc(location||'未限定具体位置')}</dd></div><div><dt>反馈范围</dt><dd>${esc(scopes[data.scope]||data.scope)}</dd></div><div><dt>后续处理建议</dt><dd>${esc(actions[data.action]||data.action||'先诊断问题')}</dd></div></dl><h4>保留你的原话</h4><blockquote class="inline-feedback-quote">${esc(quote)}</blockquote>${note?`<h4>补充说明</h4><p class="inline-feedback-observation">${esc(note)}</p>`:''}<p class="learning-note">${pending?'保存后，这条反馈将归入上面这份稿件。':'这条反馈已归入上面这份稿件。'}不会修改原稿、改变批准状态或启动制作。</p><details class="learning-details learning-diff-review"><summary>查看完整变更记录</summary>${learning.diffHtml(proposal.diff)}</details><details class="learning-details learning-audit"><summary>核对来源与版本</summary>${learning.fields({stage:data.stage,version:data.version,artifact_sha256:data.artifact_sha256,base_library_sha256:proposal.base_library_sha256,candidate_sha256:proposal.candidate_sha256})}</details>${pending?`<label class="inline-feedback-confirm"><input type="checkbox" data-feedback-reviewed>我已核对以上稿件、具体位置和反馈内容</label><button type="button" class="primary" data-learning-action="apply" data-id="${esc(proposal.id)}">确认并保存</button>`:''}</section>`;
  }
  function createActions(request, bound, current, learning=root.StudioLearning) {
    let busy=false;
    const actions=learning.createActions(request,bound.runId);
    const perform=async fn=>{if(busy)throw new Error('反馈正在保存，请稍候。');if(!current(bound))throw new Error('项目或稿件版本已变化。原输入已保留，请回到原稿核对后再提交。');busy=true;try{return await fn();}finally{busy=false;}};
    return {preview:draft=>perform(()=>actions.preview('feedback',payload(bound,draft,learning))), apply:proposal=>perform(()=>{
      if(!relevant([proposal],bound).length)throw new Error('这条反馈不属于当前查看的稿件。');
      return actions.apply(proposal);
    })};
  }
  root.StudioInlineFeedback={binding,key,same,payload,relevant,formHtml,historyHtml,previewHtml,createActions};
  if(typeof document==='undefined')return;
  const records=new Map();
  const readDraft=id=>{try{return JSON.parse(sessionStorage.getItem(id)||'null')||{};}catch(_){return {};}};
  const persist=(id,draft)=>{try{sessionStorage.setItem(id,JSON.stringify(draft));}catch(_){/* In-memory draft still survives stage refreshes. */}};
  async function request(path, options) {
    let response;try{response=await fetch(path,{headers:{'content-type':'application/json'},...options});}catch(_){throw new Error('暂时无法连接工作台。输入已保留，可稍后重试。');}
    let body;try{body=await response.json();}catch(_){body={};}
    if(!response.ok){const error=new Error(typeof body.detail==='string'?body.detail:`请求失败（${response.status}）`);error.status=response.status;throw error;}return body;
  }
  root.wireInlineFeedback=function(host,runId,step) {
    if(!step.has_doc)return;
    const anchor=host.querySelector('.pane-doc');if(!anchor)return;
    const bound=binding(runId,step);
    const section=document.createElement('details');section.className='inline-feedback';
    section.innerHTML='<summary>留下具体反馈 <span>不启动制作</span></summary><div class="inline-feedback-body"></div><p class="inline-feedback-status" role="status" aria-live="polite"></p>';
    anchor.after(section);
    const body=section.querySelector('.inline-feedback-body'),status=section.querySelector('.inline-feedback-status');
    if(!bound){body.innerHTML='<p>这份稿件的版本暂时无法核对，请刷新工作台后再记录反馈。</p>';return;}
    const id=key(bound);
    if(!records.has(id))records.set(id,{draft:readDraft(id),open:false,busy:false,proposal:null,proposals:[],message:'',error:false,loaded:false,revision:0});
    const record=records.get(id);
    const active=()=>typeof state!=='undefined' && state.runId===runId && state.activeStage===step.stage && same(bound,binding(runId,(state.rail||[]).find(s=>s.stage===step.stage)));
    const writable=()=>{if(!active())return false;if(state.busy||state.decisionBusy)throw new Error('当前制作仍在运行，请结束后再整理反馈。输入已保留。');return true;};
    const actions=createActions(request,bound,writable);
    record.paint=()=>{if(!section.isConnected)return;status.textContent=record.message;status.classList.toggle('error',record.error);body.querySelectorAll('input,textarea,select,button').forEach(n=>n.disabled=record.busy);const apply=body.querySelector('[data-learning-action="apply"]');if(apply)apply.disabled=record.busy||!body.querySelector('[data-feedback-reviewed]')?.checked;};
    function showPreview() {
      const preview=body.querySelector('.inline-feedback-preview');preview.innerHTML=record.proposal?previewHtml(record.proposal):'';
      const confirmation=preview.querySelector('[data-feedback-reviewed]');if(confirmation)confirmation.onchange=record.paint;
      record.paint();
    }
    function draw() {
      if(!section.isConnected)return;
      body.innerHTML=formHtml(bound,record.draft);showPreview();
      body.querySelector('.inline-feedback-history').innerHTML=historyHtml(record.proposals);
      body.querySelector('[data-feedback-records]').onclick=()=>{if(active())root.StudioLearning.open?.({tab:'feedback'});};
      record.paint();
    }
    record.draw=draw;
    async function perform(fn) {
      if(record.busy)return;
      record.busy=true;record.message='正在保存…';record.error=false;record.paint();
      try{await fn();}catch(error){record.error=true;record.message=error.status===409?'暂未保存：稿件、学习记录或执行状态已经变化。输入已保留，请待制作结束后重新预览并核对差异。':error.message;}finally{record.busy=false;record.paint();}
    }
    async function loadHistory() {
      const revision=record.revision;
      try{const data=await request(`/api/runs/${encodeURIComponent(runId)}/learning`);if(revision!==record.revision)return;record.proposals=relevant(data.proposals,bound).reverse();record.loaded=true;if(section.isConnected){body.querySelector('.inline-feedback-history').innerHTML=historyHtml(record.proposals);record.paint();}}catch(_){if(!record.message){record.message='反馈记录暂时无法读取；你填写的文字已保留。';record.paint();}}
    }
    section.open=record.open;draw();
    section.addEventListener('toggle',()=>{record.open=section.open;if(section.open&&!record.loaded)loadHistory();});
    body.addEventListener('input',event=>{
      if(!event.target.closest('[data-feedback-form]')||record.busy)return;
      const form=body.querySelector('form');record.draft={location:form.elements.location.value,quote:form.elements.quote.value,scope:form.elements.scope.value};persist(id,record.draft);
      record.proposal=null;record.message='未提交的文字已暂存。';record.error=false;showPreview();
    });
    body.addEventListener('submit',event=>{
      event.preventDefault();if(!event.target.matches('[data-feedback-form]'))return;
      perform(async()=>{record.revision++;record.proposal=await actions.preview(record.draft);record.proposals=[record.proposal,...record.proposals.filter(p=>p.id!==record.proposal.id)];record.message='反馈预览已准备好。请核对下方稿件、位置和原话，再确认保存。';record.draw();});
    });
    body.addEventListener('click',event=>{
      const entry=event.target.closest('[data-feedback-proposal]');if(entry&&!record.busy){record.proposal=record.proposals.find(p=>p.id===entry.dataset.feedbackProposal);showPreview();return;}
      const apply=event.target.closest('[data-learning-action="apply"]');if(!apply||!body.querySelector('[data-feedback-reviewed]')?.checked)return;
      perform(async()=>{record.revision++;const result=await actions.apply(record.proposal);record.proposal=result;record.proposals=record.proposals.map(p=>p.id===result.id?result:p);record.message='反馈已保存，绑定这份稿件与具体范围。没有启动改稿或模型调用。';record.draw();});
    });
  };
})(typeof globalThis!=='undefined'?globalThis:this);
