/* Pure, escaped rendering for explicit image calls and human selection. */
"use strict";
(function(root) {
  const esc = value => String(value ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  function enrich(data, images) {
    if (!images) return data;
    return {...data, materials:data.materials.map(item=>{
      const pending=images.candidates.some(c=>c.name===item.name && c.sha256===item.sha256 && c.status==='pending');
      return pending ? {...item,status_label:'分析完成 · 待你确认',empty_notice:'分析结果尚未确认，暂未提供给创作团队。'} : item;
    })};
  }
  function setup(material, data) {
    if (!data.connections.length) return `<div class="analysis-empty"><h3>先连接一个能看图的模型</h3><p>在模型设置中添加支持图片输入的 OpenAI 兼容或 Anthropic 兼容服务，保存密钥后再回来分析。</p><p class="muted">当前 DeepSeek 和 Codex CLI 连接尚未接入此图片分析入口。你仍可查看素材并记录人工观察。</p><button type="button" class="primary" data-model-settings>打开模型设置</button></div>`;
    return `<form class="material-observation-editor" data-analysis-form><p>分析 <strong>${esc(material.name)}</strong>，提取后续创作可用的视觉观察。</p><label>图片分析服务<select name="connection_id">${data.connections.map(c=>`<option value="${esc(c.id)}">${esc(c.name)}</option>`).join('')}</select></label><label>视觉模型<input name="model" required maxlength="200" placeholder="填写此服务支持图片输入的模型名称" autocomplete="off"></label><p class="muted">兼容接口不代表所有模型都能看图，请使用服务提供方列明的视觉模型。</p><label>素材用途<select name="purpose"><option value="product">商品素材 · 观察可见特征</option><option value="reference">创意参考 · 观察视觉表达</option></select></label><div class="analysis-cost" data-analysis-cost></div><label class="analysis-check"><input name="consent" type="checkbox" required><span>我确认发送这张图片至所选服务，开始一次可能产生费用的分析。</span></label><p class="muted">仅发送这张原图及观察要求，不发送项目对话。结果需由你选择采用，不会自动进入创作依据。</p><p role="alert" data-analysis-error></p><button type="submit" class="primary">开始图片分析</button></form>`;
  }
  function candidateCard(candidate) {
    const labels = {pending:'分析完成 · 待你确认',accepted:'已选择并用于创作',stale:'原图片已变化 · 仅供追溯'};
    const preview = candidate.status === 'accepted' ? candidate.findings[candidate.selected_findings?.[0]] : candidate.findings[0];
    return `<article class="analysis-candidate"><span class="material-status">${esc(labels[candidate.status] || '状态待确认')}</span><p>${esc(preview)}</p><p class="muted">${esc(candidate.connection_name)} · ${esc(candidate.model)}</p><button class="${candidate.status === 'pending' ? 'primary' : 'ghost'} small" data-analysis-review="${esc(candidate.analysis_id)}">${candidate.status === 'pending' ? '核对分析结果' : '查看分析记录'}</button></article>`;
  }
  function review(candidate) {
    const pending = candidate.status === 'pending';
    const cost = candidate.estimated_usd == null ? '费用待核实' : `本次估算 $${Number(candidate.estimated_usd).toFixed(6)}`;
    return `<form class="analysis-review" data-review-form><p>${pending ? '对照图片，勾选你认可的观察。未选内容不会提供给创作团队。' : '这是原始分析记录，勾选标记表示你此前采用的观察。'}</p><details class="analysis-source"><summary>分析来源与费用 · ${cost}</summary><p>${esc(candidate.connection_name)} · ${esc(candidate.model)}</p><p>${esc(new Date(candidate.observed_at).toLocaleString('zh-CN'))}</p></details><fieldset ${pending ? '' : 'disabled'}><legend>可见观察</legend>${candidate.findings.map((finding,index)=>`<label class="analysis-check"><input type="checkbox" name="finding" value="${index}" ${candidate.selected_findings?.includes(index) ? 'checked' : ''}><span>${esc(finding)}</span></label>`).join('')}</fieldset><div class="analysis-limits"><strong>这些内容仍然未知</strong><ul>${candidate.limitations.map(l=>`<li>${esc(l)}</li>`).join('')}</ul></div><p class="muted">采用后会保留模型来源、图片版本及你的选择，并提醒依赖旧依据的方案重新检查。这不是商品性能验证，也不是制作批准。</p><p role="alert" data-analysis-error></p>${pending ? '<div class="analysis-actions"><button type="submit" class="primary" disabled>采用所选观察</button></div>' : `<p class="material-status">${candidate.status === 'accepted' ? '这份分析已保存你的选择；需要修正时可补充人工观察。' : '图片已变化，这份结果不能再采用。'}</p>`}</form>`;
  }
  function costText(connection, model) {
    const price = connection?.pricing?.[model];
    const rates = price?.input_usd_per_mtok != null && price?.output_usd_per_mtok != null
      ? `已登记单价：输入 $${price.input_usd_per_mtok} / 百万 token，输出 $${price.output_usd_per_mtok} / 百万 token。`
      : '此模型尚无已核实单价，费用将显示为待核实。';
    return `${rates} 图片计费取决于服务与模型；本次仅调用一次，最多输出 2048 token，不自动重试。接收地址：${connection?.base_url || ''}`;
  }
  root.StudioImages = {enrich, setup, candidateCard, review, costText};
  if (typeof module !== 'undefined') module.exports = root.StudioImages;
})(typeof window !== 'undefined' ? window : globalThis);
