/* Attach to the message; upload once, then dispatch the original conversation. */
(function(root){
  'use strict';
  const drafts=new Map();
  const videoUses=new Map();
  function isVideo(file){return file.type.startsWith('video/')||/\.(mp4|mov|m4v|webm)$/i.test(file.name);}
  function files(runId){return drafts.get(runId)||[];}
  function setFiles(runId,value,use){
    const selected=[...value];drafts.set(runId,selected);
    videoUses.set(runId,Object.fromEntries(selected.flatMap((file,index)=>isVideo(file)&&['product','reference'].includes(use)?[[index,use]]:[])));
  }
  function setVideoUse(runId,index,use){
    if(!['product','reference'].includes(use))return;
    const uses=videoUses.get(runId)||{};uses[index]=use;videoUses.set(runId,uses);
  }
  function missingVideoUse(runId){return files(runId).some((file,index)=>isVideo(file)&&!videoUses.get(runId)?.[index]);}
  function describe(runId,names){
    return names.map((name,index)=>{
      const use=videoUses.get(runId)?.[index];
      return name+(use==='product'?'（商品视频，用于补全商品信息）':use==='reference'?'（参考视频，用于拆解表达）':'');
    }).join('、');
  }
  function clear(runId){drafts.delete(runId);videoUses.delete(runId);}
  async function upload(runId,request){
    const names=[];
    for(const file of files(runId)){
      if(file.size>64*1024*1024)throw Error('视频或图片超过64 MB，请使用较小的原文件。');
      const sha=[...new Uint8Array(await crypto.subtle.digest('SHA-256',await file.arrayBuffer()))].map(b=>b.toString(16).padStart(2,'0')).join('');
      const body=new FormData();body.append('file',file,file.name);
      const response=await request(`/api/runs/${encodeURIComponent(runId)}/materials?retry_sha256=${sha}`,{method:'POST',body});
      if(!response.ok){const error=await response.json();throw Error(typeof error.detail==='string'?error.detail:'附件上传未完成，请保留原文件重试。');}
      names.push(file.name);
    }
    return names;
  }
  function render(){
    const tray=document.getElementById('chat-attachments');if(!tray)return;
    tray.replaceChildren();
    for(const [i,file] of files(state.runId).entries()){
      const chip=box('span','attachment-chip',file.name),remove=box('button','ghost small','移除');
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
        render();
      };
      chip.append(remove);tray.append(chip);
    }
  }
  root.StudioComposer={files,setFiles,setVideoUse,missingVideoUse,describe,clear,upload,render};
  if(typeof module!=='undefined')module.exports=root.StudioComposer;
})(typeof window!=='undefined'?window:globalThis);
