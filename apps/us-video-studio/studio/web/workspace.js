/* Product navigation and read-only discovery. Creating a project never starts a model. */
"use strict";
const STUDIO_ICONS = {
  grid:'<rect x="3" y="3" width="7" height="7" rx="2"/><rect x="14" y="3" width="7" height="7" rx="2"/><rect x="3" y="14" width="7" height="7" rx="2"/><rect x="14" y="14" width="7" height="7" rx="2"/>',
  film:'<rect x="3" y="4" width="18" height="16" rx="3"/><path d="M3 9h18M8 4v5m8-5v5m-6 4 5 3-5 3z"/>',
  spark:'<path d="m12 3 2.6 6.4L21 12l-6.4 2.6L12 21l-2.6-6.4L3 12l6.4-2.6z"/>',
  sliders:'<path d="M4 7h5m6 0h5M4 17h9m6 0h1"/><circle cx="12" cy="7" r="3"/><circle cx="16" cy="17" r="3"/>',
  search:'<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
  upload:'<path d="M12 16V3m-5 5 5-5 5 5M4 15v5h16v-5"/>',
  folder:'<path d="M3 6a2 2 0 0 1 2-2h5l2 3h7a2 2 0 0 1 2 2v10H3z"/>',
};
function studioIcon(name) {return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${STUDIO_ICONS[name] || STUDIO_ICONS.film}</svg>`;}
function studioDate(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '日期未记录' : date.toLocaleDateString('zh-CN', {month:'short',day:'numeric'});
}
function shortTitle(title, max = 18) {
  const text = String(title ?? '').trim();
  if (!text) return '未命名作品';
  return text.length <= max ? text : `${text.slice(0, Math.max(1, max - 1))}…`;
}
function filterProjects(runs, query) {
  const urgent=new Set(['needs_input','reviewed','returned','review_failed','interrupted','stale']);
  return runs.filter(run => (run.title || '').toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()))
    .sort((a,b)=>Number(urgent.has(b.project_brief?.phase))-Number(urgent.has(a.project_brief?.phase))
      || (Date.parse(b.updated_at||b.created_at)||0)-(Date.parse(a.updated_at||a.created_at)||0));
}
function inspirationCases(cases) {
  return cases.filter(c => c.media && c.partition === 'calibration' && ['user_reference','discovered_reference'].includes(c.origin)
    && (c.eligible_for || []).includes('message')).slice(0,4);
}
function projectCardHtml(run, cover) {
  const id = escapeHtml(run.run_id), fullTitle = String(run.title || ''), title = escapeHtml(shortTitle(fullTitle));
  const learning=run.kind==='learning';
  const analysis=!run.content_format&&/只分析.{0,20}参考视频/u.test(run.brief||'');
  const format=learning?'学习':analysis?'分析':run.content_format==='photo'?'图文':run.content_format==='video'?'视频':'创作';
  const requestedCaption=/(?:与|和|及|附|加上|需要|附带|[+＋]).{0,6}(?:英文)?(?:发布文案|caption)/iu.test(run.brief||'');
  const goal=learning?'参考研究与知识变更':run.creator_view?.goal|| (analysis?'参考视频拆解报告'
    :run.content_format==='photo'?'带货图片与文案'
    :run.content_format==='video'?`商品视频${requestedCaption?'与发布文案':''}`:'交付形式待确认');
  const next=learning?'打开原片、讨论与变更记录':run.creator_view?.current||'打开查看当前作品与下一步';
  const visual = cover ? `<img src="${escapeHtml(cover)}" alt="" loading="lazy">` : `<div class="project-cover-art">${studioIcon(learning?'spark':'film')}<span>${format}项目</span></div>`;
  const date=studioDate(run.updated_at||run.created_at);
  return `<div class="project-card-item"><button class="project-card" data-open-project="${id}" title="${escapeHtml(fullTitle)}"><span class="project-cover">${visual}<span class="project-open">${learning?'打开研究':'打开作品'} ↗</span></span><span class="project-card-copy"><strong title="${escapeHtml(fullTitle)}">${title}</strong>` +
    `<span class="project-next">${escapeHtml(next)}</span><span>目标：${escapeHtml(goal)} · 更新于 ${escapeHtml(date)}<i aria-hidden="true">↗</i></span></span></button><button class="project-delete ghost small" data-delete-project="${id}" aria-label="删除项目：${title}">删除</button></div>`;
}
function referenceCardHtml(item, hero=false) {
  const id = encodeURIComponent(item.id), title = escapeHtml(item.title);
  const media = `/api/learning/cases/${id}/media#t=0.1`;
  return `<button class="${hero?'hero-reference':'reference-card'}" data-open-reference="${escapeHtml(item.id)}" aria-label="查看参考：${title}"><span class="reference-visual"><video src="${media}" preload="metadata" muted playsinline aria-hidden="true" tabindex="-1"></video><span class="reference-play" aria-hidden="true">▷</span><span class="reference-label">参考片</span></span><span class="reference-copy"><strong>${title}</strong>${!hero?`<span>${escapeHtml(item.strategy?.situation || '打开查看表达方法与适用范围')}</span>`:''}</span></button>`;
}
if (typeof module !== 'undefined' && module.exports) module.exports={filterProjects,inspirationCases,projectCardHtml,referenceCardHtml,shortTitle};

function setStudioView(view) {
  const project = view === 'project' && Boolean(state.runId);
  state.view = project ? 'project' : 'home';
  document.body.dataset.view = state.view;
  const selectedRun=project?state.runs.find(r=>r.run_id===state.runId):null;
  const learning=selectedRun?.kind==='learning';
  document.body.dataset.runKind=selectedRun?.kind||'';
  $('workspace').hidden = project;
  $('project-workspace').hidden = !project;
  $('project-workspace').setAttribute('aria-label',learning?'学习工作区':'项目工作区');
  $('stage').setAttribute('role',learning?'region':'tabpanel');
  if(learning){$('stage').removeAttribute('aria-labelledby');$('stage').setAttribute('aria-label','学习记录');}
  else{$('stage').setAttribute('aria-labelledby','journey-tab-plan');$('stage').removeAttribute('aria-label');}
  document.querySelector('.composer').setAttribute('aria-label',learning?'学习对话':'制片对话');
  document.querySelector('.project-more summary').textContent=learning?'学习记录与设置':'项目与设置';
  document.querySelector('.producer-heading strong').textContent=learning?'你的学习搭档':'你的制片团队';
  document.querySelector('.producer-heading strong+span').textContent=learning?'讨论观察、疑问与方法变更':'讨论、分工与结果，都在这里';
  document.querySelector('.conversation-bar>span').textContent=learning?'学习对话':'项目对话';
  $('chat-input').setAttribute('aria-label',learning?'讨论参考与纠正观察':'与制片团队对话');
  $('page-heading').textContent = project ? (state.runs.find(r=>r.run_id===state.runId)?.title || '创作项目') : '工作空间';
  for (const [id, active] of [['nav-home',!project],['nav-project',project]]) {
    $(id).classList.toggle('active',active);
    if (active) $(id).setAttribute('aria-current','page'); else $(id).removeAttribute('aria-current');
  }
  document.querySelector('.skip-link').href = project ? '#stage' : '#workspace';
  $('chat-input').placeholder=selectedRun?.kind==='learning'?'说说你对参考的判断、疑问或纠正…':'说说想做什么，也可直接粘贴 TK 链接…';
  $('spend-panel').hidden = true;
  if (!project) toggleMaterials(false);
  if(project){state.atBottom=true;requestAnimationFrame(scrollDown);}
}
function renderProjects() {
  const host = $('project-grid');
  if (!host) return;
  document.body.dataset.hasProjects = state.runs.length ? 'true' : 'false';
  const found = filterProjects(state.runs || [], $('project-search').value);
  host.innerHTML = found.map(run => {
    const cover = run.run_id === state.runId ? state.materials.find(m=>m.kind==='image' && m.usable) : null;
    return projectCardHtml(run,cover ? materialUrl(cover.name) : null);
  }).join('') || `<div class="project-empty"><span>${studioIcon('film')}</span><h3>${state.runs.length?'还没有找到这个项目':'你的第一个故事，从这里开始'}</h3><p>${state.runs.length?'换一个关键词，或清空搜索。':'添加商品素材，和制片团队一起构思。'}</p>${state.runs.length?'':'<button class="primary" data-create-format="video">新建创作项目</button>'}</div>`;
  if (found.length) host.insertAdjacentHTML('beforeend','<button class="project-new" data-create-format="video"><span>＋</span><strong>新的想法</strong><small>开启下一个创作项目</small></button>');
}
async function loadInspiration() {
  try {
    const catalog = await api('/api/learning');
    const cases = inspirationCases(catalog.cases || []);
    $('inspiration-grid').innerHTML = cases.map(c=>referenceCardHtml(c)).join('') || '<p class="muted">还没有可用参考，可在灵感库登记。</p>';
    if (cases.length) {
      $('hero-visual').innerHTML = '<div class="hero-gallery">' + cases.slice(0,3).map(c=>referenceCardHtml(c,true)).join('') + '</div><div class="hero-caption"><span class="small-dot"></span> 从真实参考中，发现表达的可能</div>';
    }
    for (const video of document.querySelectorAll('.workspace video')) video.addEventListener('error',()=> {
      const visual=video.closest('.reference-visual'); video.remove(); visual.classList.add('media-unavailable');
      const note=document.createElement('span');note.textContent='参考暂不可预览';visual.append(note);
    },{once:true});
  } catch {
    $('inspiration-grid').innerHTML = '<div class="project-empty"><p>灵感资料暂时没有加载成功。</p><button class="ghost" data-retry-inspiration>重新加载</button></div>';
  }
}
function initStudioNavigation() {
  if(typeof initProjectManagement==='function')initProjectManagement();
  if(typeof initJourney==='function')initJourney();
  for (const node of document.querySelectorAll('[data-project-focus]')) node.onclick = () => setProjectFocus(node.dataset.projectFocus);
  $('delivery-open').onclick = openDelivery;
  $('outcomes-open').onclick = openOutcomes;
  $('production-open').onclick = ()=>showJourneyTab('storyboard');
  $('history-open').onclick = openProjectHistory;
  for (const node of document.querySelectorAll('[data-icon]')) node.innerHTML = studioIcon(node.dataset.icon);
  $('nav-home').onclick = $('brand-home').onclick = () => setStudioView('home');
  $('nav-project').onclick = () => {
    if (state.runId) setStudioView('project'); else newRunDialog();
  };
  $('top-create').onclick = ()=>newRunDialog();
  $('start-learning').onclick=()=>StudioLearningSession.openCreateDialog();
  $('browse-inspiration').onclick = ()=>window.StudioLearning.open({tab:'cases'});
  $('project-search').oninput = renderProjects;
  $('material-upload').onchange = async event => {await uploadFiles([...event.target.files]);event.target.value='';};
  document.addEventListener('click',async event=> {
    const create=event.target.closest('[data-create-format]');
    if (create) newRunDialog(create.dataset.createFormat);
    const project=event.target.closest('[data-open-project]');
    if (project && !state.decisionBusy) {
      const id=project.dataset.openProject;
      await selectRun(id); setStudioView('project');
    }
    const reference=event.target.closest('[data-open-reference]');
    if (reference) window.StudioLearning.open({tab:'cases',caseId:reference.dataset.openReference});
    if (event.target.closest('[data-retry-inspiration]')) loadInspiration();
  });
}
