/* Project accounting is a read-only view of effective ledger entries. */
(function(root) {
  'use strict';
  const roles = {orchestrator:'主制片',director:'创意导演',material:'视觉策划',writer:'编剧',reviewer:'独立审片'};
  const stages = {intake:'需求整理',direction:'创意方向',material_plan:'素材规划',writing:'脚本创作',image_analysis:'图片分析',video_analysis:'视频观察',image_generation:'图片生成',video_generation:'视频生成',production_review:'制作检查'};
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const numeric = value => typeof value === 'number' && Number.isFinite(value) && value >= 0;
  const count = value => numeric(value) && Number.isInteger(value);
  const number = value => Number(value).toLocaleString('zh-CN');
  // Only approved presentation fields are used. Error text, URLs, endpoint and
  // request metadata are deliberately absent from both the UI and CSV.
  function label(value, fallback='未标明') {
    const text = String(value ?? '').trim();
    if (!text || /(?:https?:\/\/|bearer\s|sk-[\w-]{8,}|api[_-]?key\s*[:=])/i.test(text)) return fallback;
    return text.slice(0,140);
  }
  function status(row) {
    if (row.call_status === 'pending') return '未返回用量';
    if (row.call_status === 'reconciled') return '已核账 · 执行未完成';
    if (row.ok === true) return '调用完成';
    if (/cancel|interrupt|abort/i.test(String(row.stop_reason || ''))) return '已停止';
    return '执行未完成';
  }
  function summarize(rows) {
    const summary = {calls:0,input:0,output:0,knownCost:0,knownCostCalls:0,unknownCost:0,unknownUsage:0,pending:0,incomplete:0};
    for (const row of rows) {
      summary.calls++;
      if (count(row.input_tokens)) summary.input += row.input_tokens;
      if (count(row.output_tokens)) summary.output += row.output_tokens;
      if (row.usage_status !== 'known' || (row.usage_unit && row.usage_unit !== 'tokens' ? !numeric(row.usage_quantity) : !count(row.input_tokens) || !count(row.output_tokens)) || row.call_status === 'pending') summary.unknownUsage++;
      // The contract names the currency explicitly: estimated_usd. Never add
      // other amounts/currencies or apply a live connection's prices to history.
      if (numeric(row.estimated_usd)) { summary.knownCost += row.estimated_usd; summary.knownCostCalls++; }
      else summary.unknownCost++;
      if (row.call_status === 'pending') summary.pending++;
      else if (row.ok !== true) summary.incomplete++;
    }
    return summary;
  }
  function groups(rows, by) {
    const result = new Map();
    for (const row of rows) {
      const model = label(row.actual_model || row.model,'未标明模型');
      const provider = row.provider === 'codex_cli' ? 'Codex CLI' : label(row.provider,'未标明服务');
      const key = by === 'role' ? (roles[row.role] || '其他角色') : `${model} · ${provider}`;
      if (!result.has(key)) result.set(key,[]);
      result.get(key).push(row);
    }
    return [...result].map(([name,items])=>({name,...summarize(items)}));
  }
  function dollars(value) { return `US$ ${value.toFixed(6).replace(/0+$/,'').replace(/\.$/,'.00')}`; }
  function amount(summary) {
    if (!summary.calls) return '暂无调用';
    if (!summary.knownCostCalls) return '金额未知';
    return `${dollars(summary.knownCost)}${summary.unknownCost ? ' + 待计金额' : ''}`;
  }
  function groupTable(rows,by) {
    return `<div class="cost-table-wrap"><table class="cost-table"><thead><tr><th>${by==='role'?'角色':'模型与服务'}</th><th>调用</th><th>已记录 Tokens</th><th>估算金额（USD）</th></tr></thead><tbody>${groups(rows,by).map(g=>`<tr><th scope="row">${esc(g.name)}</th><td>${g.calls}</td><td>${number(g.input+g.output)}${g.unknownUsage?' · 未齐':''}</td><td>${esc(amount(g))}${g.unknownCost?`<small>${g.unknownCost} 次未计入美元金额</small>`:''}</td></tr>`).join('')}</tbody></table></div>`;
  }
  function render(data) {
    const rows = Array.isArray(data.rows) ? data.rows : [];
    const s = summarize(rows);
    const media = rows.filter(r=>['images','video_seconds'].includes(r.usage_unit));
    const cny = media.filter(r=>numeric(r.estimated_cny));
    const omniCny = rows.filter(r=>r.provider === 'bailian_omni' && numeric(r.estimated_cny));
    const omniNote = omniCny.length ? `<div class="cost-notice"><strong>百炼声画分析 · 人民币</strong><p>${omniCny.length} 次有完整计费记录，人民币已知估算 CNY ${omniCny.reduce((n,r)=>n+r.estimated_cny,0).toFixed(6)}。缓存按原价保守估算，实际金额以百炼账单为准；不与美元相加。</p></div>` : '';
    const mediaNote = media.length ? `<div class="cost-notice"><strong>媒体制作 · 单独计量</strong><p>图片 ${media.filter(r=>r.usage_unit==='images').length} 次任务 · 视频 ${media.filter(r=>r.usage_unit==='video_seconds').length} 次任务。视频人民币已知估算：${cny.length?`CNY ${cny.reduce((n,r)=>n+r.estimated_cny,0).toFixed(2)}`:'未知'}。</p><p>人民币费用不与美元相加，也不受文字模型美元预算限制。图片/视频用量不换算为 Tokens；实际费用请在提供方平台查看。</p></div>` : '';
    return `<div class="cost-heading"><div><p class="cost-eyebrow">当前项目</p><h2>用量与费用</h2></div><button type="button" class="ghost small" data-cost-close aria-label="关闭用量与费用">关闭</button></div>
      <div class="cost-metrics"><div><span>模型调用</span><strong>${s.calls}</strong><small>${s.pending ? `${s.pending} 次等待确认` : '已记录的请求次数'}</small></div><div><span>已记录 Tokens</span><strong>${number(s.input+s.output)}</strong><small>输入 ${number(s.input)} · 输出 ${number(s.output)}${s.unknownUsage?' · 用量未齐':''}</small></div><div><span>已知估算金额</span><strong>${s.calls && !s.knownCostCalls?'金额未知':dollars(s.knownCost)}</strong><small>${s.unknownCost?`另有 ${s.unknownCost} 次金额未知`:'USD · 不是最终账单'}</small></div></div>
      ${mediaNote}${omniNote}${!s.calls?'<p class="cost-message">这个项目还没有模型调用。资料整理和本地编辑不会产生模型调用。</p>':''}
      ${s.unknownUsage?`<div class="cost-notice"><strong>${s.unknownUsage} 次未返回完整用量</strong><p>仅作运行记录，不影响继续。实际费用请在提供方平台查看。</p></div>`:''}
      ${s.incomplete?`<p class="cost-message">${s.incomplete} 次执行未完成。失败、停止或中断仍可能产生费用，已记录用量会保留。</p>`:''}
      ${rows.length?`<div class="cost-tabs" role="group" aria-label="费用分组"><button type="button" class="ghost small" data-cost-group="role" aria-pressed="true">按角色</button><button type="button" class="ghost small" data-cost-group="model" aria-pressed="false">按模型</button></div><div data-cost-groups>${groupTable(rows,'role')}</div>
      <details class="cost-details"><summary>查看 ${rows.length} 次调用明细</summary><div class="cost-table-wrap"><table class="cost-table"><thead><tr><th>时间 / 角色</th><th>模型 / 进度</th><th>输入 / 输出 Tokens</th><th>估算金额（USD）</th></tr></thead><tbody>${rows.slice().reverse().map(row=>`<tr><td>${esc(label(row.at))}<small>${esc(roles[row.role]||'其他角色')} · ${esc(stages[row.stage]||'创作调用')}</small></td><td>${esc(label(row.actual_model||row.model,'未标明模型'))}<small>${esc(status(row))}</small></td><td>${['images','video_seconds'].includes(row.usage_unit)?`${numeric(row.usage_quantity)?number(row.usage_quantity):'待核实'} ${row.usage_unit==='images'?'张（提供方用量）':'秒'}<small>不按 Tokens 计量</small>`:`${count(row.input_tokens)?number(row.input_tokens):'未知'} / ${count(row.output_tokens)?number(row.output_tokens):'未知'}`}${row.usage_status!=='known'?'<small>用量未返回</small>':''}</td><td>${numeric(row.estimated_usd)?dollars(row.estimated_usd):numeric(row.estimated_cny)?`CNY ${row.estimated_cny.toFixed(6)}<small>人民币单独记录</small>`:'未知'}</td></tr>`).join('')}</tbody></table></div><button type="button" class="ghost small" data-cost-export>导出费用记录 CSV</button></details>`:''}
      <details class="cost-details"><summary>费用如何计算</summary><p>仅累加每次调用当时记录的美元估算；不将未知金额补为零，也不跨币种相加。金额未知与用量未返回是两回事：完整用量已记录但价格未配置时，金额仍会未知。</p><p>Codex CLI 使用订阅或账户额度，本工作台不分摊订阅费用；金额未知不代表免费。金额、调用次数与 Tokens 都不是提供方剩余额度。</p><p>预算是停止后续请求的软阈值，不能保证最终账单不超出。最终费用请以提供方账单为准。</p></details>`;
  }
  function csvCell(value) {
    let text = String(value ?? '');
    // Spreadsheet formula injection can hide after spaces/control characters.
    if (/^[\s\u0000-\u001f]*[=+\-@]/.test(text)) text = `'${text}`;
    return `"${text.replace(/"/g,'""')}"`;
  }
  function csv(rows) {
    const records = [['时间','角色','阶段','服务','请求模型','实际模型','执行状态','用量状态','输入 Tokens','输出 Tokens','已知估算金额','币种']];
    for (const row of rows) records.push([label(row.at),roles[row.role]||'其他角色',stages[row.stage]||'创作调用',label(row.provider),label(row.model),label(row.actual_model,''),status(row),row.usage_status==='known'?'已知':'待核实',count(row.input_tokens)?row.input_tokens:'未知',count(row.output_tokens)?row.output_tokens:'未知',numeric(row.estimated_usd)?row.estimated_usd:numeric(row.estimated_cny)?row.estimated_cny:'未知',numeric(row.estimated_cny) && !numeric(row.estimated_usd)?'CNY':'USD']);
    return '\ufeff'+records.map(values=>values.map(csvCell).join(',')).join('\r\n');
  }
  async function mount(panel,options) {
    const {runId,isCurrent=()=>true,onRecover=()=>{}} = options;
    const token = {};
    panel.costRequest = token;
    panel.classList.add('project-cost-panel');
    panel.innerHTML = '<p role="status">正在读取项目费用…</p><button type="button" class="ghost small" data-cost-close>关闭</button>';
    const valid = ()=>panel.costRequest===token && isCurrent(runId);
    const close = ()=>{panel.hidden=true;};
    panel.querySelector('[data-cost-close]').onclick=close;
    try {
      const response = await fetch(`/api/runs/${encodeURIComponent(runId)}/ledger`);
      if (!response.ok) throw new Error('ledger unavailable');
      const data = await response.json();
      if (!Array.isArray(data.rows)) throw new Error('invalid ledger');
      if (!valid()) return;
      panel.innerHTML = render(data);
      panel.querySelector('[data-cost-close]').onclick=close;
      const recovery = panel.querySelector('[data-cost-recover]');
      if (recovery) recovery.onclick=()=>{if(valid()){close();onRecover(runId);}};
      panel.querySelectorAll('[data-cost-group]').forEach(button=>button.onclick=()=>{
        if(!valid()) return;
        panel.querySelectorAll('[data-cost-group]').forEach(b=>b.setAttribute('aria-pressed',String(b===button)));
        panel.querySelector('[data-cost-groups]').innerHTML=groupTable(data.rows,button.dataset.costGroup);
      });
      const download=panel.querySelector('[data-cost-export]');
      if(download) download.onclick=()=>{
        if(!valid()) return;
        const url=URL.createObjectURL(new Blob([csv(data.rows)],{type:'text/csv;charset=utf-8'}));
        const link=document.createElement('a');link.href=url;link.download=`project-costs-${String(runId).replace(/[^a-z0-9-]/gi,'')}.csv`;link.click();
        setTimeout(()=>URL.revokeObjectURL(url),1000);
      };
    } catch (_) {
      if(!valid()) return;
      panel.innerHTML='<p role="alert">暂时无法读取项目费用，已有记录不会丢失。</p><button type="button" class="ghost small" data-cost-retry>重新读取</button> <button type="button" class="ghost small" data-cost-close>关闭</button>';
      panel.querySelector('[data-cost-close]').onclick=close;
      panel.querySelector('[data-cost-retry]').onclick=()=>mount(panel,options);
    }
  }
  const api={summarize,groups,amount,status,render,csv,csvCell,mount};
  if(typeof module!=='undefined' && module.exports) module.exports=api;
  else root.StudioProjectCosts=api;
})(typeof globalThis!=='undefined'?globalThis:this);
