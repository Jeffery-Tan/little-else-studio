"use strict";
(function(root){
  const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const text=v=>v==null?'（空）':typeof v==='string'?v:JSON.stringify(v,null,2);
  const title=(data,row,index)=>data.stage==='direction'?`方向 ${row.key || index+1}`:`镜头 / 页面 ${index+1}`;
  function editor(data, selected, values={}) {
    const row=data.rows[selected];
    if(!row) return `<p class="revision-empty">${esc(data.reason || '这一版还没有方向或镜头卡片。')}</p>`;
    return `<div class="revision-edit-layout"><nav class="revision-objects" aria-label="选择编辑对象">${data.rows.map((r,i)=>`<button type="button" class="revision-object ${i===selected?'selected':''}" data-edit-row="${i}" aria-pressed="${i===selected}"><strong>${esc(title(data,r,i))}</strong><span>${esc((data.stage==='direction'?r.claim:r.picture)||'未填写')}</span></button>`).join('')}</nav><section class="revision-fields"><h3>${esc(title(data,row,selected))}</h3><fieldset ${data.editable?'':'disabled'}>${data.columns.map(field=>`<label>${esc(data.labels[field] || field)}<textarea rows="3" maxlength="4000" data-edit-field="${esc(field)}" data-row="${selected}">${esc(values[`${selected}:${field}`] ?? row[field] ?? '')}</textarea></label>`).join('')}</fieldset></section></div>`;
  }
  function cells(data, values) {
    const edits=[];
    data.rows.forEach((row,index)=>data.columns.forEach(field=>{
      const key=`${index}:${field}`;
      if(Object.hasOwn(values,key) && values[key].trim() !== (row[field]||'')) edits.push({row:index,field,value:values[key].trim()});
    }));
    return edits;
  }
  function changes(rows, labels={}) {
    return `<div class="revision-changes">${rows.map(r=>`<article><h4>${esc(r.path ? r.path.map(p=>typeof p==='number'?`第 ${p+1} 项`:(labels[p]||p)).join(' / ') : `第 ${r.row+1} 项 · ${labels[r.field]||r.field}`)}</h4><div class="revision-before-after"><div><span>修改前</span><pre>${esc(text(r.before))}</pre></div><div><span>修改后</span><pre>${esc(text(r.after))}</pre></div></div></article>`).join('')}</div>`;
  }
  function comparison(diff, labels) {
    const sources=diff.changes.filter(c=>c.path?.[0]==='evidence'),content=diff.changes.filter(c=>c.path?.[0]!=='evidence');
    return `<p class="revision-compare-title">版本 ${diff.from_version} → 版本 ${diff.to_version} · ${content.length} 处内容变化${sources.length?` · ${sources.length} 处依据变化`:''}</p>${diff.inputs_changed?'<p class="material-warning">这两版使用的上游方案或素材依据也有变化。</p>':''}${changes(content,labels)}${sources.length?`<details class="revision-prose"><summary>查看依据记录的变化</summary>${changes(sources,labels)}</details>`:''}<details class="revision-prose"><summary>查看完整正文的文字变化</summary>${diff.prose_truncated?'<p class="material-warning">正文较长，此处仅显示部分差异；请打开两版原文核对。</p>':''}<pre>${esc(diff.prose_diff || '正文没有变化。')}</pre></details>`;
  }
  root.StudioRevisions={editor,cells,changes,comparison};
  if(typeof module!=='undefined')module.exports=root.StudioRevisions;
})(typeof window!=='undefined'?window:globalThis);
