/* On-demand task and method evidence. Read-only, without inferred quality scores. */
"use strict";
(function(root){
  if(!root.document)return;
  const doc=root.document,list=value=>Array.isArray(value)?value:[];
  const el=(tag,cls,text)=>{const node=doc.createElement(tag);if(cls)node.className=cls;if(text!==undefined)node.textContent=String(text);return node;};
  const button=(text,fn)=>{const node=el('button','ghost small',text);node.type='button';node.onclick=fn;return node;};
  const active=runId=>typeof state!=='undefined'&&state.runId===runId;
  function open(title,runId){
    const opener=doc.activeElement,panel=el('dialog','object-dialog evidence-dialog'),header=el('header','object-dialog-header');
    panel.setAttribute('aria-label',title);header.append(el('h2','',title),button('关闭',()=>panel.close()));
    const status=el('p','object-status','正在读取准确记录…'),body=el('div','object-dialog-body');status.setAttribute('role','status');
    panel.append(header,status,body);doc.body.append(panel);panel.addEventListener('close',()=>{panel.remove();
      const modal=doc.querySelector('dialog[open]')||[doc.getElementById('reader'),doc.getElementById('viewer')].find(node=>node&&!node.hidden);
      if(opener?.isConnected&&(!modal||modal.contains(opener)))opener.focus({preventScroll:true});});panel.showModal();
    return {body,status,current:()=>panel.open&&panel.isConnected&&active(runId)};
  }
  async function get(path){const response=await root.fetch(path),data=await response.json();if(!response.ok)throw new Error(typeof data.detail==='string'?data.detail:`记录未能读取（${response.status}）`);return data;}
  const value=(number,suffix='')=>typeof number==='number'&&Number.isFinite(number)?`${number}${suffix}`:'尚未记录';
  const fields=(parent,entries)=>{const list=el('dl','evidence-fields');for(const [key,text]of entries)list.append(el('dt','',key),el('dd','',text));parent.append(list);};
  const api=root.StudioEvidence={};
  api.openUsage=async function({runId,jobId}){
    if(!active(runId)||!jobId)return;const ui=open('任务用量与输入组成',runId);
    try{const data=await get(`/api/runs/${encodeURIComponent(runId)}/tasks/${encodeURIComponent(jobId)}/usage`);if(!ui.current())return;
      ui.status.textContent='本页统计本任务的文字与理解调用；图片/视频生成费用请查看制作记录。未知用量与费用保持未核实。';
      fields(ui.body,[['任务',jobId],['文字与理解调用',value(data.calls,' 次')],['输入 Token',value(data.input_tokens)],['输出 Token',value(data.output_tokens)],
        ['用量记录',data.usage_complete?'完整':'尚有未确认项'],['估算费用',value(data.estimated_usd,' USD')],['费用记录',data.cost_complete?'已核对完整':'尚有未知项'],
        ['任务耗时',value(data.elapsed_seconds,' 秒')],['模型累计耗时',value(data.model_elapsed_seconds,' 秒')],
        ['最多模型调用',value(data.budget?.max_calls,' 次')],['最长执行时间',value(data.budget?.timeout_seconds,' 秒')]]);
      ui.body.append(el('h3','','输入字节组成'));
      fields(ui.body,Object.entries({system:'系统说明',messages:'对话与工作材料',tools:'工具定义',tool_results:'工具结果'}).map(([key,title])=>[title,value(data.input_bytes?.[key],' bytes')]));
      const details=el('details','learning-details');details.append(el('summary','','逐次调用归属'));
      for(const call of list(data.call_details)){const row=el('article','object-queue-row');row.append(el('strong','',`${call.role||'角色未记录'} · ${call.stage||'阶段未记录'}`),el('p','',call.call_id||'调用身份未记录'));
        fields(row,Object.entries(call.input_bytes||{}).map(([key,count])=>[key,value(count,' bytes')]));details.append(row);}
      ui.body.append(details);
    }catch(error){if(ui.current())ui.status.textContent=`任务证据未能读取：${error.message}`;}
  };
  api.openMethodResults=async function(runId){
    if(!active(runId))return;const ui=open('方法与作品证据',runId);
    try{const data=await get(`/api/runs/${encodeURIComponent(runId)}/learning/method-results`);if(!ui.current())return;
      const rows=list(data.results);ui.status.textContent='保留方法版本、作品、原反馈及输入可比范围；不将采用等同于方法有效。';
      if(!rows.length){ui.body.append(el('p','object-note','还没有同时绑定准确方法和作品的使用记录。'));return;}
      const before=el('select'),after=el('select'),leftLabel=el('label','','原记录'),rightLabel=el('label','','比较记录');
      before.setAttribute('aria-label','原方法记录');after.setAttribute('aria-label','比较方法记录');
      for(const row of rows){
        const title=`${row.method?.method_id||'方法'} · ${row.artifact?.stage||'作品'} 第 ${row.artifact?.version} 版`;
        for(const picker of [before,after]){const option=el('option','',title);option.value=row.usage_request_id;picker.append(option);}
        const card=el('article','object-queue-row');card.append(el('h3','',title),el('p','object-note',`方法指纹 ${row.method?.method_sha256||'缺失'} · 作品指纹 ${row.artifact?.sha256||'缺失'}`));
        for(const observation of list(row.observations))card.append(el('p','',observation.claim),el('small','object-note',observation.limits||'仅代表原反馈范围'));
        if(!list(row.observations).length)card.append(el('p','object-note','尚无绑定该作品的已保存用户反馈。'));
        card.append(el('p','object-note',row.input_binding_evidence?'角色任务输入已有准确绑定；媒体与提供方参数尚未核对。':'输入证据不足，暂不能判断是否同条件。'),button('阅读准确作品',()=>openReader(row.artifact.stage,row.artifact.version)));
        ui.body.append(card);
      }
      leftLabel.append(before);rightLabel.append(after);const output=el('section','method-comparison-output');
      const compare=button('比较两份记录',async()=>{
        if(before.value===after.value){output.textContent='请选择两份不同记录。';return;}
        compare.disabled=true;
        try{const result=await get(`/api/runs/${encodeURIComponent(runId)}/learning/method-comparison?baseline=${encodeURIComponent(before.value)}&candidate=${encodeURIComponent(after.value)}`);if(!ui.current())return;
          output.replaceChildren(el('h3','','可比范围'),el('p','',result.same_task_inputs_verified?'已核对文字任务输入相同。':'文字任务输入尚未证实相同，不能据此判断方法改善。'),
            el('p','object-note',result.same_inputs_verified?'完整输入核验范围以记录为准。':'媒体像素与提供方参数尚未核对。'));
          for(const limit of list(result.limits))output.append(el('p','object-note',limit));
          output.append(el('p','object-note','创意验收未评估，因果改善未成立；成本和耗时未归因到方法。'));
        }catch(error){if(ui.current())output.textContent=`比较未完成：${error.message}`;}finally{compare.disabled=false;}
      });
      compare.disabled=rows.length<2;before.value=rows[0].usage_request_id;after.value=rows.at(-1).usage_request_id;
      ui.body.prepend(leftLabel,rightLabel,compare,output);
    }catch(error){if(ui.current())ui.status.textContent=`方法证据未能读取：${error.message}`;}
  };
})(typeof window!=='undefined'?window:globalThis);
