/* Attach to the message; upload once, then dispatch the original conversation. */
(function(root){
  'use strict';
  const drafts=new Map();
  function files(runId){return drafts.get(runId)||[];}
  function setFiles(runId,value){drafts.set(runId,[...value]);}
  function clear(runId){drafts.delete(runId);}
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
      remove.type='button';remove.disabled=state.busy;
      remove.onclick=()=>{const next=[...files(state.runId)];next.splice(i,1);setFiles(state.runId,next);render();};
      chip.append(remove);tray.append(chip);
    }
  }
  root.StudioComposer={files,setFiles,clear,upload,render};
  if(typeof module!=='undefined')module.exports=root.StudioComposer;
})(typeof window!=='undefined'?window:globalThis);
