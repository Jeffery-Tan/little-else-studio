"use strict";
(function(root){
  const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  function preview(card){
    return `<article class="product-card-preview"><h4>${esc(card.name)}</h4><p class="muted">来自 ${esc(card.source_title)} · ${esc(card.source_label)}</p><dl>${[['商品信息',card.facts],['证据来源',card.evidence],['未知与限制',card.limitations],['来源保留的边界',(card.source_limits||[]).join('\n')]].map(([label,value])=>`<div><dt>${label}</dt><dd>${esc(value)}</dd></div>`).join('')}</dl><p class="form-note">作为新项目的资料快照使用。请核对是否仍然适用；不会复制原项目的素材、方案或批准。</p></article>`;
  }
  function binding(card){return card?{source_run_id:card.source_run_id,card_id:card.card_id,sha256:card.sha256}:null;}
  root.StudioProductCards={preview,binding};
  if(typeof module!=='undefined')module.exports=root.StudioProductCards;
})(typeof window!=='undefined'?window:globalThis);

function attachProductCardPicker(dialog){
  const host=dialog.querySelector('[data-product-card-picker]');if(!host)return;
  let selected=null;
  dialog.productCardBinding=()=>StudioProductCards.binding(selected);
  host.innerHTML='<p class="form-note">正在读取可复用的商品资料…</p>';
  async function load(){
    try{
      const data=await api('/api/product-cards');if(!dialog.isConnected)return;
      if(!data.cards.length){host.innerHTML='<p class="form-note">已有项目可在「需求整理」中保存商品资料卡，下次创作直接复用。</p>';return;}
      host.innerHTML=`<label>复用商品资料<select data-card-choice><option value="">不使用资料卡</option>${data.cards.map((c,i)=>`<option value="${i}">${escapeHtml(c.name)} · ${escapeHtml(c.source_title)} · ${escapeHtml(c.saved_at.slice(0,10))}</option>`).join('')}</select></label><details class="product-card-selection" hidden><summary>核对这份商品资料</summary><div data-card-preview></div></details>`;
      const select=host.querySelector('[data-card-choice]'),details=host.querySelector('details');
      select.onchange=()=>{selected=select.value===''?null:data.cards[Number(select.value)];details.hidden=!selected;details.open=!!selected;host.querySelector('[data-card-preview]').innerHTML=selected?StudioProductCards.preview(selected):'';};
    }catch(err){host.innerHTML='<p class="form-note">暂时无法读取商品资料；你仍可直接填写本次需求。</p><button type="button" class="text-button" data-retry-cards>重新读取</button>';host.querySelector('button').onclick=load;}
  }
  load();
}

function wireProductCards(host,runId,step){
  if(step.stage!=='intake')return;
  const section=box('details','product-card-workspace');
  section.innerHTML='<summary>商品资料 · 保存供下次创作使用</summary><div class="product-card-body"></div>';
  host.append(section);
  let loaded=false,data,saving=false,request=null;
  const body=section.querySelector('.product-card-body');
  section.addEventListener('toggle',()=>{if(section.open&&!loaded)load();});
  async function load(){
    loaded=true;body.textContent='正在读取本项目的商品资料…';
    try{
      data=await api(`/api/runs/${runId}/product-cards`);if(!section.isConnected)return;
      const key=`studio-product-card:${runId}:${data.source.sha256}`;
      let draft={};try{draft=JSON.parse(sessionStorage.getItem(key)||'{}')||{};}catch(_){}
      body.innerHTML=`<p class="form-note">整理可反复使用的商品信息。保存不调用模型，也不改变当前方案。</p><p class="muted">来源：${escapeHtml(data.source_label)} · ${escapeHtml(data.source_title)}</p><details class="product-card-source"><summary>查看来源原文</summary><pre>${escapeHtml(data.source_text)}</pre></details><form><label>商品名称<input name="name" maxlength="120" required value="${escapeHtml(draft.name??data.name)}"></label><label>商品信息<textarea name="facts" rows="4" maxlength="6000" required placeholder="填写可复用的规格、用途或用户提供的信息，并区分已知事实与假设。">${escapeHtml(draft.facts??data.facts)}</textarea></label><label>证据来源<textarea name="evidence" rows="2" maxlength="3000" required placeholder="信息来自哪里、哪个版本或哪次确认？">${escapeHtml(draft.evidence??data.evidence)}</textarea></label><label>未知与限制<textarea name="limitations" rows="2" maxlength="3000" required placeholder="仍待核对什么？哪些说法不能直接使用？">${escapeHtml(draft.limitations??data.source_limits.join('\n'))}</textarea></label><p class="form-note">原来源的限制会一并保留；再次修改会另存一份资料卡。</p><p role="alert" class="form-error"></p><button type="submit" class="primary">保存商品资料卡</button></form><div data-saved-cards></div>`;
      const form=body.querySelector('form'),error=form.querySelector('[role="alert"]'),button=form.querySelector('[type="submit"]');
      const values=()=>Object.fromEntries(['name','facts','evidence','limitations'].map(k=>[k,form.elements.namedItem(k).value]));
      form.oninput=()=>{request=null;button.disabled=false;button.textContent='保存商品资料卡';try{sessionStorage.setItem(key,JSON.stringify(values()));}catch(_){error.textContent='本窗口无法暂存，请及时保存到项目。';}};
      function saved(){body.querySelector('[data-saved-cards]').innerHTML=data.cards.length?`<h4>本项目已保存的资料</h4>${data.cards.slice().reverse().map(c=>`<details><summary>${escapeHtml(c.name)} · ${escapeHtml(c.saved_at.slice(0,10))}</summary>${StudioProductCards.preview(c)}</details>`).join('')}`:'';}
      saved();
      form.onsubmit=async event=>{
        event.preventDefault();if(saving)return;
        if(state.runId!==runId){error.textContent='项目已切换，请回到原项目再保存。';return;}
        request ||= {request_id:crypto.randomUUID(),source:data.source,...values()};
        saving=true;button.disabled=true;error.textContent='';for(const field of form.querySelectorAll('input,textarea'))field.disabled=true;
        try{
          const card=await api(`/api/runs/${runId}/product-cards`,{method:'POST',body:JSON.stringify(request)});
          if(!data.cards.some(c=>c.card_id===card.card_id))data.cards.push(card);
          saved();button.textContent='已保存 · 新建项目时可选用';try{sessionStorage.removeItem(key);}catch(_){}
        }catch(err){error.textContent=err.message;button.disabled=false;}
        finally{saving=false;for(const field of form.querySelectorAll('input,textarea'))field.disabled=false;}
      };
    }catch(err){loaded=false;body.textContent=err.status===404?'商品资料工具等待服务更新，正常重启工作台后可用。':err.message;}
  }
}
