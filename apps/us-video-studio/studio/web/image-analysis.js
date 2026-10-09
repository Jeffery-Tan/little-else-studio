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
    const video = material.kind === 'video';
    const native = Object.prototype.hasOwnProperty.call(data, 'video_connection') ? data.video_connection : data.bailian_omni;
    if (video && native?.native_video && data.video?.available !== false) {
      const c = native;
      if (c.has_key === false) return `<div class="analysis-empty"><h3>${esc(c.name)} 视频理解尚未就绪</h3><p>请在模型设置中为所选 ${esc(c.name)} 服务填写 API Key，再开始声画分析。不会自动改用其他服务；原视频已保留。</p><button type="button" class="primary" data-model-settings>打开模型设置</button></div>`;
      const mode = c.provider === 'mimo' ? '默认 2 fps 与默认清晰度；实际分析效果待验证。' : '';
      return `<form class="material-observation-editor" data-analysis-form><p>观察 <strong>${esc(material.name)}</strong> 的画面与声音。商品视频用于补全可见商品信息；参考视频用于拆解表达。</p><label>声画分析服务<select name="connection_id"><option value="${esc(c.id)}">${esc(c.name)}</option></select></label><label>声画模型<input name="model" readonly value="${esc(c.suggested_model)}"></label><label>素材用途<select name="purpose"><option value="product">商品素材 · 检查实际声画</option><option value="reference">参考视频 · 拆解镜头、声音与节奏</option></select></label><div class="analysis-cost" data-analysis-cost></div><label class="analysis-check"><input name="consent" type="checkbox" required><span>我确认将此视频及可用音轨发送至 ${esc(c.name)}，开始一次可能产生费用的分析。</span></label><p class="muted">最长90秒。较大视频先制作保留原音轨的观察副本，原件保留；不发送项目对话。${mode}模型采样理解不保证逐帧验收。结果需选择采用，不会自动进入创作依据。</p><p role="alert" data-analysis-error></p><button type="submit" class="primary">开始声画分析</button></form>`;
    }
    if (video && data.video?.available === false) return '<div class="analysis-empty"><h3>视频观察组件尚未就绪</h3><p>请更新工作台运行依赖后重启服务，再使用视频画面分析。原素材已保留。</p></div>';
    const subject = video ? '最多 12 张带时间点的视频抽帧' : '这张图片';
    const coverage = video ? '视频最长 90 秒；本地提取最多 12 帧，仅发送缩小后的画面及时间点，不发送原视频、音频或项目对话。无法判断帧间动作、对白或完整声画效果。' : '仅发送这张原图及观察要求，不发送项目对话。';
    if (!data.connections.length) return `<div class="analysis-empty"><h3>先连接一个能看图的模型</h3><p>在模型设置中添加 MiMo 或支持图片输入的 OpenAI 兼容、Anthropic 兼容服务，保存密钥后再回来分析。</p><p class="muted">当前 DeepSeek 和 Codex CLI 连接尚未接入此图片分析入口。你仍可查看素材并记录人工观察。</p><button type="button" class="primary" data-model-settings>打开模型设置</button></div>`;
    return `<form class="material-observation-editor" data-analysis-form><p>分析 <strong>${esc(material.name)}</strong>，提取后续创作可用的视觉观察。</p><label>${video ? '画面分析服务' : '图片分析服务'}<select name="connection_id">${data.connections.map(c=>`<option value="${esc(c.id)}">${esc(c.name)}</option>`).join('')}</select></label><label>视觉模型<input name="model" required maxlength="200" placeholder="填写此服务支持图片输入的模型名称" autocomplete="off"></label><p class="muted">兼容接口不代表所有模型都能看图，请使用服务提供方列明的视觉模型。</p><label>素材用途<select name="purpose"><option value="product">商品素材 · 观察可见特征</option><option value="reference">创意参考 · 观察视觉表达</option></select></label><div class="analysis-cost" data-analysis-cost></div><label class="analysis-check"><input name="consent" type="checkbox" required><span>我确认发送${subject}至所选服务，开始一次可能产生费用的分析。</span></label><p class="muted">${coverage}结果需由你选择采用，不会自动进入创作依据。</p><p role="alert" data-analysis-error></p><button type="submit" class="primary">${video ? '开始视频画面分析' : '开始图片分析'}</button></form>`;
  }
  function candidateCard(candidate) {
    const labels = {pending:'分析完成 · 待你确认',accepted:candidate.adoption_source_kind === 'requested_model_reference' ? '参考任务已使用 · 未经人工核对' : '已选择并用于创作',stale:'原素材已变化 · 仅供追溯'};
    const preview = candidate.status === 'accepted' ? candidate.findings[candidate.selected_findings?.[0]] : candidate.findings[0];
    return `<article class="analysis-candidate"><span class="material-status">${esc(labels[candidate.status] || '状态待确认')}</span><p>${esc(preview)}</p><p class="muted">${esc(candidate.connection_name)} · ${esc(candidate.model)}</p><button class="${candidate.status === 'pending' ? 'primary' : 'ghost'} small" data-analysis-review="${esc(candidate.analysis_id)}">${candidate.status === 'pending' ? '核对分析结果' : '查看分析记录'}</button></article>`;
  }
  function review(candidate) {
    const pending = candidate.status === 'pending';
    const automatic = candidate.adoption_source_kind === 'requested_model_reference';
    const methods = (candidate.reference_methods || []).map(m=>`<article class="analysis-limits"><strong>参考机制假设</strong><p>${esc(m.mechanism)}</p><p>依据观察：${m.supports.map(i=>Number(i)+1).join('、')}；只有全部依据被采用，这条假设才会进入创作。</p><p>不能照搬：${esc(m.non_transferable)}</p><p>下一次验证：${esc(m.test_change)}</p></article>`).join('');
    const scope = candidate.audiovisual ? `<p class="muted">已发送 ${esc(candidate.audiovisual.duration_seconds)} 秒视频${candidate.audiovisual.audio_sent ? "及其可用音轨" : "；原文件无音轨"}。声音范围：${esc(JSON.stringify(candidate.audiovisual.audio_ranges))}。时间定位为模型估计，不保证逐帧验收。</p>` : candidate.media_kind === 'video' ? `<p class="muted">实际分析 ${esc(candidate.sampling?.frames?.length)} 个画面；${esc(candidate.sampling?.duration_seconds)} 秒视频。${candidate.sampling?.audio_track_present ? '检测到音轨，但没有发送或听取。' : '未检测到音轨。'}这是抽帧分析，不是完整看听。</p>` : '';
    const cost = candidate.estimated_cny != null ? `本次估算 ¥${Number(candidate.estimated_cny).toFixed(6)}` : candidate.estimated_usd == null ? '费用待核实' : `本次估算 $${Number(candidate.estimated_usd).toFixed(6)}`;
    return `<form class="analysis-review" data-review-form><p>${pending ? '对照素材，勾选你认可的观察。未选内容不会提供给创作团队。' : automatic ? '本次参考任务自动使用了这些模型观察，勾选不表示人工确认。' : '这是原始分析记录，勾选标记表示你此前采用的观察。'}</p><details class="analysis-source"><summary>分析来源与费用 · ${cost}</summary><p>${esc(candidate.connection_name)} · ${esc(candidate.model)}</p><p>${esc(new Date(candidate.observed_at).toLocaleString('zh-CN'))}</p></details><fieldset ${pending ? '' : 'disabled'}><legend>可见观察</legend>${candidate.findings.map((finding,index)=>`<label class="analysis-check"><input type="checkbox" name="finding" value="${index}" ${candidate.selected_findings?.includes(index) ? 'checked' : ''}><span>${esc(finding)}</span></label>`).join('')}</fieldset>${scope}${methods}<div class="analysis-limits"><strong>这些内容仍然未知</strong><ul>${candidate.limitations.map(l=>`<li>${esc(l)}</li>`).join('')}</ul></div><p class="muted">采用后会保留模型来源、素材版本及你的选择，并提醒依赖旧依据的方案重新检查。这不是商品性能验证，也不是制作批准。</p><p role="alert" data-analysis-error></p>${pending ? '<div class="analysis-actions"><button type="submit" class="primary" disabled>采用所选观察</button></div>' : `<p class="material-status">${candidate.status === 'accepted' ? automatic ? '模型观察未经人工核对；需要修正时可补充人工观察。' : '这份分析已保存你的选择；需要修正时可补充人工观察。' : '素材已变化，这份结果不能再采用。'}</p>`}</form>`;
  }
  function costText(connection, model, kind='image') {
    const price = connection?.pricing?.[model];
    if (connection?.native_video) {
      const rates = price?.input_cny_per_mtok != null && price?.output_cny_per_mtok != null
        ? `人民币单价：输入 ¥${price.input_cny_per_mtok} / 百万 token，输出 ¥${price.output_cny_per_mtok} / 百万 token。`
        : price?.input_usd_per_mtok != null && price?.output_usd_per_mtok != null
          ? `已登记单价：输入 $${price.input_usd_per_mtok} / 百万 token，输出 $${price.output_usd_per_mtok} / 百万 token。`
          : '此模型尚无已核实单价，费用将显示为待核实。';
      return `${rates}包含视频画面与可用音轨；本次最多输出${connection.provider === 'mimo' ? 8192 : 4096} token（含思考），不自动重试。实际用量以返回记录为准。接收地址：${connection.base_url}`;
    }
    const rates = price?.input_usd_per_mtok != null && price?.output_usd_per_mtok != null
      ? `已登记单价：输入 $${price.input_usd_per_mtok} / 百万 token，输出 $${price.output_usd_per_mtok} / 百万 token。`
      : '此模型尚无已核实单价，费用将显示为待核实。';
    return `${rates} 图片计费取决于服务与模型；本次仅调用一次，最多输出 ${kind === 'video' ? 4096 : 2048} token，不自动重试。接收地址：${connection?.base_url || ''}`;
  }
  root.StudioImages = {enrich, setup, candidateCard, review, costText};
  if (typeof module !== 'undefined') module.exports = root.StudioImages;
})(typeof window !== 'undefined' ? window : globalThis);
