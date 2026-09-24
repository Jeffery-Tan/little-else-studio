"use strict";
let materialWorkspaceRequest = 0;
async function loadMaterialWorkspace() {
  const runId = state.runId, request = ++materialWorkspaceRequest;
  if (!runId || !state.materialsOpen) return;
  const grid = $('materials-grid');
  try {
    const [data, images, references] = await Promise.all([
      api(`/api/runs/${encodeURIComponent(runId)}/material-observations`),
      api(`/api/runs/${encodeURIComponent(runId)}/image-analysis`).catch(err=>{if(err.status===404)return null;throw err;}),
      api(`/api/runs/${encodeURIComponent(runId)}/reference-videos`).catch(err=>{if(err.status===404)return null;throw err;}),
    ]);
    if (runId !== state.runId || request !== materialWorkspaceRequest || !state.materialsOpen) return;
    grid.innerHTML = StudioMaterials.render(StudioImages.enrich(data, images));
    attachImageActions(grid, runId, data, images);
    const referenceList=$('reference-video-results');
    if(referenceList) {
      referenceList.innerHTML=references?StudioReferenceVideo.tasks({...references,runId}):'';
      for(const button of referenceList.querySelectorAll('[data-reference-resume]')) button.onclick=()=>
        openReferenceVideo(runId,references.tasks.find(t=>t.reference_id===button.dataset.referenceResume),button).catch(err=>toast(err.message,true));
    }
    const referenceButton=$('reference-video-open');
    if(referenceButton){
      const learning=state.runs.find(run=>run.run_id===runId)?.kind==='learning';
      referenceButton.textContent=learning?'分析参考视频':'在对话中提供参考';
      referenceButton.onclick=()=>learning
        ? StudioLearningSession.openAnalysisDialog().catch(err=>toast(err.message,true))
        : openReferenceVideo(runId,null,referenceButton).catch(err=>toast(err.message,true));
    }
    for (const button of grid.querySelectorAll('[data-material-observe]')) button.onclick = () => {
      const material = data.materials.find(m=>m.name === button.dataset.materialObserve);
      if (material) openObservationEditor(runId, material, data, button);
    };
  } catch (err) {
    if (runId !== state.runId || request !== materialWorkspaceRequest) return;
    grid.replaceChildren(box('p','form-error','素材状态暂时无法读取：' + err.message));
    const retry = box('button','ghost','重新读取'); retry.onclick = loadMaterialWorkspace; grid.append(retry);
  }
}
function openObservationEditor(runId, material, data, opener) {
  if (document.querySelector('.observation-dialog')) return;
  const dialog = document.createElement('dialog');
  dialog.className = 'observation-dialog'; dialog.setAttribute('aria-labelledby','observation-title');
  dialog.innerHTML = `<header class="delivery-heading"><h2 id="observation-title">记录素材观察</h2><button class="ghost" data-close>关闭</button></header><p>${escapeHtml(material.name)}</p><form>${StudioMaterials.observationForm(material)}</form>`;
  document.body.append(dialog);
  let saving = false;
  dialog.querySelector('[data-close]').onclick = () => {if (!saving) dialog.close();};
  dialog.addEventListener('cancel',e=>{if (saving) e.preventDefault();});
  dialog.addEventListener('close',()=>{dialog.remove();opener?.focus();});
  dialog.querySelector('form').onsubmit = async event => {
    event.preventDefault(); if (saving) return;
    const form = event.target, error = form.querySelector('[data-material-error]');
    if (state.runId !== runId) {error.textContent = '项目已切换，请关闭后在原项目重新操作。';return;}
    saving = true; const button = form.querySelector('[type="submit"]'); button.disabled = true;
    const payload = StudioMaterials.formPayload(form, material, data);
    try {
      await api(`/api/runs/${encodeURIComponent(runId)}/material-observations`, {method:'POST',body:JSON.stringify(payload)});
      dialog.close();
      if (runId === state.runId) {
        await refreshTimeline(); await loadMaterialWorkspace();
        toast('人工观察已保存，相关旧稿需要重新检查。');
      }
    } catch (err) {error.textContent = err.message;}
    finally {saving = false; button.disabled = false;}
  };
  dialog.showModal();
}
