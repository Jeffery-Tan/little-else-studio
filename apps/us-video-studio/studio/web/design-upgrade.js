/* User-reviewed legacy design supplements. Models propose; saving never approves. */
"use strict";
(function(root){
  const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const hash=value=>typeof value==='string'&&/^[a-f0-9]{64}$/.test(value);
  const ref=value=>Number.isSafeInteger(value?.version)&&value.version>0&&hash(value.sha256);
  const active=job=>['queued','running','cancelling'].includes(job?.status);
  const fields={audience:'观众',everyday_context:'日常情境',localization:'美国本地表达',style:'画面风格',
    product_appearance:'商品外观',id:'身份',label:'名称',relationship:'关系',appearance:'外观',
    wardrobe:'服装',performance:'表演',location:'地点',layout:'空间布局',props:'道具',
    lighting:'光线',local_details:'本地细节',tc:'时码',picture:'原画面',line:'原台词或字幕',
    sound:'原声音',duration:'原时长',character_ids:'人物身份',scene_id:'场景身份'};
  const pairs=(value,keys)=>'<dl>'+keys.map(key=>`<dt>${esc(fields[key]||key)}</dt><dd>${esc(value?.[key]??'（未填写）')}</dd>`).join('')+'</dl>';
  function proposalIssue(proposal){
    const supplement=proposal?.supplement,design=supplement?.design;
    const ids=proposal?.basis?.shot_ids,bindings=supplement?.bindings,segments=supplement?.writing?.segments;
    if(typeof proposal?.id!=='string'||!proposal.id||!hash(proposal.sha256)||
        !ref(proposal.basis?.artifacts?.material_plan)||!ref(proposal.basis?.artifacts?.writing))
      return '补充方案的准确版本无法核对，请重新读取。';
    if(!design||design.schema_version!==1||design.market!=='US'||!Array.isArray(design.characters)||!Array.isArray(design.scenes)||
        !Array.isArray(design.continuity)||!Array.isArray(design.assumptions)||
        !Array.isArray(ids)||!ids.length||!Array.isArray(bindings)||!Array.isArray(segments)||
        bindings.length!==ids.length||segments.length!==ids.length)
      return '共同设计或原镜头清单不完整，不能保存。';
    const entityFields={characters:['id','label','relationship','appearance','wardrobe','performance'],
      scenes:['id','label','location','layout','props','lighting','local_details']};
    if(['audience','everyday_context','localization','style','product_appearance'].some(key=>
        typeof design[key]!=='string'||!design[key].trim())||!design.scenes.length||!design.continuity.length||
        Object.entries(entityFields).some(([key,keys])=>design[key].some(row=>
          !row||keys.some(field=>typeof row[field]!=='string'||!row[field].trim())))||
        [...design.continuity,...design.assumptions].some(text=>typeof text!=='string'||!text.trim()))
      return '共同设计字段不完整，不能保存。';
    const characters=new Set(design.characters.map(row=>row.id)),scenes=new Set(design.scenes.map(row=>row.id));
    if(bindings.some((binding,index)=>!binding||binding.shot_id!==ids[index]||
        typeof binding.character_ids!=='string'||!scenes.has(binding.scene_id)||
        binding.character_ids.split(',').map(id=>id.trim()).filter(Boolean).some(id=>!characters.has(id))||
        !segments[index]||segments[index].character_ids!==binding.character_ids||segments[index].scene_id!==binding.scene_id||
        ['tc','picture','line','sound'].some(key=>typeof segments[index][key]!=='string')))
      return '逐镜人物、场景绑定与原脚本无法完整核对，不能保存。';
    return '';
  }
  function proposalHtml(proposal){
    const issue=proposalIssue(proposal);if(issue)return `<p class="form-error" role="alert">${esc(issue)}</p>`;
    const {design,bindings,writing}=proposal.supplement;
    const basis=proposal.basis.artifacts;
    return `<section class="production-confirm"><h3>制作设计补充预览</h3>`+
      `<p>基于素材计划第 ${basis.material_plan.version} 版与脚本第 ${basis.writing.version} 版。原方向、逐镜画面、台词、声音和时长保留；本次只补共同设计与逐镜身份。</p>`+
      `<h4>共同设计</h4>${pairs(design,['audience','everyday_context','localization','style','product_appearance'])}`+
      `<h4>人物</h4>`+(design.characters.length?design.characters.map(row=>`<section>${pairs(row,['id','label','relationship','appearance','wardrobe','performance'])}</section>`).join(''):'<p>本方案没有人物。</p>')+
      '<h4>场景</h4>'+design.scenes.map(row=>`<section>${pairs(row,['id','label','location','layout','props','lighting','local_details'])}</section>`).join('')+
      `<h4>连续性要求</h4><ul>${design.continuity.map(text=>`<li>${esc(text)}</li>`).join('')}</ul>`+
      `<h4>创作假设</h4>`+(design.assumptions.length?`<ul>${design.assumptions.map(text=>`<li>${esc(text)}</li>`).join('')}</ul>`:'<p>没有另列创作假设。</p>')+
      `<h4>原脚本与逐镜绑定</h4>${writing.segments.map((segment,index)=>
        `<section class="production-card"><strong>${esc(bindings[index].shot_id)}</strong>${pairs(segment,['tc','picture','line','sound','duration','character_ids','scene_id'])}</section>`).join('')}`+
      `<details><summary>核对原版本与补充方案指纹</summary><p>原素材计划：${esc(basis.material_plan.sha256)}<br>原脚本：${esc(basis.writing.sha256)}<br>补充方案：${esc(proposal.sha256)}</p></details></section>`;
  }
  function connectionSummary(config){
    const binding=config?.roles?.material,connection=config?.connections?.find(row=>row.id===binding?.connection);
    const revision=Number.isSafeInteger(config?.revision)&&config.revision>=0?config.revision:null;
    if(!connection||revision===null)return {ready:false,label:'素材角色连接暂时无法核对。'};
    const ready=connection.provider==='codex_cli'||Boolean(connection.has_key&&binding.model&&connection.base_url);
    return {ready,revision,label:`${connection.name||connection.id} · ${binding.model||'本机默认模型'}`,
      issue:ready?'':'请先在模型设置中补齐素材角色的模型连接与密钥。'};
  }
  function savePayload(proposal,nonce){
    const issue=proposalIssue(proposal);if(issue)throw new Error(issue);
    if(proposal.current!==true)throw new Error('原方案已变化，请重新核对补充方案。');
    if(typeof nonce!=='string'||!nonce)throw new Error('当前保存身份不可用，请刷新后再试。');
    return {proposal_id:proposal.id,expected_proposal_sha256:proposal.sha256,nonce,consent:true};
  }
  function savedResult(result){return result?.saved===true&&ref(result.artifacts?.material_plan)&&ref(result.artifacts?.writing);}
  root.StudioDesignUpgrade={esc,active,proposalIssue,proposalHtml,connectionSummary,savePayload,savedResult};
  if(typeof module!=='undefined')module.exports=root.StudioDesignUpgrade;
})(typeof window!=='undefined'?window:globalThis);

async function openDesignUpgrade(runId,initialData){
  if(!runId||state.runId!==runId)return;
  const existing=document.querySelector('.design-upgrade-dialog');
  if(existing){existing.focus();return;}
  const U=StudioDesignUpgrade,base=`/api/runs/${encodeURIComponent(runId)}/production/design-upgrade`;
  const storageKey=`studio-design-upgrade-save:${runId}`,opener=document.activeElement;
  const dialog=document.createElement('dialog');dialog.className='production-dialog design-upgrade-dialog';
  dialog.setAttribute('aria-labelledby','design-upgrade-title');
  dialog.innerHTML='<header class="production-heading"><h2 id="design-upgrade-title">补齐制作设计，保留原脚本</h2><button type="button" class="ghost" data-upgrade-close>关闭</button></header><p class="form-error" role="alert" data-upgrade-error></p><div data-upgrade-body>正在读取准确方案…</div>';
  document.body.append(dialog);dialog.showModal();
  const body=dialog.querySelector('[data-upgrade-body]'),error=dialog.querySelector('[data-upgrade-error]');
  let data=initialData,proposal=null,config=null,job=null,pendingSave=null,saved=null;
  let busy=false,closed=false,reading=false,timer=null,serial=0;
  try{
    const pending=JSON.parse(sessionStorage.getItem(storageKey)||'null');
    if(typeof pending?.proposal_id==='string'&&/^[a-f0-9]{64}$/.test(pending.expected_proposal_sha256||'')&&
        typeof pending.nonce==='string'&&pending.nonce&&pending.consent===true)
      pendingSave={proposal_id:pending.proposal_id,expected_proposal_sha256:pending.expected_proposal_sha256,
        nonce:pending.nonce,consent:true};
  }catch(_){error.textContent='上次保存记录无法读取，请刷新项目后核对已保存的版本。';}
  const current=()=>!closed&&dialog.open&&state.runId===runId;
  const modelPending=()=>typeof readPendingJob==='function'?readPendingJob(runId):null;
  function close(){if(closed)return;closed=true;serial++;clearTimeout(timer);if(dialog.open)dialog.close();dialog.remove();if(state.runId===runId)opener?.focus();}
  dialog.studioClose=close;
  dialog.querySelector('[data-upgrade-close]').onclick=close;
  dialog.addEventListener('cancel',close);
  dialog.addEventListener('close',close);
  function notify(message){if(current())error.textContent=message;}
  function button(label,attribute,handler,disabled=false){
    const element=document.createElement('button');element.type='button';element.className='primary';
    element.textContent=label;element.setAttribute(attribute,'');element.disabled=disabled;
    element.onclick=()=>{if(current()&&!element.disabled)return handler();};body.append(element);return element;
  }
  function render(){
    if(!current())return;
    if(saved){
      body.innerHTML=`<section class="production-confirm"><h3>设计补充已保存为新版本</h3><p>素材计划第 ${saved.artifacts.material_plan.version} 版与脚本第 ${saved.artifacts.writing.version} 版已保存。原方向和旧图片保留；新版脚本仍需你阅读并采用。旧批准没有转移，没有生成媒体。</p></section>`;
      button('查看新版脚本并决定采用','data-upgrade-open-writing',()=>{
        const writing=(state.rail||[]).find(row=>row.stage==='writing'),expected=saved.artifacts.writing;
        if(writing?.version!==expected.version||writing.artifact_sha256!==expected.sha256){notify('当前脚本版本尚未核对，请先刷新项目。');return;}
        close();showJourneyTab('plan',{stage:'writing'});
      });return;
    }
    if(pendingSave){
      body.innerHTML='<section class="production-confirm"><h3>这次保存结果待确认</h3><p>准确补充方案与保存身份已保留。核实同一次保存不会调用模型、生成媒体或另存一个版本。</p></section>';
      button('核实同一次保存','data-upgrade-retry-save',()=>sendSave(pendingSave),busy);return;
    }
    const pending=modelPending();
    if(pending?.request_kind==='design_upgrade'){
      body.innerHTML='<section class="production-confirm"><h3>这次设计准备请求待确认</h3><p>原请求身份、方案版本与模型设置已保留。先核实同一请求，不另开一次素材模型调用。</p></section>';
      button('核实同一准备请求','data-upgrade-retry-propose',()=>sendProposal(pending),busy);return;
    }
    if(proposal){
      body.innerHTML=U.proposalHtml(proposal);
      const issue=U.proposalIssue(proposal);
      if(issue||proposal.current!==true){
        if(!issue)body.innerHTML+='<p class="form-error">原方案已变化，这份补充仅供查看；不能沿用保存。</p>';
        return;
      }
      body.innerHTML+=`<p class="form-note">${proposal.partial_save
        ?'这份补充方案已有部分版本保存；继续保存会复用同一方案，完成余下版本。'
        :'保存会创建素材计划和原脚本的新版本，加入以上共同设计与逐镜身份。'}新版仍须从原脚本采用入口确认；不批准旧图片，不调用模型或生成媒体。</p><label class="production-consent"><input type="checkbox" data-upgrade-save-consent>我已核对共同设计和逐镜内容，同意保存这份补充方案</label>`;
      const consent=body.querySelector('[data-upgrade-save-consent]');
      const save=button(proposal.partial_save?'继续保存这份补充方案':'保存这份补充方案为新版本','data-upgrade-save',()=>{
        if(busy||!consent.checked)return;
        try{const payload=U.savePayload(proposal,data?.nonce);sessionStorage.setItem(storageKey,JSON.stringify(payload));pendingSave=payload;sendSave(payload);}
        catch(cause){notify(cause.message||'保存身份无法暂存，请保持本页并重新核对。');}
      },true);
      consent.onchange=()=>save.disabled=busy||data?.active===true||Boolean(pending)||!consent.checked;
      return;
    }
    if(activeJob()){
      body.innerHTML=job?.job_kind==='design_upgrade'
        ?`<section class="production-confirm"><h3>设计补充正在准备</h3><p>本次只准备可预览的补充方案，原脚本和批准尚未改变。${U.esc(job?.reason||'等待本次任务完成；不会自动重试模型。')}</p></section>`
        :'<section class="production-confirm"><h3>当前团队任务尚未结束</h3><p>先等待当前任务结束，再核对设计补充；这一步没有发起模型调用。</p></section>';
      return;
    }
    const action=data?.design_upgrade_action,connection=U.connectionSummary(config);
    if(job?.job_kind==='design_upgrade'&&job.status==='succeeded'&&action){
      body.innerHTML='<section class="production-confirm"><h3>本次准备任务已完成，补充方案尚未读取</h3><p>先找回这次已保存的补充方案，不要另开一次模型调用。</p></section>';
      button('重新读取已有补充方案','data-upgrade-reload',refresh);return;
    }
    body.innerHTML='<section class="production-confirm"><h3>先准备一份可核对的补充方案</h3><p>保留已采用方向、原逐镜画面、台词、声音和时长，只补人物、场景的共同设计与逐镜身份。</p><p>准备最多调用素材角色模型一次，使用已配置的素材角色连接；会消耗该模型的用量，实际费用以提供方为准。完成后先展示预览，不改当前阶段产物、不批准图片或生成媒体。</p>'+`<p>${U.esc(connection.label)}</p>`+
      (job?.job_kind==='design_upgrade'&&job.status!=='succeeded'?`<p class="form-note">上一次准备${U.esc(({failed:'失败',blocked:'已暂停',cancelled:'已停止',interrupted:'中断'})[job.status]||'未完成')}：${U.esc(job.reason||'未保存可核对的补充方案')}。没有自动重试。</p>`:'')+'</section>';
    if(!action||!/^[a-f0-9]{64}$/.test(action.expected_context_sha256||'')){
      body.innerHTML+='<p class="form-note">当前没有可用的旧项目补充入口。请刷新后核对准确脚本和当前设计。</p>';return;
    }
    if(!connection.ready||data.active||pending){
      body.innerHTML+=`<p class="form-note">${U.esc(connection.issue||'先核实或结束当前任务，再准备新的补充方案。')}</p>`;return;
    }
    body.innerHTML+='<label class="production-consent"><input type="checkbox" data-upgrade-model-consent>同意这一次素材模型调用；先准备补充方案供我预览</label>';
    const consent=body.querySelector('[data-upgrade-model-consent]');
    const payload={request_kind:'design_upgrade',request_id:crypto.randomUUID(),
      expected_context_sha256:action.expected_context_sha256,
      expected_connection_revision:connection.revision,consent:true};
    const prepare=button('确认并准备一次补充方案','data-upgrade-propose',()=>{if(consent.checked&&!busy)sendProposal(payload);},true);
    consent.onchange=()=>prepare.disabled=busy||!consent.checked;
  }
  function activeJob(){return U.active(job)||data?.active===true;}
  async function sendProposal(payload){
    if(busy||!current())return;busy=true;notify('');render();
    try{
      const result=await submitJob(runId,payload,{preserveComposerOnError:true});
      if(!current())return;
      if(result?.accepted===false)notify(result.uncertain?'准备请求的接收结果待确认，请核实同一请求。'
        :result.error?.message||result.reason||'本次准备未被接受。');
    }catch(cause){notify(cause.message||'准备请求结果待确认，请核实原请求。');}
    finally{busy=false;if(current())await refresh();}
  }
  async function sendSave(payload){
    if(busy||!current())return;busy=true;notify('');render();
    try{
      const result=await api(base+'/save',{method:'POST',body:JSON.stringify(payload)});
      if(!U.savedResult(result))throw new Error('保存回执无法核对，请核实同一次保存。');
      sessionStorage.removeItem(storageKey);pendingSave=null;saved=result;
      if(!current())return;
      try{await refreshTimeline();}catch(_){notify('新版本已保存，项目暂未刷新。请刷新后阅读准确新版。');}
    }catch(cause){
      if([400,403,404,409,422].includes(cause.status)){
        sessionStorage.removeItem(storageKey);pendingSave=null;
        if(current())await refresh();
      }
      notify(cause.message||'保存结果待确认，请核实同一次保存。');
    }finally{busy=false;if(current())render();}
  }
  async function refresh(){
    if(!current()||reading)return;reading=true;const generation=++serial;clearTimeout(timer);
    try{
      const [nextData,nextProposal,nextConfig,nextJob]=await Promise.all([
        api(`/api/runs/${encodeURIComponent(runId)}/production`),api(base+'/proposal'),
        api('/api/connections'),api(`/api/runs/${encodeURIComponent(runId)}/jobs/latest`)]);
      if(!current()||generation!==serial)return;
      data=nextData;proposal=nextProposal;config=nextConfig;job=nextJob;
      render();
    }catch(cause){notify(cause.message||'暂时无法读取补充方案；没有发起新的调用。');
      if(current()&&!body.querySelector('button'))button('重新读取补充方案','data-upgrade-reload',refresh);}
    finally{
      reading=false;if(!current())return;
      if(activeJob()||modelPending()?.request_kind==='design_upgrade')timer=setTimeout(refresh,2000);
    }
  }
  await refresh();
  return {dialog,refresh,close};
}
