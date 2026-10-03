/* Attach to the message; upload once, then dispatch the original conversation. */
(function(root){
  'use strict';
  const drafts=new Map();
  const videoUses=new Map();
  const uploaded=new Map();
  const frozen=new Map();
  const loaded=new Set();
  function isVideo(file){return String(file.type||'').startsWith('video/')||/\.(mp4|mov|m4v|webm)$/i.test(file.name);}
  function decode(items){
    const selected=[],uses={},receipts=new Set();
    for(const item of Array.isArray(items)?items:[]){
      if(!item||typeof item.name!=='string'||!item.name)continue;
      const file={name:item.name,type:String(item.type||''),size:Number(item.size||0),needsReselection:item.uploaded!==true};
      const index=selected.length;selected.push(file);
      if(['product','reference'].includes(item.use))uses[index]=item.use;
      if(item.uploaded===true)receipts.add(file);
    }
    return {selected,uses,receipts};
  }
  function encode(selected,uses,receipts){
    return selected.map((file,index)=>({name:file.name,type:file.type,size:file.size,
      uploaded:receipts.has(file),use:uses[index]||null}));
  }
  function load(runId){
    if(loaded.has(runId))return;loaded.add(runId);
    let saved;
    try{saved=JSON.parse(root.sessionStorage?.getItem(`studio-composer-files:${runId}`)||'null');}catch(_){}
    if(saved?.version!==1||saved.run_id!==runId)return;
    const current=decode(saved.files);
    drafts.set(runId,current.selected);videoUses.set(runId,current.uses);uploaded.set(runId,current.receipts);
    if(saved.frozen?.requestId)frozen.set(runId,{requestId:saved.frozen.requestId,rejected:saved.frozen.rejected===true,...decode(saved.frozen.files)});
  }
  function persist(runId){
    if(!root.sessionStorage)return;
    const waiting=frozen.get(runId);
    try{root.sessionStorage.setItem(`studio-composer-files:${runId}`,JSON.stringify({version:1,run_id:runId,
      files:encode(drafts.get(runId)||[],videoUses.get(runId)||{},uploaded.get(runId)||new Set()),
      ...(waiting?{frozen:{requestId:waiting.requestId,rejected:waiting.rejected===true,
        files:encode(waiting.selected,waiting.uses,waiting.receipts)}}:{})}));}
    catch(_){if(typeof root.toast==='function')root.toast('浏览器暂时无法保存附件引用，刷新后可能需要重新选择。',true);}
  }
  function files(runId){load(runId);return drafts.get(runId)||[];}
  function setFiles(runId,value,use){
    load(runId);
    const selected=[...value];drafts.set(runId,selected);
    const receipts=uploaded.get(runId)||new Set();
    uploaded.set(runId,new Set(selected.filter(file=>receipts.has(file))));
    videoUses.set(runId,Object.fromEntries(selected.flatMap((file,index)=>isVideo(file)&&['product','reference'].includes(use)?[[index,use]]:[])));
    persist(runId);
  }
  function setVideoUse(runId,index,use){
    load(runId);
    if(!['product','reference'].includes(use))return;
    const uses=videoUses.get(runId)||{};uses[index]=use;videoUses.set(runId,uses);
    persist(runId);
  }
  function missingVideoUse(runId){return files(runId).some((file,index)=>isVideo(file)&&!videoUses.get(runId)?.[index]);}
  function describe(runId,names){
    load(runId);
    return names.map((name,index)=>{
      const use=videoUses.get(runId)?.[index];
      return name+(use==='product'?'（商品视频，用于补全商品信息）':use==='reference'?'（参考视频，用于拆解表达）':'');
    }).join('、');
  }
  function clear(runId,expectedFiles){
    load(runId);
    if(expectedFiles && (files(runId).length!==expectedFiles.length ||
      files(runId).some((file,index)=>file!==expectedFiles[index])))return;
    drafts.delete(runId);videoUses.delete(runId);uploaded.delete(runId);
    persist(runId);
  }
  function freeze(runId,requestId,expectedFiles){
    if(!files(runId).length)return;
    if(expectedFiles&&(files(runId).length!==expectedFiles.length||files(runId).some((file,index)=>file!==expectedFiles[index])))return;
    frozen.set(runId,{requestId,selected:files(runId),uses:videoUses.get(runId)||{},receipts:uploaded.get(runId)||new Set()});
    drafts.set(runId,[]);videoUses.set(runId,{});uploaded.set(runId,new Set());persist(runId);
  }
  function restoreFrozen(runId){
    const waiting=frozen.get(runId);
    if(!waiting||files(runId).length)return false;
    drafts.set(runId,waiting.selected);videoUses.set(runId,waiting.uses);uploaded.set(runId,waiting.receipts);
    frozen.delete(runId);persist(runId);return true;
  }
  function settle(runId,requestId,accepted){
    load(runId);const waiting=frozen.get(runId);
    if(!waiting||waiting.requestId!==requestId)return;
    if(accepted)frozen.delete(runId);
    else{waiting.rejected=true;restoreFrozen(runId);}
    persist(runId);
  }
  function replaceMissingFile(runId,index,file){
    const prior=files(runId)[index];
    if(!prior?.needsReselection||!file||file.name!==prior.name){
      if(typeof root.toast==='function')root.toast('请重新选择同名原文件，或移除这项后添加其他文件。',true);
      return false;
    }
    const selected=[...files(runId)];selected[index]=file;drafts.set(runId,selected);persist(runId);return true;
  }
  async function upload(runId,request){
    const names=[];
    const selected=files(runId);
    const missing=selected.find(file=>file.needsReselection);
    if(missing)throw Error(`「${missing.name}」尚未上传，刷新后需重新选择原文件；已保留标注用途。`);
    const receipts=uploaded.get(runId)||new Set();uploaded.set(runId,receipts);
    for(const file of selected){
      if(receipts.has(file)){names.push(file.name);continue;}
      if(file.size>64*1024*1024)throw Error('视频或图片超过64 MB，请使用较小的原文件。');
      const sha=[...new Uint8Array(await crypto.subtle.digest('SHA-256',await file.arrayBuffer()))].map(b=>b.toString(16).padStart(2,'0')).join('');
      const body=new FormData();body.append('file',file,file.name);
      const response=await request(`/api/runs/${encodeURIComponent(runId)}/materials?retry_sha256=${sha}`,{method:'POST',body});
      if(!response.ok){const error=await response.json();throw Error(typeof error.detail==='string'?error.detail:'附件上传未完成，请保留原文件重试。');}
      receipts.add(file);
      persist(runId);
      names.push(file.name);
    }
    return names;
  }
  function render(){
    const tray=document.getElementById('chat-attachments');if(!tray)return;
    tray.replaceChildren();
    for(const [i,file] of files(state.runId).entries()){
      const runId=state.runId;
      const chip=box('span','attachment-chip'),remove=box('button','ghost small','移除');
      chip.append(box('span','',file.name+(uploaded.get(runId)?.has(file)?' · 已上传，待发送':file.needsReselection?' · 尚未上传，需重新选择':'')));
      if(file.needsReselection){
        const reselect=box('button','ghost small','重新选择');reselect.type='button';reselect.disabled=state.busy;
        reselect.onclick=()=>{
          const picker=document.createElement('input');picker.type='file';
          picker.onchange=()=>{if(replaceMissingFile(runId,i,picker.files?.[0]))render();};
          picker.click();
        };
        chip.append(reselect);
      }
      if(isVideo(file)){
        const select=document.createElement('select');select.setAttribute('aria-label',`${file.name} 的用途`);
        for(const [value,label] of [['','这个视频用于…'],['product','补全商品信息'],['reference','拆解参考表达']]){
          const option=document.createElement('option');option.value=value;option.textContent=label;select.append(option);
        }
        select.value=videoUses.get(state.runId)?.[i]||'';
        select.disabled=state.busy;
        select.onchange=()=>setVideoUse(state.runId,i,select.value);
        chip.append(select);
      }
      remove.type='button';remove.disabled=state.busy;
      remove.onclick=()=>{
        const runId=state.runId,next=[...files(runId)],uses=videoUses.get(runId)||{};
        next.splice(i,1);drafts.set(runId,next);
        videoUses.set(runId,Object.fromEntries(Object.entries(uses).filter(([key])=>Number(key)!==i).map(([key,use])=>[Number(key)>i?Number(key)-1:Number(key),use])));
        persist(runId);
        render();
      };
      chip.append(remove);tray.append(chip);
    }
    if(frozen.get(state.runId)?.rejected){
      const restore=box('button','ghost small','恢复上次被拒绝的附件');restore.type='button';
      restore.disabled=state.busy||files(state.runId).length>0;
      restore.title=files(state.runId).length?'先移除当前附件，再恢复此前附件。':'';
      restore.onclick=()=>{restoreFrozen(state.runId);render();};tray.append(restore);
    }
  }
  root.StudioComposer={files,setFiles,setVideoUse,missingVideoUse,describe,clear,upload,render,freeze,settle,replaceMissingFile};
  if(typeof module!=='undefined')module.exports=root.StudioComposer;
})(typeof window!=='undefined'?window:globalThis);
