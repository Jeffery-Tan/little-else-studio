/* Published work and user-reported observations. No publishing or model calls. */
"use strict";
(function(root){
  const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const metrics={views:'播放',likes:'点赞',clicks:'商品点击',orders:'归因订单',revenue_usd:'归因销售额（USD）',spend_usd:'广告花费（USD）'};
  const traffic={organic:'自然流量',paid:'付费流量',mixed:'混合流量',unknown:'流量来源未知'};
  const date=value=>value?new Date(value).toLocaleString('zh-CN',{hour12:false}):'未提供';
  function metric(value,money=false){return value==null?'未提供':money?`$${Number(value).toFixed(2)}`:Number(value).toLocaleString('zh-CN');}
  function numeric(value){if(String(value).trim()==='')return null;const n=Number(value);if(!Number.isFinite(n)||n<0)throw new Error('请填写非负数字，未知项留空。');return n;}
  function iso(value){if(!value||!Number.isFinite(new Date(value).getTime()))throw new Error('请填写完整日期和时间。');return new Date(value).toISOString();}
  function safeUrl(value){try{const u=new URL(value);return u.protocol==='https:'&&!u.username&&!u.password?u.href:'';}catch(_){return '';}}
  function submission(kind,values,context,requestId){
    const result={request_id:requestId,...values};
    if(kind==='publication'){
      const a=context.find(a=>a.version===Number(values.version)&&a.integrity_ok);
      if(!a)throw new Error('请重新核对关联的脚本版本。');
      if(!safeUrl(values.url))throw new Error('请填写 HTTPS 作品链接，不要包含登录凭据。');
      return {...result,version:a.version,artifact_sha256:a.artifact_sha256,published_at:iso(values.published_at)};
    }
    result.expected_latest_id=(context.snapshots||[]).at(-1)?.snapshot_id||null;
    for(const key of ['window_start','window_end','observed_at'])result[key]=iso(values[key]);
    for(const key of Object.keys(metrics)){result[key]=numeric(values[key]??'');if(!key.endsWith('_usd')&&result[key]!==null&&!Number.isSafeInteger(result[key]))throw new Error('播放、点赞、点击和订单请填写整数。');}
    return result;
  }
  function snapshotHtml(s){
    return `<section class="outcome-snapshot"><p class="outcome-window">${esc(date(s.window_start))} — ${esc(date(s.window_end))} · ${esc(traffic[s.traffic]||s.traffic)}</p><div class="outcome-metrics">${Object.entries(metrics).map(([key,label])=>`<div><span>${label}</span><strong>${metric(s[key],key.endsWith('_usd'))}</strong></div>`).join('')}</div><p class="form-note">观察于 ${esc(date(s.observed_at))} · 用户提供，未自动核验</p><h4>数据来源</h4><p class="outcome-text">${esc(s.source)}</p><h4>观察与限制</h4><p class="outcome-text">${esc(s.notes)}</p></section>`;
  }
  function publicationHtml(p){const list=p.snapshots||[],latest=list.at(-1),url=safeUrl(p.url);return `<article class="outcome-publication"><header><div><h3>${esc(p.title)}</h3><p>脚本与分镜 · 版本 ${esc(p.version)} · 发布于 ${esc(date(p.published_at))}</p></div>${url?`<a class="ghost small" href="${esc(url)}" target="_blank" rel="noopener noreferrer">查看作品 ↗</a>`:'<span>作品链接不可用</span>'}</header>${p.source_integrity_ok===false?'<p class="form-error">关联稿件暂时无法核对，以下为历史记录；核对来源后再补充或复盘。</p>':''}<details><summary>与策划稿的关系</summary><p class="outcome-text">${esc(p.differences)}</p><p class="form-note">这是用户登记的关联，不代表已核验成片与稿件一致。</p></details>${latest?snapshotHtml(latest):'<p class="outcome-empty">尚未记录作品表现。没有数据时保持待验证。</p>'}<div class="outcome-actions"><button class="ghost" data-outcome-observe="${esc(p.publication_id)}" ${p.source_integrity_ok===false?'disabled':''}>${latest?'补充 / 更正表现':'记录表现'}</button>${latest?`<button class="ghost" data-outcome-reflect="${esc(p.publication_id)}" ${p.source_integrity_ok===false?'disabled':''}>带到制片对话</button>`:''}</div>${list.length>1?`<details class="outcome-history"><summary>此前的 ${list.length-1} 次记录</summary><p class="form-note">原记录保留；统计区间、流量及口径可能不同，不自动计算趋势。</p>${list.slice(0,-1).reverse().map(snapshotHtml).join('')}</details>`:''}</article>`;}
  function reflection(p){if(p.source_integrity_ok===false)throw new Error('关联稿件暂时无法核对，请先检查来源。');const s=(p.snapshots||[]).at(-1);if(!s)throw new Error('先记录作品表现。');return `请结合这条作品的实际表现复盘后续创作，先诊断，不自动改稿或调用生成工具。\n作品：${p.title}\n链接：${p.url}\n关联脚本版本：${p.version}\n实际作品与稿件的差异：${p.differences}\n统计区间：${s.window_start} 至 ${s.window_end}\n流量：${traffic[s.traffic]}\n${Object.entries(metrics).map(([k,l])=>`${l}：${metric(s[k],k.endsWith('_usd'))}`).join('\n')}\n来源：${s.source}\n观察与限制：${s.notes}\n记录：${s.snapshot_id}\n以上为用户报告，未经自动核验；请区分内容、流量和交易归因，不将单次结果推广为通用方法。`;}
  function restored(draft,publications,p){
    const payload=draft.payload||null;
    const exists=payload&&(p?(p.snapshots||[]).some(s=>s.snapshot_id===payload.request_id):publications.some(x=>x.publication_id===payload.request_id));
    return {values:draft.values||draft,payload,exists:!!exists};
  }
  root.StudioOutcomes={metric,numeric,iso,safeUrl,submission,snapshotHtml,publicationHtml,reflection,restored};
  if(typeof module!=='undefined')module.exports=root.StudioOutcomes;
})(typeof window!=='undefined'?window:globalThis);

async function openOutcomes(){
  if(!state.runId){toast('先打开一个项目，再查看作品与表现。');return;}
  if(document.querySelector('.outcome-dialog'))return;
  const runId=state.runId,opener=document.activeElement,dialog=document.createElement('dialog');
  dialog.className='outcome-dialog';dialog.setAttribute('aria-labelledby','outcome-title');
  dialog.innerHTML='<header class="outcome-heading"><div><span class="eyebrow">从创作到实际表现</span><h2 id="outcome-title">作品与表现</h2></div><button class="ghost" data-close>关闭</button></header><p class="form-note">关联已发布作品，保留统计区间、来源和你的观察。这里仅做记录，不会发布内容或读取平台账户。</p><div data-outcome-content role="status">正在读取作品…</div>';
  document.body.append(dialog);let busy=false,data;
  const content=dialog.querySelector('[data-outcome-content]');
  dialog.querySelector('[data-close]').onclick=()=>{if(!busy)dialog.close();};
  dialog.addEventListener('cancel',e=>{if(busy)e.preventDefault();});
  dialog.addEventListener('close',()=>{dialog.remove();opener?.focus();});dialog.showModal();
  function current(){if(state.runId!==runId)throw new Error('当前项目已切换，请重新打开作品记录。');}
  const input=(label,name,type='text',extra='')=>`<label>${label}<input name="${name}" type="${type}" ${extra}></label>`;
  const area=(label,name,max=4000)=>`<label>${label}<textarea name="${name}" rows="3" maxlength="${max}" required></textarea></label>`;
  function form(kind,p){
    const host=content.querySelector('[data-outcome-editor]');
    if(!host)return;
    const fields=kind==='publication'?`${input('作品名称','title','text','maxlength="160" required')}${input('已发布作品链接','url','url','placeholder="https://…" maxlength="2000" required')}${input('发布时间','published_at','datetime-local','required')}<label>关联的脚本版本<select name="version" required><option value="">请选择实际关联的版本</option>${data.artifacts.map(a=>`<option value="${a.version}">脚本与分镜 · 版本 ${a.version}${a.current?' · 当前版本':''}</option>`).join('')}</select></label>${area('实际作品与稿件有哪些差异？未核对的部分请写明','differences')}`:`<h3>${escapeHtml(p.title)} · 记录表现</h3><p class="form-note">填平台实际统计的区间；未知指标留空。更正会另存一条，保留原记录。</p><div class="outcome-form-grid">${input('统计开始','window_start','datetime-local','required')}${input('统计结束','window_end','datetime-local','required')}${input('观察时间','observed_at','datetime-local','required')}<label>流量来源<select name="traffic"><option value="unknown">未知</option><option value="organic">自然流量</option><option value="paid">付费流量</option><option value="mixed">混合流量</option></select></label>${[['播放','views'],['点赞','likes'],['商品点击','clicks'],['归因订单','orders'],['归因销售额（USD）','revenue_usd'],['广告花费（USD）','spend_usd']].map(([l,n])=>input(l,n,'number',`min="0" step="${n.endsWith('_usd')?'0.01':'1'}" placeholder="未知请留空"`)).join('')}</div>${area('数据来源与统计口径','source',2000)}${area('你的观察、限制或本次更正原因','notes')}`;
    host.innerHTML=`<form class="outcome-form">${kind==='publication'?'<h3>关联已发布作品</h3>':''}<p class="form-note">时间按当前设备时区填写：${escapeHtml(Intl.DateTimeFormat().resolvedOptions().timeZone)}</p>${fields}<p class="form-error" role="alert"></p><div class="outcome-actions"><button type="submit" class="primary">保存记录</button><button type="button" class="ghost" data-dismiss>收起</button></div></form>`;
    const el=host.querySelector('form'),error=el.querySelector('[role="alert"]'),key=`studio-outcome:${runId}:${p?.publication_id||'new'}`;
    let payload=null,draftValues={};
    const values=()=>Object.fromEntries(new FormData(el).entries());
    try{const saved=StudioOutcomes.restored(JSON.parse(sessionStorage.getItem(key)||'{}'),data.publications,p);if(saved.exists){sessionStorage.removeItem(key);host.innerHTML='<p class="form-note">上次记录已经保存，请在下方查看。再次补充时会另存一条。</p>';return;}payload=saved.payload;for(const [name,value] of Object.entries(saved.values)){if(el.elements.namedItem(name))el.elements.namedItem(name).value=value;}}catch(_){}
    const local=value=>{const d=new Date(value);return new Date(d.getTime()-d.getTimezoneOffset()*60000).toISOString().slice(0,16);};
    if(kind==='snapshot'){if(!el.elements.window_start.value)el.elements.window_start.value=local(p.published_at);if(!el.elements.window_end.value)el.elements.window_end.value=local(new Date());if(!el.elements.observed_at.value)el.elements.observed_at.value=local(new Date());}
    if(kind==='publication'){
      const select=el.elements.version,note=document.createElement('p');note.className='form-note';select.parentElement.after(note);
      const show=()=>{const a=data.artifacts.find(a=>a.version===Number(select.value));note.textContent=a?`稿件摘要：${a.summary||'没有摘要，请先在脚本页核对原稿。'}`:'请选择作品实际参考的稿件，不会自动套用最新版。';};
      select.addEventListener('change',show);show();
    }
    draftValues=values();
    const persist=()=>{try{sessionStorage.setItem(key,JSON.stringify({values:draftValues,payload}));}catch(_){error.textContent='此窗口无法暂存文字，请及时保存。';}};
    const lockPending=()=>{el.querySelectorAll('input,textarea,select').forEach(n=>n.disabled=!!payload);if(payload)error.textContent='上次保存结果未确认。请重试同一请求，避免重复记录。';};
    lockPending();
    el.oninput=()=>{if(payload)return;draftValues=values();persist();};
    el.querySelector('[data-dismiss]').onclick=()=>{if(!busy)host.innerHTML='';};
    el.onsubmit=async e=>{
      e.preventDefault();if(busy)return;
      try{current();if(!payload){draftValues=values();payload=StudioOutcomes.submission(kind,draftValues,kind==='publication'?data.artifacts:p,crypto.randomUUID());}persist();
        busy=true;error.textContent='';el.querySelectorAll('input,textarea,select,button').forEach(n=>n.disabled=true);dialog.querySelector('[data-close]').disabled=true;
        await api(`/api/runs/${encodeURIComponent(runId)}/outcomes${kind==='snapshot'?`/${encodeURIComponent(p.publication_id)}/snapshots`:''}`,{method:'POST',body:JSON.stringify(payload)});
        try{sessionStorage.removeItem(key);}catch(_){}await load();toast('记录已保存，没有发布内容或调用模型。');
      }catch(err){if([400,409,422].includes(err.status)){payload=null;persist();}error.textContent=err.status===422?'请检查公开作品链接、日期先后顺序和非负指标；至少填写一项已知指标。':err.message;if(err.status===409)error.textContent+=' 输入已保留；若已有新记录，请重新打开核对。';}
      finally{busy=false;if(el.isConnected){el.querySelectorAll('input,textarea,select,button').forEach(n=>n.disabled=false);if(payload)el.querySelectorAll('input,textarea,select').forEach(n=>n.disabled=true);}dialog.querySelector('[data-close]').disabled=false;}
    };
    host.scrollIntoView({block:'nearest'});el.querySelector('input')?.focus();
  }
  async function load(){
    try{data=await api(`/api/runs/${encodeURIComponent(runId)}/outcomes`);if(!dialog.open)return;
      const unavailable=data.artifacts.some(a=>!a.integrity_ok);data.artifacts=data.artifacts.filter(a=>a.integrity_ok);
      content.innerHTML=`<div class="outcome-toolbar"><span>${data.publications.length} 条关联作品</span><button class="primary" data-add ${data.artifacts.length?'':'disabled'}>关联已发布作品</button></div>${!data.artifacts.length?'<p class="outcome-empty">先完成并保存脚本，再关联实际作品，保证每条反馈有明确版本。</p>':''}<div data-outcome-editor></div>${data.publications.map(StudioOutcomes.publicationHtml).join('')||'<p class="outcome-empty">还没有关联作品。策划与发布表现会在这里连起来。</p>'}`;
      content.querySelector('[data-add]').onclick=()=>{if(!busy)form('publication');};
      if(unavailable){const notice=document.createElement('p');notice.className='form-error';notice.textContent='部分稿件与保存记录不一致，暂不提供关联。请先核对原稿；已登记的历史记录仍可阅读。';content.prepend(notice);}
      for(const b of content.querySelectorAll('[data-outcome-observe]'))b.onclick=()=>{if(!busy)form('snapshot',data.publications.find(p=>p.publication_id===b.dataset.outcomeObserve));};
      for(const b of content.querySelectorAll('[data-outcome-reflect]'))b.onclick=()=>{if(busy)return;try{current();const p=data.publications.find(p=>p.publication_id===b.dataset.outcomeReflect);dialog.close();prepareProjectDraft(StudioOutcomes.reflection(p));}catch(err){toast(err.message);}};
    }catch(err){if(!dialog.open)return;content.innerHTML=`<p class="form-error">${err.status===404?'作品记录工具等待服务更新，正常重启工作台后可用。':escapeHtml(err.message)}</p><button class="ghost" data-retry>重新读取</button>`;content.querySelector('[data-retry]').onclick=load;}
  }
  await load();
}
