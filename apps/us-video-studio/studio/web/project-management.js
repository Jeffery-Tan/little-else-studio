"use strict";

function deletionPromptHtml(run) {
  return `<h2 id="project-management-title">删除「${escapeHtml(run.title)}」？</h2><p>项目将移入回收站，从首页和项目切换列表中移除。对话、素材和制作成果会保留，之后可以恢复。</p><p>正在执行的项目需要先停止任务并等待结束。</p><p role="status" data-feedback></p><div class="management-actions"><button class="ghost" data-cancel>取消</button><button class="danger" data-confirm>移到回收站</button></div>`;
}

function managementDialog(html) {
  const dialog=document.createElement('dialog');
  dialog.className='project-management-dialog';
  dialog.setAttribute('aria-labelledby','project-management-title');
  dialog.innerHTML=html;
  dialog.addEventListener('close',()=>dialog.remove());
  document.body.append(dialog);dialog.showModal();
  return dialog;
}

async function syncProjectRemoval(runId) {
  try {
    const first=JSON.parse(sessionStorage.getItem('studio-first-message')||'null');
    if(first?.request_id){
      const sha=[...new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(first.request_id)))].map(x=>x.toString(16).padStart(2,'0')).join('');
      if(runId===`chat-${sha.slice(0,24)}`)sessionStorage.removeItem('studio-first-message');
    }
  }catch(_){}
  const removedCurrent=state.runId===runId;
  if(state.runId===runId) {
    detachProjectJob();
    closeEmbeddedProduction();
    state.runId=null;state.timeline=null;state.projectBrief=null;state.rail=[];
    state.materials=[];state.activeStage=null;state.artifactBodies.clear();
    state.busy=false;state.recoveryBlocked=false;state.refreshSerial++;
    sessionStorage.removeItem('studio-selected-run');
    $('chat-input').value='';$('conversation-body').replaceChildren();$('stage').replaceChildren();
    $('project-progress').replaceChildren();$('job-status').hidden=true;
    $('reader').hidden=true;$('viewer').hidden=true;
    setStudioView('home');renderMaterials();
  }
  await loadRuns();
  if(state.runId && removedCurrent){const next=state.runId;state.runId=null;await selectRun(next);}
}

function confirmProjectDeletion(runId) {
  if(state.decisionBusy)return;
  const run=state.runs.find(r=>r.run_id===runId);if(!run)return;
  const dialog=managementDialog(deletionPromptHtml(run));
  let sending=false;
  dialog.querySelector('[data-cancel]').onclick=()=>dialog.close();
  dialog.addEventListener('cancel',event=>{if(sending)event.preventDefault();});
  dialog.querySelector('[data-confirm]').onclick=async()=>{
    if(sending)return;sending=true;state.decisionBusy=true;
    for(const button of dialog.querySelectorAll('button'))button.disabled=true;
    try {
      await api(`/api/runs/${encodeURIComponent(runId)}`,{method:'DELETE'});
      state.decisionBusy=false;
      await syncProjectRemoval(runId);
      dialog.close();toast('项目已移到回收站，可以随时恢复。');
    } catch(error) {
      dialog.querySelector('[data-feedback]').textContent=error.message||'删除未完成，请重试。';
    } finally {
      sending=false;state.decisionBusy=false;
      for(const button of dialog.querySelectorAll('button'))button.disabled=false;
    }
  };
}

async function openProjectTrash() {
  const dialog=managementDialog('<h2 id="project-management-title">回收站</h2><p>已删除的项目保留全部内容，恢复后可继续创作。</p><div data-trash-list>正在读取…</div><p role="status" data-feedback></p><div class="management-actions"><button class="ghost" data-close>关闭</button></div>');
  dialog.querySelector('[data-close]').onclick=()=>dialog.close();
  async function draw(){
    const rows=await api('/api/trash');if(!dialog.isConnected)return;
    dialog.querySelector('[data-trash-list]').innerHTML=rows.length?rows.map(run=>`<div class="trash-project"><div><strong>${escapeHtml(run.title)}</strong><small>${escapeHtml(studioDate(run.deleted_at))}删除</small></div><button class="ghost" data-restore-project="${escapeHtml(run.run_id)}">恢复</button></div>`).join(''):'<p>回收站是空的。</p>';
  }
  dialog.addEventListener('click',async event=>{
    const button=event.target.closest('[data-restore-project]');if(!button)return;
    button.disabled=true;
    try {
      await api(`/api/runs/${encodeURIComponent(button.dataset.restoreProject)}/restore`,{method:'POST',body:'{}'});
      const hadSelection=Boolean(state.runId);
      await loadRuns();
      if(!hadSelection&&state.runId){const next=state.runId;state.runId=null;await selectRun(next);}
      await draw();toast('项目已恢复到首页。');
    }catch(error){dialog.querySelector('[data-feedback]').textContent=error.message;button.disabled=false;}
  });
  try{await draw();}catch(error){dialog.querySelector('[data-feedback]').textContent=error.message;}
}

function initProjectManagement(){
  $('trash-open').onclick=openProjectTrash;
  $('project-delete-current').onclick=()=>confirmProjectDeletion(state.runId);
  document.addEventListener('click',event=>{
    const button=event.target.closest('[data-delete-project]');
    if(button)confirmProjectDeletion(button.dataset.deleteProject);
  });
}
if(typeof module!=='undefined')module.exports={deletionPromptHtml};
