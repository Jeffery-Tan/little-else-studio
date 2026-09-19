/* Material evidence UI. Upload and human observations are deliberately distinct. */
"use strict";
(function (root) {
  const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const list = value => Array.isArray(value) ? value : [];
  const levels = {image_observed:"查看图片",frames_observed:"查看部分画面",transcript_only:"仅记录台词",audiovisual_review:"观看视频并听声音"};
  const purposes = {product:"商品素材",reference:"创意参考"};
  function preview(item) {
    // Only accept the run-local media route; imported metadata cannot load external URLs.
    const url = typeof item.url === "string" && /^\/api\/runs\/[a-z0-9][a-z0-9-]*\/materials\/[^/?#]+$/.test(item.url) ? item.url : "";
    if (!url) return '<p class="muted">无法预览此素材</p>';
    return item.kind === "image" ? `<img src="${esc(url)}" alt="${esc(item.name)}" loading="lazy">` : `<video src="${esc(url)}" controls playsinline preload="metadata" aria-label="${esc(item.name)}"></video>`;
  }
  function recordHtml(record) {
    return `<article class="material-observation"><p><strong>${esc(purposes[record.purpose] || "未分类的导入观察")}</strong> · ${esc(levels[record.level] || "观察记录")}</p><p class="muted">来源：${esc(record.source_kind === "human_observation" ? "工作台用户手动填写" : record.observer)} · ${esc(record.observed_at)}</p><p>观察范围：${esc(record.coverage)}</p><ul>${list(record.findings).map(s=>`<li>${esc(s)}</li>`).join("")}</ul><p class="muted">未知与限制：${list(record.limitations).map(esc).join("；") || "原记录未注明"}</p></article>`;
  }
  function render(data) {
    return `<div class="material-understanding"><p class="muted">${esc(data.notice)}</p>${list(data.errors).length ? '<p role="status" class="material-warning">部分旧观察无法与当前素材匹配，未提供给模型。请查看对应素材并补充新记录。</p>' : ""}<div class="material-evidence-grid">${list(data.materials).map(item=>`<article class="material-evidence-card"><div class="material-evidence-preview">${preview(item)}</div><h4>${esc(item.label || item.name)}</h4>${item.origin_notice ? `<p class="muted">${esc(item.origin_notice)}</p>` : ""}<p class="material-status">${esc(item.status_label)}</p><button type="button" class="ghost" data-material-observe="${esc(item.name)}">${list(item.observations).length ? "补充观察" : "查看并记录观察"}</button>${list(item.observations).length ? `<details><summary>已记录 ${item.observations.length} 条观察</summary>${item.observations.map(recordHtml).join("")}</details>` : `<p class="muted">${esc(item.empty_notice || '尚无内容观察；文件存在不能证明商品特征或视频效果。')}</p>`}</article>`).join("")}</div></div>`;
  }
  function observationForm(item) {
    const options = item.kind === "image" ? ["image_observed"] : ["frames_observed", "transcript_only", "audiovisual_review"];
    return `<div class="material-observation-editor"><div class="material-evidence-preview">${preview(item)}</div><p>查看后记录你实际观察到的内容。这些文字会作为人工提供的素材证据，供后续创作使用。</p><label>素材用途<select name="purpose" required><option value="product">商品素材</option><option value="reference">创意参考（不作为商品事实）</option></select></label><label>观察方式<select name="level">${options.map(level=>`<option value="${level}">${levels[level]}</option>`).join("")}</select></label><label>观察范围<input name="coverage" required maxlength="500" placeholder="${item.kind === 'image' ? '例如：正面图片，可见提手与拉链' : '例如：00:02–00:08；只看了这段画面'}"></label><label>实际看到或听到什么<textarea name="findings" required maxlength="12000" rows="5" placeholder="每行一条，区分可见内容和推测"></textarea></label><label>未知、未覆盖或不确定的内容<textarea name="limitations" required maxlength="12000" rows="3" placeholder="例如：看不到背面；没有尺寸证据；没有听声音"></textarea></label><p class="muted">保存会保留此前观察；依赖旧素材记录的内容需要重新检查。此操作不调用模型、不产生费用，也不批准制作。</p><p role="alert" data-material-error></p><button type="submit" class="primary">保存人工观察</button></div>`;
  }
  function formPayload(form, item, data) {
    const field = name => String(form.elements.namedItem(name)?.value || "").trim();
    return {name:item.name,expected_sha256:item.sha256,expected_manifest_sha256:data.expected_manifest_sha256 ?? null,
      purpose:field("purpose"),level:field("level"),coverage:field("coverage"),
      findings:field("findings").split(/\r?\n/).map(s=>s.trim()).filter(Boolean),
      limitations:field("limitations").split(/\r?\n/).map(s=>s.trim()).filter(Boolean)};
  }
  root.StudioMaterials = {render, observationForm, formPayload};
  if (typeof module !== "undefined") module.exports = root.StudioMaterials;
})(typeof window !== "undefined" ? window : globalThis);
