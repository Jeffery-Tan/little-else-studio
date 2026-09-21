"use strict";
function wireRevisionActions(host, runId, step) {
  if (!step.has_doc || !['direction','writing'].includes(step.stage)) return;
  const anchor=host.querySelector('.pane-doc');if(!anchor)return;
  const actions=box('div','revision-entry');
  const edit=box('button','ghost small',step.stage==='direction'?'编辑方向 / 比较版本':'编辑镜头 / 比较版本');
  edit.onclick=()=>openRevisionWorkspace(runId,step.stage,step.version);
  actions.append(edit);anchor.before(actions);
}
async function openRevisionWorkspace(runId,stage,version=0) {
  if(state.runId!==runId || document.querySelector('.revision-dialog'))return;
  const dialog=document.createElement('dialog');dialog.className='revision-dialog';
  dialog.setAttribute('aria-labelledby','revision-title');
  dialog.innerHTML='<header class="revision-heading"><div><span class="eyebrow">创作修改</span><h2 id="revision-title">把修改落到具体内容</h2></div><button class="ghost" data-close>关闭</button></header><div class="revision-workspace"><p>正在读取版本与修改记录…</p></div>';
  document.body.append(dialog);dialog.showModal();
  let saving=false,selected=0,data,config,values={},saveRequest=null,view='editor',compareSerial=0;
  const area=dialog.querySelector('.revision-workspace');
  const key=()=>`studio-object-draft:${runId}:${stage}:${data.version}:${data.sha256}`;
  const leave=()=>{if(!saving)dialog.close();};
  dialog.querySelector('[data-close]').onclick=leave;
  dialog.addEventListener('cancel',event=>{if(saving)event.preventDefault();});
  dialog.addEventListener('close',()=>dialog.remove());
  function remember(){try{sessionStorage.setItem(key(),JSON.stringify(values));}catch(_){toast('本窗口无法暂存修改，请及时保存到项目。',true);}}
  function changed(resetRequest=true){remember();if(resetRequest)saveRequest=null;const edits=StudioRevisions.cells(data,values);
    const button=area.querySelector('[data-save-edit]');if(button){button.disabled=saving||!data.editable||!edits.length;button.textContent=edits.length?`保存修改草稿 · ${edits.length} 处`:'保存修改草稿';}
    const hint=area.querySelector('[data-draft-state]');if(hint)hint.textContent=edits.length?'修改暂存在本窗口；保存后可从项目中找回。':'保存草稿不调用模型，也不会替换当前版本。';
    if(hint&&stage==='writing'&&edits.some(e=>['tc','duration'].includes(e.field)))hint.textContent+=' 新版总时长会标为待复核，需重新检查全片节奏。';}
  function draw(){
    area.innerHTML=`<div class="revision-version"><span>正在查看版本 ${data.version}</span><button class="text-button" data-read-original>查看原文 ↗</button></div>${!data.editable?`<p class="material-warning">${escapeHtml(data.reason)}</p>`:''}<div class="revision-tabs" role="group" aria-label="修改视图"><button class="ghost" data-view="editor" aria-pressed="${view==='editor'}">局部编辑</button><button class="ghost" data-view="drafts" aria-pressed="${view==='drafts'}">修改草稿 · ${data.drafts.length}</button><button class="ghost" data-view="compare" aria-pressed="${view==='compare'}">版本比较</button></div><div data-revision-content></div><p class="form-error" role="alert" data-revision-error></p>`;
    area.querySelector('[data-read-original]').onclick=()=>{leave();openReader(stage,data.version).catch(err=>toast(err.message,true));};
    for(const button of area.querySelectorAll('[data-view]'))button.onclick=()=>{if(saving)return;view=button.dataset.view;draw();};
    const content=area.querySelector('[data-revision-content]');
    if(view==='editor'){
      content.innerHTML=StudioRevisions.editor(data,selected,values)+`<footer class="revision-footer"><p data-draft-state></p><button class="primary" data-save-edit>保存修改草稿</button></footer>`;
      for(const button of content.querySelectorAll('[data-edit-row]'))button.onclick=()=>{selected=Number(button.dataset.editRow);draw();};
      for(const input of content.querySelectorAll('[data-edit-field]'))input.oninput=()=>{values[`${input.dataset.row}:${input.dataset.editField}`]=input.value;changed();};
      content.querySelector('[data-save-edit]').onclick=save;
      changed(false);
    } else if(view==='drafts') drawDrafts(content);
    else drawCompare(content);
  }
  async function save(){
    if(saving)return;
    const error=area.querySelector('[data-revision-error]');
    if(state.runId!==runId){error.textContent='项目已切换，请回到原项目再保存。';return;}
    const edits=StudioRevisions.cells(data,values);if(!edits.length)return;
    saveRequest ||= {request_id:crypto.randomUUID(),expected_version:data.version,expected_sha256:data.sha256,edits};
    saving=true;area.querySelector('[data-save-edit]').disabled=true;
    for(const field of area.querySelectorAll('textarea,[data-edit-row]'))field.disabled=true;
    try{
      const draft=await api(`/api/runs/${runId}/edits/${stage}`,{method:'POST',body:JSON.stringify(saveRequest)});
      if(!data.drafts.some(d=>d.draft_id===draft.draft_id))data.drafts.push(draft);
      values={};sessionStorage.removeItem(key());saveRequest=null;view='drafts';draw();
      toast('修改草稿已保存；当前方案尚未改变。');
    }catch(err){error.textContent=err.message;}
    finally{saving=false;for(const field of area.querySelectorAll('textarea,[data-edit-row]'))field.disabled=!data.editable;const b=area.querySelector('[data-save-edit]');if(b)b.disabled=!data.editable||!StudioRevisions.cells(data,values).length;}
  }
  function drawDrafts(content){
    if(!data.drafts.length){content.innerHTML='<p class="revision-empty">还没有保存的修改。先选中一个方向或镜头，修改具体内容。</p>';return;}
    content.innerHTML=data.drafts.slice().reverse().map(d=>`<article class="revision-draft"><header><h3>基于版本 ${d.base_version} · ${d.changes.length} 处修改</h3><span>${d.status==='applied'?`已整理为版本 ${d.result_version}`:d.status==='stale'?'原稿已有新版本':'修改已保存'}</span></header>${StudioRevisions.changes(d.changes,data.labels)}${d.status==='pending'?`<button class="ghost" data-reedit="${d.draft_id}">继续编辑这份修改</button><div data-generate="${d.draft_id}"></div>`:'<p class="muted">保留原始修改记录，不会自动套用到新版本。</p>'}</article>`).join('');
    for(const button of content.querySelectorAll('[data-reedit]'))button.onclick=()=>{
      const d=data.drafts.find(x=>x.draft_id===button.dataset.reedit);
      if(d.base_sha256!==data.sha256){toast('请从对应的原版本继续编辑。',true);return;}
      if(StudioRevisions.cells(data,values).length){toast('本窗口还有修改，请先保存，避免覆盖。',true);return;}
      values=Object.fromEntries(d.edits.map(e=>[`${e.row}:${e.field}`,e.value]));remember();view='editor';draw();
    };
    for(const node of content.querySelectorAll('[data-generate]')){
      const d=data.drafts.find(x=>x.draft_id===node.dataset.generate);
      if(!data.editable || d.base_sha256!==data.sha256){node.append(box('p','material-warning',data.reason || '请打开这份修改对应的当前版本。'));continue;}
      if(stage==='writing'&&d.changes.some(e=>['tc','duration'].includes(e.field)))node.append(box('p','material-warning','镜头时码或时长已修改；新版总时长将标为待复核，需要重新检查全片节奏。'));
      const role=stage==='direction'?'director':'writer',binding=config.roles[role],connection=config.connections.find(c=>c.id===binding.connection);
      const local=data.revision_mode==='local';
      const explanation=box('p','muted',local ? '保存后，正文与创作板会同步更新。此次保存不调用模型，新版本仍需审核与采用。' : `由${connection?.name || '所选服务'} · ${binding.model || 'Codex 默认模型'}同步完整正文。会调用一次模型并计入费用；新版本仍需独立审核与采用，依赖旧稿的内容需重新检查。`);
      node.append(explanation);
      const label=box('label','analysis-check'),consent=document.createElement('input');consent.type='checkbox';
      label.append(consent,box('span','','确认使用上述模型整理这一份修改，可能产生费用。'));if(!local)node.append(label);
      const button=box('button','primary',local?'保存为新版本':'整理为新版本');button.disabled=!local;consent.onchange=()=>{button.disabled=!consent.checked;};
      button.onclick=()=>{
        if(state.runId!==runId||state.busy||state.decisionBusy||state.recoveryBlocked){toast('请先结束当前任务或处理恢复事项。',true);return;}
        dialog.close();setProjectFocus('chat');
        submitJob(runId,{request_kind:'artifact_revision',stage,draft_id:d.draft_id,request_id:crypto.randomUUID(),
          expected_draft_sha256:d.draft_sha256,expected_revision:config.revision,consent:true});
      };
      node.append(button);
    }
  }
  function drawCompare(content){
    if(data.versions.length<2){content.innerHTML='<p class="revision-empty">目前只有一个版本。新版本保存后，可以在这里逐项比较变化。</p>';return;}
    const choices=data.versions.map(v=>`<option value="${v.version}">版本 ${v.version}</option>`).join('');
    content.innerHTML=`<div class="revision-comparison-controls"><label>修改前<select data-from>${choices}</select></label><span>→</span><label>修改后<select data-to>${choices}</select></label></div><div data-diff-result></div>`;
    const from=content.querySelector('[data-from]'),to=content.querySelector('[data-to]'),result=content.querySelector('[data-diff-result]');
    from.value=data.versions.at(-2).version;to.value=data.versions.at(-1).version;
    async function compare(){const serial=++compareSerial;result.textContent='正在比较两个版本…';try{
      const diff=await api(`/api/runs/${runId}/artifacts/${stage}/compare?from_version=${from.value}&to_version=${to.value}`);
      if(serial===compareSerial&&dialog.isConnected)result.innerHTML=StudioRevisions.comparison(diff,data.labels);
    }catch(err){if(serial===compareSerial)result.textContent=err.message;}}
    from.onchange=compare;to.onchange=compare;compare();
  }
  try{
    [data,config]=await Promise.all([api(`/api/runs/${runId}/edits/${stage}?version=${version}`),api('/api/connections')]);
    if(!dialog.isConnected)return;
    try{const v=JSON.parse(sessionStorage.getItem(key())||'{}');if(v&&typeof v==='object'&&!Array.isArray(v))values=Object.fromEntries(Object.entries(v).filter(([,x])=>typeof x==='string'));}catch(_){}
    draw();
  }catch(err){area.textContent=err.status===404
    ?'编辑工具已更新，但当前服务尚未加载这一功能。正常重启工作台后再打开；已有方案和修改记录会保留。'
    :'暂时无法读取修改工作区：'+err.message;}
}
