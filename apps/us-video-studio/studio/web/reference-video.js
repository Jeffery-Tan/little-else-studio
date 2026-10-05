/* Two input sources, one reference task. All returned text is escaped. */
(function(root){
  'use strict';
  const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const recoveryGoal=task=>({analysis:'参考拆解',reference_prompt:'本品提示词',intake:'需求方案',direction:'创意方向',
    writing:'完整方案',production_input:'整套制作输入',production_media:'原制作目标',revision:'原作品修改'})[task?.recovery?.deliverable]||'原任务';
  function recoveryPayload(task,text,{name=null,sha256=null}={}){
    const recovery=task?.recovery;
    if(!recovery?.allowed)throw Error(recovery?.reason||'原任务已变化，请刷新后再继续。');
    const target=Object.fromEntries(['reference_id','parent_job_id','source_user_input_id','interpretation_id']
      .map(key=>[key,recovery[key]]));
    if(name){target.name=name;target.expected_sha256=sha256;}
    const payload={text,reference_recovery:target,reference_consent:recovery.requires_observation===true};
    if(name)payload.attachments=[{name,purpose:'reference'}];
    return payload;
  }
  function form(data, task=null){
    if(!data.connection&&(!task?.recovery||task.recovery.requires_observation)) return '<p>请先配置现有百炼北京连接，再开始参考视频任务。</p>';
    if(!task)return '<p>在对话里说想做什么，附上视频或 TK 链接即可。</p>';
    const pinned=task.sha256;
    const savedAnalysis=Boolean(task.analysis_id||task.candidate||task.report);
    return `<form class="material-observation-editor" data-reference-form><p>继续${task.recovery?esc(recoveryGoal(task)):task.intent==='recreate'?'参考复刻':'视频分析'}。${pinned?(savedAnalysis?'已完成的分析会复用。':'原片已保存，声画分析尚未完成。'):task.source_url?'不选文件会重新获取原链接；也可以选择原视频上传，已保存的目标无需重填。':'选择原视频继续，已保存的目标无需重填。'}</p>${task.recovery?.original_text?`<details><summary>原来的要求</summary><p>${esc(task.recovery.original_text)}</p></details>`:''}${pinned?`<p>使用：${esc(task.name)}</p>`:''}<label>原视频${task.source_url&&!pinned?'（可选）':''}<input name="file" type="file" accept=".mp4,.mov,.m4v,.webm" ${pinned?'disabled':''}></label><p class="muted">继续会按需进行百炼声画分析；已完成分析不重复调用。复刻的后续文字策划按当前团队连接计费。</p><p role="alert" data-reference-error></p><button type="submit" class="primary">继续处理</button></form>`;
  }

  function taskPresentation(task,{busy=false}={}){
    const hasAnalysis=Boolean(task?.analysis_id||task?.candidate||task?.report);
    const step=({started:'原片尚未获取',downloaded:'原片已保存，声画分析尚未完成',
      analyzed:'声画分析已保存',completed:'参考处理已完成',stale:'参考来源已变化'})[task?.status]||'参考步骤无法核对';
    const execution=({queued:'任务已排队',running:'任务正在执行',cancelling:'正在停止本轮',
      waiting:'等待继续',succeeded:'本轮任务已结束',failed:'本轮任务失败',blocked:'本轮任务受阻',
      cancelled:'本轮任务已停止',interrupted:'本轮任务已中断',
      ...(hasAnalysis?{failed:'后续任务失败',blocked:'后续任务受阻'}:{})})[task?.execution_status]||
      (task?.job_id||task?.execution_status?'本轮任务结果待核实':'历史任务状态未记录');
    const stopped=['failed','blocked','cancelled','interrupted'].includes(task?.execution_status);
    const active=['queued','running','cancelling','waiting'].includes(task?.execution_status);
    const historical=!task?.job_id&&!task?.execution_status;
    const recoverable=!task?.recovery||task.recovery.allowed===true;
    return {step,execution,label:`${step} · ${execution}`,reason:task?.failure_reason||'',
      canResume:recoverable&&!busy&&!hasAnalysis&&!['stale','completed'].includes(task?.status)&&!active&&(stopped||historical),
      canContinue:recoverable&&!busy&&hasAnalysis&&Boolean(task?.candidate)&&task?.status!=='stale'&&stopped};
  }
  function tasks(data){
    return (data.tasks||[]).slice().reverse().map(t=>{
      const presentation=taskPresentation(t,{busy:data.busy===true});
      return `<article class="analysis-candidate"><strong>${t.recovery?esc(recoveryGoal(t)):t.intent==='recreate'?'参考复刻':'视频分析'}</strong><p>${esc(t.title||t.name||t.source_url)}</p>`+
      `<p class="reference-task-state">${esc(presentation.label)}</p>`+
      (presentation.reason?`<p role="alert">${esc(presentation.reason)}</p>`:'')+
      (t.recovery?.reason?`<p class="muted">${esc(t.recovery.reason)}</p>`:'')+
      (t.name?`<video controls preload="none" style="max-width:100%;max-height:240px" src="${esc('/api/runs/'+encodeURIComponent(data.runId)+'/materials/'+encodeURIComponent(t.name))}"></video>`:'')+
      (t.candidate?`<button type="button" class="ghost small" data-reference-view="${esc(t.reference_id)}">查看逐段拆解 ↗</button>`:
        t.report?`<details><summary>查看分析报告</summary><pre style="white-space:pre-wrap;font:inherit">${esc(t.report)}</pre></details>`:'')+
      (t.report?'<p class="muted">模型观察未经人工核对。</p>':'')+
      (presentation.canResume?`<button class="ghost small" data-reference-resume="${esc(t.reference_id)}">继续 / 上传原视频</button>`:'')+
      (presentation.canContinue?`<button class="ghost small" data-reference-continue="${esc(t.reference_id)}">交给主制片继续 ↗</button>`:'')+`</article>`;
    }).join('');
  }

  const stamp=value=>Number.isFinite(Number(value))?`${Number(value).toFixed(1)}s`:'时间未定';
  function breakdownHtml(task,runId,{adaptation=true}={}){
    if(!task)return '<section class="reference-breakdown"><h2>参考拆解</h2><p>分析尚未完成，可在素材与参考中查看任务状态。</p><button type="button" class="ghost" data-reference-open-materials>查看参考任务</button></section>';
    const c=task.candidate, rows=Array.isArray(c?.timeline)?c.timeline:[];
    const methods=Array.isArray(c?.reference_methods)?c.reference_methods:[];
    const video=task.name?`<video controls preload="metadata" src="${esc('/api/runs/'+encodeURIComponent(runId)+'/materials/'+encodeURIComponent(task.name))}" aria-label="参考原视频"></video>`:'<p>原视频尚未保存，请从参考任务继续。</p>';
    const segments=rows.map((row,index)=>{
      const timed=Number.isFinite(Number(row.start_seconds));
      const start=timed?Number(row.start_seconds):Number(row.time_seconds?.[0]);
      const range=timed?`${stamp(row.start_seconds)}–${stamp(row.end_seconds)}`:
        Array.isArray(row.time_seconds)?row.time_seconds.map(stamp).join(' / '):'时间未定';
      return `<article class="reference-segment"><div class="reference-segment-head"><span>片段 ${index+1} · ${esc(range)}</span>`+
        (task.name&&Number.isFinite(start)?`<button type="button" class="ghost small" data-reference-seek="${start}">播放此处 ▶</button>`:'')+`</div>`+
        `<p><strong>画面</strong>${esc(row.visible||'未记录')}</p>`+
        (timed?`<p><strong>声音</strong>${esc(row.audible||'未能确认')}</p><p><strong>声画关系</strong>${esc(row.relationship||'未能确认')}</p>`:
          '<p class="muted">仅抽帧观察；未听取原片声音，也未核对帧间动作。</p>')+
        `</article>`;
    }).join('');
    const methodCards=methods.map((method,index)=>`<article class="reference-method"><span>方法假设 ${index+1} · 对应片段 ${esc((method.supports||[]).map(i=>i+1).join('、'))}</span>`+
      `<p>${esc(method.mechanism)}</p><dl><dt>不能直接照搬</dt><dd>${esc(method.non_transferable)}</dd><dt>可尝试改变</dt><dd>${esc(method.test_change)}</dd></dl></article>`).join('');
    const prompt=task.prompt?.text||'';
    const promptPanel=adaptation&&c?`<section class="reference-prompt"><div class="reference-section-head"><h3>本品提示词</h3><span>${task.prompt?`已保存 · ${task.prompt.actor==='user'?'手动修改':'模型生成'}`:'尚未保存'}</span></div>`+
      `<p class="muted">依据当前参考观察改写；请核对本款商品事实、市场语言和促销条件。保存文字不会生成媒体。</p>`+
      `<textarea data-reference-prompt rows="12" maxlength="20000" aria-label="本品提示词" ${task.status==='stale'?'disabled':''}>${esc(prompt)}</textarea>`+
      `<div class="reference-prompt-actions"><button type="button" class="primary" data-reference-save ${task.status==='stale'?'disabled':''}>保存修改</button><button type="button" class="ghost" data-reference-copy>复制提示词</button><button type="button" class="ghost" data-reference-compose>请主制片适配 ↗</button></div><p role="status" data-reference-feedback></p></section>`:'';
    return `<section class="reference-breakdown" data-reference-breakdown="${esc(task.reference_id)}"><header class="reference-intro"><span class="creator-kicker">参考视频 · ${task.status==='stale'?'来源已变化':'模型观察'}</span><h2>${esc(task.title||task.name||'参考视频拆解')}</h2><p>逐段时间由模型估计，点击可回看原片；观察未经人工核对，不证明销量或本款商品性能。</p></header>`+
      `<div class="reference-layout"><div class="reference-source">${video}<p class="muted">${esc(task.name||task.source_url||'原片待提供')}</p>`+
      (c?.limitations?.length?`<details><summary>观察范围与限制</summary><ul>${c.limitations.map(v=>`<li>${esc(v)}</li>`).join('')}</ul></details>`:'')+`</div>`+
      `<div class="reference-timeline"><div class="reference-section-head"><h3>逐段观察</h3><span>${rows.length} 段</span></div>${segments||'<p>还没有可定位的观察；请查看原始报告或继续参考任务。</p>'}`+
      (!rows.length&&task.report?`<details><summary>原始分析报告</summary><p>${esc(task.report)}</p></details>`:'')+
      (methodCards?`<div class="reference-section-head"><h3>可借鉴的表达</h3></div>${methodCards}`:'')+`</div></div>${promptPanel}</section>`;
  }

  function mountBreakdowns(host,{runId,tasks,request,reload,compose,notify}={}){
    for(const panel of host.querySelectorAll('[data-reference-breakdown]')){
      const task=(tasks||[]).find(item=>item.reference_id===panel.dataset.referenceBreakdown);
      if(!task)continue;
      const video=panel.querySelector('video');
      for(const button of panel.querySelectorAll('[data-reference-seek]'))button.onclick=()=>{
        if(!video)return;
        video.currentTime=Number(button.dataset.referenceSeek);
        video.play?.().catch(()=>{});
      };
      const field=panel.querySelector('[data-reference-prompt]');
      if(!field)continue;
      const feedback=panel.querySelector('[data-reference-feedback]');
      panel.querySelector('[data-reference-compose]').onclick=()=>compose?.(task);
      panel.querySelector('[data-reference-copy]').onclick=async()=>{
        if(!field.value.trim()){feedback.textContent='还没有可复制的提示词。';return;}
        try{await navigator.clipboard.writeText(field.value);feedback.textContent='提示词已复制。';}
        catch{feedback.textContent='复制失败，请选中文字后手动复制。';}
      };
      const save=panel.querySelector('[data-reference-save]');
      let pending=null;
      save.onclick=async()=>{
        const prompt=field.value.trim();
        if(!prompt){feedback.textContent='请先填写提示词。';return;}
        if(!task.candidate||task.status==='stale'){feedback.textContent='参考分析已变化，请刷新后重新核对。';return;}
        save.disabled=true;
        if(!pending||pending.prompt!==prompt)pending={requestId:crypto.randomUUID(),prompt,
          expectedPromptSha:task.prompt?.sha256||null,expectedPromptRevision:task.prompt?.revision||null};
        let result;
        try{
          result=await request(`/api/runs/${encodeURIComponent(runId)}/reference-videos/${encodeURIComponent(task.reference_id)}/prompt`,{
            method:'POST',body:JSON.stringify({request_id:pending.requestId,analysis_id:task.candidate.analysis_id,
              candidate_sha256:task.candidate.candidate_sha256,expected_prompt_sha256:pending.expectedPromptSha,
              expected_prompt_revision:pending.expectedPromptRevision,prompt})});
        }catch(error){
          if(error.status===409)pending=null;
          feedback.textContent=error.message;save.disabled=false;return;
        }
        pending=null;task.prompt=result.prompt;feedback.textContent='修改已保存到当前项目。';save.disabled=false;
        try{await reload?.();}catch{feedback.textContent='修改已保存；页面暂时无法刷新，请重新打开当前项目查看。';}
      };
    }
  }
  root.StudioReferenceVideo={form,tasks,taskPresentation,breakdownHtml,mountBreakdowns,recoveryPayload};
  if(typeof module!=='undefined') module.exports=root.StudioReferenceVideo;
})(typeof window!=='undefined'?window:globalThis);

async function openReferenceVideo(runId, task=null, opener=null){
  if(state.runId!==runId || state.busy || document.querySelector('.observation-dialog')) return;
  if(!task){toggleMaterials(false);setProjectFocus('chat');$('chat-input').focus();return;}
  const data=await api(`/api/runs/${encodeURIComponent(runId)}/reference-videos`);
  if(state.runId!==runId || document.querySelector('.observation-dialog')) return;
  const dialog=document.createElement('dialog');
  dialog.className='observation-dialog image-analysis-dialog';
  dialog.setAttribute('aria-labelledby','reference-video-title');
  dialog.innerHTML=`<header class="delivery-heading"><h2 id="reference-video-title">参考视频</h2><button class="ghost" data-close>关闭</button></header>${StudioReferenceVideo.form(data,task)}`;
  document.body.append(dialog);
  let busy=false;
  dialog.querySelector('[data-close]').onclick=()=>{if(!busy)dialog.close();};
  dialog.addEventListener('cancel',e=>{if(busy)e.preventDefault();});
  dialog.addEventListener('close',()=>{dialog.remove();opener?.focus();});
  dialog.showModal();
  const form=dialog.querySelector('form');if(!form)return;
  const requestId=crypto.randomUUID(), referenceId=task?.reference_id||crypto.randomUUID();
  form.onsubmit=async event=>{
    event.preventDefault(); if(busy)return;
    const error=form.querySelector('[data-reference-error]');
    if(state.runId!==runId || state.busy || state.decisionBusy){error.textContent='请先结束当前任务。';return;}
    const file=form.elements.file.files[0], link=task.source_url||'';
    if(!task?.sha256 && !file && !link){error.textContent='请选择原视频或粘贴TK链接。';return;}
    if(file&&link&&!task){error.textContent='请选择一种来源：本地视频或链接。';return;}
    busy=true;form.querySelector('[type="submit"]').disabled=true;
    try{
      let name=task?.sha256?task.name:null, sha=task?.sha256||null;
      if(file){
        if(file.size>64*1024*1024)throw Error('视频超过64 MB，请使用较小原视频。');
        sha=[...new Uint8Array(await crypto.subtle.digest('SHA-256',await file.arrayBuffer()))].map(b=>b.toString(16).padStart(2,'0')).join('');
        const current=await api(`/api/runs/${encodeURIComponent(runId)}/material-observations`);
        const same=current.materials.find(m=>m.sha256===sha&&m.kind==='video');
        name=same?.name||file.name;
        if(!same){
          const body=new FormData();body.append('file',file);
          const response=await fetch(`/api/runs/${encodeURIComponent(runId)}/materials?retry_sha256=${sha}`,{method:'POST',body});
          const result=await response.json();
          if(!response.ok)throw Error(typeof result.detail==='string'?result.detail:'视频上传失败。');
        }
      }
      if(state.runId!==runId)throw Error('项目已切换；文件已留在原项目，未开始分析。');
      const payload=task.recovery?{request_id:requestId,...StudioReferenceVideo.recoveryPayload(task,
        '请恢复这项未完成的原任务，先补齐参考观察，再按原来的交付继续。',
        {name:file?name:null,sha256:file?sha:null})}:{request_kind:'reference_video',request_id:requestId,reference_id:referenceId,
        intent:task.intent,instruction:task.instruction||'',
        name,expected_sha256:sha,source_url:name?null:link,consent:true,
        expected_revision:data.revision,expected_endpoint:data.connection.base_url};
      dialog.close();toggleMaterials(false);setProjectFocus('chat');await submitJob(runId,payload);
    }catch(err){error.textContent=err.message;}
    finally{busy=false;form.querySelector('[type="submit"]').disabled=false;}
  };
}
