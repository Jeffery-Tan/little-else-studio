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
  const referencePurposes={product:'商品外观',character:'人物外观',scene:'场景环境',continuity:'画面衔接'};
  function draftReferences(draft){
    // Explicit references are the actual provider inputs. An empty set must not
    // silently bring back the legacy single-reference field.
    if(Array.isArray(draft.references))return draft.references;
    return draft.reference?[{...draft.reference,purpose:draft.kind==='video'?'continuity':'product'}]:[];
  }
  function generationPrompt(draft){return typeof draft.compiled_prompt==='string'?draft.compiled_prompt:String(draft.prompt||'');}
  function referenceInputs(draft,data,runId){
    return draftReferences(draft).map(ref=>{
      const material=(data.materials||[]).find(m=>m.kind==='image'&&m.name===ref?.name&&m.sha256===ref?.sha256);
      const labels=draft.design_labels||{};
      const entity=ref?.purpose==='character'?(labels.characters||[]).find(row=>row.id===ref.design_id)
        :ref?.purpose==='scene'&&labels.scene?.id===ref.design_id?labels.scene:null;
      const identity=ref?.design_id?entity?.label?`${entity.label}（${ref.design_id}）`:ref.design_id:'';
      return {...ref,label:(referencePurposes[ref?.purpose]||'用途未明确')+(identity?` · ${identity}`:''),url:mediaUrl(material?.url,runId)};
    });
  }
  function referenceInputIssue(draft,data){
    if(!Array.isArray(draft.references))return '';
    const refs=draftReferences(draft);
    if(draft.kind==='video'&&(refs.length!==1||refs[0]?.purpose!=='continuity'))return '视频每段必须使用一张已确认的首帧，请重新核对制作输入。';
    if(draft.kind==='image'&&refs.length>5)return '图片最多使用五张参考图，请重新核对制作输入。';
    if(refs.some(ref=>!referencePurposes[ref?.purpose]))return '参考图用途尚未明确，请重新核对制作输入。';
    if(refs.some(ref=>!(data.materials||[]).some(m=>m.kind==='image'&&m.name===ref?.name&&m.sha256===ref?.sha256)))return '本次参考图文件已变化或不可用，请重新核对制作输入。';
    return '';
  }
  function designBindingHtml(draft){
    const binding=draft.design_binding,labels=draft.design_labels||{};
    const issue=String(draft.design_issue||'');
    if(!binding)return issue?`<p class="production-design-issue" role="status">${esc(issue)}</p>`:'';
    const characters=(binding.character_ids||[]).map(id=>{
      const row=(labels.characters||[]).find(item=>item.id===id);
      return row?.label?`${row.label}（${id}）`:id;
    });
    const scene=labels.scene?.id===binding.scene_id&&labels.scene.label?`${labels.scene.label}（${binding.scene_id}）`:binding.scene_id;
    return `<section class="production-design-binding"><strong>本镜采用的共享设计${binding.material_plan?.version?` · 素材计划第 ${esc(binding.material_plan.version)} 版`:''}</strong>`+
      `<dl><dt>人物</dt><dd>${esc(characters.join('、')||'本镜不包含人物')}</dd><dt>场景</dt><dd>${esc(scene||'未绑定场景')}</dd></dl>`+
      '<p class="form-note">人物、场景与衔接要求用于本次制作输入，实际结果仍需逐镜核对。</p>'+
      (issue?`<p class="production-design-issue" role="status">${esc(issue)}</p>`:'')+
      `<details><summary>查看设计版本与指纹</summary><p>素材计划：${esc(binding.material_plan?.sha256||'未提供')}<br>脚本版本 ${esc(binding.writing?.version||draft.source_version)}：${esc(binding.writing?.sha256||draft.source_sha256||'未提供')}<br>共享设计：${esc(binding.design_sha256||'未提供')}</p></details></section>`;
  }
  function referencesHtml(draft,data,runId){
    const refs=referenceInputs(draft,data,runId);
    if(!refs.length)return '<p class="form-note">本方案未使用参考图像素输入。</p>';
    const inputIssue=referenceInputIssue(draft,data);
    return `<section class="production-reference-inputs"><strong>${inputIssue?'待核对的参考图':draft.kind==='video'?'本段实际使用的首帧':'本次参考图像素输入'} · ${refs.length} 张</strong><div class="production-reference-grid">`+
      refs.map(ref=>`<figure class="production-shot-reference production-reference-input">`+
        (ref.url?`<a href="${esc(ref.url)}" target="_blank" rel="noopener"><img src="${esc(ref.url)}" alt="${esc(ref.label)}参考图：${esc(ref.name)}"></a>`:'<span class="production-reference-missing">参考图不可用</span>')+
        `<figcaption><strong>${esc(ref.label)}</strong><span>${esc(ref.name)}</span><span>${draft.kind==='video'?ref.purpose==='continuity'?'作为本段视频的起始画面':'此用途不能用于本段视频提交':ref.purpose==='product'?'上传商品原图，仅供生成参考':'供本次图片生成参考'}</span>`+
        (ref.url?'':`<span>文件已变化或不可用，请重新核对。</span>`)+`</figcaption></figure>`).join('')+'</div>'+
      `<details><summary>查看本次参考图文件与指纹</summary>${refs.map(ref=>`<p>${esc(ref.label)} · ${esc(ref.name)}<br>${esc(ref.sha256)}</p>`).join('')}</details></section>`;
  }
  function reviewGate(draft,data){
    if(data.active)return '制作团队正在执行，请等待本轮结束';
    if(draft.design_issue)return String(draft.design_issue);
    if(data.visual_design_status?.ready===false)return (data.visual_design_status.issues||[]).join(' ')||'当前共同人物与场景设计尚未就绪。';
    if(draft.source_ready===false)return '关联稿件已变化，请核对后保存新方案';
    return '';
  }
  function gate(draft,data){
    const common=reviewGate(draft,data);if(common)return common;
    const inputIssue=referenceInputIssue(draft,data);if(inputIssue)return inputIssue;
    if(!data.capabilities?.[draft.kind]?.available)return data.capabilities?.[draft.kind]?.reason||'该制作方式尚未就绪';
    if(draft.source_ready===false)return '关联稿件已变化，请核对后保存新方案';
    if(draft.kind==='video'&&(!draftReferences(draft).length||draft.reference_approved!==true))return '先查看并确认本张参考图';
    if(draft.can_generate===false)return draft.reason||'当前方案暂不能生成，请核对记录';
    if((draft.attempts||[]).some(a=>['queued','running','preparing','submitting','submitted','downloading','retrieve_failed','unknown','submission_unknown','recovering','cancelling'].includes(a.status)))return '已有制作任务，请先查询或核实结果';
    return '';
  }
  function reuseCandidatesHtml(data,kind){
    const rows=(data?.reuse_candidates||[]).filter(row=>row.kind===kind);
    if(!rows.length)return '';
    const targets=data.targets?.[kind]?.shots||[];
    return '<details class="production-record"><summary>沿用未受修改影响的镜头素材</summary><p class="form-note">沿用只保存现有素材，不调用生成、不新增生成费用。素材会重新绑定当前方案；图片确认和生成授权不会沿用。</p>'+rows.map(row=>{
      const shot=targets.find(item=>item.shot_id===row.shot_id);
      return `<div class="production-card"><strong>${shot?`第 ${esc(shot.ordinal)} 镜`:'旧镜头'}</strong><p>${esc(row.reason||'依赖未变化，可沿用已保存素材。')}</p>`+
        (row.eligible?`<button class="ghost small" data-reuse-attempt="${esc(row.source_attempt_id)}" ${data.active?'disabled':''}>沿用此镜素材</button>`:'')+'</div>';
    }).join('')+'</details>';
  }
  function submission(values,data,requestId){
    const source=(data.sources||[]).find(s=>s.version===Number(values.source_version)&&s.artifact_sha256===values.source_sha256);
    if(!source||!source.ready)throw new Error('请选择已就绪的准确脚本版本。');
    if(!['image','video'].includes(values.kind))throw new Error('请选择制作类型。');
    const material=(data.materials||[]).find(m=>m.name===values.reference_name&&m.sha256===values.reference_sha256&&m.kind==='image');
    const refs=Array.isArray(values.references)?values.references.map(ref=>({name:ref.name,sha256:ref.sha256,purpose:ref.purpose,...(ref.design_id?{design_id:ref.design_id}:{})})):null;
    if(refs){const issue=referenceInputIssue({kind:values.kind,references:refs},data);if(issue)throw new Error(issue);}
    else{
      if(values.reference_name&&!material)throw new Error('参考图已变化，请重新选择。');
      if(values.kind==='video'&&!material)throw new Error('请明确选择视频使用的参考图。');
    }
    if(!String(values.prompt||'').trim())throw new Error('请填写制作描述。');
    const model=values.kind==='video'?(data.capabilities?.video?.models||[]).find(m=>m.id===values.model):null;
    const duration=values.kind==='video'?Number(values.duration):null;
    if(values.kind==='video'&&(!model||!Number.isInteger(duration)||duration<model.durations.min||duration>model.durations.max||!model.resolutions.includes(values.resolution)))throw new Error('请核对视频模型、时长与画质。');
    return {request_id:requestId,kind:values.kind,prompt:values.prompt.trim(),source_version:source.version,source_sha256:source.artifact_sha256,
      ...(refs?{references:refs}:{reference:material?{name:material.name,sha256:material.sha256}:null}),
      ...(values.shot_id?{shot_id:values.shot_id}:{}),...(values.supersedes?{supersedes:values.supersedes}:{}),
      model:values.kind==='image'?'codex-image':model.id,duration,resolution:values.kind==='image'?null:values.resolution};
  }
  function editValues(draft){return {kind:draft.kind,source_version:String(draft.source_version),source_sha256:draft.source_sha256,
    reference_name:draft.reference?.name||'',reference_sha256:draft.reference?.sha256||'',
    references:Array.isArray(draft.references)?draft.references.map(ref=>({...ref})):null,
    shot_id:draft.shot_id||'',supersedes:draft.draft_id,
    prompt:typeof draft.shot_prompt==='string'?draft.shot_prompt:draft.prompt,
    model:draft.model,duration:String(draft.duration||5),resolution:draft.resolution||'720P'};}
  function reusePayload(candidate,data,requestId){
    const source=(data.sources||[]).find(row=>row.ready&&row.version===candidate?.source_version
      &&row.artifact_sha256===candidate?.source_sha256);
    const old=(data.drafts||[]).find(row=>row.draft_id===candidate?.source_draft_id
      &&row.kind===candidate?.kind&&row.shot_id===candidate?.shot_id);
    if(!candidate?.eligible||!Array.isArray(candidate.changed_dependencies)||candidate.changed_dependencies.length
        ||!old||!source)throw new Error('素材依赖或当前方案已变化，请重新读取。');
    const currentDraft=(data.drafts||[]).find(row=>row.kind===candidate.kind&&row.shot_id===candidate.shot_id
      &&row.source_version===source.version&&row.source_sha256===source.artifact_sha256
      &&row.source_ready&&row.is_current!==false);
    const references=candidate.kind==='video'?[candidate.reuse_reference]:draftReferences(old);
    const issue=referenceInputIssue({kind:candidate.kind,references},data);
    if(issue)throw new Error(issue);
    if(candidate.kind==='video'&&(!candidate.reuse_reference
        ||candidate.reuse_reference.sha256!==draftReferences(old)[0]?.sha256))
      throw new Error('当前首帧与旧视频的字节依赖无法核对，请重新读取。');
    // Rebuild the allowed preparation fields rather than copying historical
    // request identities, approvals or a generated frame's obsolete name.
    return {request_id:requestId,kind:candidate.kind,
      prompt:typeof old.shot_prompt==='string'?old.shot_prompt:old.prompt,
      source_version:source.version,source_sha256:source.artifact_sha256,
      shot_id:candidate.shot_id,reuse_attempt_id:candidate.source_attempt_id,
      references:references.map(ref=>({name:ref.name,sha256:ref.sha256,purpose:ref.purpose,
        ...(ref.design_id?{design_id:ref.design_id}:{})})),
      model:old.model||old.request_payload?.model,duration:old.duration??null,resolution:old.resolution??null,
      ...(currentDraft?{supersedes:currentDraft.draft_id}:{})};
  }
  function canRecoverUsage(attempt,data){return !data.active&&['unknown','unresolved'].includes(attempt?.usage_status)&&!['queued','running','preparing','submitting','submitted','downloading','recovering'].includes(attempt.status);}
  function usageQuantity(value,kind){const quantity=Number(value);if(String(value??'').trim()===''||!Number.isFinite(quantity)||quantity<0||quantity>10000||(kind==='image'&&!Number.isSafeInteger(quantity)))throw new Error(kind==='video'?'请按提供方凭证填写有效的非负视频秒数。':'请按提供方凭证填写非负整数图片用量。');return quantity;}
  function usageFormHtml(kind){const video=kind==='video';return `<form class="production-confirm production-editor"><h3>核实${video?'视频':'图片'}用量</h3><label>提供方确认的${video?'计费时长（秒）':'图片用量'}<input name="quantity" type="number" min="0" max="10000" step="${video?'any':'1'}" required placeholder="请按提供方凭证填写"></label><label>凭证与核实依据<textarea name="evidence_note" rows="3" maxlength="2000" required></textarea></label><p class="form-note">${video?'请填写提供方确认的实际计费秒数；不按方案时长或下载文件推算。':'不按生成文件数量推算，不将订阅视为零费用。'}仅补录确认的用量，不发起新的生成。</p><div class="production-actions"><button type="submit" class="primary">保存用量凭证</button><button type="button" class="ghost" data-dismiss>返回</button></div></form>`;}
  function reviewHtml(review){if(!review)return '';const checks={opening:'开场',progression:'推进',payoff:'回报',product:'商品作用',execution:'执行与限制'};return `<div class="production-review"><strong>${review.verdict==='ready'?'文字制作检查通过':'文字制作检查需要调整'}</strong><p class="form-note">${esc(review.scope||'仅检查文字与已记录证据，未查看参考图像素；不代表图审或成片验收。')}</p>${(review.findings||[]).map(f=>`<p>${esc(typeof f==='string'?f:f.message||f.description||f.title||'请核对制作输入。')}</p>`).join('')}<details><summary>查看五项检查依据</summary>${Object.entries(checks).map(([key,label])=>`<h4>${label}</h4><p>${esc(review.checks?.[key]||'未提供检查依据')}</p>`).join('')}</details></div>`;}
  function captionBlock(writing){
    return (writing?.blocks||[]).find(item=>item.type==='text'&&item.title==='发布文案'&&item.text);
  }
  function captionMatches(writing,work){
    return Boolean(writing&&work&&Number.isInteger(Number(work.displaySourceVersion))&&work.displaySourceSha256&&
      Number(writing.version)===Number(work.displaySourceVersion)&&
      (writing.artifact_sha256||writing.sha256)===work.displaySourceSha256&&writing.integrity_ok===true);
  }
  function captionHtml(rail,work=null,archived=null){
    const writing=(rail||[]).find(step=>step.stage==='writing'&&step.has_doc);
    const displayedVideo=work?.kind==='video'&&work.outputUrl;
    if(!displayedVideo){
      if(!writing)return '';
      if(writing.stale||writing.integrity_ok===false)return '<section class="creator-caption"><h3>发布文案</h3><p>脚本来源已变化，文案需要与当前版本重新核对。</p></section>';
      const block=captionBlock(writing);
      return `<section class="creator-caption"><h3>发布文案 · 脚本第 ${esc(writing.version)} 版</h3>`+
        (block?`<p data-caption-text>${esc(block.text)}</p><button type="button" class="ghost small" data-copy-caption>复制文案</button>`:'<p>当前脚本没有独立发布文案，可以在对话中请团队补充。</p>')+'</section>';
    }
    const paired=captionMatches(writing,work)?writing:captionMatches(archived?.artifact,work)?archived.artifact:null;
    const pairedBlock=captionBlock(paired);
    const version=work.displaySourceVersion;
    const unavailable=archived?.status==='missing'||!version||!work.displaySourceSha256;
    const pairedHtml=`<section class="creator-caption creator-caption-paired"><h3>当前视频对应的发布文案 · 脚本第 ${esc(version||'未知')} 版</h3>`+
      (paired?pairedBlock
        ? `<p data-caption-text>${esc(pairedBlock.text)}</p>${writing?.stale&&paired===writing?'<p class="form-note">这版脚本依据已变化，请先核对作品再发布。</p>':''}<button type="button" class="ghost small" data-copy-caption>复制这段视频的配文</button>`
        : '<p>这段视频对应的脚本没有独立发布文案。</p>'
        : `<p>${unavailable?'找不到可核对的同版配文；不会用新版文案补上。':archived?.status==='error'?'暂时无法读取同版配文，请重新核对。':'正在核对这段视频对应的脚本与配文…'}</p>${archived?.status==='error'?'<button type="button" class="ghost small" data-reload-caption>重新核对配文</button>':''}`)+
      '</section>';
    if(!writing||captionMatches(writing,work))return pairedHtml;
    const currentBlock=captionBlock(writing),newer=Number(writing.version)>Number(version);
    return pairedHtml+`<section class="creator-caption creator-caption-unpaired"><h3>${newer?'新版':'当前脚本'}文案 · 脚本第 ${esc(writing.version)} 版</h3>`+
      '<p class="form-note">尚未对应当前视频，不能与上方视频作为同一套发布交付。</p>'+
      (writing.stale||writing.integrity_ok===false?'<p>当前脚本来源已变化，文案需要重新核对。</p>':currentBlock
        ? `<p data-caption-text>${esc(currentBlock.text)}</p><button type="button" class="ghost small" data-copy-caption>复制未配对文案</button>`
        : '<p>当前脚本没有独立发布文案。</p>')+'</section>';
  }
  function uncertain(error){return ![400,403,404,409,422].includes(error?.status);}
  function actionRequest(action,draft,nonce,requestId,previous,connectionRevision){
    if(previous){if(previous.action!==action||previous.draft_id!==draft.draft_id)throw new Error('先核实上一次请求的结果。');return previous;}
    return {action,draft_id:draft.draft_id,path:`/${encodeURIComponent(draft.draft_id)}/${action}`,body:{request_id:requestId,expected_sha256:draft.draft_sha256,consent:true,...(action==='generate'?{nonce,...(connectionRevision===undefined?{}:{expected_connection_revision:connectionRevision})}:{})}};
  }
  const statuses={preparing:'准备输入',downloading:'正在取回结果',retrieve_failed:'取回未完成，可查询原任务',blocked:'需要处理后继续',interrupted:'服务中断，请核实原任务',cancelling:'正在停止本地等待',queued:'等待处理',running:'处理中',submitting:'正在提交',submitted:'已提交，等待结果',succeeded:'制作完成',completed:'制作完成',failed:'未完成',cancelled:'本地等待已停止',unknown:'提交结果待核实',submission_unknown:'提交结果待核实',recovering:'正在核实',retry_authorized:'已保留未知记录，可重新核对生成'};
  function outputUrl(attempt,data,runId){
    if(!attempt?.output)return '';
    const found=(data.materials||[]).some(m=>m.name===attempt.output.name&&m.sha256===attempt.output.sha256);
    return found?mediaUrl(attempt.output.url,runId):'';
  }
  function readableResult(draft,data,runId){
    // Image drafts are separate storyboard frames. A result for one frame must
    // never appear as the result for another frame that has not been generated.
    const drafts=draft.kind==='image'?[draft]:(data.drafts||[]).filter(row=>row.kind===draft.kind);
    if(!drafts.some(row=>row.draft_id===draft.draft_id))drafts.push(draft);
    let result=null,order=0;
    for(const row of drafts)for(const attempt of row.attempts||[]){
      order+=1;
      if(!['succeeded','completed'].includes(attempt.status))continue;
      const url=outputUrl(attempt,data,runId);
      if(!url)continue;
      const time=Date.parse(attempt.at||'')||0;
      const isCurrent=row.draft_id===draft.draft_id;
      if(!result||(isCurrent&&!result.isCurrent)
          ||(isCurrent===result.isCurrent&&(time>result.time||(time===result.time&&order>result.order))))
        result={draft:row,attempt,url,name:attempt.output.name,time,order,isCurrent};
    }
    return result;
  }
  // The server already decides each shot's stage and why it is stuck
  // (studio/production.py::coverage). Re-deriving that here is how the screen and
  // the chat started disagreeing, so this only reads it. Keyed by draft_id as
  // well as shot_id, because a shot that has no current input has no draft to
  // match on and its row is still the one that must be shown.
  function shotRows(data,kind){
    const rows=data?.shots?.[kind]?.shots;
    return Array.isArray(rows)?rows:[];
  }
  function shotCoverage(data,kind){return data?.shots?.[kind]||null;}
  function shotForDraft(draft,data,kind){
    return shotRows(data,kind).find(row=>row.draft_id&&row.draft_id===draft.draft_id)||null;
  }
  function supersededDrafts(data,kind){
    return (data?.drafts||[]).filter(draft=>draft.kind===kind&&draft.is_current===false);
  }
  // A shot whose latest attempt is settled but whose result is not readable is
  // the one place where "already generated" and "not retrieved yet" must not be
  // blurred: one is money already spent, the other is money not yet spent.
  function shotCause(row){
    if(!row)return '';
    const parts=[];
    if(row.reason)parts.push(row.reason);
    if(row.blocker==='attempt_unresolved')parts.push('这次提交的结果还没有核实，先查询原任务，不要重新提交。');
    if(row.blocker==='stale_source')parts.push('这版依据已经变化，需要按当前脚本重新保存输入。');
    if(row.blocker==='unprepared')parts.push('这面镜头还没有制作输入。');
    return parts.join(' ');
  }
  function shotBadge(row,work){
    if(row)return row.stage_label||row.stage||'';
    return work.label||'';
  }
  function productionWorkForDraft(draft,data,runId){

    const attempt=(draft.attempts||[]).at(-1)||null;
    const refs=referenceInputs(draft,data,runId);
    const ref=refs.find(item=>item.purpose===(draft.kind==='video'?'continuity':'product'))||refs[0];
    const referenceUrl=ref?.url||'',currentOutput=outputUrl(attempt,data,runId);
    const result=readableResult(draft,data,runId);
    const base={kind:draft.kind,draft,attempt,displayAttempt:result?.attempt||null,
      displaySourceVersion:result?.draft?.source_version||null,
      displaySourceSha256:result?.draft?.source_sha256||null,
      outputUrl:result?.url||'',referenceUrl,referenceName:ref?.name||'',outputName:result?.name||'',
      resultIsPrevious:Boolean(result&&(result.draft.draft_id!==draft.draft_id||result.attempt.attempt_id!==attempt?.attempt_id)),
      primaryAction:'none',phase:'blocked',label:'当前制作需要核对',detail:''};
    if(attempt){
      if(['unknown','submission_unknown'].includes(attempt.status))return {...base,phase:'unknown',
        primaryAction:attempt.task_id?'refresh':'recover',label:'这次生成结果还未确认',
        detail:'先核对或查询原任务；现在重新提交可能产生第二次生成和费用。'};
      if(['queued','running','preparing','submitting','submitted','downloading','recovering','cancelling'].includes(attempt.status))
        return {...base,phase:'processing',label:statuses[attempt.status]||'正在处理',detail:'这次任务仍在进行，结果会在此显示。'};
      if(attempt.status==='retrieve_failed')return {...base,phase:'unknown',primaryAction:attempt.task_id?'refresh':'recover',
        label:'这次结果尚未取回',detail:'查询原任务并取回结果，不会重新生成。'};
    }
    if(draft.design_issue||draft.source_ready===false)return {...base,phase:'stale',primaryAction:'discuss',label:draft.design_issue?'本镜制作输入需要重新核对':'这版作品依据已变化',detail:draft.design_issue||'告诉团队变化在哪里，旧结果仍可在这里阅读；不能沿用旧批准。'};
    if(attempt){
      if(['succeeded','completed'].includes(attempt.status)){
        if(!currentOutput)return {...base,phase:'missing',primaryAction:attempt.task_id?'refresh':'none',
          label:'结果文件暂时无法核对',detail:'记录显示任务已完成，但本地媒体文件不可用；请先核实原任务。'};
        if(draft.kind==='image'&&!(data.materials||[]).some(m=>m.name===attempt.output.name&&m.sha256===attempt.output.sha256&&m.approved))
          return {...base,phase:'image_pending',primaryAction:'approve_image',label:'这张图片等你确认',
            detail:'打开大图核对商品、画面和文字。确认只适用于这一张图片，不会启动视频生成。'};
        return {...base,phase:'completed',primaryAction:'download',
          label:draft.kind==='video'?'视频已取回':'图片已确认',
          detail:draft.kind==='video'?'播放核对画面和声音，再下载或针对时间点反馈。':'这张图片可以下载，也可以继续提出修改。'};
      }
      if(['failed','blocked','interrupted','cancelled'].includes(attempt.status))return {...base,phase:'failed',
        primaryAction:'discuss',label:'这次制作未完成',detail:attempt.message||'团队可以核对已记录的失败；再次生成仍需重新核对并确认。'};
    }
    if(draft.kind==='video'&&(!referenceUrl||draft.reference_approved!==true))return {...base,phase:'image_pending',
      primaryAction:referenceUrl?'approve_image':'view_images',label:'先确认这张视频参考图',
      detail:referenceUrl?'打开大图核对商品、画面和文字。图审不会启动视频生成。':'参考图已变化或不可用，请回到画面区核对。'};
    const reason=gate(draft,data);
    if(reason)return {...base,phase:'blocked',
      primaryAction:data.active?'none':!data.capabilities?.[draft.kind]?.available?'settings':'discuss',
      label:'本次制作还不能提交',detail:reason};
    return {...base,phase:'ready',primaryAction:'generate',label:draft.kind==='video'?'视频可以生成':'首帧待生成',
      detail:'核对准确参考、生成范围与费用后，由你明确启动这一次任务。'};
  }
  function currentProductionWork(data,kind,runId){
    const all=(data?.drafts||[]).filter(d=>d.kind===kind);
    const drafts=all.filter(d=>d.is_current!==false),current=drafts.filter(d=>d.source_ready!==false);
    const pool=current.length?current:(drafts.length?drafts:all);
    const unresolved=drafts.filter(d=>['unknown','submission_unknown','retrieve_failed','queued','running','preparing','submitting','submitted','downloading','recovering']
      .includes((d.attempts||[]).at(-1)?.status));
    const draft=(unresolved.length?unresolved:pool).at(-1);
    if(draft)return productionWorkForDraft(draft,data,runId);
    const readySource=(data?.sources||[]).some(s=>s.ready);
    const readyReference=(data?.materials||[]).some(m=>m.kind==='image'&&m.approved);
    const chosenReference=kind==='video'?data?.latest_approved_image:null;
    const base={kind,phase:'needs_plan',draft:null,attempt:null,outputUrl:'',referenceUrl:'',outputName:'',
      primaryAction:'open_plan',label:'先完成当前作品方案',detail:'脚本确认后，团队才能准备准确的制作输入。'};
    if(!readySource)return base;
    if(data.visual_design_status?.ready===false)return {...base,phase:'needs_design',label:'先补齐共同人物与场景设计',
      detail:(data.visual_design_status.issues||[]).join(' ')||'当前素材计划没有可用的共同设计，请先补齐设计与分镜。'};
    if(kind==='video'&&!readyReference)return {...base,phase:'image_pending',primaryAction:'view_images',
      label:'先确认用于视频的图片',detail:'在画面区查看并确认准确图片，然后准备视频输入。'};
    return {...base,phase:'prepare',primaryAction:'prepare',
      referenceName:chosenReference?.name||'',referenceUrl:mediaUrl(chosenReference?.url,runId),
      label:kind==='video'?'视频制作输入待准备':'画面制作输入待准备',
      detail:kind==='video'&&chosenReference?`已确认参考图：${chosenReference.name}。团队将基于这张图和当前脚本准备视频输入。`
        :'让团队基于已确认的脚本准备准确输入，之后再核对是否生成。'};
  }
  function batchBlocker(data,runId){
    return (data?.drafts||[]).filter(draft=>draft.kind==='image'&&draft.source_ready!==false&&draft.is_current!==false)
      .map((draft,index)=>({draft,index,work:productionWorkForDraft(draft,data,runId)}))
      .find(({work})=>['unknown','processing'].includes(work.phase))||null;
  }
  function batchGenerationDrafts(data,runId){
    if(batchBlocker(data,runId))return [];
    return (data?.drafts||[]).filter(draft=>draft.kind==='image'&&draft.source_ready!==false
      &&draft.is_current!==false&&
      !gate(draft,data)&&!productionWorkForDraft(draft,data,runId).outputUrl);
  }
  function batchApprovalImages(data,runId){
    const seen=new Set();
    return (data?.drafts||[]).filter(draft=>draft.kind==='image'&&draft.source_ready!==false&&draft.is_current!==false)
      .map((draft,index)=>({draft,index,work:productionWorkForDraft(draft,data,runId)}))
      .filter(({work})=>work.outputUrl&&work.displayAttempt?.output)
      .map(({draft,index,work})=>({draft,index,output:work.displayAttempt.output,url:work.outputUrl}))
      .filter(({output})=>{
        const key=`${output.name}:${output.sha256}`;
        const material=(data.materials||[]).find(m=>m.name===output.name&&m.sha256===output.sha256);
        if(!material||material.approved||seen.has(key))return false;
        seen.add(key);return true;
      });
  }
  function videoSequenceState(data,runId){
    const drafts=new Map((data?.drafts||[]).map(d=>[d.draft_id,d]));
    return (data?.video_sequence||[]).map(shot=>{
      const draft=drafts.get(shot.video_draft_id),reference=(data.materials||[]).find(m=>
        m.name===shot.reference?.name&&m.sha256===shot.reference?.sha256);
      const attempts=draft?.attempts||[];
      const completed=attempts.filter(a=>a.status==='succeeded'&&outputUrl(a,data,runId)).at(-1)||null;
      const unresolved=attempts.find(a=>['queued','running','preparing','submitting','submitted',
        'downloading','unknown','submission_unknown','recovering','retrieve_failed','cancelling'].includes(a.status))
        ||(!completed?attempts.find(a=>a.status==='succeeded'&&a.output):null);
      return {...shot,draft,referenceUrl:mediaUrl(reference?.url,runId),
        completed,outputUrl:completed?outputUrl(completed,data,runId):'',unresolved};
    });
  }
  function videoBatchGenerationDrafts(data,runId){
    const sequence=videoSequenceState(data,runId);
    if(sequence.length<2||data.active||sequence.some(shot=>!shot.approved||!shot.draft||shot.unresolved))return [];
    return sequence.filter(shot=>!shot.completed).map(shot=>shot.draft)
      .filter(draft=>!gate(draft,data));
  }
  function assemblyClips(data,runId){
    const sequence=videoSequenceState(data,runId);
    if(sequence.length<2||sequence.some(shot=>!shot.approved||!shot.draft||!shot.completed||shot.unresolved))return [];
    return sequence.map(shot=>({draft_id:shot.draft.draft_id,draft_sha256:shot.draft.draft_sha256,
      attempt_id:shot.completed.attempt_id,name:shot.completed.output.name,sha256:shot.completed.output.sha256}));
  }
  function currentAssembly(data,runId){
    const clips=assemblyClips(data,runId);
    if(!clips.length)return null;
    return (data.assemblies||[]).slice().reverse().find(assembly=>
      JSON.stringify(assembly.clips)===JSON.stringify(clips)&&
      assembly.edit_plan_sha256===data.edit_plan?.plan_sha256&&
      (data.materials||[]).some(m=>m.name===assembly.output?.name&&m.sha256===assembly.output?.sha256)
      &&mediaUrl(assembly.output?.url,runId))||null;
  }
  function batchConfirmationHtml(intent,data,runId){
    const generation=['generate','video_generate'].includes(intent.kind);
    const video=intent.kind==='video_generate';
    if(intent.kind==='generate'&&intent.connection_revision!==undefined&&
        intent.connection_revision!==data.capabilities?.image?.connection_revision)return '';
    if(intent.kind==='video_prepare'){
      const images=intent.images.map((item,index)=>{
        const material=(data.materials||[]).find(m=>m.name===item.name&&m.sha256===item.sha256&&m.approved);
        const url=mediaUrl(material?.url,runId);
        return url?`<li><strong>第 ${index+1} 段参考图</strong><img src="${esc(url)}" alt="第 ${index+1} 段已确认参考图"><small>${esc(item.name)}</small></li>`:null;
      });
      if(images.some(item=>!item))return '';
      return `<section class="production-batch-confirm"><strong>准备 ${images.length} 段视频输入</strong><p>团队将基于已确认脚本和这组图片准备逐段描述、时长与费用；这一步只调用文字模型，不生成视频。</p><ol>${images.join('')}</ol><button type="button" class="primary" data-batch-confirm>确认准备视频输入</button><button type="button" class="ghost small" data-batch-dismiss>继续修改</button><p role="status" data-batch-status></p></section>`;
    }
    if(intent.kind==='assemble'){
      const clips=assemblyClips(data,runId);
      const plan=data.edit_plan;
      if(JSON.stringify(clips)!==JSON.stringify(intent.clips)||!plan||
          plan.request_id!==intent.edit_plan_id||plan.plan_sha256!==intent.edit_plan_sha256)return '';
      return `<section class="production-batch-confirm"><strong>本地拼接 ${clips.length} 段成片</strong>`+
        `<p>按已确认脚本的 ${plan.cuts.length} 个切点剪辑，总长 ${esc(plan.duration_seconds)} 秒；本地转码与拼接，不发起提供方生成。完成后仍须播放核对声画。</p>`+
        `<ol>${plan.cuts.map((cut,index)=>`<li>切点 ${index+1}：画面来自第 ${cut.clip_index} 段 ${esc(cut.start_seconds)}–${esc(cut.start_seconds+cut.duration_seconds)} 秒；声音来自第 ${cut.audio_clip_index} 段 ${esc(cut.audio_start_seconds)} 秒起，时长 ${esc(cut.duration_seconds)} 秒</li>`).join('')}</ol>`+
        (plan.overlays?.length?`<p>片内字卡：${plan.overlays.map(overlay=>`${esc(overlay.text)}（${esc(overlay.start_seconds)}–${esc(overlay.start_seconds+overlay.duration_seconds)} 秒）`).join('；')}</p>`:'')+
        `<button type="button" class="primary" data-batch-confirm>按这份计划拼接</button><button type="button" class="ghost small" data-batch-dismiss>返回</button><p role="status" data-batch-status></p></section>`;
    }
    const items=generation?intent.drafts.map((item,index)=>{
      const draft=(data.drafts||[]).find(row=>row.draft_id===item.draft_id&&row.draft_sha256===item.expected_sha256);
      if(!draft||gate(draft,data)||referenceInputs(draft,data,runId).some(ref=>!ref.url))return null;
      const description=generationPrompt(draft).trim();
      const shotDescription=String(draft.shot_prompt??description).trim();
      const sentences=shotDescription.match(/[^.!?。]+[.!?。]?/g)||[];
      const scene=(sentences.find(sentence=>!/^\s*Vertical\b/i.test(sentence))||sentences[0]||shotDescription).trim();
      return `<li><strong>${video?'视频段':'首帧'} ${String(index+1).padStart(2,'0')}</strong>`+
        designBindingHtml(draft)+referencesHtml(draft,data,runId)+
        `<p>${esc(scene)}</p><details><summary>查看完整生成描述</summary><p>${esc(description)}</p></details>`+
        `<small>${video?`${esc(draft.duration)} 秒 · ${esc(draft.resolution)} · `:''}${price(draft.estimate)} · ${esc(draft.estimate?.notice||'实际费用以提供方为准')}</small></li>`;
    }):intent.images.map((item,index)=>{
      const material=(data.materials||[]).find(m=>m.name===item.name&&m.sha256===item.sha256);
      const url=mediaUrl(material?.url,runId);
      return url?`<li><strong>生成图 ${String(index+1).padStart(2,'0')}</strong><a href="${esc(url)}" target="_blank" rel="noopener" aria-label="查看第 ${index+1} 张生成图大图"><img src="${esc(url)}" alt="待确认的第 ${index+1} 张生成图"></a><small>${esc(item.name)}</small></li>`:null;
    });
    if(items.some(item=>!item))return '';
    const amount=generation?intent.drafts.reduce((sum,item)=>{
      const draft=(data.drafts||[]).find(row=>row.draft_id===item.draft_id);
      return sum+(typeof draft?.estimate?.amount==='number'?draft.estimate.amount:0);
    },0):0;
    const known=generation&&intent.drafts.every(item=>typeof (data.drafts||[]).find(row=>row.draft_id===item.draft_id)?.estimate?.amount==='number');
    // What this authorisation is bound to. Scenario F depends on it being visible:
    // if any shot changes afterwards the frozen fingerprint stops matching and the
    // confirmation is refused rather than quietly generating the new version.
    const sourceRow=generation?(data.drafts||[]).find(row=>row.draft_id===intent.drafts[0]?.draft_id):null;
    const sourceLine=generation&&sourceRow
      ? `来源：脚本第 ${esc(sourceRow.source_version)} 版 · 输入：当前 ${items.length} 面准确分镜 · 模型：${esc(video?(sourceRow.model||'wan3.0-video'):`Codex Image / ${data.capabilities?.image?.controller_model||'本机默认模型'}`)} · 费用：${known?`估算 CNY ${amount.toFixed(2)}`:'未知'}`
      : '';
    return `<section class="production-batch-confirm production-batch-${intent.kind}"><strong>${generation?`一次生成整组${video?'视频段':'首帧'}`:'一次确认这些生成图'}</strong>`+
      (sourceLine?`<p class="production-batch-source">${sourceLine}</p>`:'')+
      `<p>${generation?`将按分镜顺序提交 ${items.length} 个${video?'视频':'图片'}任务；${known?`估算合计 CNY ${amount.toFixed(2)}`:'费用未知'}。中途失败或结果未知会停止后续提交。`:`这 ${items.length} 张已生成图片将一起确认；图片确认不会自动提交视频生成。`}</p>`+
      `<ol>${items.join('')}</ol><button type="button" class="primary" data-batch-confirm>${generation?`确认并生成 ${items.length} ${video?'段':'张'}`:`确认这 ${items.length} 张图片`}</button>`+
      `<button type="button" class="ghost small" data-batch-dismiss>继续修改</button><p role="status" data-batch-status></p></section>`;
  }
  function canvasHtml(data,kind,runId,rail){
    if(kind==='image'){
      const rows=shotRows(data,'image'),coverage=shotCoverage(data,'image');
      // A revision replaces its shot's input. Showing the replaced one next to it
      // is how "改第二镜" read as "there are now seven shots".
      const drafts=(data?.drafts||[]).filter(draft=>draft.kind==='image'&&(draft.source_ready!==false||draft.design_issue)&&draft.is_current!==false);
      const superseded=supersededDrafts(data,'image');
      if(drafts.length||rows.length){
        const generated=drafts.filter(draft=>productionWorkForDraft(draft,data,runId).outputUrl).length;
        const items=drafts.map((draft,index)=>{
          const work=productionWorkForDraft(draft,data,runId);
          const row=shotForDraft(draft,data,'image');
          const description=String(draft.shot_prompt??draft.prompt??'').trim();
          const sentences=description.match(/[^.!?。]+[.!?。]?/g)||[];
          const scene=(sentences.find(sentence=>!/^\s*Vertical\b/i.test(sentence))||sentences[0]||'查看出图描述').trim();
          const shortScene=scene.length>72?`${scene.slice(0,72).trimEnd()}…`:scene;
          // Position falls back to the visible order when the server sent no
          // coverage for this project (older payload, or a legacy script with no
          // readable shot list). An empty label would read as a rendering fault.
          const ordinal=row?.ordinal??(index+1);
          const cause=shotCause(row);
          const badge=shotBadge(row,work);
          const shotKey=row?.shot_id||draft.shot_id||draft.draft_id;
          const revision=row?.revision&&row.revision>1?`第 ${row.revision} 版输入`:'';
          return `<details class="production-shot" data-draft-id="${esc(draft.draft_id)}" data-shot-id="${esc(shotKey)}"><summary><strong>镜头 ${String(ordinal).padStart(2,'0')}</strong><span>${esc(shortScene)}</span><small>${esc(badge)}</small></summary>`+
            `<div class="production-shot-body">`+
            `<p class="production-shot-stage">${esc(badge)}${row?.blocker?` · ${esc(row.blocker)}`:''}${revision?` · ${esc(revision)}`:''}</p>`+
            (cause?`<p class="production-shot-cause" role="status">${esc(cause)}</p>`:'')+
            (work.outputUrl?'':work.detail?`<p class="production-shot-next">${esc(work.detail)}</p>`:'')+
            designBindingHtml(draft)+
            `<p class="production-prompt">${esc(description||'暂无出图描述')}</p>`+
            (generationPrompt(draft)!==description?`<details class="production-confirm-prompt"><summary>查看实际发送的完整画面描述</summary><p class="production-prompt">${esc(generationPrompt(draft))}</p></details>`:'')+
            (work.outputUrl?`<figure class="production-shot-output"><img src="${esc(work.outputUrl)}" alt="镜头 ${ordinal} 的生成结果"><figcaption>这面镜头的生成结果</figcaption></figure>`:'')+
            referencesHtml(draft,data,runId)+
            `<button type="button" class="ghost small" data-feedback-image-draft="${esc(draft.draft_id)}">对这条描述提意见</button>`+
            (work.outputUrl&&work.displayAttempt?`<button type="button" class="ghost small" data-feedback-image-attempt="${esc(work.displayAttempt.attempt_id)}">批注这张生成图</button>`:'')+
            `</div></details>`;
        }).join('');
        // A shot with no current input still deserves a line: otherwise a
        // six-shot storyboard with three inputs silently looks like a three-shot
        // storyboard.
        const blanks=(coverage?.shots||[]).filter(row=>!drafts.some(draft=>draft.draft_id===row.draft_id))
          .map(row=>`<details class="production-shot production-shot-empty" data-shot-id="${esc(row.shot_id)}"><summary><strong>镜头 ${String(row.ordinal).padStart(2,'0')}</strong><span>${esc(row.label||'')}</span><small>${esc(row.stage_label||'')}</small></summary>`+
            `<div class="production-shot-body"><p class="production-shot-stage">${esc(row.stage_label||'')}</p>`+
            (shotCause(row)?`<p class="production-shot-cause" role="status">${esc(shotCause(row))}</p>`:'')+
            `<p class="production-shot-next">这面镜头还没有制作输入。</p></div></details>`).join('');
        const total=coverage?.total??(drafts.length+superseded.length);
        const countLine=coverage?.complete_list===false
          ? `${coverage.notice||'目标镜头清单不可读'}`
          : coverage?.complete_list
            ? `目标 ${total} 面镜头，已准备输入 ${(coverage.shots||[]).filter(item=>item.stage!=='unprepared').length} 面，已生成 ${generated} 张。`
            : `已准备输入 ${drafts.length} 面，已生成 ${generated} 张。`;
        const supersededLine=superseded.length
          ? `<details class="production-shot-history"><summary>已被替换的历史输入 · ${superseded.length}</summary><ul>${superseded.map(draft=>`<li>${esc(String(draft.prompt||'').slice(0,80))} · 已被 ${esc(draft.superseded_by||'新版本')} 替换</li>`).join('')}</ul><p class="form-note">历史输入保留可读，不会和当前输入一起生成。</p></details>`
          : '';
        return `<article class="production-canvas production-storyboard"><header><span class="creator-kicker">画面</span><h2>分镜首帧（${total}）</h2><p>${esc(countLine)} 点开一条查看这一镜的状态、共享设计、出图描述与参考图用途。</p></header><div class="production-shot-list">${items}${blanks}</div>${supersededLine}</article>`;
      }
    }
    if(kind==='video'&&(data?.video_sequence||[]).length>1){
      const sequence=videoSequenceState(data,runId),assembly=currentAssembly(data,runId);
      const plan=data.edit_plan;
      const planHtml=plan?`<section class="creator-caption production-edit-plan"><h3>当前脚本的成片剪辑计划</h3>`+
        `<p>${esc(plan.duration_seconds)} 秒 · ${plan.cuts.length} 个画面切点`+
        (plan.confirmed_product?` · 商品：${esc(plan.confirmed_product)}`:'')+`</p>`+
        `<details><summary>查看逐段剪辑与字卡</summary><ol>${plan.cuts.map(cut=>
          `<li>画面 ${cut.clip_index}：${esc(cut.start_seconds)} 秒起，取 ${esc(cut.duration_seconds)} 秒；声音 ${cut.audio_clip_index}：${esc(cut.audio_start_seconds)} 秒起</li>`).join('')}</ol>`+
        (plan.overlays?.length?`<p>字卡：${plan.overlays.map(overlay=>`${esc(overlay.text)}（${esc(overlay.start_seconds)} 秒起，${esc(overlay.duration_seconds)} 秒）`).join('；')}</p>`:'')+
        `</details></section>`:'';
      const items=sequence.map(shot=>`<details class="production-shot" ${shot.outputUrl?'':'open'}><summary><strong>视频段 ${String(shot.index).padStart(2,'0')}</strong><small>${shot.outputUrl?'已取回':shot.unresolved?statuses[shot.unresolved.status]||'待核实':shot.draft?'待生成':shot.approved?'待准备输入':'待确认首帧'}</small></summary>`+
        `<div class="production-shot-body">${shot.outputUrl?`<video controls preload="metadata" src="${esc(shot.outputUrl)}"></video>`:shot.referenceUrl?`<img src="${esc(shot.referenceUrl)}" alt="第 ${shot.index} 段首帧">`:''}`+
        (shot.draft?designBindingHtml(shot.draft)+`<p class="production-prompt">${esc(shot.draft.shot_prompt??shot.draft.prompt)}</p>`+
          (shot.draft.shot_prompt!==undefined?`<details class="production-confirm-prompt"><summary>查看实际发送的完整画面描述</summary><p class="production-prompt">${esc(generationPrompt(shot.draft))}</p></details>`:'')+
          referencesHtml(shot.draft,data,runId)+`<small>${esc(shot.draft.duration)} 秒 · ${esc(shot.draft.resolution)} · ${price(shot.draft.estimate)}</small>`:'<p>这段的视频输入尚未准备。</p>')+
        `</div></details>`).join('');
      const writing=(rail||[]).find(step=>step.stage==='writing'&&step.has_doc);
      const caption=assembly&&writing&&!writing.stale&&writing.integrity_ok!==false?captionBlock(writing)?.text:'';
      return `<article class="production-canvas production-video-sequence"><header><span class="creator-kicker">成片与文案</span><h2>${assembly?'五段成片已保存':`${sequence.length} 段视频制作`}</h2><p>每段使用一张已确认首帧，按脚本顺序生成并拼接。</p></header>`+
        (assembly?`<div class="production-canvas-media"><video controls preload="metadata" src="${esc(mediaUrl(assembly.output.url,runId))}"></video><a class="primary production-download" href="${esc(mediaUrl(assembly.output.url,runId))}" download="${esc(assembly.output.name)}">下载成片</a></div>`:'')+
        planHtml+`<div class="production-shot-list">${items}</div>`+
        (assembly?`<section class="creator-caption"><h3>当前成片对应的发布文案</h3><p>${caption?esc(caption):'请核对这版成片对应的脚本与发布文案。'}</p></section>`:'')+'</article>';
    }
    const work=currentProductionWork(data,kind,runId);
    const media=work.outputUrl?(kind==='video'
      ? `<video controls preload="metadata" src="${esc(work.outputUrl)}"></video>`
      : `<img src="${esc(work.outputUrl)}" alt="当前生成图片">`):'';
    const reference=work.referenceUrl&&!work.outputUrl?`<figure class="production-canvas-reference"><img src="${esc(work.referenceUrl)}" alt="本次使用的参考素材"><figcaption>参考素材 · 尚未生成${kind==='video'?'视频':'画面'}</figcaption></figure>`:'';
    const writing=(rail||[]).find(step=>step.stage==='writing'&&step.has_doc);
    const paired=kind==='video'&&work.outputUrl&&captionMatches(writing,work);
    const caption=paired?captionBlock(writing)?.text:'';
    return `<article class="production-canvas"><header><span class="creator-kicker">${kind==='video'?'成片与文案':'当前画面'}</span><h2>${esc(work.label)}</h2></header>`+
      (media?`<div class="production-canvas-media">${media}</div>`:`<div class="creator-production-empty">这里还没有生成的${kind==='video'?'视频':'画面'}。</div>`)+reference+
      `<p>${esc(work.detail)}</p>`+
      (work.draft?designBindingHtml(work.draft)+referencesHtml(work.draft,data,runId):'')+
      (kind==='video'&&work.outputUrl?`<section class="creator-caption"><h3>这段视频对应的发布文案</h3><p>${caption?esc(caption):'暂时没有可核对的同版发布文案。'}</p></section>`:'')+
      '</article>';
  }
  function historyDraftHtml(draft,data,runId){
    const reason=gate(draft,data),ref=(data.materials||[]).find(m=>m.name===draft.reference?.name&&m.sha256===draft.reference?.sha256),url=mediaUrl(ref?.url,runId);
    const attempts=draft.attempts||[];
    const availableOutput=a=>{
      const found=(data.materials||[]).some(m=>m.name===a.output?.name&&m.sha256===a.output?.sha256);
      return found?mediaUrl(a.output?.url,runId):'';
    };
    const complete=attempts.slice().reverse().find(a=>['succeeded','completed'].includes(a.status)&&availableOutput(a));
    const output=complete?availableOutput(complete):'';
    const latest=attempts.at(-1);
    const missingResult=!!(latest?.output&&['succeeded','completed'].includes(latest.status)&&!availableOutput(latest));
    const hero=output
      ? `<div class="creator-result">${draft.kind==='video'
        ? `<video controls preload="metadata" src="${esc(output)}"></video>`
        : `<button class="production-thumb" data-image="${esc(complete.output.name)}"><img src="${esc(output)}" alt="生成图片，点击查看与确认"></button>`}
        <div class="creator-result-caption">${draft.kind==='video'?'视频已取回，可播放核对画面和声音。':'图片已取回，点击查看并确认画面。'} <button type="button" class="ghost small" data-feedback-attempt="${esc(complete.attempt_id)}">${draft.kind==='video'?'反馈当前时间点':'反馈这张图'}</button> <a href="${esc(output)}" download="${esc(complete.output.name||'作品')}">下载${draft.kind==='video'?'视频':'图片'}</a></div></div>`
      : missingResult ? '<div class="creator-production-empty">结果记录已保存，但媒体文件暂时无法核对。请检查制作记录和素材。</div>'
        : url ? `<div class="creator-result"><button class="production-thumb" data-image="${esc(ref.name)}"><img src="${esc(url)}" alt="参考图：${esc(ref.name)}"></button><div class="creator-result-caption">${ref.approved?'已确认参考图':'参考图等待你查看与确认'}</div></div>`
          : `<div class="creator-production-empty">${latest?esc(statuses[latest.status]||'结果尚未取回'):'画面尚未生成'}。核对并确认输入后，作品会显示在这里。</div>`;
    const records=attempts.map(a=>{
      const prior=availableOutput(a),unknown=['unknown','submission_unknown'].includes(a.status);
      return `<section><strong>${esc(statuses[a.status]||a.status)}</strong><p>${esc(a.message||'')}</p>`+
        (prior&&a!==complete?(draft.kind==='video'?`<video controls preload="metadata" src="${esc(prior)}"></video>`:`<button class="production-thumb" data-image="${esc(a.output.name)}"><img src="${esc(prior)}" alt="历史生成图片"></button>`):'')+
        (a.task_id&&a.status!=='succeeded'&&a.status!=='completed'?`<button class="ghost small" data-refresh="${esc(a.attempt_id)}">查询并取回结果</button>`:'')+
        (unknown?`<details><summary>核实提供方提交记录</summary><p class="form-note">${draft.kind==='video'?'请从提供方控制台核对。填写任务编号后，只会取回该任务；不会重新生成。':'如提供方证明未提交且未产生费用，可登记核实结果；不会重新生成。'}</p><button class="ghost small" data-recover="${esc(a.attempt_id)}">填写核实结果</button></details>`:'')+'</section>';
    }).join('');
    return `<article class="production-card"><header><div><span class="eyebrow">${draft.kind==='video'?'视频':'图片'} · 脚本版本 ${esc(draft.source_version)}</span><h3>${draft.kind==='video'?`${esc(draft.duration)} 秒 · ${esc(draft.resolution)}`:'图片制作方案'}</h3></div><span class="production-price">${price(draft.estimate)}</span></header>`+
      hero+`<p class="creator-production-state">${missingResult?'结果文件暂不可用':esc(latest?statuses[latest.status]||latest.status:reason||'等待制作')}${latest?.message?` · ${esc(latest.message)}`:''}</p>`+
      `<div class="production-actions"><button class="primary" data-generate="${esc(draft.draft_id)}" ${reason?'disabled':''}>核对并生成</button><button class="ghost" data-edit-input="${esc(draft.draft_id)}">手动调整制作输入</button></div>`+
      (reason?`<p class="form-note">${esc(reason)}</p>`:'')+
      designBindingHtml(draft)+referencesHtml(draft,data,runId)+
      `<details class="creator-production-detail"><summary>制作描述、检查依据与费用说明</summary><p class="production-prompt">${esc(generationPrompt(draft))}</p><p class="form-note">${esc(draft.estimate?.notice||'费用以提供方实际结算为准。')}</p>${reviewHtml(draft.review)}</details>`+
      (records?`<details class="creator-production-detail"><summary>制作与取回记录 · ${attempts.length}</summary><div class="production-attempts">${records}</div></details>`:'')+'</article>';
  }
  function canDiagnoseWork(work){
    if(work?.primaryAction!=='discuss')return false;
    if(work.phase==='failed')return ['failed','blocked','interrupted'].includes(work.attempt?.status);
    return false;
  }
  function primaryWorkAction(work){
    const draft=work.draft,attempt=work.attempt;
    switch(work.primaryAction){
      case 'approve_image':return `<button class="primary" data-image="${esc(draft.kind==='video'?work.referenceName:attempt?.output?.name)}">查看并确认这张图</button>`;
      case 'generate':return `<button class="primary" data-generate="${esc(draft.draft_id)}">核对并生成${draft.kind==='video'?'视频':'图片'}</button>`;
      case 'refresh':return `<button class="primary" data-refresh="${esc(attempt.attempt_id)}">查询这次结果</button>`;
      case 'recover':return `<button class="primary" data-recover="${esc(attempt.attempt_id)}">核实提供方提交记录</button>`;
      case 'discuss':return canDiagnoseWork(work)
        ? `<button class="primary" data-production-diagnose="${esc(draft.draft_id)}">让团队检查并提出修正</button><button class="ghost" data-production-feedback="${esc(draft.draft_id)}">自己描述问题</button>`
        : `<button class="primary" data-production-feedback="${esc(draft.draft_id)}">告诉团队这次问题</button>`;
      case 'settings':return '<button class="primary" data-model-settings>打开模型设置</button>';
      case 'download':return `<a class="primary production-download" href="${esc(work.outputUrl)}" download="${esc(work.outputName||'作品')}">下载${draft.kind==='video'?'视频':'图片'}</a>`;
      case 'view_images':return '<button class="primary" data-view-images>查看并确认画面</button>';
      default:return '';
    }
  }
  function draftHtml(draft,data,runId){
    const work=productionWorkForDraft(draft,data,runId),attempts=draft.attempts||[];
    const media=work.outputUrl
      ? draft.kind==='video'?`<video controls preload="metadata" src="${esc(work.outputUrl)}"></video>`
        : `<button class="production-thumb" data-image="${esc(work.outputName)}"><img src="${esc(work.outputUrl)}" alt="${work.phase==='image_pending'?'待确认图片':'当前图片'}"></button>`
      : draft.kind==='video'&&work.referenceUrl&&work.phase!=='missing'?`<button class="production-thumb" data-image="${esc(work.referenceName)}"><img src="${esc(work.referenceUrl)}" alt="本段视频使用的首帧参考"></button>`:'';
    const resultCaption=work.resultIsPrevious?`上一份已生成结果${work.displaySourceVersion ? ` · 来自脚本第 ${work.displaySourceVersion} 版` : ''} · 可继续核对；本次尝试的状态见下方。`
      :work.outputUrl?work.phase==='image_pending'?'这张图片已取回，等待你核对并确认。'
        :work.phase==='completed'?'当前可查看的作品':'已生成结果仍可核对；本次任务状态见下方。'
      :'首帧仅作本段视频的起始参考；这段视频尚未生成。';
    const hero=media?`<div class="creator-result${!work.outputUrl?' creator-result-reference':''}">${media}<div class="creator-result-caption">${esc(resultCaption)}`+
      (work.displayAttempt&&work.outputUrl?` <button type="button" class="ghost small" data-feedback-attempt="${esc(work.displayAttempt.attempt_id)}">${draft.kind==='video'?'反馈当前时间点':'反馈这张图'}</button>`:'')+'</div></div>'
      : `<div class="creator-production-empty">${esc(work.phase==='missing'?'结果记录已保存，但媒体文件暂时无法核对。':work.phase==='processing'?'本次任务正在处理，结果取回后会显示在这里。':'本次画面尚未生成。')}</div>`;
    const pendingReference=draft.kind==='video'&&work.phase==='image_pending'&&work.referenceUrl&&work.outputUrl
      ? `<div class="creator-result"><button class="production-thumb" data-image="${esc(work.referenceName)}"><img src="${esc(work.referenceUrl)}" alt="本次待确认的视频参考图"></button><div class="creator-result-caption">本次待确认的参考图 · 查看大图后才能确认。</div></div>` : '';
    const records=attempts.map(a=>{
      const prior=outputUrl(a,data,runId),unknown=['unknown','submission_unknown'].includes(a.status);
      return `<section><strong>${esc(statuses[a.status]||a.status)}</strong><p>${esc(a.message||'')}</p>`+
        (prior&&a!==work.attempt?(draft.kind==='video'?`<video controls preload="metadata" src="${esc(prior)}"></video>`:`<button class="production-thumb" data-image="${esc(a.output.name)}"><img src="${esc(prior)}" alt="历史生成图片"></button>`):'')+
        (a.task_id&&a.status!=='succeeded'&&a.status!=='completed'?`<button class="ghost small" data-refresh="${esc(a.attempt_id)}">查询并取回结果</button>`:'')+
        (unknown?`<details><summary>核实提供方提交记录</summary><p class="form-note">${draft.kind==='video'?'请从提供方控制台核对。填写任务编号后，只会取回该任务；不会重新生成。':'如提供方证明未提交且未产生费用，可登记核实结果；不会重新生成。'}</p><button class="ghost small" data-recover="${esc(a.attempt_id)}">填写核实结果</button></details>`:'')+'</section>';
    }).join('');
    const reason=gate(draft,data);
    const next=`<p class="creator-production-state">${esc(work.detail)}</p>`+
      `<div class="production-actions production-primary-actions">${primaryWorkAction(work)}</div>`;
    return `<article class="production-card production-work production-phase-${esc(work.phase)}" data-draft-id="${esc(draft.draft_id)}"><header><div><span class="eyebrow">${draft.kind==='video'?'视频':'图片'} · 脚本第 ${esc(draft.source_version)} 版</span><h3>${esc(work.label)}</h3></div>`+
      (work.phase==='ready'?`<span class="production-price">${price(draft.estimate)}</span>`:'')+'</header>'+
      (work.phase==='unknown'?next+hero:hero+pendingReference+next)+
      designBindingHtml(draft)+referencesHtml(draft,data,runId)+
      `<details class="creator-production-detail"><summary>制作描述、检查依据与费用说明</summary><p class="production-prompt">${esc(generationPrompt(draft))}</p><p class="form-note">${esc(draft.estimate?.notice||'费用以提供方实际结算为准。')}</p>${reviewHtml(draft.review)}</details>`+
      `<details class="creator-production-detail"><summary>再次制作或高级编辑</summary><div class="production-actions"><button class="ghost" data-edit-input="${esc(draft.draft_id)}">手动调整制作输入</button><button class="ghost" data-generate="${esc(draft.draft_id)}" ${reason?'disabled':''}>再生成一次（可能产生新费用）</button></div>${reason?`<p class="form-note">${esc(reason)}</p>`:''}</details>`+
      (records?`<details class="creator-production-detail"><summary>制作与取回记录 · ${attempts.length}</summary><div class="production-attempts">${records}</div></details>`:'')+'</article>';
  }
  function emptyWorkHtml(work){
    const action=work.primaryAction==='prepare'?`<button class="primary" data-prepare>让团队${work.kind==='video'?'准备这张图的视频输入':'准备画面输入'}</button>`
      : work.primaryAction==='view_images'?'<button class="primary" data-view-images>查看并确认画面</button>'
      : work.primaryAction==='open_plan'?'<button class="primary" data-view-plan>查看当前方案</button>':'';
    const reference=work.referenceUrl?`<img class="production-preview" src="${esc(work.referenceUrl)}" alt="已确认的准确参考图：${esc(work.referenceName)}">`:'';
    return `<section class="production-empty production-work"><span>${esc(work.label)}</span><p>${esc(work.detail)}</p>${reference}${action}</section>`;
  }
  function confirmationHtml(draft,data,runId){
    const inputs=referenceInputs(draft,data,runId);
    const blocked=reviewGate(draft,data)||referenceInputIssue(draft,data)||(inputs.some(ref=>!ref.url)?'本次参考图文件已变化或不可用，请重新核对制作输入。':'');
    const scope=draft.kind==='video'
      ? `1 条视频 · ${esc(draft.duration)} 秒 · ${esc(draft.resolution)} · 原生声音开启 · 跟随参考图比例 · 不自动扩写`
      : '1 个图片生成任务；可能包含多次出图调用';
    const findings=draft.review?.findings||[];
    return `<section class="production-confirm"><span class="eyebrow">生成前最后核对</span>`+
      `<h3>启动一个生成任务</h3>`+
      `<p class="production-confirm-scope">脚本第 ${esc(draft.source_version)} 版 · ${scope} · ${esc(draft.kind==='image'?`Codex Image / ${data.capabilities?.image?.controller_model||'本机默认模型'}`:draft.model)}</p>`+
      (draft.shot_prompt?`<p class="production-prompt">${esc(draft.shot_prompt)}</p>`:'')+
      designBindingHtml(draft)+referencesHtml(draft,data,runId)+
      `<p class="production-confirm-price"><strong>${price(draft.estimate)}</strong></p>`+
      `<p class="form-note">${esc(draft.estimate?.notice||'费用未知的部分保持未知；不代表免费。')} 不会自动重复提交。</p>`+
      (findings.length?`<div class="production-confirm-findings"><strong>历史检查意见（供核对）</strong>${findings.map(f=>`<p>${esc(typeof f==='string'?f:f.message||f.description||f.title||'请核对制作输入。')}</p>`).join('')}</div>`:'')+
      `<details class="production-confirm-prompt"><summary>查看实际发送的完整画面描述</summary><p class="production-prompt">${esc(generationPrompt(draft))}</p></details>`+
      (blocked?`<p class="production-design-issue" role="status">${esc(blocked)}</p>`:'<label class="production-consent"><input type="checkbox" data-consent>我已核对以上输入，同意本次生成任务及其费用</label>')+
      `<div class="production-actions">${blocked?'':'<button class="primary" data-authorize disabled>启动生成任务</button>'}<button class="ghost" data-dismiss>返回修改</button></div></section>`;
  }
  function generationConsentReady(checked,images){return Boolean(checked)&&images.every(img=>img.complete&&img.naturalWidth>0);}
  root.StudioProduction={esc,mediaUrl,price,draftReferences,generationPrompt,referenceInputs,designBindingHtml,referencesHtml,generationConsentReady,editValues,reusePayload,gate,reviewGate,canRecoverUsage,usageQuantity,usageFormHtml,reviewHtml,captionHtml,captionMatches,canDiagnoseWork,uncertain,submission,actionRequest,currentProductionWork,batchBlocker,batchGenerationDrafts,batchApprovalImages,videoSequenceState,videoBatchGenerationDrafts,assemblyClips,currentAssembly,batchConfirmationHtml,canvasHtml,draftHtml,historyDraftHtml,emptyWorkHtml,confirmationHtml,shotRows,shotCoverage,shotForDraft,shotCause,shotBadge,supersededDrafts,reuseCandidatesHtml};
  if(typeof module!=='undefined')module.exports=root.StudioProduction;
})(typeof window!=='undefined'?window:globalThis);

async function openProduction(options={}){
  if(!state.runId){toast('先打开一个项目，再开始制作。');return;}
  if(document.querySelector('.production-dialog')){
    const existing=document.querySelector('.production-dialog');
    const selected=[...existing.querySelectorAll('[data-draft-id]')].find(el=>el.dataset.draftId===options.focusDraft);
    (selected?.closest('.production-record-shot')||selected)?.scrollIntoView({block:'start'});
    return;
  }
  const runId=state.runId,base=`/api/runs/${encodeURIComponent(runId)}/production`,U=StudioProduction,opener=document.activeElement;
  const mount=options.mount,kind=options.kind||'image';
  const dialog=document.createElement(mount?'section':'dialog');dialog.className='production-dialog'+(mount?' production-embedded':'');dialog.setAttribute('aria-labelledby','production-title');
  dialog.innerHTML='<header class="production-heading"><div><span class="eyebrow">把方案变成画面</span><h2 id="production-title">制作</h2></div><button class="ghost" data-close>关闭</button></header><p class="form-note">保存方案后核对准确输入，再确认启动生成任务。关闭窗口只停止本地等待，提供方可能仍在处理。</p><p class="form-error" data-error role="alert"></p><div data-production-availability role="status"></div><div data-production-job role="status"></div><div data-confirm></div><div data-body>正在读取制作记录…</div>';
  if(mount){mount.replaceChildren(dialog);}else{document.body.append(dialog);dialog.showModal();}
  const body=dialog.querySelector('[data-body]'),error=dialog.querySelector('[data-error]'),confirm=dialog.querySelector('[data-confirm]');
  if(mount)dialog.querySelector('.form-note').remove();
  const storageKey=`studio-production:${runId}`;let data=null,timer=null,reading=false,busy=false,closed=false,pending=null,lastJob=null,renderedRecords='',values={kind:'video',source_version:'',source_sha256:'',reference_name:'',reference_sha256:'',prompt:'',model:'wan3.0-video',duration:'5',resolution:'720P'};
  const captionCache=new Map();
  let feedbackOnClose=false;
  try{const saved=JSON.parse(sessionStorage.getItem(storageKey)||'null');if(saved){values={...values,...saved.values};pending=saved.pending||null;lastJob=saved.lastJob||null;}}catch(_){}
  if(pending?.action==='review')pending=null;
  if(!pending){values.kind=kind;values.model=kind==='image'?'codex-image':'wan3.0-video';}
  function persist(){try{sessionStorage.setItem(storageKey,JSON.stringify({values,pending,lastJob}));}catch(_){error.textContent='浏览器无法暂存本次输入，请保持窗口直到请求结果确认。';}}
  function current(){return !closed&&(mount?dialog.isConnected:dialog.open)&&state.runId===runId;}
  function close(){closed=true;clearTimeout(timer);if(mount)dialog.remove();else dialog.close();}
  function handoffFeedback(bound){
    if(!bound||mount)return;
    feedbackOnClose=true;close();$('chat-input').focus();
  }
  dialog.studioClose=close;
  dialog.querySelector('[data-close]').onclick=close;
  dialog.addEventListener('cancel',()=>{closed=true;clearTimeout(timer);});
  dialog.addEventListener('close',()=>{
    closed=true;clearTimeout(timer);dialog.remove();
    if(feedbackOnClose){if(state.runId===runId)$('chat-input').focus();}
    else opener?.focus();
  });
  function notify(message){if(current())error.textContent=message;}
  function lock(){const save=body.querySelector('[data-save]');if(save)save.disabled=busy||!!pending||!!data?.active;if(busy||pending)body.querySelectorAll('input,textarea,select,button').forEach(b=>b.disabled=true);dialog.querySelectorAll('button[data-generate],button[data-image],button[data-refresh],button[data-recover],button[data-recover-usage],button[data-save],button[data-upload]').forEach(b=>{if(busy||pending)b.disabled=true;});}
  function editor(){
    const video=values.kind==='video',cap=data.capabilities?.[values.kind],models=data.capabilities?.video?.models||[];
    const references=Array.isArray(values.references)
      ? U.referencesHtml({kind:values.kind,references:values.references},data,runId)+'<p class="form-note">手动修改沿用以上准确参考图与用途。需要更换参考图时，请在对话中说明，团队会准备新版输入。</p>'
      : `<label>${video?'视频参考图':'参考图片（可选）'}<select name="reference_name" ${video?'required':''}><option value="">${video?'请选择一张图片':'不使用参考图'}</option>${(data.materials||[]).filter(m=>m.kind==='image').map(m=>`<option value="${U.esc(m.name)}">${U.esc(m.name)} · ${m.approved?'已确认':'待图审'}</option>`).join('')}</select></label>`;
    return `<form class="production-editor"><h3>${values.supersedes?'修改本镜制作输入':'新制作方案'}</h3><div class="production-tabs" role="group" aria-label="制作类型"><button type="button" class="${!video?'active':''}" data-kind="image" ${values.supersedes?'disabled':''}>图片</button><button type="button" class="${video?'active':''}" data-kind="video" ${values.supersedes?'disabled':''}>视频</button></div>`+
      (!cap?.available?`<p class="production-notice">${U.esc(cap?.reason||'该制作方式暂未就绪')}。可以保存方案，或上传已有图片用于视频制作。</p>`:'')+
      (!(data.sources||[]).some(s=>s.ready)?'<p class="production-notice">请先在创作板完成并确认当前脚本，再保存制作方案。</p>':'')+
      (data.visual_design_status?.ready===false?`<p class="production-design-issue">${U.esc((data.visual_design_status.issues||[]).join(' '))}</p>`:'')+
      `<label>关联脚本<select name="source_version" required><option value="">请选择准确版本</option>${(data.sources||[]).map(s=>`<option value="${s.version}" ${s.ready?'':'disabled'}>版本 ${s.version}${s.ready?'':' · 尚未就绪'}</option>`).join('')}</select></label><p class="form-note" data-source-note></p>`+
      (values.shot_id?`<p class="form-note">本镜身份：${U.esc(values.shot_id)} · 保存为替换本镜的新输入</p>`:'')+references+
      '<button type="button" class="ghost small" data-upload>上传现有图片</button><label>画面与动作描述<textarea name="prompt" rows="7" maxlength="5000" required placeholder="写清楚画面、主体、动作与镜头。保存后需核对并确认才会生成。"></textarea></label>'+
      (video?`<div class="production-grid"><label>模型<select name="model">${models.map(m=>`<option value="${U.esc(m.id)}">${U.esc(m.label)}</option>`).join('')}</select></label><label>画质<select name="resolution">${(models.find(m=>m.id===values.model)?.resolutions||['720P']).map(r=>`<option value="${U.esc(r)}">${U.esc(r)}</option>`).join('')}</select></label><label>时长（秒）<input name="duration" type="number" min="${models.find(m=>m.id===values.model)?.durations?.min||2}" max="${models.find(m=>m.id===values.model)?.durations?.max||30}" step="1" required></label></div>`:'')+
      '<button class="primary" data-save type="submit">保存新方案</button><p class="form-note">保存不调用模型。旧方案及生成记录会保留。</p></form>';
  }
  function captionMarkup(){
    if(kind!=='video')return '';
    const work=U.currentProductionWork(data,'video',runId);
    const writing=(state.rail||[]).find(step=>step.stage==='writing'&&step.has_doc);
    if(!work.outputUrl||U.captionMatches(writing,work))return U.captionHtml(state.rail,work);
    const version=Number(work.displaySourceVersion),sha=work.displaySourceSha256;
    if(!Number.isSafeInteger(version)||version<1||!(/^[a-f0-9]{64}$/).test(sha||''))
      return U.captionHtml(state.rail,work,{status:'missing'});
    const key=`${version}:${sha}`;
    if(!captionCache.has(key)){
      captionCache.set(key,{status:'loading'});
      api(`/api/runs/${encodeURIComponent(runId)}/artifacts/writing/${version}`).then(artifact=>{
        captionCache.set(key,U.captionMatches(artifact,work)?{status:'ready',artifact}:{status:'missing'});
        if(current())updateCaption();
      }).catch(error=>{
        captionCache.set(key,{status:error?.status===404?'missing':'error'});
        if(current())updateCaption();
      });
    }
    return U.captionHtml(state.rail,work,captionCache.get(key));
  }
  function updateCaption(){
    const host=body.querySelector('[data-caption]');
    if(host){const html=captionMarkup();if(host.innerHTML!==html)host.innerHTML=html;}
  }
  function capture(form){for(const [key,value] of new FormData(form))values[key]=value;}
  function render(){
    if(!current())return;
    const advancedWasOpen=body.querySelector('.production-advanced')?.open;
    renderedRecords=records();
    const readySource=(data.sources||[]).some(s=>s.ready);
    const readyReference=(data.materials||[]).some(m=>m.kind==='image'&&m.approved);
    const canPrepare=readySource&&(kind==='image'||readyReference);
    body.innerHTML=mount
      ? `<div class="production-workspace"><section><div data-records>${renderedRecords}</div><div data-caption>${captionMarkup()}</div></section><details class="production-advanced"><summary>高级：手动编辑制作输入</summary>${editor()}</details></div>`
      : kind==='image'?`<div class="production-workspace"><div data-records>${renderedRecords}</div><details class="production-advanced"><summary>手动调整制作输入</summary>${editor()}</details></div>`
      : `<div class="production-layout"><aside>${editor()}</aside><section><div class="production-record-heading"><h3>当前制作</h3></div><div data-records>${renderedRecords}</div></section></div>`;
    if(advancedWasOpen)body.querySelector('.production-advanced').open=true;
    const prepare=body.querySelector('[data-prepare]');if(prepare){prepare.disabled=!!data.active||!canPrepare;prepare.onclick=()=>{
      const reference=data.latest_approved_image;
      if(kind==='video'&&state.creatorView?.format==='video'&&reference){
        pending={action:'continue-approval',path:'/material-approvals/continue',
          body:{name:reference.name,sha256:reference.sha256,request_id:crypto.randomUUID()}};
        persist();sendPending();return;
      }
      submitJob(runId,{request_id:crypto.randomUUID(),text:kind==='image'?'请根据我已确认的完整方案，准备分镜参考图的图片制作输入并自检，让我核对后生成。':'请基于我已确认的分镜图准备视频输入并自检，展示画面描述、时长与费用，等我确认后生成。'});
    };}
    body.querySelector('[data-view-images]')?.addEventListener('click',()=>showJourneyTab('storyboard'));
    body.querySelector('[data-view-plan]')?.addEventListener('click',()=>showJourneyTab('plan'));
    const form=body.querySelector('form');for(const [key,value] of Object.entries(values)){const el=form.elements.namedItem(key);if(el)el.value=value;}
    function showSource(){const s=data.sources.find(s=>s.version===Number(values.source_version));form.querySelector('[data-source-note]').textContent=s?.summary||'不会自动选用最新稿件，请核对制作依据。';}
    showSource();
    form.oninput=e=>{capture(form);if(e.target.name==='source_version')values.source_sha256=data.sources.find(s=>s.version===Number(values.source_version))?.artifact_sha256||'';if(e.target.name==='reference_name')values.reference_sha256=data.materials.find(m=>m.name===values.reference_name)?.sha256||'';persist();showSource();};
    form.onsubmit=e=>{e.preventDefault();if(busy||pending||data.active)return;try{capture(form);const payload=U.submission(values,data,crypto.randomUUID());pending={action:'drafts',path:'/drafts',body:payload};persist();sendPending();}catch(err){notify(err.message);}};
    lock();if(pending)pendingView();
  }
  function records(){
    return U.reuseCandidatesHtml(data,mount?kind:values.kind)+workRecords();
  }
  function workRecords(){
    const currentKind=mount?kind:values.kind;
    if(options.focusDraft){
      const drafts=(data.drafts||[]).filter(d=>d.kind===currentKind&&(d.source_ready!==false||d.design_issue));
      const index=drafts.findIndex(d=>d.draft_id===options.focusDraft);
      if(index>=0)return `<section class="production-record-shot"><h3>${currentKind==='video'?'视频段':'首帧'} ${String(index+1).padStart(2,'0')}</h3>${U.draftHtml(drafts[index],{...data,drafts:[drafts[index]]},runId)}</section>`;
    }
    if(currentKind==='video'&&(data.video_sequence||[]).length>1){
      const shots=U.videoSequenceState(data,runId);
      return shots.map(shot=>`<section class="production-record-shot"><h3>视频段 ${String(shot.index).padStart(2,'0')}</h3>`+
        (shot.draft?U.draftHtml(shot.draft,{...data,drafts:[shot.draft]},runId):
          `<p>${shot.approved?'视频输入尚未准备。':'先确认这张首帧。'}</p>`)+`</section>`).join('');
    }
    const work=U.currentProductionWork(data,currentKind,runId);
    const main=work.draft?U.draftHtml(work.draft,data,runId):mount?U.emptyWorkHtml(work)
      :'<div class="production-empty"><span>从一个画面开始</span><p>可以继续在右侧讨论，团队会根据确认的方案准备制作输入。</p></div>';
    return main;
  }
  function pendingView(){confirm.innerHTML='<section class="production-confirm"><h3>上次请求结果待确认</h3><p>请重试同一请求。输入与请求身份已保留，不会另开一次生成。</p><button class="primary" data-retry>核实上次请求</button></section>';confirm.querySelector('[data-retry]').onclick=()=>{if(!busy)sendPending();};}
  async function sendPending(){
    if(busy||!pending||!current())return;busy=true;lock();const request=pending;notify('');
    try{
      const result=await api(base+request.path,{method:'POST',body:JSON.stringify(request.body)});
      const continuation=result.continuation;
      const jobId=result.job_id||continuation?.job_id;
      if(jobId){lastJob={job_id:jobId,status:result.status||continuation?.job_status||'running',reason:result.reason||'',action:continuation?'prepare':request.action};restoreJob(runId);}
      pending=null;persist();if(!current())return;confirm.innerHTML='';await load(true);
      if(continuation?.status==='started'&&typeof refreshTimeline==='function'){
        try{await refreshTimeline();}catch(_){/* The accepted job remains visible in its own watcher. */}
      }
      if(continuation?.status==='not_started'){
        notify(`图片已确认；${continuation.reason} 可在这里继续准备视频输入。`);
        toast('图片已确认，视频准备尚未开始。',true);
      }else if(request.action==='material-approvals'){
        toast(result.approved?(continuation?.status==='started'?'图片已确认，团队正在准备视频输入。':'图片已确认；生成视频仍需单独核对和授权。'):'这张图片的确认已撤回。');
      }else if(request.action==='continue-approval'){
        toast(continuation?.status==='started'?'团队正在准备视频输入。':
          continuation?.draft_id?'已读取准确视频输入。':'此前的准备任务已保存；若没有准确视频输入，请在对话中说明问题。');
      }else toast(request.action==='drafts'?'制作方案已保存。':'请求已接收，请在生成记录查看进度。');
    }
    catch(err){if(!U.uncertain(err)){pending=null;persist();if(current()){confirm.innerHTML='';await load(true);}}else if(current())pendingView();notify(err.message||'请求结果暂未确认，请核实上次请求。');}
    finally{busy=false;if(current()){if(pending)pendingView();else render();}}
  }
  function generateAction(draft){
    if(busy||pending)return;const blocked=U.gate(draft,data);if(blocked){notify(blocked);return;}
    const connectionRevision=draft.kind==='image'?data.capabilities?.image?.connection_revision:undefined;
    confirm.innerHTML=U.confirmationHtml(draft,data,runId);
    const button=confirm.querySelector('[data-authorize]'),consent=confirm.querySelector('[data-consent]');
    const images=[...confirm.querySelectorAll('.production-reference-input img')];
    const ready=()=>U.generationConsentReady(consent?.checked,images);
    const update=()=>{if(button)button.disabled=!ready();};
    images.forEach(img=>{img.onload=update;img.onerror=()=>{update();notify('参考图未能完整加载，暂不能提交生成。');};});
    if(consent)consent.onchange=update;
    if(button)button.onclick=()=>{if(busy||pending||!current()||!ready())return;const reason=U.gate(draft,data);if(reason){notify(reason);return;}pending=U.actionRequest('generate',draft,data.nonce,crypto.randomUUID(),null,connectionRevision);persist();sendPending();};
    update();
    confirm.querySelector('[data-dismiss]').onclick=()=>{confirm.innerHTML='';};confirm.scrollIntoView({block:'start'});
  }
  function imageView(name){
    const material=data.materials.find(m=>m.name===name&&m.kind==='image'),url=U.mediaUrl(material?.url,runId);if(!url){notify('此图片暂不可用，请重新读取素材。');return;}
    const videoGoal=state.creatorView?.format==='video';
    confirm.innerHTML=`<section class="production-confirm"><h3>参考图审核</h3><img class="production-preview" src="${U.esc(url)}" alt="${U.esc(name)}"><p>${U.esc(name)}</p><p class="form-note">请检查商品外观、画面和文字。${videoGoal?'确认后团队会继续准备视频输入；视频生成仍需由你单独核对并授权。':'本次确认只针对这张图片，不会自动生成视频。'}</p><button class="primary" data-approve disabled>${material.approved?'撤回本图确认':videoGoal?'我已查看，确认此图可用于视频':'我已查看，确认这张图片'}</button><button class="ghost" data-dismiss>返回</button></section>`;
    const img=confirm.querySelector('img'),button=confirm.querySelector('[data-approve]');img.onload=()=>button.disabled=false;img.onerror=()=>notify('图片未能完整加载，暂不能确认。');if(img.complete&&img.naturalWidth)button.disabled=false;
    button.onclick=()=>{if(busy||pending||!current())return;if(data.active){notify('制作团队正在执行，请本轮结束后再确认参考图。');return;}pending={action:'material-approvals',path:'/material-approvals',body:{name:material.name,sha256:material.sha256,approved:!material.approved,nonce:data.nonce}};persist();sendPending();};
    confirm.querySelector('[data-dismiss]').onclick=()=>confirm.innerHTML='';confirm.scrollIntoView({block:'start'});
  }
  function recoverUsage(id){
    const draft=data.drafts.find(d=>(d.attempts||[]).some(a=>a.attempt_id===id)),attempt=draft?.attempts.find(a=>a.attempt_id===id);
    if(!attempt||!U.canRecoverUsage(attempt,data)){notify('本轮任务仍在执行，请等待结果后再核账。');return;}
    confirm.innerHTML=U.usageFormHtml(draft.kind);
    const form=confirm.querySelector('form');form.onsubmit=e=>{e.preventDefault();if(busy||pending||!current())return;const currentAttempt=data.drafts.flatMap(d=>d.attempts||[]).find(a=>a.attempt_id===id);if(!currentAttempt||!U.canRecoverUsage(currentAttempt,data)){notify('任务状态已变化，请等待结果后再核账。');return;}try{const f=new FormData(form),quantity=U.usageQuantity(f.get('quantity'),draft.kind);pending={action:'recover',path:`/attempts/${encodeURIComponent(id)}/recover`,body:{quantity,evidence_note:String(f.get('evidence_note')).trim(),nonce:data.nonce}};persist();sendPending();}catch(err){notify(err.message);}};form.querySelector('[data-dismiss]').onclick=()=>confirm.innerHTML='';confirm.scrollIntoView({block:'start'});
  }
  function recover(id){
    const imageAttempt=data.drafts.some(d=>d.kind==='image'&&(d.attempts||[]).some(a=>a.attempt_id===id));
    const retryAvailable=(data.recovery_modes||[]).includes('retry_with_unknown_cost');
    const choices=imageAttempt
      ?'<fieldset class="production-recovery-choices"><legend>这次图片任务如何处理</legend><label><input type="radio" name="recovery_choice" value="not_submitted" required>有提供方证据证明未提交、未产生费用</label>'+
        (retryAvailable?'<label><input type="radio" name="recovery_choice" value="retry_with_unknown_cost" required>接受旧请求的结果与费用仍未知，允许之后重新核对并生成</label>':'')+
        '</fieldset><p class="form-note">'+(retryAvailable?'第二项只记录你的决定，不会立刻再次生成；旧请求的未知用量会保留。':'当前服务仍是旧版。接受未知费用后重试的入口须在更新服务后使用。')+'</p>'
      :'<label>提供方任务编号<input name="task_id" maxlength="160" placeholder="从提供方任务记录复制"></label><label class="production-consent"><input type="checkbox" name="not_submitted">提供方证据确认：未提交任务且未产生费用</label><p class="form-note">未知不等于未提交；如已有任务编号，应查询原任务。</p>';
    confirm.innerHTML=`<form class="production-confirm production-editor"><h3>核实原任务</h3><p class="form-note">本次没有取得可核对的图片结果。先处理这条记录，后续首帧才会重新开放。</p>${choices}<label>核对依据或继续理由<textarea name="evidence_note" rows="3" maxlength="2000" required placeholder="写下你核对了什么，或确认接受可能重复计费"></textarea></label><div class="production-actions"><button type="submit" class="primary">保存这次决定</button><button type="button" class="ghost" data-dismiss>返回</button></div></form>`;
    const form=confirm.querySelector('form');
    form.onsubmit=e=>{
      e.preventDefault();if(busy||pending||!current())return;
      const f=new FormData(form),task=String(f.get('task_id')||'').trim();
      const choice=imageAttempt?String(f.get('recovery_choice')||''):(f.has('not_submitted')?'not_submitted':'task_id');
      if((!imageAttempt&&Boolean(task)===f.has('not_submitted'))||
          (imageAttempt&&!['not_submitted',...(retryAvailable?['retry_with_unknown_cost']:[])].includes(choice))){
        notify('请选择一种明确的处理方式。');return;
      }
      pending={action:'recover',path:`/attempts/${encodeURIComponent(id)}/recover`,body:{
        ...(choice==='not_submitted'?{not_submitted:true}
          :choice==='retry_with_unknown_cost'?{retry_with_unknown_cost:true}:{task_id:task}),
        evidence_note:String(f.get('evidence_note')).trim(),nonce:data.nonce}};
      persist();sendPending();
    };
    form.querySelector('[data-dismiss]').onclick=()=>confirm.innerHTML='';
    confirm.scrollIntoView({block:'start'});
  }
  body.addEventListener('click',e=>{
    const button=e.target.closest('[data-feedback-attempt]');
    if(!button||!current())return;
    const draft=data.drafts.find(d=>(d.attempts||[]).some(a=>a.attempt_id===button.dataset.feedbackAttempt));
    const attempt=draft?.attempts.find(a=>a.attempt_id===button.dataset.feedbackAttempt);
    const time=draft?.kind==='video'?button.closest('.creator-result')?.querySelector('video')?.currentTime:null;
    handoffFeedback(setMediaFeedbackTarget(runId,attempt,draft?.kind,time));
  });
  body.addEventListener('click',e=>{
    const b=e.target.closest('button');if(!b||busy||pending||!current())return;
    if(b.hasAttribute('data-copy-caption')){
      const value=b.parentElement.querySelector('[data-caption-text]')?.textContent||'';
      if(!navigator.clipboard?.writeText){notify('当前环境不能自动复制，请手动选择文案。');return;}
      navigator.clipboard.writeText(value).then(()=>notify('发布文案已复制。')).catch(()=>notify('复制失败，请手动选择文案。'));
    }else if(b.hasAttribute('data-reload-caption')){
      const work=U.currentProductionWork(data,'video',runId);
      captionCache.delete(`${work.displaySourceVersion}:${work.displaySourceSha256}`);
      updateCaption();
    }else if(b.dataset.kind){
      capture(body.querySelector('form'));values.kind=b.dataset.kind;
      values.model=values.kind==='video'?'wan3.0-video':'codex-image';persist();render();
    }else if(b.hasAttribute('data-upload')){close();toggleMaterials(true);
    }else if(b.hasAttribute('data-model-settings')){$('connections-open')?.click();
    }else if(b.dataset.reuseAttempt){
      if(busy||pending||data.active)return;
      const candidate=(data.reuse_candidates||[]).find(row=>row.source_attempt_id===b.dataset.reuseAttempt&&row.eligible);
      let payload;
      try{payload=U.reusePayload(candidate,data,crypto.randomUUID());}
      catch(err){notify(err.message);return;}
      pending={action:'drafts',path:'/drafts',body:payload};persist();sendPending();
    }else if(b.dataset.productionDiagnose){
      const d=data.drafts.find(d=>d.draft_id===b.dataset.productionDiagnose);
      const work=d&&U.currentProductionWork({...data,drafts:[d]},d.kind,runId);
      if(d&&U.canDiagnoseWork(work)){
        const diagnosisAttempt=work.phase==='failed'?work.attempt:null;
        if(requestProductionDiagnosis(runId,d,diagnosisAttempt,Boolean(data.active))&&!mount)close();
      }else notify('这次问题记录已变化，请重新读取当前作品。');
    }else if(b.dataset.productionFeedback){
      const d=data.drafts.find(d=>d.draft_id===b.dataset.productionFeedback);
      if(d)handoffFeedback(setProductionFeedbackTarget(runId,d,(d.attempts||[]).at(-1)||null));
    }else if(b.dataset.editInput){
      const d=data.drafts.find(d=>d.draft_id===b.dataset.editInput);
      if(d){
        values=U.editValues(d);
        persist();render();
        const advanced=body.querySelector('.production-advanced');if(advanced)advanced.open=true;
        body.querySelector('textarea')?.focus();
      }
    }else if(b.dataset.generate){
      const d=data.drafts.find(d=>d.draft_id===b.dataset.generate);
      if(d)generateAction(d);
    }else if(b.dataset.image)imageView(b.dataset.image);
    else if(b.dataset.refresh){
      pending={action:'refresh',path:`/attempts/${encodeURIComponent(b.dataset.refresh)}/refresh`,body:{request_id:crypto.randomUUID()}};
      persist();sendPending();
    }else if(b.dataset.recoverUsage)recoverUsage(b.dataset.recoverUsage);
    else if(b.dataset.recover)recover(b.dataset.recover);
  });
  async function showJob(){
    if(!lastJob||!current())return;
    if(!['succeeded','failed','blocked','cancelled','interrupted'].includes(lastJob.status)){try{const job=await api(`/api/runs/${encodeURIComponent(runId)}/jobs/${encodeURIComponent(lastJob.job_id)}`);if(!current())return;lastJob={...lastJob,status:job.status,reason:job.reason||''};persist();}catch(err){notify('暂不能读取本次执行状态：'+err.message);}}
    if(!current())return;const labels={queued:'等待处理',running:'正在执行',succeeded:'执行已结束',failed:'本次执行未完成',blocked:'本次执行需要处理',cancelled:'本地执行已停止',interrupted:'执行被中断',cancelling:'正在停止本地等待'};
    dialog.querySelector('[data-production-job]').innerHTML=`<p class="production-notice">${lastJob.action==='review'?'制作检查':'本次制作'} · ${U.esc(labels[lastJob.status]||'状态待核实')}${lastJob.reason?`<br>${U.esc(lastJob.reason)}`:''}</p>`;
  }
  async function load(full=false){
    if(!current()||reading)return;if(state.runId!==runId){close();return;}reading=true;
    try{const result=await api(base);if(!current()){close();return;}data=result;if(mount&&!values.source_version){const source=data.sources.find(s=>s.ready);if(source){values.source_version=String(source.version);values.source_sha256=source.artifact_sha256;}}dialog.querySelector('[data-production-availability]').textContent=data.active?'制作团队正在执行。当前输入会保留，本轮结束后可以继续。':'';await showJob();if(!current())return;if(full||!body.querySelector('form'))render();else{const nextRecords=records();if(nextRecords!==renderedRecords)render();else{updateCaption();lock();}}}
    catch(err){notify(err.status===404?'制作功能等待服务更新，重启工作台后再试。':err.message);if(!data)body.innerHTML='<button class="ghost" data-load>重新读取</button>';body.querySelector('[data-load]')?.addEventListener('click',()=>load(true));}
    finally{reading=false;clearTimeout(timer);if(current())timer=setTimeout(()=>{if(state.runId!==runId){close();return;}load();},2000);}
  }
  await load(true);
  if(options.focusDraft){
    const selected=[...body.querySelectorAll('[data-draft-id]')].find(el=>el.dataset.draftId===options.focusDraft);
    (selected?.closest('.production-record-shot')||selected)?.scrollIntoView({block:'start'});
  }
}
