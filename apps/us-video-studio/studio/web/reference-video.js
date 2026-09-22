/* Two input sources, one reference task. All returned text is escaped. */
(function(root){
  'use strict';
  const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  function form(data, task=null){
    if(!data.connection) return '<p>请先配置现有百炼北京连接，再开始参考视频任务。</p>';
    if(!task)return '<p>在对话里说想做什么，附上视频或 TK 链接即可。</p>';
    const pinned=task.sha256;
    return `<form class="material-observation-editor" data-reference-form><p>继续${task.intent==='recreate'?'参考复刻':'视频分析'}。${pinned?'已完成的分析会复用。':'选择原视频继续，已保存的目标无需重填。'}</p>${pinned?`<p>使用：${esc(task.name)}</p>`:''}<label>原视频<input name="file" type="file" accept=".mp4,.mov,.m4v,.webm" ${pinned?'disabled':''}></label><p class="muted">继续会按需进行百炼声画分析；已完成分析不重复调用。复刻的后续文字策划按当前团队连接计费。</p><p role="alert" data-reference-error></p><button type="submit" class="primary">继续处理</button></form>`;
  }

  function tasks(data){
    return (data.tasks||[]).slice().reverse().map(t=>`<article class="analysis-candidate"><strong>${t.intent==='recreate'?'参考复刻':'视频分析'}</strong><p>${esc(t.title||t.name||t.source_url)}</p>${t.report?`<details><summary>查看声画拆解报告</summary><pre style="white-space:pre-wrap;font:inherit">${esc(t.report)}</pre></details><p class="muted">模型观察未经人工核对。</p>`:''}${t.name?`<video controls preload="none" style="max-width:100%;max-height:240px" src="${esc('/api/runs/'+encodeURIComponent(data.runId)+'/materials/'+encodeURIComponent(t.name))}"></video>`:''}${!['completed','stale'].includes(t.status)?`<button class="ghost small" data-reference-resume="${esc(t.reference_id)}">继续 / 上传原视频</button>`:''}</article>`).join('');
  }
  root.StudioReferenceVideo={form,tasks};
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
      const payload={request_kind:'reference_video',request_id:requestId,reference_id:referenceId,
        intent:task.intent,instruction:task.instruction||'',
        name,expected_sha256:sha,source_url:name?null:link,consent:true,
        expected_revision:data.revision,expected_endpoint:data.connection.base_url};
      dialog.close();toggleMaterials(false);setProjectFocus('chat');await submitJob(runId,payload);
    }catch(err){error.textContent=err.message;}
    finally{busy=false;form.querySelector('[type="submit"]').disabled=false;}
  };
}
