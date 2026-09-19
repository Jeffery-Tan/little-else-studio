"use strict";
function imageDialog(runId, material, title, content, opener) {
  const dialog = document.createElement('dialog');
  dialog.className = 'observation-dialog image-analysis-dialog';
  dialog.setAttribute('aria-labelledby','image-analysis-title');
  dialog.innerHTML = `<header class="delivery-heading"><h2 id="image-analysis-title">${escapeHtml(title)}</h2><button class="ghost" data-close>关闭</button></header><div class="analysis-layout"><div class="analysis-preview"></div><div class="analysis-content">${content}</div></div>`;
  const preview = document.createElement('img');
  preview.src = `/api/runs/${encodeURIComponent(runId)}/materials/${encodeURIComponent(material.name)}`;
  preview.alt = material.name; dialog.querySelector('.analysis-preview').append(preview);
  document.body.append(dialog);
  dialog.querySelector('[data-close]').onclick = () => dialog.close();
  dialog.addEventListener('close',()=>{dialog.remove();opener?.focus();});
  dialog.showModal();
  return dialog;
}
function openImageSetup(runId, material, data, opener) {
  if (state.runId !== runId) return;
  const dialog = imageDialog(runId, material, '让模型看懂这张图', StudioImages.setup(material, data), opener);
  const settings = dialog.querySelector('[data-model-settings]');
  if (settings) {settings.onclick=()=>{dialog.close();$('connections-open').click();};return;}
  const form = dialog.querySelector('form');
  const connection = () => data.connections.find(c=>c.id === form.elements.connection_id.value);
  function updateCost() {dialog.querySelector('[data-analysis-cost]').textContent = StudioImages.costText(connection(),form.elements.model.value.trim());form.elements.consent.checked = false;}
  form.elements.connection_id.onchange = () => {form.elements.model.value = connection()?.suggested_model || '';updateCost();};
  form.elements.model.oninput = updateCost;
  form.elements.connection_id.onchange();
  form.onsubmit = event => {
    event.preventDefault();
    const error = form.querySelector('[data-analysis-error]');
    if (state.runId !== runId) {error.textContent='项目已切换，请在原项目重新操作。';return;}
    if (state.busy || state.decisionBusy || state.recoveryBlocked) {error.textContent='请先结束当前任务，或在制片对话中处理待恢复事项。';return;}
    const payload = {request_kind:'image_analysis',request_id:crypto.randomUUID(),name:material.name,
      expected_sha256:material.sha256,expected_revision:data.revision,connection_id:form.elements.connection_id.value,
      model:form.elements.model.value.trim(),purpose:form.elements.purpose.value,consent:form.elements.consent.checked};
    dialog.close(); toggleMaterials(false); setProjectFocus('chat');
    submitJob(runId,payload);
  };
}
function openImageReview(runId, material, candidate, data, opener) {
  if (state.runId !== runId) return;
  const dialog = imageDialog(runId, material, '核对图片观察', StudioImages.review(candidate), opener);
  const form = dialog.querySelector('form'), button = form.querySelector('[type="submit"]');
  if (!button) return;
  let saving = false;
  const selected = () => [...form.querySelectorAll('input[name="finding"]:checked')].map(el=>Number(el.value));
  form.onchange = () => {button.disabled=saving || !selected().length;};
  dialog.querySelector('[data-close]').onclick = () => {if (!saving) dialog.close();};
  dialog.addEventListener('cancel',event=>{if (saving) event.preventDefault();});
  form.onsubmit = async event => {
    event.preventDefault(); if (saving) return;
    const error = form.querySelector('[data-analysis-error]');
    if (state.runId !== runId) {error.textContent='项目已切换，请在原项目重新操作。';return;}
    saving=true; button.disabled=true;
    try {
      await api(`/api/runs/${encodeURIComponent(runId)}/image-analysis/${encodeURIComponent(candidate.analysis_id)}/confirm`,{
        method:'POST',body:JSON.stringify({expected_candidate_sha256:candidate.candidate_sha256,
          expected_manifest_sha256:data.expected_manifest_sha256,selected_findings:selected()})});
      dialog.close();
      if (state.runId === runId) {await refreshTimeline();await loadMaterialWorkspace();toast('所选观察已用于创作；依赖旧依据的方案需重新检查。');}
    } catch(err) {error.textContent=err.message;}
    finally {saving=false;button.disabled=!selected().length;}
  };
}
function attachImageActions(grid, runId, data, images) {
  if (!images) return;
  for (const observe of grid.querySelectorAll('[data-material-observe]')) {
    const material = data.materials.find(m=>m.name === observe.dataset.materialObserve);
    if (material?.kind !== 'image') continue;
    const card = observe.closest('.material-evidence-card');
    const hasAnalysis = images.candidates.some(c=>c.name === material.name);
    const analyze = box('button',hasAnalysis ? 'ghost small' : 'primary small',hasAnalysis ? '重新分析' : '分析图片');
    analyze.onclick=()=>openImageSetup(runId,material,images,analyze);
    observe.before(analyze);
    for (const candidate of images.candidates.filter(c=>c.name === material.name).reverse()) {
      const panel = document.createElement('div');panel.innerHTML=StudioImages.candidateCard(candidate);
      const review=panel.querySelector('[data-analysis-review]');
      review.onclick=()=>openImageReview(runId,material,candidate,data,review);
      card.append(panel);
    }
  }
}
