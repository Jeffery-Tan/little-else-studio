/* Native learning room. Reads shared canonical sources; writes only on an
 * explicit preview/apply gesture. No model, scraping, or media-generation call. */
"use strict";
(function (root) {
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const list = (v) => Array.isArray(v) ? v : [];
  const labels = { id:"标识",title:"标题",role:"注入角色",intent:"请求用途",format:"内容形式",purpose:"检索用途",scope:"适用范围",source:"来源记录",source_url:"来源链接",source_market_status:"地域证据边界",partition:"资料分区",origin:"来源类型",platform:"平台",strategy:"表达机制",evidence:"证据记录",limits:"限制与未决项",user_feedback:"用户反馈",feedback_history:"反馈历史",market_effect:"市场效果",eligible_for:"创作用途资格",tags:"标签",decision:"方法",when:"适用情境",countercondition:"不适用条件",supports:"支持证据",counterexamples:"反例",status:"状态",observation:"观察原文",kind:"证据类型",quote:"用户原话",action:"后续动作",hypothesis:"待验证假设",rationale:"理由",changes:"候选变更",stage:"产物阶段",version:"版本",artifact_sha256:"产物 SHA256",sha256:"SHA256",hash:"快照 SHA256",skills:"实际注入 Skills",seed_case_ids:"检索种子案例",case_ids:"含依赖的案例",evidence_ref:"注入记录",created_at:"建立时间",base_library_sha256:"原库 SHA256",candidate_sha256:"候选库 SHA256",payload:"提案内容",type:"提案类型",library_sha256:"原库 SHA256",library_version:"库版本",content:"内容",text:"内容" };
  const scopes = {user_preference:"局部偏好",direction:"内容方向",persona_dialogue:"人物与说话方式",pace:"节奏",product_fact:"商品事实",input_drift:"制作输入偏移",generation_execution:"生成执行",tool_failure:"工具故障"};
  const actions = {diagnose:"先诊断",local_change:"局部调整",stop:"暂停",select:"选择方向",continue:"继续",keep:"保留",revise:"修订",retire:"退役"};
  const stages = {intake:"商品与需求",direction:"内容方向",material_plan:"素材计划",writing:"脚本与文案"};
  const skills = {"us-commerce-strategy":{title:"购买策略",description:"先找到购买处境，再确定值得说的理由。"},"us-creator-video":{title:"视频创作",description:"把理由写进人物、台词和看得见的动作。"},"us-photo-copy":{title:"图文创作",description:"让每张图回答一个问题，让文案补上选择理由。"}};
  const roles = {orchestrator:"制片",director:"创意导演",writer:"编剧",material:"素材策划",reviewer:"独立审核"};
  const methodTitles = {"role-to-benefit":"把功能接到真实负担","reason-not-filler":"用具体理由代替填充话","choose-speaking-form":"选择合适的说话方式","pace-preserves-reasons":"节奏加快，理由留下","audio-can-lead":"让讲述引出画面证据","diagnose-before-repair":"先诊断，再决定怎样改"};
  const tags = {packing:"装包",organization:"收纳",speech:"口播",proof:"证据",pace:"节奏",persona:"人物",benefit:"使用理由",family:"家庭",lunch:"午餐",dialogue:"对话",product:"商品",feedback:"反馈",audio:"声音",visual:"画面"};
  Object.assign(labels,{audience_hypothesis:"可能的人群（假设）",situation:"使用处境",belief_change:"希望改变的判断",speaker_relationship:"讲述者的关系",proof:"画面提供的证据",form:"表达形式"});
  const names = {calibration:"校准材料",holdout:"保留测试 · 不入创作",pending:"待研究 · 不入创作",retired:"已退役",hypothesis:"假设 · 待验证",user_calibrated:"用户校准 · 非效果认证",applied:"已入库",feedback:"自然反馈",source:"新参考登记",method:"方法调整",observation:"人工观察",message:"表达与说法",storyboard:"动作与分镜",performance_review:"结果复盘"};
  const name = (v) => names[v] || v;
  const badge = (s,restricted=false) => `<span class="learning-badge${restricted?' restricted':''}">${esc(name(s))}</span>`;
  const meta = (s) => `<div class="learning-meta">${esc(s)}</div>`;
  const empty = (s) => `<p class="learning-empty">${esc(s)}</p>`;
  const button = (label,action,id="",cls="ghost") => `<button type="button" class="${cls}" data-learning-action="${action}" data-id="${esc(id)}">${esc(label)}</button>`;
  const short = (value,limit=120) => {const text=String(value||"");return text.length>limit?text.slice(0,limit)+"…":text;};
  const methodTitle = (item) => item.title||methodTitles[item.id]||short(item.decision,28)||"创作经验";
  const audit = (value,title="查看来源与版本记录") => `<details class="learning-details learning-audit"><summary>${esc(title)}</summary>${fields(value)}</details>`;
  const bullets = (items) => list(items).length?`<ul class="learning-reading-list">${items.map(s=>`<li>${esc(s)}</li>`).join("")}</ul>`:"";
  const caseBoundary = (item) => item.partition==="holdout"?"仅供对照，不用于创作":item.partition==="pending"?"待补充观察":item.partition==="retired"?"已归档":"已收录观察";
  function mediaHtml(item,thumbnail=false) {
    if(!item.media)return `<div class="learning-no-preview"><span>暂无本地预览</span><small>可阅读已有观察记录</small></div>`;
    const url=`/api/learning/cases/${encodeURIComponent(item.id)}/media`;
    const image=/\.(png|jpe?g|webp|gif)$/i.test(item.media.path||item.media.resource_id||"");
    if(image)return `<img class="${thumbnail?'learning-thumbnail':'learning-media'}" src="${esc(url)}" loading="lazy" alt="${esc(item.title||'参考图片')}">`;
    return `<video class="${thumbnail?'learning-thumbnail':'learning-media'}" ${thumbnail?'muted tabindex="-1" aria-hidden="true"':'controls'} playsinline preload="metadata" src="${esc(url)}#t=0.1"></video>`;
  }
  function safeURL(raw) { try { const url = new URL(raw); return ["http:","https:"].includes(url.protocol) && !url.username && !url.password ? url.href : null; } catch (_) { return null; } }
  function fields(value) {
    if (value == null) return '<span class="learning-note">未记录 / 未验证</span>';
    if (Array.isArray(value)) return value.length ? `<ul>${value.map(v=>`<li>${fields(v)}</li>`).join("")}</ul>` : '<span class="learning-note">未记录</span>';
    if (typeof value === "object") return `<dl class="learning-fields">${Object.entries(value).filter(([k])=> !["media","path"].includes(k)).map(([k,v])=>`<div><dt>${esc(labels[k]||k)}</dt><dd>${fields(v)}</dd></div>`).join("")}</dl>`;
    return esc(value);
  }
  function filtered(items, query="", purpose="", tag="") {
    const q=query.trim().toLowerCase();
    return list(items).filter(item=>(!q || JSON.stringify(item).toLowerCase().includes(q)) && (!purpose || list(item.eligible_for || item.purposes).includes(purpose)) && (!tag || list(item.tags).includes(tag)));
  }
  function caseCard(item) {
    const observation=list(item.evidence).find(e=>e.observation)?.observation||item.strategy?.situation||item.scope||"尚无观察记录，可先查看来源。";
    return `<article class="learning-card learning-reference"><button type="button" class="learning-thumb" data-learning-action="case" data-id="${esc(item.id)}" aria-label="查看${esc(item.title||'参考')}">${mediaHtml(item,true)}${item.media?'<span class="learning-preview-label">查看参考 ↗</span>':""}</button><div class="learning-reference-body"><h4>${esc(item.title||"未命名参考")}</h4><p class="learning-excerpt">${esc(short(observation))}</p><div class="learning-reference-footer"><span>${esc(caseBoundary(item))}</span>${button("查看记录","case",item.id)}</div>${audit({id:item.id,partition:item.partition,eligible_for:item.eligible_for},"资料范围")}</div></article>`;
  }
  function methodCard(item) {
    return `<article class="learning-card learning-method"><span class="learning-kicker">${esc(item.status==='user_calibrated'?'来自用户校准':item.status==='retired'?'已归档':'待验证的经验')}</span><h4>${esc(methodTitle(item))}</h4><p>${esc(item.decision)}</p>${button("适用场景与反例 →","method",item.id)}</article>`;
  }
  function skillCard(item) {
    const skill=skills[item.id]||{title:item.title||"创作方法",description:"阅读已登记的方法与使用边界。"};
    return `<article class="learning-card learning-skill"><h4>${esc(skill.title)}</h4><p>${esc(skill.description)}</p>${button("阅读方法 →","skill",item.id)}${audit({id:item.id,version:item.version,sha256:item.sha256},"版本记录")}</article>`;
  }
  function contextCard(context,renderText=esc) {
    const snapshot=context.snapshot||{},provided=list(snapshot.skills||context.skills);
    const methods=list(snapshot.library?.methods),directory=list(snapshot.method_directory?.methods),references=list(snapshot.seed_case_ids||context.seed_case_ids);
    return `<article class="learning-card learning-context-card"><div class="learning-context-heading"><h4>${esc(roles[context.role]||"创作角色")}的参考</h4><span>${esc(name(context.purpose)||"创作准备")}</span></div><p>${provided.length?`本次提供了${provided.map(s=>esc(skills[s.id]?.title||s.title||"创作方法")).join("、")}。`:"本次没有提供创作方法文本。"}${references.length?`同时提供 ${references.length} 条参考案例。`:"未检索参考案例。"}</p>${directory.length?`<h5>合资格方法目录 · 提供过，不代表选用</h5>${bullets(directory.map(m=>`${m.id} · v${m.revision} · ${m.retrieval_summary||m.decision||''}`))}`:""}${methods.length?`<h5>案例关联的方法</h5>${bullets(methods.map(m=>m.decision))}`:""}${snapshot.text?`<details class="learning-details"><summary>阅读这次提供的完整内容</summary><article class="learning-prose">${renderText(snapshot.text)}</article></details>`:""}${audit(context,"核对完整快照与版本")}</article>`;
  }
  function methodUsageCard(item) {
    const chosen=item.decision==='chosen',artifact=item.artifact||{};
    return `<article class="learning-card"><h4>${esc(item.method_id||'方法')} · v${esc(item.revision||'?')} · ${chosen?'声明已用':'决定不用'}</h4>`+
      `<p>${esc(item.reason||'未记录理由')}</p>`+
      (chosen?`<p class="learning-note">对应 ${esc(stages[artifact.stage]||artifact.stage||'稿件')} 第 ${esc(artifact.version||'?')} 版：${esc(artifact.passage||'未记录段落')}</p>`:'')+
      `<p class="learning-note">制片使用声明；实现情况和效果尚未独立核实。</p>`+
      `${audit({method_id:item.method_id,revision:item.revision,library_sha256:item.library_sha256,artifact:item.artifact,task_profile:item.task_profile},"核对方法与作品版本")}</article>`;
  }
  function caseDetail(item) {
    const source=safeURL(item.source_url);
    const observations=list(item.evidence).filter(e=>e.observation);
    const feedback=[...(item.user_feedback?.quote?[item.user_feedback]:[]),...list(item.feedback_history)];
    return `<h3>${esc(item.title||"参考记录")}</h3><p class="learning-note">${esc(caseBoundary(item))}${item.market_effect==null?" · 市场效果尚未验证":""}</p><div class="learning-case-layout"><div>${mediaHtml(item)}${source?`<p><a href="${esc(source)}" target="_blank" rel="noopener noreferrer">打开原始来源 ↗</a></p>`:""}</div><section><h4>已记录的观察</h4>${observations.length?observations.map(e=>`<p class="learning-observation">${esc(e.observation)}</p>`).join(""):empty("还没有具体观察。文件存在，不代表已经看过。")}${item.strategy&&Object.keys(item.strategy).length?`<details class="learning-details"><summary>表达方式与购买理由</summary>${fields(item.strategy)}</details>`:""}</section></div><section class="learning-reading-section"><h4>使用边界</h4>${bullets(item.limits)||'<p class="learning-note">尚未记录具体限制，请先补充观察。</p>'}</section><section class="learning-reading-section"><h4>用户反馈</h4>${feedback.length?feedback.map(f=>`<blockquote class="learning-quote">${esc(f.quote||f.observation||"")}</blockquote>`).join(""):empty("暂无用户反馈。")}</section>${audit(item,"查看完整证据与版本记录")}`;
  }
  const referenceUpdateMessage="项目参考工具待服务更新。现有灵感、方法与学习记录仍可阅读；项目参考尚未加载。";
  async function loadProjectReferences(request, projectId) {
    if(!projectId)return {capability:true,snapshot:null,references:[]};
    try{return {...await request(`/api/runs/${encodeURIComponent(projectId)}/references`),capability:true};}
    catch(error){if(error.status===404)return {capability:false,snapshot:null,references:null};throw error;}
  }
  function referenceForm(item, projectId, snapshot, chosen, capability=true) {
    if(!projectId)return empty("先新建或选择一个项目，再将灵感应用到该项目。");
    if(capability===false)return empty(referenceUpdateMessage);
    const purposes=item.partition==='calibration'?list(item.eligible_for).filter(p=>['message','storyboard','performance'].includes(p)):[];
    if(!purposes.length)return empty("这条参考目前仅供阅读，还没有可用于创作的观察资格。");
    return `<section class="learning-reading-section"><h4>应用到当前项目</h4><p class="learning-note">只借鉴表达，不沿用他品规格、卖点或效果。保存免费；后续创作才会使用，不会立即调用模型。已有方向与后续稿件会标记为需要更新。</p><form class="learning-form" data-kind="reference" data-id="${esc(item.id)}" data-hash="${esc(item.case_sha256)}"><label>用在哪里<select name="purpose">${purposes.map(p=>`<option value="${esc(p)}" ${chosen?.purpose===p?'selected':''}>${esc(p==='performance'?'结果分析':name(p))}</option>`).join('')}</select></label><label>这次想借鉴什么<textarea name="expression" required maxlength="2000" placeholder="例如：先展示取东西时的麻烦，再用一个连续动作呈现解决过程。只借鉴这个表达顺序。">${esc(chosen?.expression||'')}</textarea></label><button class="primary" type="submit">${chosen?'更新项目参考':'加入项目参考'}</button></form></section>`;
  }
  function projectReferences(items, capability=true) {
    if(capability===false)return `<section class="learning-reading-section"><h3>为当前项目选择的灵感</h3>${empty(referenceUpdateMessage)}</section>`;
    return `<section class="learning-reading-section"><h3>为当前项目选择的灵感</h3><p class="learning-note">仅在你选择的用途与表达范围内提供给后续创作。已生成的旧稿不会自动改写。</p>${list(items).map(item=>`<article class="learning-card"><h4>${esc(item.title)}</h4><p>${esc(item.purpose==='performance'?'结果分析':name(item.purpose))} · ${esc(item.expression)}</p>${item.status==='ready'?'':`<p class="learning-notice">${item.status==='missing'?'原案例已不可用':'参考内容已经变化'}，请重新核对并选择，或移除；不会自动换用新版。</p>`}<div class="learning-toolbar">${button('查看参考','case',item.case_id)}${button('移出本项目','reference-remove',item.case_id,'ghost')}</div></article>`).join('')||empty('还没有选择项目参考。打开一条灵感，写下这次想借鉴的表达。')}</section>`;
  }
  function diffHtml(diff) {
    const lines = typeof diff === "string" ? diff.split("\n") : list(diff);
    return `<pre class="learning-diff">${lines.map(line=>`<span class="${String(line).startsWith('+')?'added':String(line).startsWith('-')?'removed':''}">${esc(line)}</span>`).join("\n")}</pre>`;
  }
  function proposalHtml(p) {
    const data=p.payload||{};
    let summary;
    if(p.type==="feedback")summary={quote:data.quote,stage:`${stages[data.stage]||data.stage} · v${data.version}`,scope:scopes[data.scope]||data.scope,action:actions[data.action]||data.action,hypothesis:data.hypothesis||"未提出"};
    else if(p.type==="source")summary={title:data.title,source_url:data.url,tags:data.tags};
    else if(p.type==="method")summary={action:actions[data.action]||data.action,rationale:data.rationale,...(data.changes?{changes:Object.fromEntries(Object.entries(data.changes).filter(([key])=>!["supports","counterexamples"].includes(key)))}:{})};
    else if(p.type==="observation")summary={observation:data.observation,scope:data.scope,source:data.source,purpose:name(data.purpose),strategy:data.strategy,limits:data.limits};
    else summary={type:p.type};
    const effect={feedback:"保存后，这段原话会留在所选稿件的反馈记录中。",source:"保存后，这条参考会进入待研究列表，等待补充观察。",method:"保存后，将按这里的判断更新这条方法。",observation:"保存后，这份观察将连同范围与限制进入参考记录。"};
    return `<section class="learning-preview"><span class="learning-kicker">${esc(p.status==='pending'?'待审阅 · 等待你的确认':name(p.status))}</span><h3>${esc(name(p.type))}</h3><p class="learning-note">${esc(effect[p.type]||"请核对这次准备保存的内容。")}</p>${fields(summary)}<details class="learning-details learning-diff-review"><summary>查看完整候选差异</summary>${diffHtml(p.diff)}</details><details class="learning-details learning-audit"><summary>核对原始记录与版本</summary>${meta(p.id)}${fields({base_library_sha256:p.base_library_sha256,candidate_sha256:p.candidate_sha256})}${fields(data)}</details><p class="learning-note">仅保存学习记录，不启动制作。</p>${p.status==='pending'?button("确认并保存","apply",p.id,"primary"):""}</section>`;
  }
  const documentPaths = {
    "learning/README.md":"learning-readme", "learning/strategy-guide.md":"strategy-guide",
    "learning/execution.md":"execution", "learning/workflow-skills.md":"workflow-skills",
    "learning/batch-001.md":"batch-001", "learning/commerce-methods-research.md":"commerce-methods-research",
    "learning/photo-copy-methods-research.md":"photo-copy-methods-research", "learning/tiktok-field-study.md":"tiktok-field-study",
    "photo-copy/README.md":"photo-copy-readme", "comparison/README.md":"comparison-readme",
    "comparison/baseline-001.md":"baseline-001", "docs/ai-video-recovery-policy.md":"recovery-policy",
    "docs/creative-package-contract.md":"creative-package-contract"
  };
  function documentHtml(text, available, renderer) {
    text=String(text||"");let marker="STUDIODOCREF";
    while(text.includes(marker))marker+="X";
    const replacements=[];
    const prepared=text.replace(/\[([^\]\n]+)\]\(([^\s)]+)\)/g,(whole,label,path)=>{
      if(/^https?:\/\//i.test(path))return whole;
      const clean=path.split("#")[0].replaceAll("\\","/");
      const candidates=Object.entries(documentPaths).filter(([suffix])=>clean===suffix||clean.endsWith("/"+suffix)||(!clean.includes("/")&&suffix.endsWith("/"+clean)));
      const found=candidates.length===1?candidates[0]:null;
      const skill=/(us-commerce-strategy|us-creator-video|us-photo-copy)\/SKILL\.md$/.exec(clean)?.[1];
      const id=skill||found?.[1];
      if(!id||!available.includes(id))return `${label}（${clean.split("/").at(-1)||"本页章节"}）`;
      const token=`${marker}${replacements.length}END`;
      replacements.push([token,button(label,skill?"skill":"document",id,"ghost learning-doc-link")]);return token;
    });
    let html=renderer(prepared);
    for(const [token,link] of replacements)html=html.replaceAll(token,link);
    return html;
  }
  function feedbackPayload(artifact, quote, scope) {
    if (!artifact?.stage || !Number.isInteger(artifact.version) || !artifact.artifact_sha256) throw new Error("先选择有版本与哈希的实际产物。");
    if (!quote.trim() || !Object.hasOwn(scopes, scope)) throw new Error("请写下原话，以及它针对的具体范围。");
    return {stage:artifact.stage,version:artifact.version,artifact_sha256:artifact.artifact_sha256,quote,scope,action:"diagnose",observation:quote,hypothesis:null};
  }
  function createActions(request,runId) {
    const base=`/api/runs/${encodeURIComponent(runId)}/learning/proposals`;
    return {
      preview:(type,payload)=>{if(!runId) throw new Error("先选择一个项目，再建立绑定该项目的提案。"); return request(base,{method:"POST",body:JSON.stringify({type,payload})});},
      apply:(proposal)=>{if(!runId || !proposal?.id || !proposal.base_library_sha256 || proposal.status!=="pending") throw new Error("此提案不能保存，请重新打开提案查看状态。"); return request(`${base}/${encodeURIComponent(proposal.id)}/apply`,{method:"POST",body:JSON.stringify({expected_library_sha256:proposal.base_library_sha256})});}
    };
  }
  root.StudioLearning={esc,safeURL,fields,filtered,caseCard,methodCard,methodUsageCard,diffHtml,proposalHtml,feedbackPayload,createActions,documentHtml,skillCard,contextCard,mediaHtml,caseDetail,referenceForm,projectReferences,loadProjectReferences};
  if(typeof document==="undefined") return;
  const opener=document.getElementById("learning-open");
  if(!opener) return;
  const tabs={cases:"灵感参考",sources:"创作方法",methods:"经验与判断",context:"本轮参考",feedback:"反馈记录"};
  let dialog,main,status,catalog={},run={},runId=null,tab="cases",proposal=null,serial=0,busy=false,referenceState={snapshot:null,references:[]};
  function notify(message,error=false) {status.textContent=message;status.className="learning-status"+(error?" error":"");}
  async function request(path,options) { let response;try{response=await fetch(path,{headers:{"content-type":"application/json"},...options});}catch(_){throw new Error("暂时无法连接资料库，请确认本地制片室正在运行后重试。");} let data;try{data=await response.json();}catch(_){data={};} if(!response.ok){const error=new Error(typeof data.detail==="string"?data.detail:`请求失败（${response.status}）`);error.status=response.status;throw error;}return data; }
  function actionError(err) {notify(err.status===409?`未保存：${err.message}。若库或产物版本已变化，请重新建立并审阅候选；没有自动覆盖或重基。`:err.message,true);}
  function lock(value){busy=value;dialog.querySelectorAll("button").forEach(n=>{if(value){n.dataset.learningDisabled=String(n.disabled);n.disabled=true;}else if(n.dataset.learningDisabled!==undefined){n.disabled=n.dataset.learningDisabled==="true";delete n.dataset.learningDisabled;}});}
  async function perform(fn) {if(busy)return;lock(true);try{await fn();}catch(err){actionError(err);}finally{lock(false);}}
  function runPath(suffix=""){return `/api/runs/${encodeURIComponent(runId)}/learning${suffix}`;}
  async function refresh() {
    const token=++serial;
    const c=await request("/api/learning");
    const r=runId?await request(runPath()):{artifacts:[],contexts:[],proposals:[]};
    const packages=runId?await request(`/api/runs/${encodeURIComponent(runId)}/creative-packages`):{packages:[]};
    const refs=await loadProjectReferences(request,runId);
    if(token!==serial||!dialog.open)return;
    catalog=c;run={...r,packages:list(packages.packages)};referenceState=refs;
    dialog.querySelector(".learning-sub").textContent="让参考有出处，让每次判断留得下来。";
    render();
  }
  function openRoom(options={}) {
    const requestedTab=options.caseId?"cases":Object.hasOwn(tabs,options.tab)?options.tab:"cases";
    if(dialog?.open)return perform(async()=>{tab=requestedTab;await refresh();if(options.caseId){tab="cases";await act("case",options.caseId);}});
    runId=typeof state!=="undefined"?state.runId:document.getElementById("run-select")?.value;
    proposal=null;tab=requestedTab;
    dialog=document.createElement("dialog");dialog.className="learning-dialog";dialog.setAttribute("aria-labelledby","learning-heading");
    dialog.innerHTML=`<div class="learning-shell"><header class="learning-head"><div><h2 id="learning-heading">灵感与方法</h2><div class="learning-sub">读取共享资料…</div></div><button type="button" class="ghost" data-learning-close>关闭</button></header><p class="learning-status" role="status" aria-live="polite"></p><div class="learning-layout"><nav class="learning-nav" aria-label="学习资料">${Object.entries(tabs).map(([id,title])=>button(title,"tab",id)).join("")}<p>参考不等于验证。<br>先看内容，再看边界。</p></nav><section class="learning-main" aria-label="学习内容" tabindex="0">${empty("正在读取…")}</section></div></div>`;
    document.body.append(dialog);main=dialog.querySelector(".learning-main");status=dialog.querySelector(".learning-status");
    dialog.querySelector("[data-learning-close]").onclick=()=>dialog.close();
    dialog.addEventListener("close",()=>{serial++;dialog.remove();opener.focus();});
    dialog.addEventListener("click",e=>{const target=e.target.closest("[data-learning-action]");if(target)perform(()=>act(target.dataset.learningAction,target.dataset.id));});
    dialog.addEventListener("submit",e=>{if(e.target.matches(".learning-form")){e.preventDefault();perform(()=>submit(e.target));}});
    dialog.showModal();return perform(async()=>{await refresh();if(dialog.open&&options.caseId){tab="cases";await act("case",options.caseId);}});
  }
  root.StudioLearning.open=openRoom;
  opener.addEventListener("click",()=>openRoom());
  function setContent(html){main.innerHTML=html;main.scrollTop=0;}
  function back(){return button("← 返回列表","tab",tab,"ghost learning-back");}
  function prose(text){return documentHtml(text,[...list(catalog.documents),...list(catalog.skills)].map(d=>d.id),typeof renderMarkdown==="function"?renderMarkdown:t=>`<pre class="learning-plain">${esc(t)}</pre>`);}
  function render(){
    dialog.querySelectorAll('[data-learning-action="tab"]').forEach(n=>n.setAttribute("aria-current",n.dataset.id===tab?"page":"false"));
    if(tab==="sources")renderSources();
    if(tab==="cases"||tab==="methods")renderSearch(tab);
    if(tab==="context")renderContext();
    if(tab==="feedback")renderFeedback();
  }
  function renderSources(){
    setContent(`<div class="learning-section-heading"><h3>从购买理由，到具体表达</h3><p class="learning-note">三份创作方法，按需要阅读。本轮实际提供给创作角色的内容，保留在「本轮参考」。</p></div><div class="learning-grid learning-skills-grid">${list(catalog.skills).map(skillCard).join("")}</div><h3>研究与说明</h3><div class="learning-document-list">${list(catalog.documents).map(d=>`<article class="learning-document-row"><h4>${esc(d.title||"研究资料")}</h4>${button("阅读 →","document",d.id)}</article>`).join("")}</div><h3>已保存的创作偏好</h3><p class="learning-note">个人或项目范围仅在身份匹配的新任务提供给创作角色，不替代市场判断与主制片自检；品牌范围暂只存档，尚未回流。</p><div class="learning-grid">${list(catalog.preferences).map(p=>`<article class="learning-card"><h4>${esc(p.dimension||p.id)}</h4><p>${esc(p.quote)}</p><small>${esc(p.scope)} · ${esc(p.owner)} · ${p.application_status==='stored_only'?'只存档':'按范围匹配'}</small></article>`).join('')||empty('尚无已保存偏好。')}</div><details class="learning-details learning-archive-list"><summary>历史资料与 Oumomo 对照</summary><p class="learning-note">保留旧方案和受限比较。Oumomo 仅作偏好对照，不代表方法或市场效果已经验证。</p><div class="learning-document-list">${list(catalog.archives).map(a=>`<article class="learning-document-row"><h4>${esc(a.title||"历史资料")}</h4>${button("查看 →","archive",a.id)}</article>`).join("")||empty("暂无历史资料。")}</div></details>`);
  }
  function renderSearch(kind){
    const items=list(catalog[kind]).map(item=>{
      if(kind!=="methods")return item;
      const ids=new Set([...list(item.supports),...list(item.counterexamples)].map(ref=>String(ref).split("/")[0]));
      const linked=list(catalog.cases).filter(c=>ids.has(c.id));
      return {...item,eligible_for:[...new Set(linked.flatMap(c=>list(c.eligible_for)))],tags:[...new Set([...list(item.tags),...linked.flatMap(c=>list(c.tags))])]};
    });
    const purposes=[...new Set(items.flatMap(c=>list(c.eligible_for||c.purposes)))];
    const tagOptions=[...new Set(items.flatMap(c=>list(c.tags)))].sort();
    setContent(`<h3>${kind==="cases"?"从真实参考中找灵感":"值得留下的创作经验"}</h3><p class="learning-note">${kind==="cases"?"看看别人如何呈现商品，再读已有观察。尚未观察的部分和市场效果，保持未知。":"每条经验都保留适用场景和反例，便于判断这次是否适用。"}</p><div class="learning-toolbar"><input type="search" data-filter="query" aria-label="搜索案例或方法" placeholder="搜索标题、证据、反馈或方法…"><select data-filter="purpose" aria-label="按用途筛选"><option value="">所有用途</option>${purposes.map(p=>`<option value="${esc(p)}">${esc(name(p))}</option>`).join("")}</select><select data-filter="tag" aria-label="按标签筛选"><option value="">所有标签</option>${tagOptions.map(t=>`<option value="${esc(t)}">${esc(tags[t]||t)}</option>`).join("")}</select></div><div class="learning-grid ${kind==='cases'?'learning-reference-grid':''}" data-results></div>`);
    const update=()=>{const found=filtered(items,...["query","purpose","tag"].map(k=>main.querySelector(`[data-filter="${k}"]`).value));main.querySelector("[data-results]").innerHTML=found.map(kind==="cases"?caseCard:methodCard).join("")||empty("没有匹配记录。试试更宽的用途或标签。");};
    main.querySelectorAll("[data-filter]").forEach(n=>n.addEventListener("input",update));update();
  }
  function renderContext(){
    const fileNames={"review.md":"阅读方案","index.html":"打开图文包","prompt.txt":"生成提示词","caption.txt":"发布文案","prompts.md":"逐页文案与提示词"};
    const fileLink=f=>{const url=localFileURL(f.url);return url?`<a class="learning-file-link" href="${esc(url)}" target="_blank" rel="noopener">${esc(fileNames[f.name]||f.name)} ↗</a>`:"";};
    const pin=run.pin_status;
    const libraryAvailable=pin?.library_refresh_available ?? pin?.refresh_available;
    const libraryCard=libraryAvailable?`<article class="learning-card"><h3>项目学习库可更新</h3><p class="learning-note">${pin.historical_readonly?'旧库完整备份无法核对，当前只能回看既有项目记录。':'原项目知识版本仍固定，已有稿件和批准保留原版本。'}更新后，仅后续任务使用当前共享库；方法正文仍保持原项目版本。</p><p class="learning-meta">项目库 ${esc(pin.run_library_sha256)} · 当前库 ${esc(pin.current_library_sha256)}</p>${button('后续任务采用当前库','refresh-library','','primary')}</article>`:'';
    const methodCard=pin?.methods_refresh_available?`<article class="learning-card"><h3>创意方法可更新</h3><p class="learning-note">${esc(pin.method_note)}更新将把下方方法版本用于后续任务；原作品、采用记录与生成授权保留原绑定。</p>${list(pin.method_changes).map(change=>`<details class="learning-details"><summary>${esc(change.title||change.id)}</summary><p class="learning-meta">原版：${esc(change.before_sha256||'正文缺失')}<br>当前：${esc(change.after_sha256||'当前已不存在')}</p>${change.diff?`<pre class="object-diff">${esc(change.diff)}</pre>`:'<p class="learning-note">该项未提供正文差异；旧正文缺失时不能推断变化内容。</p>'}</details>`).join('')}<p class="learning-meta">项目方法 ${esc(pin.run_methods_sha256)}<br>当前方法 ${esc(pin.current_methods_sha256)}</p><label class="learning-note"><input type="checkbox" data-methods-reviewed> 我已核对上述方法变化及缺失项，用当前方法处理后续任务</label>${button('采用当前方法版本','refresh-methods','','primary')}</article>`:pin?.method_note?`<p class="learning-note">${esc(pin.method_note)}</p>`:'';
    const refreshCard=libraryCard+methodCard;
    setContent(`${refreshCard}${projectReferences(referenceState.references,referenceState.capability)}<h3>这次创作参考了什么</h3><p class="learning-note">以下记录对应实际提供给各角色的内容。提供过，不等于内容或效果已经通过验证。</p><div class="learning-context-list">${list(run.contexts).map(c=>contextCard(c,prose)).join("")||empty("还没有本轮参考记录。开始创作后，这里会留下实际提供的方法和案例。")}</div><h3>方法选择与使用记录</h3>${button("查看方法与作品证据","method-results")}<p class="learning-note">区分方法已提供、制片选择和稿件实际体现；以下选择记录仍待作品核实。</p><div class="learning-context-list">${list(run.method_usage).map(methodUsageCard).join("")||empty("尚无方法选择记录。")}</div><h3>方案与制作文件</h3><p class="learning-note">可阅读的方案、文案和制作输入。待生成的素材仍保持待生成。</p><div class="learning-grid">${list(run.packages).map(p=>`<article class="learning-card"><h4>${p.format==='photo'?'图文方案':'视频方案'} · ${esc(stages[p.stage]||p.stage)} 第 ${esc(p.version)} 版</h4><div class="learning-file-links">${list(p.files).filter(f=>fileNames[f.name]).map(fileLink).join("")}</div><details class="learning-details learning-audit"><summary>全部文件与版本记录</summary>${meta(p.artifact_sha256)}${list(p.files).map(fileLink).join("")}</details></article>`).join("")||empty("还没有保存的制作文件。")}</div>`);
  }
  function localFileURL(url){return typeof url==="string"&&/^\/api\/(runs|learning)\/[a-zA-Z0-9%_./?=&-]+$/.test(url)&&!url.includes("..")?url:null;}
  function renderFeedback(){
    const artifacts=list(run.artifacts);
    setContent(`<h3>把这次判断留给下一次</h3><p class="learning-note">选好稿件，写下具体判断。预览准备保存的内容，再确认。</p>${runId?`<form class="learning-form" data-kind="feedback"><label>反馈针对的产物<select name="artifact" required>${artifacts.map((a,i)=>`<option value="${i}">${esc(stages[a.stage]||a.stage)} · 第 ${esc(a.version)} 版 · ${esc(a.title||"")}</option>`).join("")}</select></label><details class="learning-details learning-audit"><summary>核对稿件版本</summary><div class="learning-meta" data-artifact-hash></div></details><label>你的原话<textarea name="quote" required placeholder="直接写你的判断和具体位置，例如：方向 B 的开场理由太空。不必改成研究术语。"></textarea></label><label>反馈范围<select name="scope">${Object.entries(scopes).map(([key,label])=>`<option value="${key}">${label}</option>`).join("")}</select></label><button type="submit" class="primary" ${artifacts.length?"":"disabled"}>预览反馈</button></form>`:empty("先选择一个项目，再记录绑定该项目的反馈。") }<div class="learning-toolbar">${button("＋ 登记参考链接","source-form")}</div><h3>待确认与已保存</h3>${list(run.proposals).map(p=>`<article class="learning-card"><h4>${esc(name(p.type))} · ${esc(p.status==="pending"?"待审阅":name(p.status))}</h4>${meta(p.created_at)}<p>${esc(p.payload?.quote||p.payload?.title||p.payload?.rationale||"")}</p>${button("查看内容","proposal",p.id)}</article>`).join("")||empty("还没有候选提案。模型建议也必须在这里明确审阅后保存。")}`);
    const select=main.querySelector('[name="artifact"]');
    if(select){
      const active=typeof state!=="undefined"?state.activeStage:null;
      const latest=artifacts.reduce((best,a,index)=>a.stage===active&&(best<0||a.version>artifacts[best].version)?index:best,-1);
      if(latest>=0)select.value=String(latest);
      const update=()=>{const a=artifacts[Number(select.value)];main.querySelector("[data-artifact-hash]").textContent=a?`绑定 ${a.stage} v${a.version} · SHA256 ${a.artifact_sha256}`:"尚无产物可绑定";};select.addEventListener("change",update);update();}
  }
  async function act(action,id){
    if(action==="tab"){tab=id;proposal=null;notify("");render();return;}
    if(action==='method-results'){
      if(!runId||typeof StudioEvidence==='undefined')throw new Error('请先打开项目，再读取方法证据。');
      await StudioEvidence.openMethodResults(runId);return;
    }
    if(action==="refresh-library"||action==="refresh-methods"){
      if(!runId||!run.pin_status?.refresh_available)throw new Error('请重新打开项目知识版本。');
      const methods=action==='refresh-methods';
      if(methods&&(!run.pin_status.methods_refresh_available||!main.querySelector('[data-methods-reviewed]')?.checked))throw new Error('请先核对方法版本变化并明确选择。');
      const target=main.querySelector(`[data-learning-action="${action}"]`);
      if(target&&!target.dataset.requestId)target.dataset.requestId=crypto.randomUUID();
      await request(runPath('/refresh'),{method:'POST',body:JSON.stringify({
        expected_run_sha256:run.pin_status.run_library_sha256,
        expected_current_sha256:run.pin_status.current_library_sha256,
        ...(methods?{expected_run_methods_sha256:run.pin_status.run_methods_sha256,
          expected_current_methods_sha256:run.pin_status.current_methods_sha256}:{}),
        request_id:target?.dataset.requestId||crypto.randomUUID()})});
      await refresh();notify(methods?'当前方法版本已用于后续任务；旧稿、采用与授权仍属原版本。':'当前共享库已用于这个项目的后续任务；方法版本、旧稿和旧批准保持原样。');return;
    }
    if(action==="skill"||action==="document"){
      const doc=action==="skill"?list(catalog.skills).find(s=>s.id===id):await request(`/api/learning/documents/${encodeURIComponent(id)}`);
      if(!doc)throw new Error("找不到该文本。");
      setContent(`${back()}<h3>${esc(skills[doc.id]?.title||doc.title||"创作资料")}</h3>${audit({version:doc.version,sha256:doc.sha256},"版本记录")}<article class="learning-prose">${prose(doc.content??doc.text??"")}</article>`);return;
    }
    if(action==="archive"){
      const archive=list(catalog.archives).find(a=>a.id===id);const url=`/api/learning/archive/${encodeURIComponent(id)}`;
      const response=await fetch(url);if(!response.ok)throw new Error("档案当前不可读取。");
      const mime=response.headers.get("content-type")||"";
      let html;
      if(mime.startsWith("video/"))html=`<video class="learning-media" controls preload="metadata" src="${esc(url)}"></video>`;
      else if(mime.startsWith("image/"))html=`<img class="learning-media" src="${esc(url)}" alt="历史对照媒体">`;
      else {const text=await response.text();html=mime.includes("text/html")?`<p><a href="${esc(url)}" target="_blank" rel="noopener">查看原版只读审阅页面 ↗</a></p><details class="learning-details"><summary>查看档案源文</summary><pre class="learning-plain">${esc(text)}</pre></details>`:mime.includes("json")?`<pre class="learning-plain">${esc(text)}</pre>`:prose(text);}
      setContent(`${back()}<h3>${esc(archive?.title||id)}</h3><div class="learning-notice">历史只读对照。Oumomo 偏好锚点不进入创作者方法证据；旧授权不沿用到当前项目。</div><article class="learning-prose">${html}</article>`);return;
    }
    if(action==="case"){
      const c=list(catalog.cases).find(c=>c.id===id);if(!c)throw new Error("案例不存在。");
      setContent(`${back()}${caseDetail(c)}${referenceForm(c,runId,referenceState.snapshot,list(referenceState.references).find(r=>r.case_id===c.id),referenceState.capability)}${["pending","calibration"].includes(c.partition)?button("补充观察","observation-form",id):""}`);return;
    }
    if(action==="reference-remove"){
      if(!runId||(typeof state!=="undefined"&&state.runId!==runId))throw new Error("当前项目已经切换，请重新打开灵感页。");
      const target=main.querySelector(`[data-learning-action="reference-remove"][data-id="${CSS.escape(id)}"]`);
      if(target&&!target.dataset.requestId)target.dataset.requestId=crypto.randomUUID();
      referenceState=await request(`/api/runs/${encodeURIComponent(runId)}/references/${encodeURIComponent(id)}/remove`,{method:"POST",body:JSON.stringify({request_id:target?.dataset.requestId||crypto.randomUUID(),expected_snapshot:referenceState.snapshot})});
      renderContext();notify("已移出项目参考。旧稿保留，方向与后续稿件需要按当前参考更新。");
      if(typeof refreshTimeline==="function")await refreshTimeline();return;
    }
    if(action==="method"){
      const m=list(catalog.methods).find(m=>m.id===id);if(!m)throw new Error("方法不存在。");
      const references=refs=>{const ids=[...new Set(list(refs).map(ref=>String(ref).split("/")[0]))];return ids.map(cid=>{const item=list(catalog.cases).find(c=>c.id===cid);return item?button(item.title||"查看参考","case",cid):"";}).join("");};
      setContent(`${back()}<h3>${esc(methodTitle(m))}</h3><p class="learning-lead">${esc(m.decision)}</p><div class="learning-reading-section"><h4>什么时候适用</h4><p>${esc(m.when||"尚未记录")}</p></div><div class="learning-reading-section"><h4>什么时候不适用</h4><p>${esc(m.countercondition||"尚未记录")}</p></div><h4>相关参考</h4><div class="learning-related">${references(m.supports)||empty("尚无支持参考。")}</div>${list(m.counterexamples).length?`<h4>反例</h4><div class="learning-related">${references(m.counterexamples)}</div>`:""}${audit(m,"查看方法状态与来源记录")}${button("提出调整","method-form",id)}`);return;
    }
    if(action==="context") {const c=list(run.contexts).find(c=>c.id===id);if(c)setContent(`${back()}<h3>实际注入记录</h3>${fields(c)}`);return;}
    if(action==="proposal"){proposal=list(run.proposals).find(p=>p.id===id);if(!proposal)throw new Error("提案不存在。");setContent(`${back()}${proposalHtml(proposal)}`);return;}
    if(action==="apply"){
      if(!proposal||proposal.id!==id)throw new Error("请先打开该候选差异。");
      const result=await createActions(request,runId).apply(proposal);proposal=result;
      notify("已保存到共享学习库；来源、旧版本与操作记录已保留。");await refresh();setContent(`${back()}${proposalHtml(result)}`);return;
    }
    if(action.endsWith("-form")){showForm(action,id);return;}
  }
  function showForm(action,id){
    if(!runId)throw new Error("先选择一个项目再登记候选。");
    let content="",kind;
    if(action==="source-form"){
      kind="source";content=`<h3>登记一条参考</h3><p class="learning-note">只保存已解析的 TikTok 公开视频链接及登记信息，不抓取、不下载，也不声明已经观察。新记录进入待研究区。</p><label>参考标题<input name="title" required></label><label>原始链接<input name="url" type="url" required placeholder="https://www.tiktok.com/@user/video/123…"></label><label>标签（用逗号分隔，可留空）<input name="tags"></label>`;
    }else if(action==="method-form"){
      const m=list(catalog.methods).find(m=>m.id===id);kind="method";
      content=`<h3>调整方法：${esc(methodTitle(m||{}))}</h3><p class="learning-note">这里调整适用范围与内容，不提升效果认证状态。支持证据与反例保持可追溯。</p><label>处理方式<select name="action"><option value="keep">保留</option><option value="revise">修订</option><option value="retire">退役</option></select></label><label>判断理由<textarea name="rationale" required></textarea></label><div data-revise hidden>${["decision","when","countercondition"].map(k=>`<label>${labels[k]}<textarea name="${k}">${esc(m?.[k])}</textarea></label>`).join("")}<details><summary>调整支持证据或反例</summary>${["supports","counterexamples"].map(k=>`<label>${labels[k]}（每行一个已存在的 案例ID/证据ID）<textarea name="${k}">${esc(list(m?.[k]).join("\n"))}</textarea></label>`).join("")}</details></div>`;
    }else if(action==="observation-form"){
      kind="observation";const own=list(catalog.cases).find(c=>c.id===id)?.origin==="own_output";content=`<h3>补充人工观察</h3><p class="learning-note">仅记录你实际观察到的内容与范围，不把人工记录说成 Agent 完整看片，不登记未经验证的市场效果。</p><label>观察原文<textarea name="observation" required></textarea></label><label>观察范围<input name="scope" required placeholder="例如：0–8 秒画面；未听音频"></label><label>来源记录<input name="source" required placeholder="来源链接或本次观察记录"></label><label>允许用于哪类创作问题<select name="purpose"><option value="message">表达与说法</option>${own?"":'<option value="storyboard">动作与分镜</option>'}</select></label><label>可借鉴的具体表达机制<textarea name="strategy" required placeholder="写清楚观察支持了什么表达选择，不是他品规格或效果结论。"></textarea></label><label>证据限制（每行一项）<textarea name="limits" required placeholder="例如：只看过开头 8 秒，未听音频；没有成交证据。"></textarea></label>`;
    }
    setContent(`${back()}<form class="learning-form" data-kind="${kind}" data-id="${esc(id)}">${content}<button type="submit" class="primary">预览变更</button></form>`);
    const actionSelect=main.querySelector('[name="action"]');if(actionSelect)actionSelect.addEventListener("change",()=>main.querySelector("[data-revise]").hidden=actionSelect.value!=="revise");
  }
  async function submit(form){
    const data=new FormData(form),kind=form.dataset.kind;let payload;
    if(kind==="reference"){
      if(!runId||(typeof state!=="undefined"&&state.runId!==runId))throw new Error("当前项目已经切换，请重新打开灵感页。");
      const expression=String(data.get("expression")||"");if(!expression.trim())throw new Error("请写下想借鉴的具体表达。");
      const body={case_id:form.dataset.id,case_sha256:form.dataset.hash,purpose:data.get("purpose"),expression,expected_snapshot:referenceState.snapshot};
      const identity=JSON.stringify(body);if(form.dataset.identity!==identity){form.dataset.identity=identity;form.dataset.requestId=crypto.randomUUID();}
      referenceState=await request(`/api/runs/${encodeURIComponent(runId)}/references`,{method:"POST",body:JSON.stringify({...body,request_id:form.dataset.requestId})});
      tab="context";render();notify("已保存项目参考，未启动创作。后续创作会读取你选定的用途与表达范围。");
      if(typeof refreshTimeline==="function")await refreshTimeline();return;
    }
    if(kind==="feedback")payload=feedbackPayload(list(run.artifacts)[Number(data.get("artifact"))],data.get("quote"),data.get("scope"));
    if(kind==="source"){
      const url=safeURL(data.get("url"));if(!url)throw new Error("参考链接须为不含登录信息的 HTTP 或 HTTPS 地址。");
      payload={title:data.get("title"),url,tags:data.get("tags").split(/[,，]/).map(t=>t.trim()).filter(Boolean)};
    }
    if(kind==="method"){
      payload={method_id:form.dataset.id,action:data.get("action"),rationale:data.get("rationale")};
      if(payload.action==="revise"){payload.changes=Object.fromEntries(["decision","when","countercondition"].map(k=>[k,data.get(k)]));for(const key of ["supports","counterexamples"])payload.changes[key]=data.get(key).split("\n").map(s=>s.trim()).filter(Boolean);}
    }
    if(kind==="observation")payload={case_id:form.dataset.id,observation:data.get("observation"),scope:data.get("scope"),source:data.get("source"),purpose:data.get("purpose"),strategy:{proof:data.get("strategy")},limits:data.get("limits").split("\n").map(s=>s.trim()).filter(Boolean)};
    const result=await createActions(request,runId).preview(kind,payload);
    proposal=result;run.proposals=[result,...list(run.proposals).filter(p=>p.id!==result.id)];tab="feedback";
    setContent(`${back()}${proposalHtml(result)}`);notify("候选已生成，尚未入库。请核对原话、来源、版本与差异后再保存。");
  }
})(globalThis);
