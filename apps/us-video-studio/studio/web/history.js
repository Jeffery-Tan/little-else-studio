/* Read-only project handoff archive. Historical approvals never authorize new work. */
"use strict";
(function(root){
  const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const labels={executed:'历史已执行',historical_executed:'历史已执行',draft:'未采用草稿',unadopted_draft:'未采用草稿',feedback:'用户反馈',user_feedback:'用户反馈',paused:'暂停记录',pause_record:'暂停记录',product:'商品资料',product_evidence:'商品资料',approval:'历史批准（不用于新生成）',historical_approval:'历史批准（不用于新生成）'};
  function status(value){return labels[value]||String(value||'历史资料');}
  function endpoint(runId,id){
    if(typeof runId!=='string'||!runId||/[\/\\]/.test(runId)||runId==='.'||runId==='..')throw new Error('项目标识无效。');
    const base=`/api/runs/${encodeURIComponent(runId)}/history`;
    if(id===undefined)return base;
    if(typeof id!=='string'||!id||/[\/\\]/.test(id)||id==='.'||id==='..')throw new Error('资料标识无效。');
    return `${base}/${encodeURIComponent(id)}`;
  }
  function requestScope(runId,getRunId){
    let revision=0,closed=false;
    return {next:()=>++revision,valid:token=>!closed&&token===revision&&getRunId()===runId,close:()=>{closed=true;revision++;}};
  }
  function listHtml(documents,selected){return documents.map(doc=>`<button type="button" class="history-document${doc.id===selected?' selected':''}" data-document="${esc(doc.id)}" aria-pressed="${doc.id===selected}"><strong>${esc(doc.title||'历史资料')}</strong><span>${esc(status(doc.status))}</span></button>`).join('');}
  function overviewHtml(data){
    if(!data.available)return '<div class="history-empty"><h3>这个项目还没有迁移历史</h3><p>项目当前的方案与素材仍可在创作板查看。</p></div>';
    const materials=Array.isArray(data.materials)?data.materials:[];
    return `<section class="history-overview"><p>${esc(data.summary||'旧案例资料已保留，可按版本阅读。')}</p><p class="history-boundary">历史批准仅供追溯。继续修改后，仍需核对新版本；迁移不会启动生成。</p>${data.historical_cost_note?`<p class="history-cost">${esc(data.historical_cost_note)}</p>`:''}${materials.length?`<details><summary>已接收的素材 · ${materials.length} 项</summary><ul>${materials.map(m=>`<li>${esc(m.label||m.name)}${m.label&&m.name!==m.label?` <span>${esc(m.name)}</span>`:''}</li>`).join('')}</ul></details>`:''}</section>`;
  }
  function documentHtml(doc,markdown){return `<header class="history-reading-heading"><span>${esc(status(doc.status))}</span><h3>${esc(doc.title||'历史资料')}</h3></header><div class="history-reading-content">${markdown(String(doc.content??''))}</div>`;}
  root.StudioHistory={esc,status,endpoint,requestScope,listHtml,overviewHtml,documentHtml};
  if(typeof module!=='undefined')module.exports=root.StudioHistory;
})(typeof window!=='undefined'?window:globalThis);

async function openProjectHistory(){
  if(!state.runId){toast('先打开一个项目，再查看迁移资料。');return;}
  if(document.querySelector('.history-dialog'))return;
  const runId=state.runId,U=StudioHistory,scope=U.requestScope(runId,()=>state.runId),opener=document.activeElement;
  const dialog=document.createElement('dialog');dialog.className='history-dialog';dialog.setAttribute('aria-labelledby','history-title');
  dialog.innerHTML='<header class="history-heading"><div><span class="eyebrow">接着上次的创作继续</span><h2 id="history-title">项目历史</h2></div><button class="ghost" data-close>关闭</button></header><div data-history-overview></div><div data-history-body aria-live="polite">正在读取迁移资料…</div>';
  document.body.append(dialog);dialog.showModal();
  let documents=[],selected='';
  const overview=dialog.querySelector('[data-history-overview]'),body=dialog.querySelector('[data-history-body]');
  function close(){scope.close();dialog.close();}
  dialog.querySelector('[data-close]').onclick=close;
  dialog.addEventListener('cancel',()=>scope.close());
  dialog.addEventListener('close',()=>{scope.close();dialog.remove();if(opener?.isConnected)opener.focus();});
  function valid(token){if(state.runId!==runId&&dialog.open)close();return dialog.open&&scope.valid(token);}
  function failed(target,message,retry){
    target.innerHTML=`<div class="history-empty" role="alert"><p>${U.esc(message)}</p><button class="ghost" data-history-retry>重新读取</button></div>`;
    target.querySelector('[data-history-retry]').onclick=retry;
  }
  async function readDocument(id){
    const doc=documents.find(item=>item.id===id);if(!doc)return;
    const token=scope.next();if(!valid(token))return;selected=id;
    body.querySelector('[data-history-list]').innerHTML=U.listHtml(documents,selected);
    const reading=body.querySelector('[data-history-reading]');reading.setAttribute('aria-busy','true');reading.textContent='正在读取这份资料…';
    try{
      const result=await api(U.endpoint(runId,id));if(!valid(token))return;
      if(result.id!==id||!doc.sha256||result.sha256!==doc.sha256){failed(reading,'资料版本已变化，请重新读取项目历史。',load);return;}
      reading.innerHTML=U.documentHtml(result,renderMarkdown);
    }catch(error){if(valid(token))failed(reading,'这份资料暂时无法读取，请重试。',()=>readDocument(id));}
    finally{if(valid(token))reading.removeAttribute('aria-busy');}
  }
  async function load(){
    const token=scope.next();if(!valid(token))return;overview.innerHTML='';body.textContent='正在读取迁移资料…';
    try{
      const data=await api(U.endpoint(runId));if(!valid(token))return;
      overview.innerHTML=U.overviewHtml(data);documents=Array.isArray(data.documents)?data.documents:[];
      if(!data.available){body.innerHTML='';return;}
      if(!documents.length){body.innerHTML='<div class="history-empty"><p>迁移说明已保留，目前没有可展开的文档。</p></div>';return;}
      body.innerHTML='<div class="history-layout"><nav data-history-list aria-label="历史文档"></nav><article data-history-reading aria-live="polite"></article></div>';
      await readDocument(documents.some(doc=>doc.id===selected)?selected:documents[0].id);
    }catch(error){if(valid(token))failed(body,'项目历史暂时无法读取，请重试。',load);}
  }
  body.addEventListener('click',event=>{const button=event.target.closest('[data-document]');if(button&&body.contains(button))readDocument(button.dataset.document);});
  await load();
}
