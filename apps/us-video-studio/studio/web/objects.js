/* Exact project objects, persistent annotations and reference edits. No implicit generation. */
"use strict";
(function(root) {
  const list = value => Array.isArray(value) ? value : [];
  const hash = value => /^[a-f0-9]{64}$/.test(value || '');
  const purposes = {product:'商品',character:'人物',scene:'场景',continuity:'连续性'};
  const sameTarget=(left,right)=>left&&right&&Object.keys(left).length===Object.keys(right).length&&Object.entries(left).every(([key,value])=>right[key]===value);
  const edgeNames = {...purposes,reference:'参考',generation:'生成来源',generated_from:'生成来源',supersedes:'版本替换',source:'创作依据',production_input:'制作依据',design:'设定来源',contains:'包含页面或镜头',uses_design:'采用设定',shot_input:'对应制作输入',note:'自由笔记'};
  const statusNames={current:'当前版本',history:'历史版本',available:'可查看',succeeded:'已生成',completed:'已生成',unknown:'结果待核实',unverified:'结果待核实',failed:'本次失败',cancelled:'已停止',submitted:'已提交',saved:'待处理',pending:'待处理'};
  const id = () => root.crypto.randomUUID();
  const base = runId => `/api/runs/${encodeURIComponent(runId)}`;
  async function request(path, body) {
    const response = await root.fetch(path, {headers:{'content-type':'application/json'},
      ...(body === undefined ? {} : {method:'POST',body:JSON.stringify(body)})});
    const data = await response.json();
    if (!response.ok) { const error = new Error(typeof data.detail === 'string' ? data.detail : `请求未完成（${response.status}）`); error.status = response.status; throw error; }
    return data;
  }
  function referencePayload(draft, references, requestId) {
    if (!draft?.draft_id || !hash(draft.draft_sha256) || !hash(draft.source_sha256) ||
        !Number.isInteger(draft.source_version) || !draft.shot_id || draft.is_current !== true || draft.source_ready !== true)
      throw new Error('当前制作输入或来源已变化，请重新读取准确版本。');
    if (draft.kind === 'video' && (references.length !== 1 || references[0].purpose !== 'continuity'))
      throw new Error('视频须保留一张连续性首帧；更换后重新核对图片与授权。');
    if (references.length > 5 || references.some(ref => !ref.name || !hash(ref.sha256) || !purposes[ref.purpose]))
      throw new Error('请核对参考图、用途及数量。');
    return {request_id:requestId,kind:draft.kind,prompt:null,
      preserve_prompt_from:{draft_id:draft.draft_id,expected_sha256:draft.draft_sha256},
      source_version:draft.source_version,source_sha256:draft.source_sha256,shot_id:draft.shot_id,
      supersedes:draft.draft_id,references:references.map(ref => ({name:ref.name,sha256:ref.sha256,purpose:ref.purpose,
        ...(ref.design_id ? {design_id:ref.design_id} : {})})),
      model:draft.model,duration:draft.duration ?? null,resolution:draft.resolution ?? null};
  }
  function normalizedRegion(start, end, rect) {
    if (!(rect.width > 0 && rect.height > 0)) return null;
    const clamp = number => Math.min(1,Math.max(0,number));
    const ax=clamp((start.x-rect.left)/rect.width), ay=clamp((start.y-rect.top)/rect.height);
    const bx=clamp((end.x-rect.left)/rect.width), by=clamp((end.y-rect.top)/rect.height);
    const region={x:Math.min(ax,bx),y:Math.min(ay,by),width:Math.abs(ax-bx),height:Math.abs(ay-by)};
    return region.width > .002 && region.height > .002 ? region : null;
  }
  function savedReferenceMatches(saved,payload) {
    const refs=rows=>list(rows).map(row=>({name:row.name,sha256:row.sha256,purpose:row.purpose,design_id:row.design_id||null}));
    return saved?.draft_id===payload.request_id && hash(saved.draft_sha256) && saved.kind===payload.kind &&
      saved.source_version===payload.source_version && saved.source_sha256===payload.source_sha256 &&
      saved.shot_id===payload.shot_id && saved.supersedes===payload.supersedes &&
      JSON.stringify(refs(saved.references))===JSON.stringify(refs(payload.references));
  }
  function savedLayoutMatches(saved,payload) {
    const stable=value=>Array.isArray(value)?value.map(stable):value&&typeof value==='object'
      ?Object.fromEntries(Object.keys(value).sort().map(key=>[key,stable(value[key])])):value;
    return saved?.revision===payload.expected_revision+1 &&
      ['view','positions','groups','notes'].every(key=>JSON.stringify(stable(saved[key]))===JSON.stringify(stable(payload[key])));
  }
  const api = {referencePayload,normalizedRegion,savedReferenceMatches,savedLayoutMatches,
    readCanvas:runId => request(`${base(runId)}/canvas`),
    saveLayout:(runId,payload) => request(`${base(runId)}/canvas/layout`,{request_id:id(),...payload})};
  root.StudioObjects = api;
  if (typeof module !== 'undefined') module.exports = api;
  if (!root.document) return;
  const doc=root.document;
  const el=(tag,cls,value)=>{const node=doc.createElement(tag);if(cls)node.className=cls;if(value!==undefined)node.textContent=String(value);return node;};
  const button=(label,action,cls='ghost small')=>{const node=el('button',cls,label);node.type='button';node.onclick=action;return node;};
  const active=runId=>typeof state!=='undefined'&&state.runId===runId;
  const readPending=key=>{try{return JSON.parse(root.sessionStorage.getItem(key)||'null');}catch(_){return null;}};
  const writePending=(key,value)=>{try{if(value)root.sessionStorage.setItem(key,JSON.stringify(value));else root.sessionStorage.removeItem(key);}catch(_){/* The open dialog retains its exact request. */}};
  function restoreFocus(opener){
    const modal=doc.querySelector('dialog[open]')||[doc.getElementById('reader'),doc.getElementById('viewer')].find(node=>node&&!node.hidden);
    if(opener?.isConnected&&(!modal||modal.contains(opener)))opener.focus({preventScroll:true});
  }
  function dialog(title,cls='') {
    const opener=doc.activeElement, panel=el('dialog',`object-dialog ${cls}`);
    panel.setAttribute('aria-label',title);
    const header=el('header','object-dialog-header');header.append(el('h2','',title),button('关闭',()=>panel.close()));
    const status=el('p','object-status');status.setAttribute('role','status');
    const body=el('div','object-dialog-body');panel.append(header,status,body);doc.body.append(panel);
    panel.addEventListener('close',()=>{panel.remove();restoreFocus(opener);});
    panel.showModal();return {panel,body,status,current:()=>panel.open&&panel.isConnected};
  }
  function embeddedCanvas(mount,onClose) {
    const panel=el('section','object-canvas-embedded'),header=el('header','object-dialog-header');
    header.append(el('h2','','对象与关系'),button('返回创作概览',onClose));
    const body=el('div','object-dialog-body'),status=el('p','object-status');status.setAttribute('role','status');
    panel.append(header,status,body);mount.replaceChildren(panel);
    return {panel,body,status,current:()=>panel.isConnected};
  }
  function notify(result,message,error=false){result.status.textContent=message;result.status.classList.toggle('error',error);}
  function safeUrl(runId,value){
    const prefix=`${base(runId)}/materials/`;
    if(typeof value!=='string'||!value.startsWith(prefix)||/[?#\\]/.test(value))return '';
    try{const name=decodeURIComponent(value.slice(prefix.length));return name&&!/[\/\\\u0000-\u001f]/.test(name)&&!['.','..'].includes(name)?value:'';}catch(_){return '';}
  }
  function objectMedia(runId,node) {
    const material=node?.data?.material || node?.data?.output || node?.data;
    const name=material?.name || node.target?.name;
    const url=safeUrl(runId,material?.url || (name ? `${base(runId)}/materials/${encodeURIComponent(name)}` : ''));
    const kind=material?.kind || node?.data?.draft?.kind;
    return url && ['image','video'].includes(kind) ? {kind,url} : null;
  }
  function targetLabel(target) {
    if(target.kind==='design')return `${target.design_kind==='character'?'人物':'场景'}设定 · ${target.design_id} · 来源第 ${target.version} 版`;
    if(target.kind==='shot')return `页面或镜头 · ${target.shot_id} · 来源第 ${target.version} 版`;
    if (target.kind==='artifact'||target.kind==='writing')return `${({writing:'方案',direction:'方向',material_plan:'素材计划',intake:'需求'})[target.stage]||'方案'} · 第 ${target.version} 版`;
    if (target.kind==='media')return `生成结果 · ${target.attempt_id}`;
    if (target.kind==='material')return `素材 · ${target.name}`;
    return `制作输入 · ${target.draft_id || ''}`;
  }
  function renderInspection(host,report) {
    const section=el('section','object-inspection');section.append(el('h4','','实际结果检查'));
    if(!report){section.append(el('p','object-note','这份准确结果尚无检查报告。'));host.append(section);return;}
    const statuses={observed_ok:'已观察到符合',defect:'发现问题',unknown:'尚无法判断'};
    const areas={product:'商品',character:'人物',scene:'场景',copy:'文案',quality:'画面质量',requirements:'任务要求',audio:'声音'};
    section.append(el('p','object-note',`${statuses[report.status]||'检查状态待核对'} · ${report.at||''}`));
    for(const finding of list(report.findings)){
      const row=el('div',`inspection-finding inspection-${finding.status}`);row.append(el('strong','',`${areas[finding.area]||finding.area} · ${statuses[finding.status]||'尚无法判断'}`),el('p','',finding.evidence));section.append(row);
    }
    if(list(report.limitations).length){section.append(el('h5','','本次限制'));for(const text of report.limitations)section.append(el('p','object-note',text));}
    section.append(el('p','object-note','检查记录不代表人工确认；不会自动重新生成。'));host.append(section);
  }
  api.openInspection=async function({runId,target,title='',mediaKind='image'}={}){
    if(!active(runId)||target?.kind!=='media')return;
    const ui=dialog(`${title||'生成结果'} · 实际检查`),reportHost=el('section'),notice=el('p','object-note'),connectionHost=el('p','object-note');
    const consent=el('input');consent.type='checkbox';const label=el('label','inspection-consent');label.append(consent,el('span','','我确认检查这份准确结果，理解模型可能产生费用。'));
    const action=button('确认并检查结果',()=>submitInspection(),'primary');action.disabled=true;
    const pendingKey=`studio-result-inspection:${runId}:${JSON.stringify(target)}`;
    let data=null,busy=false,pending=readPending(pendingKey);
    const current=()=>ui.current()&&active(runId);
    consent.onchange=()=>{action.disabled=busy||(!pending&&(!consent.checked||!connection()));};
    const connection=()=>data?.[mediaKind==='video'?'video_connection':'image_connection'];
    ui.body.append(reportHost,notice,connectionHost,label,action);
    async function load(){
      try{const value=await request(`${base(runId)}/result-inspections`);if(!current())return;data=value;
        reportHost.replaceChildren();renderInspection(reportHost,list(value.reports).filter(row=>sameTarget(row.target,target)).at(-1));
        notice.textContent=value.notice||'检查实际媒体可能产生理解模型费用；报告不代替人工确认。';
        connectionHost.textContent=connection()?`理解模型：${connection().name||connection().provider} · ${connection().suggested_model||'由当前连接确定'}`:'当前没有可用的理解模型连接，请在模型设置中配置后重新打开。';
        if(pending){consent.checked=true;consent.disabled=true;action.textContent='核对原检查请求';}
        action.disabled=busy||(!pending&&(!consent.checked||!connection()));
      }catch(error){if(current())notify(ui,`检查记录未能读取：${error.message}`,true);}
    }
    async function submitInspection(){
      if(!current()||busy||(!pending&&(!consent.checked||!connection()||!Number.isInteger(data?.revision))))return;
      pending ||= {request_id:id(),target,expected_revision:data.revision,consent:true};writePending(pendingKey,pending);
      busy=true;consent.disabled=true;action.disabled=true;notify(ui,'正在核对准确结果与本次检查确认…');
      try{const job=await request(`${base(runId)}/result-inspections`,pending);if(!current())return;
        if(!job.job_id)throw new Error('检查回执未包含准确任务身份');
        pending=null;writePending(pendingKey,null);if(typeof trackAcceptedJob==='function')trackAcceptedJob(runId,job);
        notify(ui,'检查任务已接收，请在任务状态中查看进度。完成后重新打开可阅读报告。');
        consent.checked=false;if(typeof refreshTimeline==='function')void refreshTimeline();
      }catch(error){if([400,409,422].includes(error.status)){pending=null;writePending(pendingKey,null);}if(current())notify(ui,`${pending?'检查提交结果尚未确认':'本次检查未启动'}：${error.message}${pending?'；保留原请求，可再次核对。':''}`,true);}
      finally{busy=false;consent.disabled=Boolean(pending);action.disabled=!pending&&(!consent.checked||!connection());action.textContent=pending?'核对原检查请求':'确认并检查结果';}
    }
    await load();
  };
  api.exportObjects=async function({runId,targets,status}={}){
    if(!active(runId))return;
    if(!list(targets).length){if(status)status.textContent='请勾选要导出的作品或生成结果。';return;}
    if(status)status.textContent='正在核对准确文件并打包…';
    try{
      const response=await root.fetch(`${base(runId)}/objects/export`,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({request_id:id(),targets})});
      if(!response.ok){const data=await response.json();throw new Error(typeof data.detail==='string'?data.detail:`下载未完成（${response.status}）`);}
      if(!String(response.headers.get('content-type')||'').includes('application/zip'))throw new Error('服务器没有返回可核对的作品压缩包');
      const blob=await response.blob();if(!active(runId))return;
      const url=root.URL.createObjectURL(blob),link=el('a');link.href=url;link.download=`${runId.replace(/[^a-z0-9_-]/gi,'')}-selected-objects.zip`;
      doc.body.append(link);link.click();link.remove();root.setTimeout(()=>root.URL.revokeObjectURL(url),1000);
      if(status)status.textContent='压缩包已生成，下载已交给浏览器处理。';
    }catch(error){if(status)status.textContent=`导出未完成：${error.message}。所选对象保持原样。`;}
  };
  api.openQueue=async function(){
    const ui=dialog('任务队列'),rows=el('section');let busy=false;
    ui.body.append(el('p','object-note','文字创作按顺序执行；可离开当前项目。费用生成仍需在准确制作输入中单独确认。'),rows,button('重新读取队列',()=>load()));
    async function load(){
      if(!ui.current()||busy)return;
      try{const data=await request('/api/jobs/queue');if(!ui.current())return;rows.replaceChildren();
        if(!list(data.items).length)rows.append(el('p','object-note','当前没有等待或执行中的文字任务。'));
        for(const item of list(data.items)){
          const row=el('article','object-queue-row');row.append(el('strong','',`${item.run_id} · ${item.status==='queued'?`等待执行${item.position?` · 第 ${item.position} 位`:''}`:item.status==='cancelling'?'正在停止':'正在执行'}`),el('p','',item.text||'文字任务'));
          const querying=item.lane==='query'&&['running','cancelling'].includes(item.status);
          if(querying)row.append(el('small','object-note','原任务查询中，不占用文字执行位置。'));
          if((item.status==='queued'||(querying&&item.status==='running'))&&active(item.run_id))row.append(button(querying?'停止这条本地查询':'取消这条排队任务',async()=>{
            if(busy)return;busy=true;
            try{const job=await request(`${base(item.run_id)}/jobs/${encodeURIComponent(item.job_id)}/cancel`,{});if(!ui.current())return;
              if(job.job_id!==item.job_id||!['cancelled','cancelling'].includes(job.status))throw new Error('任务状态已变化，请重新读取后在任务状态中处理');
              notify(ui,querying?'本地查询停止要求已收到；提供方原任务身份与已有结果保留。':'这条排队任务已取消，其他任务保留。');if(typeof restoreJob==='function')void restoreJob(item.run_id);
            }catch(error){if(ui.current())notify(ui,`取消结果尚未确认：${error.message}`,true);}
            finally{busy=false;await load();}
          }));
          rows.append(row);
        }
      }catch(error){if(ui.current())notify(ui,`队列暂时无法读取：${error.message}`,true);}
    }
    await load();
  };
  const queueButton=doc.getElementById('job-queue-open');if(queueButton)queueButton.onclick=()=>api.openQueue();
  api.openAnnotations=async function({runId,target=null,title='',media=null}={}) {
    if (!active(runId)) return;
    const ui=dialog(target ? `批注 · ${title||targetLabel(target)}` : '项目批注汇总','annotation-dialog');
    let snapshot=null, rows=[], region=null, atSeconds=media?.kind==='video'?Number(media.at_seconds||0):null;
    const saveKey=`studio-object-annotation:${runId}:${JSON.stringify(target)}`,executeKey=`studio-object-annotation-execute:${runId}`;
    let busy=false, savePending=readPending(saveKey), executePending=readPending(executeKey);
    const current=()=>ui.current()&&active(runId);
    const form=target?el('form','annotation-form'):null;
    const history=el('section','annotation-list');
    const selected=new Set();
    const execute=button('按所选批注修改',()=>executeSelected(),'primary');execute.disabled=true;
    const refresh=button('重新读取',()=>load());
    if (form) {
      if(media&&safeUrl(runId,media.url)) {
        const preview=el('div','annotation-media');
        if(media.kind==='video') {
          const video=el('video');video.src=media.url;video.controls=true;video.playsInline=true;video.preload='metadata';
          const stamp=el('p','annotation-position',`批注时间：${atSeconds.toFixed(1)} 秒`);
          preview.append(video,button('使用当前播放时间',()=>{atSeconds=Math.round(video.currentTime*10)/10;stamp.textContent=`批注时间：${atSeconds.toFixed(1)} 秒`;}),stamp);
          video.addEventListener('loadedmetadata',()=>{if(Number.isFinite(atSeconds))video.currentTime=atSeconds;},{once:true});
        } else {
          const frame=el('div','annotation-image-frame'), image=el('img'), mark=el('span','annotation-region');
          image.src=media.url;image.alt=title||'要批注的准确图片';image.draggable=false;mark.hidden=true;
          const stamp=el('p','annotation-position','拖动图片框选区域，或直接写整张图的批注。');
          let start=null;
          frame.onpointerdown=event=>{if(event.button!==0||busy||savePending)return;start={x:event.clientX,y:event.clientY};frame.setPointerCapture?.(event.pointerId);event.preventDefault();};
          frame.onpointermove=event=>{if(!start)return;region=normalizedRegion(start,{x:event.clientX,y:event.clientY},image.getBoundingClientRect());
            mark.hidden=!region;if(region){Object.assign(mark.style,{left:`${region.x*100}%`,top:`${region.y*100}%`,width:`${region.width*100}%`,height:`${region.height*100}%`});}};
          frame.onpointerup=event=>{if(!start)return;start=null;frame.releasePointerCapture?.(event.pointerId);stamp.textContent=region?'已选图片区域；批注会绑定原图和这个位置。':'未选区域；批注整张图。';};
          frame.onpointercancel=()=>{start=null;};
          frame.append(image,mark);preview.append(frame,stamp,button('清除区域',()=>{if(!busy&&!savePending){region=null;mark.hidden=true;stamp.textContent='批注整张图。';}}));
        }
        form.append(preview);
      }
      const label=el('label','','批注原话'),input=el('textarea');input.name='annotation';input.rows=3;input.maxLength=4000;input.required=true;
      if(savePending){input.value=savePending.text;input.disabled=true;region=savePending.region||null;atSeconds=savePending.at_seconds??null;}
      label.append(input);const save=button('保存这条批注',()=>{} ,'primary');save.type='submit';
      form.append(label,el('p','object-note','保存不启动修改。可继续批注其他作品，最后在汇总中统一发起。'),save);
      form.onsubmit=async event=>{
        event.preventDefault();if(busy||!current()||!input.value.trim())return;
        savePending ||= {request_id:id(),target,text:input.value,...(region?{region}:{}),...(atSeconds!==null?{at_seconds:atSeconds}:{})};
        writePending(saveKey,savePending);
        busy=true;input.disabled=true;save.disabled=true;notify(ui,'正在保存批注…');
        try{const saved=await request(`${base(runId)}/annotations`,savePending);if(!current())return;
          if(!saved.annotation_id)throw new Error('保存回执未含准确批注身份，请核对原请求。');
          savePending=null;writePending(saveKey,null);input.value='';notify(ui,'批注已保存；尚未启动修改。');await load();
        }catch(error){if([400,409,422].includes(error.status)){savePending=null;writePending(saveKey,null);}if(current())notify(ui,`${savePending?'批注暂未确认':'批注未保存'}：${error.message}。原文已保留${savePending?'，再次保存会核对原请求':''}。`,true);}
        finally{busy=false;input.disabled=Boolean(savePending);save.disabled=false;save.textContent=savePending?'核对原保存请求':'保存这条批注';}
      };
      ui.body.append(form);
    }
    const actions=el('div','object-actions');actions.append(execute,refresh);
    ui.body.append(el('h3','','已保存批注'),history,actions);
    function draw() {
      history.replaceChildren();
      if(!rows.length)history.append(el('p','object-note','还没有已保存的批注。'));
      for(const row of rows) {
        const card=el('label','annotation-row'),check=el('input');check.type='checkbox';check.checked=selected.has(row.annotation_id);
        check.disabled=Boolean(executePending)||!['pending','saved','open'].includes(row.state || 'pending')||row.executable===false;
        check.onchange=()=>{if(check.checked)selected.add(row.annotation_id);else selected.delete(row.annotation_id);execute.disabled=!selected.size||busy;};
        const content=el('div');content.append(el('strong','',targetLabel(row.target||{})),el('p','',row.text),
          el('small','',`${row.at_seconds!=null?`${Number(row.at_seconds).toFixed(1)} 秒 · `:row.region?'已框选区域 · ':''}${statusNames[row.state]||row.state||'已保存'}${row.reason?` · ${row.reason}`:''}`));
        card.append(check,content);history.append(card);
      }
      execute.disabled=!selected.size||busy;
    }
    async function load() {
      if(!current())return;
      try{const result=await request(`${base(runId)}/annotations`);if(!current())return;
        snapshot=result.snapshot_sha256;rows=list(result.items);for(const key of selected)if(!rows.some(row=>row.annotation_id===key&&row.executable!==false&&['pending','saved','open'].includes(row.state||'pending')))selected.delete(key);
        if(executePending)for(const key of executePending.annotation_ids)selected.add(key);draw();
      }catch(error){if(current())notify(ui,`批注列表未能读取：${error.message}`,true);}
    }
    async function executeSelected() {
      if(!current()||busy||!selected.size||!hash(snapshot))return;
      executePending ||= {request_id:id(),annotation_ids:[...selected],expected_snapshot_sha256:snapshot};
      writePending(executeKey,executePending);
      busy=true;execute.disabled=true;notify(ui,'正在核对所选批注与当前作品…');
      try{const result=await request(`${base(runId)}/annotations/execute`,executePending);if(!current())return;
        if(!result.job_id)throw new Error('未能核对本次修改任务身份。');
        if(typeof trackAcceptedJob==='function')trackAcceptedJob(runId,result);
        executePending=null;writePending(executeKey,null);selected.clear();notify(ui,'修改任务已接收；本轮只处理所选批注。');await load();
        if(typeof refreshTimeline==='function')void refreshTimeline();
      }catch(error){if([400,409,422].includes(error.status)){executePending=null;writePending(executeKey,null);}if(current())notify(ui,`${executePending?'尚未确认启动':'本次未启动'}：${error.message}。作品和批注保留，请重新读取核对。`,true);}
      finally{busy=false;draw();execute.textContent=executePending?'核对原修改请求':'按所选批注修改';}
    }
    await load();
  };
  api.openCompare=async function({runId,target,title='作品'}={}) {
    if(!active(runId)||target?.kind!=='artifact')return;
    const ui=dialog(`${title} · 版本比较`),select=el('select'),label=el('label','','另一版本'),result=el('pre','object-diff');
    label.append(select);ui.body.append(el('p','object-note',`当前阅读第 ${target.version} 版；逐字比较由本地程序完成。`),label,result);
    try {
      const versions=await request(`${base(runId)}/artifacts?stage=${encodeURIComponent(target.stage)}`);
      if(!ui.current()||!active(runId))return;
      const available=list(versions).filter(row=>row.version!==target.version&&hash(row.sha256));
      if(!available.length){notify(ui,'没有其他可核对版本。');return;}
      for(const row of available){const option=el('option','',`第 ${row.version} 版${row.withdrawn?' · 已停用':''}`);option.value=String(row.version);select.append(option);}
      const compare=async()=>{const row=available.find(item=>String(item.version)===select.value);if(!row)return;
        notify(ui,'正在比较准确原文…');select.disabled=true;
        try{const data=await request(`${base(runId)}/objects/compare`,{left:target,right:{kind:'artifact',run_id:runId,stage:target.stage,version:row.version,artifact_sha256:row.sha256}});
          if(!ui.current()||!active(runId))return;result.textContent=data.diff || (data.identical?'两份原文一致。':'比较结果未提供差异正文。');notify(ui,'比较完成；原稿与采用状态保持原样。');
        }catch(error){notify(ui,error.message,true);}finally{select.disabled=false;}};
      select.onchange=compare;select.value=String(available.at(-1).version);await compare();
    }catch(error){notify(ui,error.message,true);}
  };
  api.openCanvas=async function(runId,options={}) {
    if(!active(runId))return;
    const ui=options.mount?embeddedCanvas(options.mount,options.onClose):dialog('对象与关系','object-canvas-dialog');
    if(options.mount)ui.panel.dataset.runId=runId;
    let projection=null, selected=null, busy=false, moved=false, pendingLayout=null, readRevision=0;
    const controls=el('div','object-actions'),viewport=el('div','object-graph-viewport'),world=el('div','object-graph'),inspector=el('aside','object-inspector');
    const layout=el('div','object-workspace');layout.append(viewport,inspector);viewport.append(world);
    const save=button('保存布局',()=>savePositions(),'primary');save.disabled=true;
    const zoomControls=el('div','object-zoom-controls');zoomControls.setAttribute('role','group');zoomControls.setAttribute('aria-label','对象画布缩放');
    const zoomOut=button('−',()=>changeZoom(zoomOf()-.1)),zoomIn=button('+',()=>changeZoom(zoomOf()+.1)),zoomReset=button('100%',()=>changeZoom(1)),zoomFit=button('适应画布',()=>fitCanvas());
    zoomOut.setAttribute('aria-label','缩小对象画布');zoomIn.setAttribute('aria-label','放大对象画布');
    zoomReset.title='当前比例；点击恢复到 100%';zoomControls.append(zoomOut,zoomReset,zoomIn,zoomFit);
    const selectedNodes=new Set();
    const groupName=el('input');groupName.placeholder='选中节点的分组名';groupName.maxLength=100;groupName.setAttribute('aria-label','分组名称');
    const noteText=el('input');noteText.placeholder='自由笔记，不参与生成';noteText.maxLength=2000;noteText.setAttribute('aria-label','自由笔记');
    const layoutTools=el('details','object-layout-tools');layoutTools.append(el('summary','','分组与自由笔记'),groupName,
      button('为选中节点分组',()=>{
        if(busy||pendingLayout||!projection||!groupName.value.trim()||!selectedNodes.size)return;
        projection.layout.groups=[...list(projection.layout.groups),{id:id(),title:groupName.value.trim(),node_ids:[...selectedNodes]}];
        groupName.value='';moved=true;save.disabled=false;render();}),noteText,
      button('添加笔记',()=>{
        if(busy||pendingLayout||!projection||!noteText.value.trim())return;
        projection.layout.notes=[...list(projection.layout.notes),{id:id(),text:noteText.value.trim(),node_ids:[...selectedNodes]}];
        noteText.value='';moved=true;save.disabled=false;render();}),el('p','object-note','勾选节点后创建分组；笔记可关联选中的节点，仅用于整理，不进入制作依赖。'));
    controls.append(zoomControls,save,button('撤销未保存摆放',()=>{if(!busy){moved=false;pendingLayout=null;save.disabled=true;void load();}}),
      button('批注汇总',()=>api.openAnnotations({runId})),button('导出勾选作品',()=>api.exportObjects({runId,status:ui.status,
        targets:list(projection?.nodes).filter(node=>selectedNodes.has(node.id)&&['media','artifact'].includes(node.target?.kind)).map(node=>node.target)})),button('重新读取',()=>load()));
    ui.body.append(controls,layoutTools,layout);
    const current=()=>ui.current()&&active(runId);
    ui.panel.studioRefresh=()=>load();
    async function load(){
      if(!current()||busy)return;notify(ui,'正在核对项目对象与真实引用…');
      const revision=++readRevision;
      try{const result=await api.readCanvas(runId);if(!current()||revision!==readRevision)return;
        if(result.run_id!==runId||!hash(result.snapshot_sha256))throw new Error('项目对象快照无法核对。');
        if(moved||inspector.querySelector('[data-unsaved-reference]')){notify(ui,'当前有未保存的布局或参考调整；保留输入，保存或撤销后重新读取。');return;}
        projection=result;render();notify(ui,'引用线来自准确制作记录。拖动节点只调整布局；改变参考会保存新输入版本。');
      }catch(error){if(current()&&revision===readRevision)notify(ui,error.message,true);}
    }
    const pointFor=(node,index)=>projection.layout?.positions?.[node.id]||{x:32+(index%3)*300,y:32+Math.floor(index/3)*230};
    const zoomOf=()=>Math.min(1.6,Math.max(.35,Number(projection?.layout?.view?.zoom)||1));
    function applyCanvasZoom(){
      const zoom=zoomOf();world.style.zoom=String(zoom);
      zoomReset.textContent=`${Math.round(zoom*100)}%`;zoomReset.setAttribute('aria-label',`当前比例 ${Math.round(zoom*100)}%，恢复到 100%`);
      zoomOut.disabled=busy||Boolean(pendingLayout)||zoom<=.35;zoomIn.disabled=busy||Boolean(pendingLayout)||zoom>=1.6;
      zoomReset.disabled=zoomFit.disabled=busy||Boolean(pendingLayout);
    }
    function changeZoom(value){
      if(!projection||busy||pendingLayout)return;
      const zoom=Math.min(1.6,Math.max(.35,Math.round(value*1000)/1000));
      if(zoom===zoomOf())return;
      projection.layout.view={...projection.layout.view,zoom};moved=true;save.disabled=false;applyCanvasZoom();
    }
    function fitCanvas(){
      if(!projection||busy||pendingLayout)return;
      const width=viewport.clientWidth,height=viewport.clientHeight;
      if(!(width>0&&height>0)){notify(ui,'画布尺寸尚未可读取，请显示画布后再次适应。');return;}
      const desired=Math.min(1,(width-32)/parseFloat(world.style.width),(height-32)/parseFloat(world.style.height));
      changeZoom(desired);viewport.scrollLeft=0;viewport.scrollTop=0;
      notify(ui,desired<.35?'画布已缩至最小 35%；对象较多，可继续滚动查看。':'已适应画布；保存布局后会记住当前比例。');
    }
    function updateWorldBounds(){
      const points=[...list(projection.nodes).map(pointFor),...list(projection.layout.notes).map((note,index)=>projection.layout.positions?.[`note:${note.id}`]||{x:950,y:32+index*190})];
      world.style.width=`${Math.max(940,...points.map(point=>point.x+300))}px`;
      world.style.height=`${Math.max(650,...points.map(point=>point.y+300))}px`;
      applyCanvasZoom();
    }
    function renderEdges() {
      world.querySelector('.object-edges')?.remove();
      const svg=doc.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('class','object-edges');
      for(const edge of list(projection.edges)) {
        const from=list(projection.nodes).findIndex(node=>node.id===edge.from),to=list(projection.nodes).findIndex(node=>node.id===edge.to);if(from<0||to<0)continue;
        const a=pointFor(projection.nodes[from],from),b=pointFor(projection.nodes[to],to);
        const path=doc.createElementNS('http://www.w3.org/2000/svg','path');path.setAttribute('d',`M${a.x+252},${a.y+55} C${a.x+280},${a.y+55} ${b.x-28},${b.y+55} ${b.x},${b.y+55}`);
        path.setAttribute('class',edge.editable?'is-reference':'is-history');
        const title=doc.createElementNS('http://www.w3.org/2000/svg','title');title.textContent=edgeNames[edge.purpose||edge.kind]||edge.kind;path.append(title);svg.append(path);
      }
      world.prepend(svg);
    }
    function renderNotesAndGroups() {
      for(const group of list(projection.layout.groups)) {
        const members=list(group.node_ids).map(key=>list(projection.nodes).findIndex(node=>node.id===key)).filter(index=>index>=0);
        if(!members.length)continue;
        const points=members.map(index=>pointFor(projection.nodes[index],index)),left=Math.min(...points.map(p=>p.x))-10,top=Math.min(...points.map(p=>p.y))-24;
        const frame=el('div','object-group-frame');frame.setAttribute('aria-hidden','true');
        Object.assign(frame.style,{left:`${left}px`,top:`${top}px`,width:`${Math.max(...points.map(p=>p.x))+262-left}px`,height:`${Math.max(...points.map(p=>p.y))+210-top}px`});
        frame.append(el('span','',group.title));world.prepend(frame);
      }
      for(const [index,note]of list(projection.layout.notes).entries()) {
        const key=`note:${note.id}`,point=projection.layout.positions?.[key]||{x:950,y:32+index*190};
        const card=el('article','object-note-node');Object.assign(card.style,{left:`${point.x}px`,top:`${point.y}px`});
        const handle=button('自由笔记 · 拖动整理',()=>{},'object-node-handle');let drag=null;
        const move=(x,y)=>{if(busy||pendingLayout)return;projection.layout.positions ||= {};projection.layout.positions[key]={x:Math.max(0,x),y:Math.max(0,y)};
          Object.assign(card.style,{left:`${Math.max(0,x)}px`,top:`${Math.max(0,y)}px`});moved=true;save.disabled=false;updateWorldBounds();};
        handle.onpointerdown=event=>{if(event.button!==0||busy)return;drag={x:event.clientX,y:event.clientY,point:projection.layout.positions?.[key]||point};handle.setPointerCapture?.(event.pointerId);};
        handle.onpointermove=event=>{if(drag)move(drag.point.x+(event.clientX-drag.x)/zoomOf(),drag.point.y+(event.clientY-drag.y)/zoomOf());};
        handle.onpointerup=event=>{drag=null;handle.releasePointerCapture?.(event.pointerId);};handle.onpointercancel=()=>{drag=null;};
        handle.onkeydown=event=>{if(!['ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(event.key)||busy)return;event.preventDefault();
          const p=projection.layout.positions?.[key]||point;move(p.x+(event.key==='ArrowLeft'?-10:event.key==='ArrowRight'?10:0),p.y+(event.key==='ArrowUp'?-10:event.key==='ArrowDown'?10:0));};
        card.append(handle,el('p','',note.text),el('small','',list(note.node_ids).length?`关联 ${note.node_ids.length} 个对象 · 不参与生成`:'项目笔记 · 不参与生成'),button('移除笔记',()=>{
          if(busy||pendingLayout)return;projection.layout.notes=projection.layout.notes.filter(item=>item.id!==note.id);moved=true;save.disabled=false;render();}));world.append(card);
      }
    }
    function render() {
      world.replaceChildren();const nodes=list(projection.nodes);
      if(!nodes.length){world.append(el('p','object-note','当前项目尚无可核对对象，可以先写自由笔记。'));
        updateWorldBounds();renderNotesAndGroups();return;}
      for(const [index,node]of nodes.entries()) {
        const point=pointFor(node,index),card=el('article','object-node');card.dataset.objectId=node.id;
        Object.assign(card.style,{left:`${point.x}px`,top:`${point.y}px`});
        const handle=button(node.title||targetLabel(node.target||{}),()=>selectNode(node),'object-node-handle');
        handle.title='选择对象；拖动或用方向键调整位置';handle.setAttribute('aria-label',node.title||'查看对象');
        const move=(x,y)=>{if(busy||pendingLayout)return;projection.layout ||= {revision:0,positions:{}};projection.layout.positions ||= {};
          projection.layout.positions[node.id]={x:Math.max(0,x),y:Math.max(0,y)};Object.assign(card.style,{left:`${Math.max(0,x)}px`,top:`${Math.max(0,y)}px`});
          moved=true;save.disabled=false;updateWorldBounds();renderEdges();};
        let dragging=null;
        handle.onpointerdown=event=>{if(event.button!==0||busy)return;dragging={x:event.clientX,y:event.clientY,point:pointFor(node,index)};handle.setPointerCapture?.(event.pointerId);};
        handle.onpointermove=event=>{if(dragging)move(dragging.point.x+(event.clientX-dragging.x)/zoomOf(),dragging.point.y+(event.clientY-dragging.y)/zoomOf());};
        handle.onpointerup=event=>{dragging=null;handle.releasePointerCapture?.(event.pointerId);};handle.onpointercancel=()=>{dragging=null;};
        handle.onkeydown=event=>{if(!['ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(event.key)||busy)return;
          event.preventDefault();const p=pointFor(node,index),amount=event.shiftKey?100:10;move(p.x+(event.key==='ArrowLeft'?-amount:event.key==='ArrowRight'?amount:0),p.y+(event.key==='ArrowUp'?-amount:event.key==='ArrowDown'?amount:0));};
        const membership=el('label','object-node-select'),check=el('input');check.type='checkbox';check.checked=selectedNodes.has(node.id);
        check.onchange=()=>{if(check.checked)selectedNodes.add(node.id);else selectedNodes.delete(node.id);};
        membership.append(check,el('span','','选择此对象'));
        card.append(handle,el('p','object-node-status',`${node.current===false&&node.status!=='history'?'历史版本 · ':''}${statusNames[node.status]||node.status||'可查看'}`),membership);
        for(const group of list(projection.layout.groups).filter(row=>list(row.node_ids).includes(node.id)))card.append(el('small','object-group-label',group.title));
        const media=objectMedia(runId,node);
        if(media?.kind==='image'){const img=el('img');img.src=media.url;img.alt=node.title||'对象图片';img.loading='lazy';card.append(img);}
        else card.append(el('small','',targetLabel(node.target||{})));
        if(media?.kind==='image'&&['material','media'].includes(node.target?.kind)) {
          const port=button('拖到制作输入连接参考',()=>notify(ui,'将此控件拖到制作输入节点，再选择用途并保存新版本。'),'object-reference-port');
          port.draggable=true;port.ondragstart=event=>{event.dataTransfer.setData('application/x-studio-object',node.id);event.dataTransfer.effectAllowed='link';};
          card.append(port);
        }
        if(node.target?.kind==='production_draft'&&list(node.allowed_actions).includes('edit_references')) {
          card.ondragover=event=>{if(list(event.dataTransfer?.types).includes('application/x-studio-object'))event.preventDefault();};
          card.ondrop=event=>{
            const sourceId=event.dataTransfer?.getData('application/x-studio-object'),sourceNode=list(projection.nodes).find(row=>row.id===sourceId);
            if(!sourceNode||objectMedia(runId,sourceNode)?.kind!=='image'||!current()||busy)return;
            event.preventDefault();selectNode(node);
            const picker=inspector.querySelector('[data-reference-source]');if(selected?.id!==node.id||!picker)return;
            picker.value=sourceId;inspector.querySelector('[aria-label="参考用途"]')?.focus();
            notify(ui,'已定位这张参考图。选择用途并添加连接，再保存为新输入版本；此时尚未改动制作输入。');
          };
        }
        world.append(card);
      }
      updateWorldBounds();
      renderNotesAndGroups();renderEdges();if(selected)selectNode(nodes.find(node=>node.id===selected.id)||nodes[0]);else selectNode(nodes[0]);
    }
    async function savePositions(){
      if(!current()||busy||!moved)return;busy=true;save.disabled=true;applyCanvasZoom();
      pendingLayout ||= {request_id:id(),expected_revision:projection.layout?.revision||0,
        view:projection.layout?.view||{zoom:1,x:32,y:32},positions:projection.layout?.positions||{},groups:projection.layout?.groups||[],notes:projection.layout?.notes||[]};
      try{const result=await request(`${base(runId)}/canvas/layout`,pendingLayout);if(!current())return;
        if(!savedLayoutMatches(result,pendingLayout))throw new Error('布局保存回执与本次摆放不一致，尚未确认保存');
        projection.layout=result;pendingLayout=null;moved=false;notify(ui,'项目布局已保存。');
      }catch(error){if([400,409,422].includes(error.status))pendingLayout=null;if(current())notify(ui,`${error.message}。本地摆放保留${pendingLayout?'，保存将核对原请求；核对前暂停布局编辑':'，撤销未保存摆放后可重新读取项目布局'}。`,true);}
      finally{busy=false;save.disabled=!moved;save.textContent=pendingLayout?'核对原布局保存':'保存布局';applyCanvasZoom();}
    }
    function selectNode(node){
      if(inspector.querySelector('[data-unsaved-reference]')){notify(ui,'这份参考调整尚未保存，请先保存或撤销调整，再选择其他对象。');return;}
      selected=node;inspector.replaceChildren(el('h3','',node.title||'当前对象'),el('p','object-note',targetLabel(node.target||{})));
      if(node.data?.draft)inspector.append(el('p','object-provenance',`来源方案第 ${node.data.draft.source_version} 版 · ${node.current===true?'当前制作对象':'历史对象，只读追溯；不继承当前批准或授权'}`));
      for(const card of world.querySelectorAll('.object-node'))card.classList.toggle('selected',card.dataset.objectId===node.id);
      const actions=el('div','object-actions'),target=node.target||{},media=objectMedia(runId,node);
      if(target.kind==='artifact')actions.append(button('阅读全文与决定',()=>{if(!options.mount)ui.panel.close();openReader(target.stage,target.version);}),
        button('比较版本',()=>api.openCompare({runId,target,title:node.title})));
      if(['design','shot'].includes(target.kind)){
        actions.append(button('阅读全文与决定',()=>{if(!options.mount)ui.panel.close();openReader(target.stage,target.version);}));
        inspector.append(el('pre','object-entity',node.text||JSON.stringify(node.data?.entity||{},null,2)),
          el('p','object-note','这是原稿中的准确设定或页面。批注可留存；需要修改时，请打开来源原稿提出修改。'));
      }
      if(target.kind==='production_draft')actions.append(button('核对输入与生成',()=>{if(!options.mount)ui.panel.close();openProduction({kind:node.data?.draft?.kind||node.data?.kind||'image',focusDraft:target.draft_id});}));
      if(media) {const pixel=el(media.kind==='video'?'video':'img');pixel.src=media.url;pixel.alt=node.title||'准确对象';if(media.kind==='video'){pixel.controls=true;pixel.playsInline=true;}inspector.append(pixel);}
      if(target.kind)actions.append(button('保存批注',()=>api.openAnnotations({runId,target,title:node.title,media:{...media,at_seconds:inspector.querySelector('video')?.currentTime||0}})));
      if(['media','artifact'].includes(target.kind))actions.append(button('导出这个作品',()=>api.exportObjects({runId,targets:[target],status:ui.status})));
      if(target.kind==='media'){
        actions.append(button('检查实际结果',()=>api.openInspection({runId,target,title:node.title,mediaKind:media?.kind||node.data?.draft?.kind})));
        renderInspection(inspector,node.data?.inspection);
      }
      inspector.append(actions,el('h4','','引用与来源'));
      const edges=list(projection.edges).filter(edge=>edge.from===node.id||edge.to===node.id);
      for(const edge of edges){const other=list(projection.nodes).find(row=>row.id===(edge.from===node.id?edge.to:edge.from));
        inspector.append(el('p','object-relation',`${edgeNames[edge.purpose||edge.kind]||edge.kind} · ${other?.title||'来源待核对'}${edge.editable?'':' · 只读'}`));}
      if(!edges.length)inspector.append(el('p','object-note','此对象没有已记录的引用线。'));
      for(const group of list(projection.layout.groups).filter(row=>list(row.node_ids).includes(node.id))){
        const row=el('p','object-group-label',`分组 · ${group.title}`);row.append(button('移出分组',()=>{
          if(busy||pendingLayout)return;
          group.node_ids=group.node_ids.filter(key=>key!==node.id);moved=true;save.disabled=false;render();}));inspector.append(row);
      }
      const notes=list(projection.layout.notes).filter(row=>!list(row.node_ids).length||row.node_ids.includes(node.id));
      if(notes.length)inspector.append(el('h4','','自由笔记'));
      for(const note of notes){const row=el('div','object-note-card');row.append(el('p','',note.text),el('small','','仅整理，不参与生成'),button('移除笔记',()=>{
        if(busy||pendingLayout)return;
        projection.layout.notes=projection.layout.notes.filter(item=>item.id!==note.id);moved=true;save.disabled=false;render();}));inspector.append(row);}
      if(target.kind==='production_draft'&&list(node.allowed_actions).includes('edit_references'))referenceEditor(node);
    }
    function referenceEditor(node){
      const draft=node.data?.draft||node.data;if(!draft?.draft_id)return;
      const pendingKey=`studio-object-reference:${runId}:${draft.draft_id}`;
      let referencePending=readPending(pendingKey);
      const form=el('form','object-reference-form'),heading=el('h4','','修改参考连接'),rows=el('div');
      let refs=list(draft.references).map(ref=>({...ref}));if(!refs.length&&draft.reference)refs=[{...draft.reference,purpose:draft.kind==='video'?'continuity':'product'}];
      if(referencePending){refs=list(referencePending.references).map(ref=>({...ref}));form.setAttribute('data-unsaved-reference','');}
      const materials=list(projection.nodes).map(row=>({node:row,media:objectMedia(runId,row)})).filter(row=>row.media?.kind==='image'&&
        (row.node.target?.kind==='material'||row.node.target?.kind==='media'));
      const source=el('select'),purpose=el('select'),design=el('input');design.placeholder='人物或场景身份（适用时必填）';
      source.setAttribute('data-reference-source','');
      source.setAttribute('aria-label','参考图片');purpose.setAttribute('aria-label','参考用途');design.setAttribute('aria-label','人物或场景身份');
      for(const item of materials){const option=el('option','',item.node.title||'图片');option.value=item.node.id;source.append(option);}
      for(const [key,label]of Object.entries(purposes)){if(draft.kind==='video'&&key!=='continuity')continue;const option=el('option','',label);option.value=key;purpose.append(option);}
      const show=()=>{rows.replaceChildren();for(const [index,ref]of refs.entries()){const row=el('p','object-relation',`${purposes[ref.purpose]} · ${ref.name}`);row.append(button('移除连接',()=>{refs.splice(index,1);form.setAttribute('data-unsaved-reference','');show();}));rows.append(row);}};
      const add=button('添加参考连接',()=>{const item=materials.find(row=>row.node.id===source.value);if(!item)return;
        const data=item.node.data?.material||item.node.data?.output||item.node.data,name=data?.name||item.node.target.name,sha=data?.sha256||item.node.target.sha256||item.node.target.output_sha256;
        if(!name||!hash(sha)){notify(ui,'此图片的字节身份无法核对。',true);return;}
        refs.push({name,sha256:sha,purpose:purpose.value,...(design.value.trim()?{design_id:design.value.trim()}:{})});form.setAttribute('data-unsaved-reference','');show();});
      const saveRef=button('保存为新输入版本',()=>{},'primary');saveRef.type='submit';
      const cancelRef=button('撤销未保存参考调整',()=>{if(referencePending||busy)return;form.removeAttribute('data-unsaved-reference');selectNode(node);});
      form.append(heading,rows,el('label','','参考图片'),source,el('label','','用途'),purpose,design,add,
        el('p','object-note','保留原描述，生成新输入版本。旧输入与结果保留；新输入需重新核对，不继承费用授权。'),saveRef,cancelRef);
      form.onsubmit=async event=>{event.preventDefault();if(!current()||busy)return;
        try{referencePending ||= referencePayload(draft,refs,id());}catch(error){notify(ui,error.message,true);return;}
        writePending(pendingKey,referencePending);
        busy=true;for(const control of form.querySelectorAll('button,input,select'))control.disabled=true;
        try{const result=await request(`${base(runId)}/production/drafts`,referencePending);if(!current())return;
          if(!savedReferenceMatches(result,referencePending))throw new Error('新输入保存回执无法核对。');
          referencePending=null;writePending(pendingKey,null);form.removeAttribute('data-unsaved-reference');notify(ui,'新输入已保存；没有启动生成。');if(typeof refreshTimeline==='function')void refreshTimeline();
          busy=false;await load();
        }catch(error){if([400,409,422].includes(error.status)){referencePending=null;writePending(pendingKey,null);}if(current())notify(ui,`${error.message}。${referencePending?'原请求保留，请核对后再保存':'没有保存新输入，请重新读取准确对象后修改'}。`,true);}
        finally{busy=false;for(const control of form.querySelectorAll('button,input,select'))control.disabled=Boolean(referencePending);saveRef.disabled=false;saveRef.textContent=referencePending?'核对原保存请求':'保存为新输入版本';}};
      show();if(referencePending){for(const control of form.querySelectorAll('button,input,select'))control.disabled=true;saveRef.disabled=false;saveRef.textContent='核对原保存请求';}
      inspector.append(form);
    }
    await load();
  };
})(typeof window!=='undefined'?window:globalThis);
