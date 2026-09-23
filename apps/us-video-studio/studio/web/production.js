/* Explicit user-led production. GET polling never starts or repeats a generation. */
"use strict";
(function(root){
  const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  function mediaUrl(value,runId){
    const prefix=`/api/runs/${encodeURIComponent(runId)}/materials/`;
    if(typeof value!=='string'||!value.startsWith(prefix)||/[?#\\]/.test(value))return '';
    try{const name=decodeURIComponent(value.slice(prefix.length));return name&&!/[\/\\]/.test(name)&&name!=='.'&&name!=='..'?value:'';}catch(_){return '';}
  }
  function price(estimate){return typeof estimate?.amount==='number'&&Number.isFinite(estimate.amount)&&estimate.amount>=0?`${esc(estimate.currency||'')} ${Number(estimate.amount).toFixed(2)}（估算）`:'费用未知';}
  function reviewGate(draft,data){
    if(data.active)return '制作团队正在执行，请等待本轮结束';
    if(draft.source_ready===false)return '关联稿件已变化，请核对后保存新方案';
    return '';
  }
  function gate(draft,data){
    const common=reviewGate(draft,data);if(common)return common;
    if(!data.capabilities?.[draft.kind]?.available)return data.capabilities?.[draft.kind]?.reason||'该制作方式尚未就绪';
    if(draft.source_ready===false)return '关联稿件已变化，请核对后保存新方案';
    if(draft.review?.verdict!=='ready')return '先完成本版本的制作检查';
    if(draft.kind==='video'&&(!draft.reference||draft.reference_approved!==true))return '先查看并确认本张参考图';
    if(draft.can_generate===false)return draft.reason||'当前方案暂不能生成，请核对记录';
    if((draft.attempts||[]).some(a=>['queued','running','preparing','submitting','submitted','downloading','unknown','submission_unknown','recovering'].includes(a.status)))return '已有制作任务，请先查询或核实结果';
    return '';
  }
  function submission(values,data,requestId){
    const source=(data.sources||[]).find(s=>s.version===Number(values.source_version)&&s.artifact_sha256===values.source_sha256);
    if(!source||!source.ready)throw new Error('请选择已就绪的准确脚本版本。');
    if(!['image','video'].includes(values.kind))throw new Error('请选择制作类型。');
    const material=(data.materials||[]).find(m=>m.name===values.reference_name&&m.sha256===values.reference_sha256&&m.kind==='image');
    if(values.reference_name&&!material)throw new Error('参考图已变化，请重新选择。');
    if(values.kind==='video'&&!material)throw new Error('请明确选择视频使用的参考图。');
    if(!String(values.prompt||'').trim())throw new Error('请填写制作描述。');
    const model=values.kind==='video'?(data.capabilities?.video?.models||[]).find(m=>m.id===values.model):null;
    const duration=values.kind==='video'?Number(values.duration):null;
    if(values.kind==='video'&&(!model||!Number.isInteger(duration)||duration<model.durations.min||duration>model.durations.max||!model.resolutions.includes(values.resolution)))throw new Error('请核对视频模型、时长与画质。');
    return {request_id:requestId,kind:values.kind,prompt:values.prompt.trim(),source_version:source.version,source_sha256:source.artifact_sha256,reference:material?{name:material.name,sha256:material.sha256}:null,model:values.kind==='image'?'codex-image':model.id,duration,resolution:values.kind==='image'?null:values.resolution};
  }
  function canRecoverUsage(attempt,data){return !data.active&&['unknown','unresolved'].includes(attempt?.usage_status)&&!['queued','running','preparing','submitting','submitted','downloading','recovering'].includes(attempt.status);}
  function usageQuantity(value,kind){const quantity=Number(value);if(String(value??'').trim()===''||!Number.isFinite(quantity)||quantity<0||quantity>10000||(kind==='image'&&!Number.isSafeInteger(quantity)))throw new Error(kind==='video'?'请按提供方凭证填写有效的非负视频秒数。':'请按提供方凭证填写非负整数图片用量。');return quantity;}
  function usageFormHtml(kind){const video=kind==='video';return `<form class="production-confirm production-editor"><h3>核实${video?'视频':'图片'}用量</h3><label>提供方确认的${video?'计费时长（秒）':'图片用量'}<input name="quantity" type="number" min="0" max="10000" step="${video?'any':'1'}" required placeholder="请按提供方凭证填写"></label><label>凭证与核实依据<textarea name="evidence_note" rows="3" maxlength="2000" required></textarea></label><p class="form-note">${video?'请填写提供方确认的实际计费秒数；不按方案时长或下载文件推算。':'不按生成文件数量推算，不将订阅视为零费用。'}仅补录确认的用量，不发起新的生成。</p><div class="production-actions"><button type="submit" class="primary">保存用量凭证</button><button type="button" class="ghost" data-dismiss>返回</button></div></form>`;}
  function reviewHtml(review){if(!review)return '';const checks={opening:'开场',progression:'推进',payoff:'回报',product:'商品作用',execution:'执行与限制'};return `<div class="production-review"><strong>${review.verdict==='ready'?'文字制作检查通过':'文字制作检查需要调整'}</strong><p class="form-note">${esc(review.scope||'仅检查文字与已记录证据，未查看参考图像素；不代表图审或成片验收。')}</p>${(review.findings||[]).map(f=>`<p>${esc(typeof f==='string'?f:f.message||f.description||f.title||'请核对制作输入。')}</p>`).join('')}<details><summary>查看五项检查依据</summary>${Object.entries(checks).map(([key,label])=>`<h4>${label}</h4><p>${esc(review.checks?.[key]||'未提供检查依据')}</p>`).join('')}</details></div>`;}
  function uncertain(error){return ![400,403,404,409,422].includes(error?.status);}
  function actionRequest(action,draft,nonce,requestId,previous){
    if(previous){if(previous.action!==action||previous.draft_id!==draft.draft_id)throw new Error('先核实上一次请求的结果。');return previous;}
    return {action,draft_id:draft.draft_id,path:`/${encodeURIComponent(draft.draft_id)}/${action}`,body:{request_id:requestId,expected_sha256:draft.draft_sha256,consent:true,...(action==='generate'?{nonce}:{})}};
  }
  const statuses={preparing:'准备输入',downloading:'正在取回结果',retrieve_failed:'取回未完成，可查询原任务',blocked:'需要处理后继续',interrupted:'服务中断，请核实原任务',cancelling:'正在停止本地等待',queued:'等待处理',running:'处理中',submitting:'正在提交',submitted:'已提交，等待结果',succeeded:'制作完成',completed:'制作完成',failed:'未完成',cancelled:'本地等待已停止',unknown:'提交结果待核实',submission_unknown:'提交结果待核实',recovering:'正在核实'};
  function draftHtml(draft,data,runId){
    const reason=gate(draft,data),ref=(data.materials||[]).find(m=>m.name===draft.reference?.name&&m.sha256===draft.reference?.sha256),url=mediaUrl(ref?.url,runId);
    return `<article class="production-card"><header><div><span class="eyebrow">${draft.kind==='video'?'视频':'图片'} · 脚本版本 ${esc(draft.source_version)}</span><h3>${draft.kind==='video'?`${esc(draft.duration)} 秒 · ${esc(draft.resolution)}`:'图片制作方案'}</h3></div><span class="production-price">${price(draft.estimate)}</span></header>${url?`<button class="production-thumb" data-image="${esc(ref.name)}"><img src="${esc(url)}" alt="参考图：${esc(ref.name)}"><span>${ref.approved?'已确认参考图':'查看并确认参考图'}</span></button>`:''}<p class="production-prompt">${esc(draft.prompt)}</p><p class="form-note">${esc(draft.estimate?.notice||'费用以提供方实际结算为准。')}</p>${reviewHtml(draft.review)}<div class="production-actions"><button class="ghost" data-revise="${esc(draft.draft_id)}">修改为新方案</button><button class="ghost" data-review="${esc(draft.draft_id)}" ${reviewGate(draft,data)?'disabled':''}>${draft.review?'重新检查':'制作检查'}</button><button class="primary" data-generate="${esc(draft.draft_id)}" ${reason?'disabled':''}>核对并生成</button></div>${reason?`<p class="form-note">${esc(reason)}</p>`:''}<div class="production-attempts">${(draft.attempts||[]).map(a=>{
      const output=mediaUrl(a.output?.url,runId),unknown=['unknown','submission_unknown'].includes(a.status),usageUnknown=['unknown','unresolved'].includes(a.usage_status);
      return `<section><strong>${esc(statuses[a.status]||a.status)}</strong><p>${esc(a.message||'')}</p>${output?(draft.kind==='video'?`<video controls preload="metadata" src="${esc(output)}"></video>`:`<button class="production-thumb" data-image="${esc(a.output.name)}"><img src="${esc(output)}" alt="生成图片，点击查看与确认"></button>`):''}${a.task_id&&a.status!=='succeeded'&&a.status!=='completed'?`<button class="ghost small" data-refresh="${esc(a.attempt_id)}">查询并取回结果</button>`:''}${unknown?`<details><summary>核实提供方提交记录</summary><p class="form-note">${draft.kind==='video'?'请从提供方控制台核对。填写任务编号后，只会取回该任务；不会重新生成。':'如提供方证明未提交且未产生费用，可登记核实结果；不会重新生成。'}</p><button class="ghost small" data-recover="${esc(a.attempt_id)}">填写核实结果</button></details>`:''}</section>`;
    }).join('')}</div></article>`;
  }
  root.StudioProduction={esc,mediaUrl,price,gate,reviewGate,canRecoverUsage,usageQuantity,usageFormHtml,reviewHtml,uncertain,submission,actionRequest,draftHtml};
  if(typeof module!=='undefined')module.exports=root.StudioProduction;
})(typeof window!=='undefined'?window:globalThis);

async function openProduction(options={}){
  if(!state.runId){toast('先打开一个项目，再开始制作。');return;}
  if(document.querySelector('.production-dialog'))return;
  const runId=state.runId,base=`/api/runs/${encodeURIComponent(runId)}/production`,U=StudioProduction,opener=document.activeElement;
  const mount=options.mount,kind=options.kind||'image';
  const dialog=document.createElement(mount?'section':'dialog');dialog.className='production-dialog'+(mount?' production-embedded':'');dialog.setAttribute('aria-labelledby','production-title');
  dialog.innerHTML='<header class="production-heading"><div><span class="eyebrow">把方案变成画面</span><h2 id="production-title">制作</h2></div><button class="ghost" data-close>关闭</button></header><p class="form-note">先保存方案、完成制作检查，再核对准确输入并启动生成任务。关闭窗口只停止本地等待，提供方可能仍在处理。</p><p class="form-error" data-error role="alert"></p><div data-production-availability role="status"></div><div data-production-job role="status"></div><div data-confirm></div><div data-body>正在读取制作记录…</div>';
  if(mount){mount.replaceChildren(dialog);}else{document.body.append(dialog);dialog.showModal();}
  const body=dialog.querySelector('[data-body]'),error=dialog.querySelector('[data-error]'),confirm=dialog.querySelector('[data-confirm]');
  if(mount)dialog.querySelector('.form-note').remove();
  const storageKey=`studio-production:${runId}`;let data=null,timer=null,reading=false,busy=false,closed=false,pending=null,lastJob=null,renderedRecords='',values={kind:'video',source_version:'',source_sha256:'',reference_name:'',reference_sha256:'',prompt:'',model:'wan3.0-video',duration:'5',resolution:'720P'};
  try{const saved=JSON.parse(sessionStorage.getItem(storageKey)||'null');if(saved){values={...values,...saved.values};pending=saved.pending||null;lastJob=saved.lastJob||null;}}catch(_){}
  if(mount&&!pending){values.kind=kind;values.model=kind==='image'?'codex-image':'wan3.0-video';}
  function persist(){try{sessionStorage.setItem(storageKey,JSON.stringify({values,pending,lastJob}));}catch(_){error.textContent='浏览器无法暂存本次输入，请保持窗口直到请求结果确认。';}}
  function current(){return !closed&&(mount?dialog.isConnected:dialog.open)&&state.runId===runId;}
  function close(){closed=true;clearTimeout(timer);if(mount)dialog.remove();else dialog.close();}
  dialog.studioClose=close;
  dialog.querySelector('[data-close]').onclick=close;
  dialog.addEventListener('cancel',()=>{closed=true;clearTimeout(timer);});
  dialog.addEventListener('close',()=>{closed=true;clearTimeout(timer);dialog.remove();opener?.focus();});
  function notify(message){if(current())error.textContent=message;}
  function lock(){const save=body.querySelector('[data-save]');if(save)save.disabled=busy||!!pending||!!data?.active;if(busy||pending)body.querySelectorAll('input,textarea,select,button').forEach(b=>b.disabled=true);dialog.querySelectorAll('button[data-review],button[data-generate],button[data-image],button[data-refresh],button[data-recover],button[data-recover-usage],button[data-save],button[data-upload]').forEach(b=>{if(busy||pending)b.disabled=true;});}
  function editor(){
    const video=values.kind==='video',cap=data.capabilities?.[values.kind],models=data.capabilities?.video?.models||[];
    return `<form class="production-editor"><h3>新制作方案</h3><div class="production-tabs" role="group" aria-label="制作类型"><button type="button" class="${!video?'active':''}" data-kind="image">图片</button><button type="button" class="${video?'active':''}" data-kind="video">视频</button></div>${!cap?.available?`<p class="production-notice">${U.esc(cap?.reason||'该制作方式暂未就绪')}。可以保存方案，或上传已有图片用于视频制作。</p>`:''}${!(data.sources||[]).some(s=>s.ready)?'<p class="production-notice">请先在创作板完成并确认当前脚本，再保存制作方案。</p>':''}<label>关联脚本<select name="source_version" required><option value="">请选择准确版本</option>${(data.sources||[]).map(s=>`<option value="${s.version}" ${s.ready?'':'disabled'}>版本 ${s.version}${s.ready?'':' · 尚未就绪'}</option>`).join('')}</select></label><p class="form-note" data-source-note></p><label>${video?'视频参考图':'参考图片（可选）'}<select name="reference_name" ${video?'required':''}><option value="">${video?'请选择一张图片':'不使用参考图'}</option>${(data.materials||[]).filter(m=>m.kind==='image').map(m=>`<option value="${U.esc(m.name)}">${U.esc(m.name)} · ${m.approved?'已确认':'待图审'}</option>`).join('')}</select></label><button type="button" class="ghost small" data-upload>上传现有图片</button><label>画面与动作描述<textarea name="prompt" rows="7" maxlength="5000" required placeholder="写清楚画面、主体、动作与镜头。保存后仍需检查和确认才会生成。"></textarea></label>${video?`<div class="production-grid"><label>模型<select name="model">${models.map(m=>`<option value="${U.esc(m.id)}">${U.esc(m.label)}</option>`).join('')}</select></label><label>画质<select name="resolution">${(models.find(m=>m.id===values.model)?.resolutions||['720P']).map(r=>`<option value="${U.esc(r)}">${U.esc(r)}</option>`).join('')}</select></label><label>时长（秒）<input name="duration" type="number" min="${models.find(m=>m.id===values.model)?.durations?.min||2}" max="${models.find(m=>m.id===values.model)?.durations?.max||30}" step="1" required></label></div>`:''}<button class="primary" data-save type="submit">保存新方案</button><p class="form-note">保存不调用模型。旧方案及生成记录会保留。</p></form>`;
  }
  function capture(form){for(const [key,value] of new FormData(form))values[key]=value;}
  function render(){
    if(!current())return;
    renderedRecords=records();
    const readySource=(data.sources||[]).some(s=>s.ready);
    const readyReference=(data.materials||[]).some(m=>m.kind==='image'&&m.approved);
    const canPrepare=readySource&&(kind==='image'||readyReference);
    const prepareReason=!readySource?'请先在创作板确认完整方案。':kind==='video'&&!readyReference?'请先确认一张当前版本的视频参考图。':'';
    body.innerHTML=mount
      ? `<div class="journey-media-intro">${kind==='image'?'确认完整方案后，团队会在这里准备分镜图。生成后点击大图，确认商品、人物和画面是否可用。':'确认分镜图后，团队会准备视频。你核对画面描述和费用后再生成。'}${prepareReason?`<p class="production-notice">${prepareReason}</p>`:''}<button class="ghost" data-prepare ${canPrepare?'':'disabled'}>让团队${kind==='image'?'准备分镜':'准备视频'}</button></div><div class="production-layout"><aside><details><summary>高级：手动编辑制作输入</summary>${editor()}</details></aside><section><div data-records>${renderedRecords}</div></section></div>`
      : `<div class="production-layout"><aside>${editor()}</aside><section><div class="production-record-heading"><h3>生成记录</h3><span>${data.drafts.length} 个方案</span></div><div data-records>${renderedRecords}</div></section></div>`;
    const prepare=body.querySelector('[data-prepare]');if(prepare){prepare.disabled=!!data.active||!canPrepare;prepare.onclick=()=>submitJob(runId,{request_id:crypto.randomUUID(),text:kind==='image'?'请根据我已确认的完整方案，准备分镜参考图的图片制作输入，完成文字制作检查，让我核对后生成。':'请基于我已确认的分镜图准备视频输入，完成文字制作检查，展示画面描述、时长与费用，等我确认后生成。'});}
    const form=body.querySelector('form');for(const [key,value] of Object.entries(values)){const el=form.elements.namedItem(key);if(el)el.value=value;}
    function showSource(){const s=data.sources.find(s=>s.version===Number(values.source_version));form.querySelector('[data-source-note]').textContent=s?.summary||'不会自动选用最新稿件，请核对制作依据。';}
    showSource();
    form.oninput=e=>{capture(form);if(e.target.name==='source_version')values.source_sha256=data.sources.find(s=>s.version===Number(values.source_version))?.artifact_sha256||'';if(e.target.name==='reference_name')values.reference_sha256=data.materials.find(m=>m.name===values.reference_name)?.sha256||'';persist();showSource();};
    form.onsubmit=e=>{e.preventDefault();if(busy||pending||data.active)return;try{capture(form);const payload=U.submission(values,data,crypto.randomUUID());pending={action:'drafts',path:'/drafts',body:payload};persist();sendPending();}catch(err){notify(err.message);}};
    lock();if(pending)pendingView();
  }
  function records(){
    const drafts=mount?data.drafts.filter(d=>d.kind===kind):data.drafts;
    const current=drafts.filter(d=>d.source_ready!==false),older=drafts.filter(d=>d.source_ready===false);
    const draw=rows=>rows.slice().reverse().map(d=>U.draftHtml(d,data,runId)).join('');
    return (current.length?draw(current):`<div class="production-empty"><span>${mount?(kind==='image'?'分镜会在这里展开':'成片会在这里呈现'):'从一个画面开始'}</span><p>可以继续在右侧讨论，团队会根据确认的方案准备制作输入。</p></div>`) +
      (older.length?`<details><summary>查看历史制作记录</summary>${draw(older)}</details>`:'');
  }
  function pendingView(){confirm.innerHTML='<section class="production-confirm"><h3>上次请求结果待确认</h3><p>请重试同一请求。输入与请求身份已保留，不会另开一次生成。</p><button class="primary" data-retry>核实上次请求</button></section>';confirm.querySelector('[data-retry]').onclick=()=>{if(!busy)sendPending();};}
  async function sendPending(){
    if(busy||!pending||!current())return;busy=true;lock();const request=pending;notify('');
    try{const result=await api(base+request.path,{method:'POST',body:JSON.stringify(request.body)});if(result.job_id){lastJob={job_id:result.job_id,status:result.status,reason:result.reason||'',action:request.action};restoreJob(runId);}pending=null;persist();if(!current())return;confirm.innerHTML='';await load(true);toast(request.action==='drafts'?'制作方案已保存。':'请求已接收，请在生成记录查看进度。');}
    catch(err){if(!U.uncertain(err)){pending=null;persist();if(current()){confirm.innerHTML='';await load(true);}}else if(current())pendingView();notify(err.message||'请求结果暂未确认，请核实上次请求。');}
    finally{busy=false;if(current()){if(pending)pendingView();else render();}}
  }
  function reviewAction(draft,action){
    if(busy||pending)return;const reviewBlocked=U.reviewGate(draft,data);if(reviewBlocked){notify(reviewBlocked);return;}if(action==='generate'){const reason=U.gate(draft,data);if(reason){notify(reason);return;}}
    const material=data.materials.find(m=>m.name===draft.reference?.name&&m.sha256===draft.reference?.sha256),url=U.mediaUrl(material?.url,runId),generation=action==='generate';
    confirm.innerHTML=`<section class="production-confirm"><span class="eyebrow">${generation?'生成前最后核对':'检查准确制作输入'}</span><h3>${generation?'启动一个生成任务':'确认一次制作检查'}</h3><p>${mount?'按已确认方案制作':`脚本版本 ${U.esc(draft.source_version)}`} · ${U.esc(draft.model)}${draft.kind==='video'?` · ${U.esc(draft.duration)} 秒 · ${U.esc(draft.resolution)} · 原生声音开启 · 跟随参考图比例 · 不自动扩写`:''}</p>${url?`<img class="production-preview" src="${U.esc(url)}" alt="本次使用的准确参考图">`:'<p class="form-note">本方案未使用参考图。</p>'}<p class="production-prompt">${U.esc(draft.prompt)}</p><p><strong>${generation?U.price(draft.estimate):'制作检查会调用一次审核模型，费用按该模型配置结算。'}</strong></p><p class="form-note">${generation?U.esc(draft.estimate?.notice||'费用未知的部分保持未知；不代表免费。'): '检查不生成素材，也不代表用户已批准图片或生成费用。'} 不会自动重复提交。</p><label class="production-consent"><input type="checkbox" data-consent>我已核对以上输入，同意本次${generation?'生成任务及其费用':'审核模型调用及其费用'}</label><div class="production-actions"><button class="primary" data-authorize disabled>${generation?'启动生成任务':'开始制作检查'}</button><button class="ghost" data-dismiss>返回修改</button></div></section>`;
    const button=confirm.querySelector('[data-authorize]');confirm.querySelector('[data-consent]').onchange=e=>button.disabled=!e.target.checked;
    button.onclick=()=>{if(busy||pending||!current()||!confirm.querySelector('[data-consent]').checked)return;const reason=action==='generate'?U.gate(draft,data):U.reviewGate(draft,data);if(reason){notify(reason);return;}pending=U.actionRequest(action,draft,data.nonce,crypto.randomUUID());persist();sendPending();};
    confirm.querySelector('[data-dismiss]').onclick=()=>{confirm.innerHTML='';};confirm.scrollIntoView({block:'start'});
  }
  function imageView(name){
    const material=data.materials.find(m=>m.name===name&&m.kind==='image'),url=U.mediaUrl(material?.url,runId);if(!url){notify('此图片暂不可用，请重新读取素材。');return;}
    confirm.innerHTML=`<section class="production-confirm"><h3>参考图审核</h3><img class="production-preview" src="${U.esc(url)}" alt="${U.esc(name)}"><p>${U.esc(name)}</p><p class="form-note">请检查商品外观、画面和文字。本次确认只针对这张图片，不会自动生成视频。</p><button class="primary" data-approve disabled>${material.approved?'撤回本图确认':'我已查看，确认此图可用于视频'}</button><button class="ghost" data-dismiss>返回</button></section>`;
    const img=confirm.querySelector('img'),button=confirm.querySelector('[data-approve]');img.onload=()=>button.disabled=false;img.onerror=()=>notify('图片未能完整加载，暂不能确认。');if(img.complete&&img.naturalWidth)button.disabled=false;
    button.onclick=()=>{if(busy||pending||!current())return;if(data.active){notify('制作团队正在执行，请本轮结束后再确认参考图。');return;}pending={action:'material-approvals',path:'/material-approvals',body:{name:material.name,sha256:material.sha256,approved:!material.approved,nonce:data.nonce}};persist();sendPending();};
    confirm.querySelector('[data-dismiss]').onclick=()=>confirm.innerHTML='';confirm.scrollIntoView({block:'start'});
  }
  function recoverUsage(id){
    const draft=data.drafts.find(d=>(d.attempts||[]).some(a=>a.attempt_id===id)),attempt=draft?.attempts.find(a=>a.attempt_id===id);
    if(!attempt||!U.canRecoverUsage(attempt,data)){notify('本轮任务仍在执行，请等待结果后再核账。');return;}
    confirm.innerHTML=U.usageFormHtml(draft.kind);
    const form=confirm.querySelector('form');form.onsubmit=e=>{e.preventDefault();if(busy||pending||!current())return;const currentAttempt=data.drafts.flatMap(d=>d.attempts||[]).find(a=>a.attempt_id===id);if(!currentAttempt||!U.canRecoverUsage(currentAttempt,data)){notify('任务状态已变化，请等待结果后再核账。');return;}try{const f=new FormData(form),quantity=U.usageQuantity(f.get('quantity'),draft.kind);pending={action:'recover',path:`/attempts/${encodeURIComponent(id)}/recover`,body:{quantity,evidence_note:String(f.get('evidence_note')).trim(),nonce:data.nonce}};persist();sendPending();}catch(err){notify(err.message);}};form.querySelector('[data-dismiss]').onclick=()=>confirm.innerHTML='';
  }
  function recover(id){
    const imageAttempt=data.drafts.some(d=>d.kind==='image'&&(d.attempts||[]).some(a=>a.attempt_id===id));
    confirm.innerHTML='<form class="production-confirm production-editor"><h3>核实提交结果</h3><label>提供方任务编号<input name="task_id" maxlength="160" placeholder="从提供方任务记录复制"></label><label>核实依据<textarea name="evidence_note" rows="3" maxlength="2000" required placeholder="记录查询位置、时间及结果"></textarea></label><label class="production-consent"><input type="checkbox" name="not_submitted">提供方证据确认：未提交任务且未产生费用</label><p class="form-note">只有核实无任务且无费用，才能选择此项。未知不等于未提交。</p><div class="production-actions"><button type="submit" class="primary">保存核实结果</button><button type="button" class="ghost" data-dismiss>返回</button></div></form>';
    const form=confirm.querySelector('form');if(imageAttempt)form.elements.task_id.closest('label').remove();form.onsubmit=e=>{e.preventDefault();if(busy||pending||!current())return;const f=new FormData(form),task=String(f.get('task_id')||'').trim(),notSubmitted=f.has('not_submitted');if((!task&&!notSubmitted)||(task&&notSubmitted)){notify('请填写任务编号，或确认有证据表明未提交；两者不能同时选择。');return;}pending={action:'recover',path:`/attempts/${encodeURIComponent(id)}/recover`,body:{...(notSubmitted?{not_submitted:true}:{task_id:task}),evidence_note:String(f.get('evidence_note')).trim(),nonce:data.nonce}};persist();sendPending();};form.querySelector('[data-dismiss]').onclick=()=>confirm.innerHTML='';
  }
  body.addEventListener('click',e=>{const b=e.target.closest('button');if(!b||busy||pending||!current())return;if(b.dataset.kind){capture(body.querySelector('form'));values.kind=b.dataset.kind;values.model=values.kind==='video'?'wan3.0-video':'codex-image';persist();render();}else if(b.hasAttribute('data-upload')){close();toggleMaterials(true);}else if(b.dataset.revise){const d=data.drafts.find(d=>d.draft_id===b.dataset.revise);if(d){values={kind:d.kind,source_version:String(d.source_version),source_sha256:d.source_sha256,reference_name:d.reference?.name||'',reference_sha256:d.reference?.sha256||'',prompt:d.prompt,model:d.model,duration:String(d.duration||5),resolution:d.resolution||'720P'};persist();render();body.querySelector('textarea')?.focus();}}else if(b.dataset.review||b.dataset.generate){const d=data.drafts.find(d=>d.draft_id===(b.dataset.review||b.dataset.generate));if(d)reviewAction(d,b.dataset.review?'review':'generate');}else if(b.dataset.image)imageView(b.dataset.image);else if(b.dataset.refresh){pending={action:'refresh',path:`/attempts/${encodeURIComponent(b.dataset.refresh)}/refresh`,body:{request_id:crypto.randomUUID()}};persist();sendPending();}else if(b.dataset.recoverUsage)recoverUsage(b.dataset.recoverUsage);else if(b.dataset.recover)recover(b.dataset.recover);});
  async function showJob(){
    if(!lastJob||!current())return;
    if(!['succeeded','failed','blocked','cancelled','interrupted'].includes(lastJob.status)){try{const job=await api(`/api/runs/${encodeURIComponent(runId)}/jobs/${encodeURIComponent(lastJob.job_id)}`);if(!current())return;lastJob={...lastJob,status:job.status,reason:job.reason||''};persist();}catch(err){notify('暂不能读取本次执行状态：'+err.message);}}
    if(!current())return;const labels={queued:'等待处理',running:'正在执行',succeeded:'执行已结束',failed:'本次执行未完成',blocked:'本次执行需要处理',cancelled:'本地执行已停止',interrupted:'执行被中断',cancelling:'正在停止本地等待'};
    dialog.querySelector('[data-production-job]').innerHTML=`<p class="production-notice">${lastJob.action==='review'?'制作检查':'本次制作'} · ${U.esc(labels[lastJob.status]||'状态待核实')}${lastJob.reason?`<br>${U.esc(lastJob.reason)}`:''}</p>`;
  }
  async function load(full=false){
    if(!current()||reading)return;if(state.runId!==runId){close();return;}reading=true;
    try{const result=await api(base);if(!current()){close();return;}data=result;if(mount&&!values.source_version){const source=data.sources.find(s=>s.ready);if(source){values.source_version=String(source.version);values.source_sha256=source.artifact_sha256;}}dialog.querySelector('[data-production-availability]').textContent=data.active?'制作团队正在执行。当前输入会保留，本轮结束后可以继续。':'';await showJob();if(!current())return;if(full||!body.querySelector('form'))render();else{const recordsHost=body.querySelector('[data-records]');const nextRecords=records();if(recordsHost&&nextRecords!==renderedRecords){recordsHost.innerHTML=nextRecords;renderedRecords=nextRecords;}lock();}}
    catch(err){notify(err.status===404?'制作功能等待服务更新，重启工作台后再试。':err.message);if(!data)body.innerHTML='<button class="ghost" data-load>重新读取</button>';body.querySelector('[data-load]')?.addEventListener('click',()=>load(true));}
    finally{reading=false;clearTimeout(timer);if(current())timer=setTimeout(()=>{if(state.runId!==runId){close();return;}load();},2000);}
  }
  await load(true);
}
