/* Independent learning room. The canonical session and proposal remain server owned. */
"use strict";
(function(root){
  const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const list=value=>Array.isArray(value)?value:[];
  const text=value=>Array.isArray(value)?value.join('、'):typeof value==='object'&&value!==null?JSON.stringify(value):String(value??'');
  const safeLink=value=>{try{const url=new URL(String(value));return ['http:','https:'].includes(url.protocol)&&!url.username&&!url.password?url.href:null;}catch{return null;}};
  const isLearningRun=run=>run?.kind==='learning';
  const titleFromIntent=brief=>String(brief||'').split(/\r?\n/).map(line=>line.trim()).find(Boolean)?.slice(0,48)||'参考研究';
  const statusText={collecting:'整理材料',discussing:'讨论中',pending_review:'待确认',saved:'已保存，待迁移验证',no_change:'已记录：暂无知识变更',closed:'已结束'};
  const proposalState={pending:'待确认',applied:'已保存，待迁移验证',rejected:'已拒绝',stale:'已过期'};
  const noChange=proposal=>proposal?.type==='delta'&&(proposal.no_change===true||proposal.payload?.outcome==='no_change'||proposal.payload?.no_change===true);
  const onlyNoChangeConclusion=session=>session?.status==='saved'&&list(session.proposals).some(p=>p.status==='applied')&&
    list(session.proposals).filter(p=>p.status==='applied').every(noChange);
  const observer={model:'模型观察',human:'人工观察',user:'用户原话',transcript:'字幕转写'};
  const rows=(entries)=>entries.filter(([,value])=>value!==undefined&&value!==null&&text(value).trim()).map(([label,value])=>`<div><dt>${esc(label)}</dt><dd>${esc(text(value))}</dd></div>`).join('');
  const fileMedia=(runId,name)=>`/api/runs/${encodeURIComponent(runId)}/materials/${encodeURIComponent(name)}`;

  function sourceCard(source,runId){
    const name=source.name||source.material_name;
    const kind=/\.(mp4|mov|m4v|webm)$/i.test(name||'')?'video':/\.(png|jpe?g|webp|gif)$/i.test(name||'')?'image':source.kind;
    const url=name?fileMedia(runId,name):null;
    const preview=url&&kind==='video'?`<video controls preload="metadata" src="${esc(url)}" aria-label="${esc(name)}"></video>`:
      url&&kind==='image'?`<img loading="lazy" src="${esc(url)}" alt="${esc(name)}">`:'';
    const external=safeLink(source.source_url||source.url);
    const title=source.title||name||source.source_url||'参考来源';
    return `<article class="learning-room-card learning-room-source">${preview}<div><h3>${esc(title)}</h3>`+
      `<p>${esc(source.status==='completed'?'已有分析记录':source.status==='stale'?'来源已变化，请重新核对':'来源已保存；观察覆盖以记录为准')}</p>`+
      (external?`<a href="${esc(external)}" target="_blank" rel="noopener noreferrer">打开来源 ↗</a>`:'')+
      (source.report?`<details><summary>阅读声画观察报告</summary><p class="learning-room-pre">${esc(source.report)}</p></details>`:'')+
      (source.coverage?`<p class="learning-room-muted">覆盖：${esc(text(source.coverage))}</p>`:'')+`</div></article>`;
  }
  function observationCard(observation){
    const content=observation.content||observation.observation||observation.finding||'';
    const range=observation.time_range;
    const time=Array.isArray(range)?range.join('–'):range?.start!==undefined?`${range.start}–${range.end}`:range;
    return `<article class="learning-room-card"><p class="learning-room-observation">${esc(content||'观察内容暂不可读取')}</p><dl class="learning-room-fields">${rows([
      ['观察者',observer[observation.observer_type]||observation.observer_type],['观察方式',observation.observation_kind],
      ['时段',time],['实际覆盖',observation.coverage],['来源文件',observation.source_name],
      ['原片 SHA-256',observation.media_sha256],['判断层级',observation.level],
      ['来源记录',observation.source_ref],['审核状态',observation.review_state]
    ])}</dl></article>`;
  }
  function summaryFields(proposal){
    const summary=proposal.summary||proposal.payload?.summary;
    if(typeof summary==='string')return `<p class="learning-room-observation">${esc(summary)}</p>`;
    const content=summary&&typeof summary==='object'?summary:{};
    const fallback=proposal.payload||{};
    const named=[['本次改变',content.what||content.change||fallback.name||fallback.decision],
      ['为什么改',content.why||fallback.rationale],['适用',content.use_when||content.applies_to||fallback.use_when],
      ['不适用',content.avoid_when||content.exclusions||fallback.avoid_when],
      ['待验证',content.unknown||content.to_validate||fallback.limitations]];
    return `<dl class="learning-room-fields">${rows(named)||'<div><dt>变更内容</dt><dd>请展开准确差异核对。</dd></div>'}</dl>`;
  }
  function proposalCard(proposal){
    const unchanged=noChange(proposal);
    const status=unchanged&&proposal.status==='applied'?'已确认，无知识变更':proposalState[proposal.status]||proposal.status||'状态待确认';
    const hash=proposal.proposal_sha256||proposal.sha256;
    const canApply=proposal.status==='pending'&&Boolean(proposal.base_library_sha256&&hash);
    const diff=typeof proposal.diff==='string'?proposal.diff:JSON.stringify(proposal.diff??{},null,2);
    return `<article class="learning-room-card learning-room-proposal" data-learning-proposal="${esc(proposal.id)}">`+
      `<header><span class="learning-room-pill">${esc(status)}</span><h3>${esc(proposal.title||proposal.name||'本次学习变更')}</h3></header>${summaryFields(proposal)}`+
      `<details><summary>核对准确差异与版本</summary><pre>${esc(diff)}</pre><p class="learning-room-muted">提案 ${esc(proposal.id)} · 库 ${esc(proposal.base_library_sha256||'未提供')} · 内容 ${esc(hash||'未提供')}</p></details>`+
      (canApply?`<p class="learning-room-muted">${unchanged?'确认后只记录本次无知识变更的结论，共享库内容与版本不变。':'确认后保存到共享库，仍需在新任务检验。不会启动制作。'}</p><div class="learning-room-proposal-actions"><button type="button" class="primary" data-learning-apply>${unchanged?'确认本次无知识变更':'确认并保存变更'}</button><button type="button" class="ghost" data-learning-reject>拒绝本次变更</button></div>`:
        proposal.status==='pending'?'<p class="learning-room-muted">缺少准确提案版本，暂不能确认；请刷新后重审。</p>':'')+
      `<p class="learning-room-action-status" role="status"></p></article>`;
  }
  function roomHtml(session,run,materials=[],referenceTasks=[],library=null){
    const sources=[...list(session?.sources)];
    const seen=new Set(sources.map(s=>s.name||s.material_name).filter(Boolean));
    for(const material of list(materials))if(!seen.has(material.name))sources.push(material);
    const tasks=list(referenceTasks);
    for(const task of tasks){
      const matching=sources.find(s=>s.reference_id&&s.reference_id===task.reference_id)
        ||sources.find(s=>s.kind==='reference_analysis'&&task.name&&s.name===task.name);
      if(matching){matching.report=task.report;matching.title=task.title||matching.title;}
      else sources.push(task);
    }
    const observations=list(session?.observations);
    const discussion=list(session?.discussion);
    const proposals=list(session?.proposals);
    const mayClose=session?.status!=='closed'&&!proposals.some(p=>p.status==='pending');
    const latestReply=[...discussion].reverse().find(row=>row.role==='assistant'&&typeof row.text==='string'&&row.text.trim());
    const schema=library?.library?.schema_version??library?.schema_version;
    return `<div class="learning-room"><header class="learning-room-intro"><span class="creator-kicker">独立学习 · ${esc(onlyNoChangeConclusion(session)?'已确认，无知识变更':statusText[session?.status]||session?.status||'整理材料')}</span>`+
      `<h2>${esc(run?.title||'学习参考')}</h2><p>围绕原片、实际观察和你的纠正讨论。只有确认后的变更才进入共享库；保存后仍需在新任务验证。</p>`+
      (run?.brief?`<details><summary>本次研究意图</summary><p class="learning-room-pre">${esc(run.brief)}</p></details>`:'')+
      (mayClose?'<button type="button" class="ghost small" data-learning-close>结束本次学习</button>':'')+`</header>`+
      (session?.load_error?`<p class="learning-room-empty" role="alert">学习记录暂时无法读取：${esc(session.load_error)}</p>`:'')+
      `<section class="learning-room-section"><div class="learning-room-section-head"><div><span>01 / 来源</span><h2>原片与观察范围</h2></div><div><button type="button" class="ghost small" data-learning-materials>添加素材</button> <button type="button" class="ghost small" data-learning-analyze>分析参考视频</button></div></div>`+
      `<div class="learning-room-list">${sources.length?sources.map(s=>sourceCard(s,run.run_id)).join(''):'<p class="learning-room-empty">还没有参考视频。可以先上传原片；上传不会自动调用模型。</p>'}</div>`+
      `<h3>已保存的观察</h3><div class="learning-room-list">${observations.length?observations.map(observationCard).join(''):'<p class="learning-room-empty">暂无可引用的观察。文件已上传不代表已经完整看听。</p>'}</div></section>`+
      `<section class="learning-room-section"><div class="learning-room-section-head"><div><span>02 / 讨论</span><h2>研究对话</h2></div><button type="button" class="ghost small" data-learning-chat>继续讨论</button></div>`+
      (latestReply?`<p class="learning-room-observation">${esc(latestReply.text)}</p>`:'<p class="learning-room-empty">指出你关心的表达、同意或不同意的解释；只分析也可以结束。</p>')+`</section>`+
      `<section class="learning-room-section"><div class="learning-room-section-head"><div><span>03 / 变更</span><h2>本次学习变更</h2></div></div>`+
      (schema===1?'<div class="learning-room-card learning-room-migration"><h3>共享知识库需要升级到 v2</h3><p>当前旧库可读；新增方法的提案需要先完成一次受审迁移。先看迁移差异与无法确定项，再生成待确认提案。</p><button type="button" class="ghost small" data-learning-migration>查看升级预览</button></div>':'')+
      `<div class="learning-room-list">${proposals.length?proposals.map(proposalCard).join(''):'<p class="learning-room-empty">尚无待确认的知识变更。讨论后没有新方法也是有效结论。</p>'}</div></section></div>`;
  }

  const applyIds=new Map();
  function mount({host,session,run,materials,referenceTasks,library,request,reload,notify,openMaterials,openChat,openAnalysis}){
    host.innerHTML=roomHtml(session,run,materials,referenceTasks,library);
    host.querySelector('[data-learning-materials]')?.addEventListener('click',openMaterials);
    host.querySelector('[data-learning-chat]')?.addEventListener('click',openChat);
    host.querySelector('[data-learning-analyze]')?.addEventListener('click',openAnalysis);
    host.querySelector('[data-learning-migration]')?.addEventListener('click',()=>openMigrationPreview(run.run_id,request,reload,notify));
    host.querySelector('[data-learning-close]')?.addEventListener('click',async event=>{
      const button=event.currentTarget;button.disabled=true;
      try{await request(`/api/runs/${encodeURIComponent(run.run_id)}/learning/close`,{method:'POST'});await reload();notify('本次学习已结束，记录仍可阅读。');}
      catch(error){notify(error.message,true);button.disabled=false;}
    });
    for(const card of host.querySelectorAll('[data-learning-proposal]')){
      const proposal=list(session?.proposals).find(p=>p.id===card.dataset.learningProposal);
      const button=card.querySelector('[data-learning-apply]');
      const reject=card.querySelector('[data-learning-reject]');if(!proposal||!button||!reject)continue;
      const shownHash=proposal.proposal_sha256||proposal.sha256, base=proposal.base_library_sha256;
      async function decide(action){
        if(!isLearningRun(run)||!shownHash||!base||proposal.status!=='pending')return;
        const status=card.querySelector('.learning-room-action-status');
        const key=`${run.run_id}:${proposal.id}:${shownHash}:${action}`;
        if(!applyIds.has(key))applyIds.set(key,crypto.randomUUID());
        button.disabled=true;reject.disabled=true;status.textContent=action==='apply'?(noChange(proposal)?'正在记录本次结论…':'正在保存你核对的版本…'):'正在拒绝本次变更…';
        try{
          const body={expected_proposal_sha256:shownHash,request_id:applyIds.get(key)};
          if(action==='apply')body.expected_library_sha256=base;
          await request(`/api/runs/${encodeURIComponent(run.run_id)}/learning/proposals/${encodeURIComponent(proposal.id)}/${action}`,{
            method:'POST',body:JSON.stringify(body)});
          status.textContent=action==='apply'?(noChange(proposal)?'已确认无知识变更；共享库内容与版本不变。':'已保存，待迁移验证。'):'已拒绝，未更新共享库。';await reload();
        }catch(error){
          status.textContent=error.status===409?'提案或共享库已变化。请刷新后重新审阅。':`暂时无法确认保存结果：${error.message}`;
          if(error.status===409){notify(status.textContent,true);await reload().catch(()=>{});}else{button.disabled=false;reject.disabled=false;}
        }
      }
      button.onclick=()=>decide('apply');reject.onclick=()=>decide('reject');
    }
  }

  async function openMigrationPreview(runId,request,reload,notify){
    if(document.querySelector('.learning-migration-dialog'))return;
    const preview=await request('/api/learning/migration-preview');
    const dialog=document.createElement('dialog');dialog.className='learning-run-dialog learning-migration-dialog';
    dialog.innerHTML=`<div><span class="creator-kicker">只读迁移预览</span><h2>升级共享学习库</h2>`+
      `<p>原库版本 ${esc(preview.base_library_version)} → 候选版本 ${esc(preview.candidate_library_version)}。来源市场无法判定的案例 ${esc(list(preview.unknown_source_markets).length)} 条；方法字段待补齐 ${esc(list(preview.methods_pending_review).length)} 条。</p>`+
      `<details><summary>核对完整差异</summary><pre class="learning-room-migration-diff">${esc(preview.diff||'')}</pre></details>`+
      `<p class="learning-room-muted">生成提案只保存候选；必须再次确认准确差异才会写入共享库。</p>`+
      `<p role="alert" data-learning-migration-error></p><div class="learning-run-actions"><button type="button" class="ghost" data-cancel>关闭</button><button type="button" class="primary" data-propose>生成升级提案</button></div></div>`;
    document.body.append(dialog);dialog.querySelector('[data-cancel]').onclick=()=>dialog.close();
    dialog.addEventListener('close',()=>dialog.remove());
    dialog.querySelector('[data-propose]').onclick=async()=>{
      const button=dialog.querySelector('[data-propose]');button.disabled=true;
      try{
        await request(`/api/runs/${encodeURIComponent(runId)}/learning/proposals`,{method:'POST',body:JSON.stringify({type:'migration',payload:{}})});
        dialog.close();notify('升级提案已准备好，请核对准确差异后再确认。');await reload();
      }catch(error){dialog.querySelector('[data-learning-migration-error]').textContent=error.message;button.disabled=false;}
    };
    dialog.showModal();
  }

  async function openCreateDialog(){
    if(document.querySelector('.learning-run-dialog')||state.decisionBusy)return;
    const dialog=document.createElement('dialog');dialog.className='learning-run-dialog';
    dialog.innerHTML=`<form><span class="creator-kicker">独立学习</span><h2>研究一条参考</h2><p>先建学习记录，原片和讨论都保留在这里；无需选择商品或创作阶段。</p>`+
      `<label>你想研究什么<textarea name="brief" required rows="4" maxlength="8000" placeholder="例如：先不复刻，研究这条为什么信息多但不生硬。"></textarea></label>`+
      `<label>原片（可稍后补充）<input name="file" type="file" accept="video/*"></label>`+
      `<p class="learning-room-muted">创建和上传不调用模型。声画分析可能收费，将在学习记录中单独确认；普通讨论按当前模型连接计费。</p>`+
      `<p role="alert" data-learning-create-error></p><div class="learning-run-actions"><button type="button" class="ghost" data-cancel>取消</button><button type="submit" class="primary">创建学习记录</button></div></form>`;
    document.body.append(dialog);
    let frozen=null,created=null,busy=false;
    dialog.querySelector('[data-cancel]').onclick=()=>{if(!busy)dialog.close();};
    dialog.addEventListener('cancel',event=>{if(busy)event.preventDefault();});
    dialog.addEventListener('close',()=>dialog.remove());
    dialog.querySelector('form').onsubmit=async event=>{
      event.preventDefault();if(busy)return;
      const form=dialog.querySelector('form'), brief=form.elements.brief.value.trim();
      if(!brief)return;
      const file=form.elements.file.files[0];
      if(file&&file.size>64*1024*1024){dialog.querySelector('[data-learning-create-error]').textContent='视频超过 64 MB，请使用较小的原文件。';return;}
      busy=true;for(const field of form.querySelectorAll('input,textarea,button'))field.disabled=true;
      try{
        if(!frozen)frozen={run_id:autoRunId(),title:titleFromIntent(brief),brief,kind:'learning'};
        if(!created){
          try{created=await api('/api/runs',{method:'POST',body:JSON.stringify(frozen)});}
          catch(error){
            const runs=await api('/api/runs');created=runs.find(r=>r.run_id===frozen.run_id&&r.kind==='learning'&&r.title===frozen.title&&r.brief===frozen.brief);
            if(!created)throw error;
          }
        }
        if(file){
          const sha=[...new Uint8Array(await crypto.subtle.digest('SHA-256',await file.arrayBuffer()))].map(v=>v.toString(16).padStart(2,'0')).join('');
          const body=new FormData();body.append('file',file,file.name);
          const response=await fetch(`/api/runs/${encodeURIComponent(frozen.run_id)}/materials?retry_sha256=${sha}`,{method:'POST',body});
          if(!response.ok){const problem=await response.json().catch(()=>({}));throw new Error(problem.detail||'原片上传尚未完成，请保留文件重试。');}
        }
        await loadRuns();
        if(!await selectRun(frozen.run_id))throw new Error('学习记录已创建，但当前决定尚未保存；请稍后打开同一记录。');
        state.chatDrafts.set(frozen.run_id,brief);document.getElementById('chat-input').value=brief;
        setStudioView('project');dialog.close();
        toast('学习记录已创建。核对原片与分析范围后，开始讨论。');
      }catch(error){dialog.querySelector('[data-learning-create-error]').textContent=error.message||'创建状态暂时无法确认，请重试。';}
      finally{busy=false;for(const field of form.querySelectorAll('input,textarea,button'))field.disabled=false;}
    };
    dialog.showModal();
  }

  async function openAnalysisDialog(){
    const runId=state.runId,run=state.runs.find(r=>r.run_id===runId);
    if(!isLearningRun(run)||state.busy||document.querySelector('.learning-analysis-dialog'))return;
    const [data,workspace]=await Promise.all([
      api(`/api/runs/${encodeURIComponent(runId)}/reference-videos`),
      api(`/api/runs/${encodeURIComponent(runId)}/material-observations`),
    ]);
    const videos=list(workspace.materials).filter(m=>m.kind==='video'&&m.usable!==false&&m.sha256);
    const dialog=document.createElement('dialog');dialog.className='learning-analysis-dialog learning-run-dialog';
    dialog.innerHTML=`<form><span class="creator-kicker">参考声画观察</span><h2>分析这条参考</h2>`+
      `<p>选择已上传的原片，或提供 TikTok 视频链接。只分析参考内容，不启动复刻或制作。</p>`+
      `<label>本地原片<select name="material"><option value="">选择一条已上传视频</option>${videos.map((m,i)=>`<option value="${i}">${esc(m.name)}</option>`).join('')}</select></label>`+
      `<label>或 TikTok 链接<input name="source_url" type="url" placeholder="https://www.tiktok.com/@.../video/..."></label>`+
      `<label>这次重点看什么<textarea name="instruction" maxlength="2000" rows="3" placeholder="例如：开头声画怎样交代人物态度"></textarea></label>`+
      `<p class="learning-room-muted">提交后会按需发送原片至百炼进行声画分析，可能产生费用；已完成的同一来源应先阅读已有观察。模型观察未经人工完整看听。</p>`+
      `<p role="alert" data-learning-analysis-error></p><div class="learning-run-actions"><button type="button" class="ghost" data-cancel>取消</button><button type="submit" class="primary">确认并开始声画分析</button></div></form>`;
    document.body.append(dialog);
    let busy=false,uncertain=false;
    dialog.querySelector('[data-cancel]').onclick=()=>{if(!busy)dialog.close();};
    dialog.addEventListener('cancel',event=>{if(busy)event.preventDefault();});
    dialog.addEventListener('close',()=>dialog.remove());
    dialog.querySelector('form').onsubmit=async event=>{
      event.preventDefault();if(busy)return;
      const form=dialog.querySelector('form'), index=form.elements.material.value, sourceUrl=form.elements.source_url.value.trim();
      const material=index===''?null:videos[Number(index)];
      const error=dialog.querySelector('[data-learning-analysis-error]');
      if(Boolean(material)===Boolean(sourceUrl)){error.textContent='请选择本地视频或填写链接，二选一。';return;}
      const previous=list(data.tasks).find(t=>material? t.name===material.name&&t.sha256===material.sha256 : t.source_url===sourceUrl);
      if(previous&&['analyzed','completed'].includes(previous.status)){error.textContent='这条来源已有分析，请先阅读已有报告并继续讨论。';return;}
      busy=true;for(const field of form.querySelectorAll('input,textarea,select,button'))field.disabled=true;
      try{
        const payload={request_kind:'reference_video',request_id:crypto.randomUUID(),reference_id:previous?.reference_id||crypto.randomUUID(),
          intent:'analyze',instruction:previous?.instruction||form.elements.instruction.value.trim(),
          name:material?.name||null,expected_sha256:material?.sha256||null,source_url:material?null:sourceUrl,
          consent:true,expected_revision:data.revision,expected_endpoint:data.connection?.base_url};
        if(!data.connection?.base_url)throw new Error('请先配置百炼北京连接。');
        const result=await submitJob(runId,payload);
        if(!result?.accepted){
          if(result?.uncertain){uncertain=true;error.textContent='提交状态尚未确认。请关闭此窗口，在对话区点击“确认提交状态”；不要重新发起分析。';return;}
          throw result?.error||new Error('分析暂时无法提交。');
        }
        dialog.close();setProjectFocus('chat');
      }catch(cause){error.textContent=cause.message||'分析暂时无法提交。';}
      finally{busy=false;for(const field of form.querySelectorAll('input,textarea,select,button'))field.disabled=false;if(uncertain)form.querySelector('[type="submit"]').disabled=true;}
    };
    dialog.showModal();
  }

  root.StudioLearningSession={isLearningRun,titleFromIntent,onlyNoChangeConclusion,roomHtml,proposalCard,mount,openCreateDialog,openAnalysisDialog};
  if(typeof module!=='undefined')module.exports=root.StudioLearningSession;
})(typeof window!=='undefined'?window:globalThis);
