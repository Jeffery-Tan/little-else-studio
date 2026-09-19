"use strict";

function progressHtml(progress, selectedStage, rail = []) {
  if (!progress) return '';
  const target = rail.find(s => s.stage === progress.stage);
  const elsewhere = progress.stage && selectedStage !== progress.stage;
  return `<section class="project-progress" aria-label="项目下一步"><div><span class="progress-eyebrow">${escapeHtml(progress.completed)} / ${escapeHtml(progress.total)} 步已就绪</span>` +
    `<h2>${escapeHtml(progress.title)}</h2><p>${escapeHtml(progress.detail)}</p>` +
    (elsewhere ? `<p class="progress-prerequisite">继续前，先处理「${escapeHtml(target?.label || '当前阶段')}」。</p>` : '') +
    `</div><button class="primary" data-progress-action="${escapeHtml(progress.action)}">${escapeHtml(progress.label)}</button></section>`;
}

function deliveryHtml(data) {
  const names = { intake:'创作要求', direction:'创意方向', material_plan:'素材策划', writing:'脚本与分镜' };
  return `<p class="delivery-status">${data.ready ? '本次策划已就绪' : '这是进行中的策划资料'} · ${escapeHtml(data.run.title)}</p>` +
    `<p class="muted">${escapeHtml(data.notice)}</p>` +
    (data.missing.length ? `<div class="delivery-missing"><strong>仍需完成</strong><p>${data.missing.map(escapeHtml).join(' · ')}</p></div>` : '') +
    `<div class="delivery-documents">${data.documents.map(d => `<article><div><h3>${escapeHtml(d.label)}</h3><span>版本 ${escapeHtml(d.version)} · ${escapeHtml(d.status)}</span></div><button class="ghost small" data-delivery-read="${escapeHtml(d.stage)}" data-version="${escapeHtml(d.version)}">阅读</button></article>`).join('') || '<p>还没有保存的策划内容。可以先整理创作要求。</p>'}</div>` +
    `<div class="delivery-download"><a class="primary" href="/api/runs/${encodeURIComponent(data.run.run_id)}/delivery/download" download>下载当前策划资料 ZIP</a><span>含版本清单与策划原文；不会发起模型调用。</span></div>` +
    `<h3>制作文件</h3>${data.packages.length ? data.packages.map(p => `<section class="delivery-package"><strong>${escapeHtml(names[p.stage] || '制作资料')} · 版本 ${escapeHtml(p.version)}</strong><p class="muted">按来源版本归档，请与上方当前版本核对。</p><div>${p.files.map(f => `<a href="${escapeHtml(f.url)}" target="_blank" rel="noopener">${escapeHtml(f.name)}</a>`).join('')}</div></section>`).join('') : '<p class="muted">尚未整理制作文件。策划完成后，可让制片团队根据当前版本编译制作包。</p>'}`;
}

function setProjectFocus(focus) {
  document.getElementById('project-workspace').dataset.focus = focus;
  for (const node of document.querySelectorAll('[data-project-focus]')) node.setAttribute('aria-pressed', String(node.dataset.projectFocus === focus));
}

function prepareProjectDraft(text) {
  if (!state.runId || state.decisionBusy) return;
  const input = $('chat-input');
  // Preserve unsent user writing, including on repeated clicks.
  if (!input.value.includes(text)) input.value = [input.value.trim(), text].filter(Boolean).join('\n\n');
  setProjectFocus('chat');
  input.focus();
  toast('要求已放入对话框，检查后发送即可。');
}

function renderProjectProgress() {
  const host = $('project-progress');
  if (!host) return;
  const runId = state.runId, timeline = state.timeline;
  let progress = state.timeline?.progress;
  if (!progress) { host.innerHTML = ''; return; }
  if (typeof currentRecovery !== 'undefined' && currentRecovery && !currentRecovery.can_continue) {
    progress = {...progress,action:'recovery',label:'查看运行状态',
      title:currentRecovery.unresolved_calls?.length ? '有模型用量需要核实' : '先处理未完成的制作',
      detail:'已有内容已保留，请在制片对话中查看当前执行与恢复事项。'};
  }
  host.innerHTML = progressHtml(progress, state.activeStage, state.rail);
  const button = host.querySelector('[data-progress-action]');
  if (!button) return;
  button.onclick = () => {
    if (state.runId !== runId || state.timeline !== timeline || state.decisionBusy) return;
    if (progress.action === 'delivery') return openDelivery();
    if (progress.action === 'recovery') { setProjectFocus('chat'); $('job-status').scrollIntoView({block:'nearest'}); return; }
    if (progress.stage) selectStage(progress.stage);
    if (progress.action === 'draft') prepareProjectDraft(progress.prompt);
    else {
      setProjectFocus('canvas');
      const target = progress.action === 'review' ? document.getElementById('stage-decision') : $('stage');
      target?.scrollIntoView({block:'start',behavior:'smooth'});
      if (target) { target.tabIndex = -1; target.focus({preventScroll:true}); }
    }
  };
}

async function openDelivery() {
  if (!state.runId || document.querySelector('.delivery-dialog')) return;
  const runId = state.runId, opener = document.activeElement;
  const dialog = document.createElement('dialog');
  dialog.className = 'delivery-dialog'; dialog.setAttribute('aria-labelledby','delivery-title');
  dialog.innerHTML = '<header class="delivery-heading"><h2 id="delivery-title">本次交付</h2><button class="ghost" data-delivery-close>关闭</button></header><div class="delivery-content" role="status">正在整理版本与文件…</div>';
  document.body.append(dialog);
  dialog.querySelector('[data-delivery-close]').onclick = () => dialog.close();
  dialog.addEventListener('close',()=>{dialog.remove();opener?.focus();});
  dialog.showModal();
  const content = dialog.querySelector('.delivery-content');
  async function load() {
    try {
      const data = await api(`/api/runs/${encodeURIComponent(runId)}/delivery`);
      if (!dialog.open) return;
      content.innerHTML = deliveryHtml(data);
      for (const button of content.querySelectorAll('[data-delivery-read]')) button.onclick = () => {
        if (state.runId !== runId) { content.textContent = '当前项目已切换，请关闭后重新打开交付。'; return; }
        dialog.close(); openReader(button.dataset.deliveryRead, Number(button.dataset.version)).catch(err=>toast(err.message,true));
      };
    } catch (err) {
      if (!dialog.open) return;
      content.replaceChildren(box('p','form-error','暂时无法读取交付资料：' + err.message));
      const retry = box('button','ghost','重新读取'); retry.onclick = load; content.append(retry);
    }
  }
  await load();
}

if (typeof module !== 'undefined') module.exports = { progressHtml, deliveryHtml };
