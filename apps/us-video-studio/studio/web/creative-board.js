/* Current creative work, arranged as a read-only canvas. No authorization API. */
"use strict";
(function (root) {
  const controllers = new WeakMap();
  const list = value => Array.isArray(value) ? value : [];
  const text = value => typeof value === 'string' ? value : '';
  const adopted = step => Boolean(step?.has_doc && !step.stale && step.integrity_ok !== false &&
    ['approved', 'approved_override'].includes(step.approval_decision));
  const field = (card, ...labels) => text(list(card?.fields).find(row => labels.includes(row.label))?.value);
  const stage = (timeline, name) => {
    const row = list(timeline?.rail).find(item => item.stage === name && item.has_doc);
    return row ? {...row} : null;
  };
  const sourceMatches = (source, writing) => Boolean(writing && Number.isInteger(writing.version) &&
    /^[a-f0-9]{64}$/.test(writing.artifact_sha256 || '') && source?.source_version === writing.version &&
    source.source_sha256 === writing.artifact_sha256);

  function safeMediaUrl(value, runId, name) {
    const prefix = `/api/runs/${encodeURIComponent(runId)}/materials/`;
    if (!runId || typeof value !== 'string' || !value.startsWith(prefix) || /[?#\\]/.test(value)) return '';
    try {
      const file = decodeURIComponent(value.slice(prefix.length));
      return file && !/[\/\\\u0000-\u001f]/.test(file) && file !== '.' && file !== '..' &&
        (!name || file === name) ? value : '';
    } catch (_) { return ''; }
  }

  function designProjection(material, production) {
    if (!material) return {available: false, issue: '共同人物与场景设计尚未保存。', characters: [], scenes: []};
    const blocks = list(material.blocks), visual = production?.visual_design_status;
    const bound = visual?.version === material.version && visual?.sha256 === material.artifact_sha256;
    const design = bound && visual.design ? visual.design : material.structured?.visual_design;
    const cards = title => list(blocks.find(block => block.type === 'cards' && block.title === title)?.cards);
    const context = cards('共同人物与场景设计')[0];
    const characters = design ? list(design.characters).map(row => ({id: text(row.id), label: text(row.label),
      summary: [row.appearance, row.wardrobe].filter(Boolean).join(' · '),
      description: [row.relationship, row.appearance, row.wardrobe, row.performance].filter(Boolean).join(' · ')})) :
      cards('人物与服装').map(card => ({id: '', label: text(card.head),
        summary: [field(card, '外观'), field(card, '服装')].filter(Boolean).join(' · '),
        description: list(card.fields).map(row => text(row.value)).filter(Boolean).join(' · ')}));
    const scenes = design ? list(design.scenes).map(row => ({id: text(row.id), label: text(row.label),
      description: [row.location, row.layout, row.props, row.lighting, row.local_details].filter(Boolean).join(' · ')})) :
      cards('场景与道具').map(card => ({id: '', label: text(card.head),
        description: list(card.fields).map(row => text(row.value)).filter(Boolean).join(' · ')}));
    const missing = blocks.find(block => block.type === 'callout' &&
      ['共同设计尚未保存', '共同设计需补齐'].includes(block.title));
    let issue = text(missing?.text);
    if (material.stale || material.integrity_ok === false) issue = material.stale_sentence || material.integrity_sentence || '这版人物与场景设计需要重新核对。';
    else if (bound && visual.ready === false) issue = list(visual.issues).map(text).filter(Boolean).join(' ') || issue;
    if (!design && !context && !issue) issue = '这份历史方案没有共同人物与场景设计，请先补齐后再准备新制作。';
    return {available: Boolean(design || context), issue, characters, scenes,
      noCharacters: Boolean(design && Array.isArray(design.characters) && !design.characters.length) ||
        blocks.some(block => block.title === '人物与服装' && block.type === 'text'),
      context: design ? [design.everyday_context, design.localization, design.style].filter(Boolean).join(' · ') :
        list(context?.fields).filter(row => row.label !== '市场').map(row => text(row.value)).filter(Boolean).join(' · '),
      continuity: design ? list(design.continuity).map(text) : list(blocks.find(block => block.title === '跨镜保持项')?.items).map(row => text(row.text)),
      version: material.version};
  }

  function writingRows(writing, target) {
    const blocks = list(writing?.blocks);
    const segments = list(blocks.find(block => block.type === 'timeline')?.segments);
    const cards = list(blocks.find(block => block.type === 'cards' && ['逐段', '逐镜', '逐页'].includes(block.title))?.cards);
    // IDs come exclusively from the backend target list, never from an ordinal.
    return Array.from({length: Math.max(segments.length, cards.length)}, (_, index) => {
      const card = cards[index], strip = segments[index];
      const shot = list(target?.shots).find(row => row.ordinal === index + 1);
      return {index: index + 1, shotId: shot?.shot_id || null,
        time: text(card?.head) || text(strip?.tc) || field(card, '时长'),
        picture: field(card, '画面', '画面与动作'), line: field(card, '台词', '台词或字幕', '口播', '图内文案', '图内文字', '文案'),
        sound: field(card, '声音'), characters: field(card, '人物身份', '本镜人物', '人物', 'character_ids'),
        scene: field(card, '场景身份', '本镜场景', '场景', 'scene_id')};
    });
  }

  function currentMedia(production, writing, target, kind, runId) {
    if (!sourceMatches(target, writing) || target.complete_list !== true) return [];
    const coverage = production?.shots?.[kind];
    const materials = list(production?.materials), drafts = list(production?.drafts);
    return list(target.shots).map(shot => {
      const row = list(coverage?.shots).find(item => item.shot_id === shot.shot_id);
      const draft = drafts.find(item => item.draft_id === row?.draft_id && item.kind === kind &&
        item.shot_id === shot.shot_id && item.is_current !== false && sourceMatches(item, writing));
      const attempt = list(draft?.attempts).at(-1), output = row?.output;
      const material = materials.find(item => item.name === output?.name && item.sha256 === output?.sha256 && item.kind === kind);
      const valid = !writing.stale && writing.integrity_ok !== false && row?.stage === 'completed' &&
        row.source_ready === true && draft?.source_ready === true && attempt?.status === 'succeeded' &&
        attempt.output_available === true && attempt.output?.name === output?.name && attempt.output?.sha256 === output?.sha256;
      const url = valid && material ? safeMediaUrl(material.url, runId, material.name) : '';
      return {kind, shotId: shot.shot_id, ordinal: shot.ordinal, label: text(shot.label),
        state: row?.stage || 'unprepared', status: text(row?.stage_label) || '尚未准备输入',
        reason: text(row?.reason), name: url ? material.name : '', url,
        issue: valid && !url ? '结果文件暂时无法核对。' : ''};
    });
  }

  function projectBoard({runId, run = {}, timeline = {}, production = {}, materials = [], format,
    analysisOnly = false, referenceTasks = [], referenceSelectedId = null} = {}) {
    run = run || {}; timeline = timeline || {}; production = production || {}; materials = materials || [];
    const id = runId || run.run_id || '', writing = stage(timeline, 'writing'), direction = stage(timeline, 'direction');
    const material = stage(timeline, 'material_plan'), intake = stage(timeline, 'intake');
    const photo = (format || run.content_format) === 'photo';
    const directionId = adopted(direction) ? text(direction.selected_option_id) : '';
    const selected = directionId ? list(direction.blocks).flatMap(block => list(block.cards))
      .find(card => text(card.head) === directionId) : null;
    const allMaterials = list(materials?.materials || materials);
    const generated = new Set(list(production.drafts).flatMap(draft => list(draft.attempts))
      .map(attempt => attempt.output?.name).filter(Boolean));
    const thumbs = allMaterials.filter(item => item.kind === 'image' && item.origin !== 'generated' &&
      !generated.has(item.name) && item.purpose !== 'reference' &&
      !list(item.observations).some(observation => observation.purpose === 'reference') &&
      safeMediaUrl(item.url, id, item.name)).slice(0, 4).map(item => ({name: text(item.name),
        url: safeMediaUrl(item.url, id, item.name)}));
    const imageTarget = production.targets?.image;
    const matchedTarget = sourceMatches(imageTarget, writing) && imageTarget.complete_list === true ? imageTarget : null;
    const rows = analysisOnly ? [] : writingRows(writing, matchedTarget);
    const media = analysisOnly ? [] : ['image', ...(photo ? [] : ['video'])].flatMap(kind =>
      currentMedia(production, writing, production.targets?.[kind], kind, id));
    let writingIssue = '';
    if (writing?.stale || writing?.integrity_ok === false) writingIssue = writing.stale_sentence || writing.integrity_sentence || '当前脚本依据需要重新核对。';
    else if (writing && !adopted(writing)) writingIssue = '这版方案尚未采用；镜头清单只供阅读，不代表可以生成。';
    const historyCount = list(production.drafts).filter(draft => draft.is_current === false ||
      !sourceMatches(draft, writing) || draft.source_ready === false).length;
    const selectedReference = referenceSelectedId
      ? list(referenceTasks).find(task => task.reference_id === referenceSelectedId) : list(referenceTasks).at(-1);
    const reference = selectedReference ? {reference_id:selectedReference.reference_id, name:selectedReference.name,source_url:selectedReference.source_url,
      title:selectedReference.title, report:selectedReference.report, status:selectedReference.status,
      execution_status:selectedReference.execution_status, job_id:selectedReference.job_id,
      failure_reason:selectedReference.failure_reason} : null;
    return {runId: id, photo, analysisOnly: Boolean(analysisOnly), title: text(run.title) || '当前作品',
      brief: text(run.brief) || text(timeline.brief?.goal) || '需求尚未保存，可以在右侧对话里说明想做什么。',
      intake, direction, directionId, directionTitle: selected ? field(selected, '标题', '方向名') || text(selected.head) : '',
      directionSummary: selected ? field(selected, '主张', '核心主张', '推进概念', '故事', '故事梗概', '开场识别点', '开场') : '',
      directionIssue: direction?.stale ? direction.stale_sentence || '此前方向已过期，请查看当前决定。' :
        adopted(direction) && !directionId ? '已采用方向，旧记录未注明具体选项。' : '',
      thumbs, material, design: designProjection(material, production), writing, writingIssue, rows, media, historyCount,
      caption: text(list(writing?.blocks).find(block => block.type === 'text' && block.title === '发布文案')?.text),
      reference, analysis: text(reference?.report)};
  }

  const clamp = value => Math.max(.35, Math.min(1.6, value));
  function fitView(width, height, bounds) {
    const zoom = clamp(Math.min(1, (width - 64) / bounds.width, (height - 72) / bounds.height));
    return {zoom, x: (width - bounds.width * zoom) / 2, y: Math.max(24, (height - bounds.height * zoom) / 2)};
  }
  function zoomView(view, factor, anchor) {
    const zoom = clamp(view.zoom * factor), ratio = zoom / view.zoom;
    return {zoom, x: anchor.x - (anchor.x - view.x) * ratio, y: anchor.y - (anchor.y - view.y) * ratio};
  }

  function createController(host) {
    const doc = host.ownerDocument || root.document;
    const el = (tag, cls, value) => { const node = doc.createElement(tag); if (cls) node.className = cls;
      if (value !== undefined) node.textContent = String(value); return node; };
    const shell = el('section', 'creative-board'), toolbar = el('div', 'creative-board-toolbar');
    shell.setAttribute('aria-label', '创作画布');
    toolbar.setAttribute('role', 'toolbar'); toolbar.setAttribute('aria-label', '画布视图');
    const viewport = el('div', 'creative-board-viewport'), world = el('div', 'creative-board-world');
    viewport.tabIndex = 0; viewport.setAttribute('role', 'region');
    viewport.setAttribute('aria-label', '需求、方案和结果；拖动空白处移动，按加减键缩放，按0适配视图');
    const label = el('span', 'creative-board-zoom', '100%'); label.setAttribute('aria-live', 'polite');
    toolbar.append(el('span', 'creative-board-toolbar-title', '创作画布'));
    const hint = el('span', 'creative-board-hint', '拖动画布 · + / − 缩放');
    let runId = null, data = null, callbacks = {}, view = {zoom: 1, x: 32, y: 32};
    let bounds = {width: 1460, height: 680}, autoFit = true, dragging = null, destroyed = false, renderKey = '';
    const listeners = [];
    const listen = (node, name, handler, options) => { node.addEventListener(name, handler, options);
      listeners.push(() => node.removeEventListener(name, handler, options)); };
    const button = (value, key, handler, cls = 'creative-board-button') => {
      const node = el('button', cls, value); node.type = 'button'; node.setAttribute('data-board-action', key);
      node.onclick = () => { if (!destroyed && host.contains(node)) handler(); }; return node;
    };
    function size() { return {width: viewport.clientWidth || host.clientWidth || 1000, height: viewport.clientHeight || host.clientHeight || 680}; }
    function apply() {
      world.style.transform = `translate(${view.x}px, ${view.y}px) scale(${view.zoom})`;
      label.textContent = `${Math.round(view.zoom * 100)}%`;
    }
    const isStacked = () => typeof root.matchMedia === 'function' && root.matchMedia('(max-width:650px)').matches;
    function fit() { autoFit = true; if (isStacked()) { viewport.scrollTop = 0; return; }
      const dimensions = size(); view = fitView(dimensions.width, dimensions.height, bounds); apply(); }
    function zoomBy(factor, anchor) { const dimensions = size(); autoFit = false;
      view = zoomView(view, factor, anchor || {x: dimensions.width / 2, y: dimensions.height / 2}); apply(); }
    function focusPlan() { autoFit = false;
      if (isStacked()) { world.querySelector(data.analysisOnly ? '.creative-board-analysis' : '.creative-board-plan')?.scrollIntoView({block: 'start'}); return; }
      const dimensions = size();
      const plan = world.querySelector(data.analysisOnly ? '.creative-board-analysis' : '.creative-board-plan');
      const height = plan?.offsetHeight || bounds.height - 64;
      const zoom = Math.max(.7, Math.min(1, (dimensions.width - 48) / 600, (dimensions.height - 48) / (height + 64)));
      view = {zoom, x: (dimensions.width - 600 * zoom) / 2 - 420 * zoom, y: 24 - 32 * zoom}; apply(); }
    const minus = button('−', 'zoom-out', () => zoomBy(1 / 1.2)); minus.setAttribute('aria-label', '缩小画布');
    const plus = button('+', 'zoom-in', () => zoomBy(1.2)); plus.setAttribute('aria-label', '放大画布');
    const focus = button('看分镜', 'focus-plan', focusPlan);
    toolbar.append(hint, focus, minus, label, plus, button('适配视图', 'fit', fit));
    viewport.append(world); shell.append(toolbar, viewport); host.replaceChildren(shell);
    const interactive = target => target?.closest?.('button,a,input,textarea,select,video,summary,[role="button"],[data-board-scroll]');
    listen(viewport, 'wheel', event => {
      if (isStacked()) return;
      if (event.ctrlKey || event.metaKey) {
        event.preventDefault(); const rect = viewport.getBoundingClientRect();
        zoomBy(Math.exp(-event.deltaY * .002), {x: event.clientX - rect.left, y: event.clientY - rect.top});
      } else if (!interactive(event.target)) {
        event.preventDefault(); autoFit = false; view.x -= event.deltaX; view.y -= event.deltaY; apply();
      }
    }, {passive: false});
    listen(viewport, 'pointerdown', event => {
      if (isStacked() || (event.button !== undefined && event.button !== 0) || interactive(event.target) ||
          event.target?.closest?.('.creative-board-node-body')) return;
      autoFit = false; dragging = {id: event.pointerId, x: event.clientX, y: event.clientY, left: view.x, top: view.y};
      viewport.setPointerCapture?.(event.pointerId); viewport.classList.add('is-dragging');
      viewport.focus({preventScroll: true}); event.preventDefault();
    });
    listen(viewport, 'pointermove', event => {
      if (!dragging || event.pointerId !== dragging.id) return;
      view.x = dragging.left + event.clientX - dragging.x; view.y = dragging.top + event.clientY - dragging.y; apply();
    });
    function stopDrag() { if (dragging && viewport.hasPointerCapture?.(dragging.id)) viewport.releasePointerCapture(dragging.id);
      dragging = null; viewport.classList.remove('is-dragging'); }
    for (const type of ['pointerup', 'pointercancel', 'lostpointercapture']) listen(viewport, type, stopDrag);
    listen(viewport, 'keydown', event => {
      if (event.ctrlKey || event.metaKey || event.altKey || interactive(event.target)) return;
      if (isStacked()) { if (['0', 'f', 'F'].includes(event.key)) { event.preventDefault(); fit(); } return; }
      if (['+', '=', '-', '_', '0', 'f', 'F', 'ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(event.key)) {
        event.preventDefault();
        if (['+', '='].includes(event.key)) zoomBy(1.2);
        else if (['-', '_'].includes(event.key)) zoomBy(1 / 1.2);
        else if (['0', 'f', 'F'].includes(event.key)) fit();
        else { autoFit = false; const amount = event.shiftKey ? 100 : 40;
          view.x += event.key === 'ArrowLeft' ? amount : event.key === 'ArrowRight' ? -amount : 0;
          view.y += event.key === 'ArrowUp' ? amount : event.key === 'ArrowDown' ? -amount : 0; apply(); }
      }
    });
    const resize = typeof root.ResizeObserver === 'function' ? new root.ResizeObserver(() => { if (autoFit && !destroyed) fit(); }) : null;
    resize?.observe(viewport);

    const call = (name, ...args) => { if (typeof callbacks[name] === 'function') callbacks[name](...args); };
    const note = value => el('p', 'creative-board-note', value);
    function node(title, sub, key, x, width) {
      const card = el('article', `creative-board-node creative-board-${key}`); card.style.left = `${x}px`; card.style.width = `${width}px`;
      card.setAttribute('data-board-node', key);
      const header = el('header', 'creative-board-node-heading');
      const dot = el('span', 'creative-board-node-dot'); dot.setAttribute('aria-hidden', 'true');
      header.append(dot, el('h2', '', title)); if (sub) header.append(el('span', 'creative-board-version', sub));
      const body = el('div', 'creative-board-node-body'); card.append(header, body); world.append(card); return body;
    }
    function linkNodes(from, to) {
      const svg = doc.createElementNS('http://www.w3.org/2000/svg', 'svg');
      svg.setAttribute('class', 'creative-board-connection'); svg.setAttribute('aria-hidden', 'true');
      svg.setAttribute('width', String(bounds.width)); svg.setAttribute('height', String(bounds.height));
      const path = doc.createElementNS('http://www.w3.org/2000/svg', 'path');
      path.setAttribute('d', `M ${from} 172 C ${from + 48} 172 ${to - 48} 172 ${to} 172`);
      path.setAttribute('fill', 'none'); svg.append(path);
      for (const x of [from, to]) { const circle = doc.createElementNS('http://www.w3.org/2000/svg', 'circle');
        circle.setAttribute('cx', String(x)); circle.setAttribute('cy', '172'); circle.setAttribute('r', '4'); svg.append(circle); }
      world.append(svg);
    }
    function needNode() {
      const body = node('需求', '01', 'brief', 0, 330);
      const brief = el('p', 'creative-board-brief-text', data.brief); brief.setAttribute('data-board-scroll', '');
      body.append(el('h3', 'creative-board-work-title', data.title), brief);
      if (data.intake) body.append(button('查看完整需求', 'intake', () => call('openStage', 'intake', data.intake.version)));
      const direction = el('section', 'creative-board-direction');
      if (data.analysisOnly) {
        direction.append(el('h3', '', '本轮分析范围'),
          note('本轮只拆解参考视频，分析报告与任务状态见参考节点。若要继续创作，请在对话中说明新的作品要求。'));
      } else {
        direction.append(el('h3', '', data.directionId ? `已采用方向 ${data.directionId}` : '创意方向'));
        if (data.directionId) { const summary = note(data.directionSummary); summary.setAttribute('data-board-scroll', '');
          direction.append(el('strong', '', data.directionTitle || `方向 ${data.directionId}`), summary); }
        else direction.append(note(data.directionIssue || (data.direction ? '方向方案已保存，当前决定请看右侧对话。' : '方向尚未准备好，先从需求开始。')));
        if (data.directionIssue && data.directionId) direction.append(note(data.directionIssue));
        if (data.direction) direction.append(button('查看方向', 'direction', () => call('openStage', 'direction', data.direction.version)));
      }
      body.append(direction);
      const shelf = el('section', 'creative-board-materials'); shelf.append(el('h3', '', data.analysisOnly ? '项目素材' : '商品素材'));
      const grid = el('div', 'creative-board-thumbnails');
      for (const item of data.thumbs) {
        const thumb = button('', `material:${item.name}`, () => call('openMedia', item.name), 'creative-board-thumbnail');
        thumb.setAttribute('aria-label', `查看素材 ${item.name}`);
        const img = el('img'); img.src = item.url; img.alt = item.name; img.loading = 'lazy'; thumb.append(img); grid.append(thumb);
      }
      shelf.append(grid); if (!data.thumbs.length) shelf.append(note('当前没有可预览的商品图片。'));
      shelf.append(button('查看商品素材', 'materials', () => call('openMaterials'))); body.append(shelf);
    }
    function planNode() {
      const body = node(data.photo ? '图文方案' : '人物、场景与分镜', data.writing ? `第 ${data.writing.version} 版` : '02', 'plan', 420, 600);
      const design = el('section', 'creative-board-design'); design.append(el('h3', '', '共同人物与场景设计'));
      if (data.design.issue) design.append(el('p', 'creative-board-warning', data.design.issue));
      if (data.design.available) {
        if (data.design.context) design.append(note(data.design.context));
        const grid = el('div', 'creative-board-design-grid');
        for (const [title, rows] of [['人物', data.design.characters], ['场景', data.design.scenes]]) {
          const panel = el('div', `creative-board-design-card creative-board-design-${title === '人物' ? 'characters' : 'scenes'}`);
          panel.setAttribute('data-board-scroll', ''); panel.append(el('h4', '', title));
          if (!rows.length) panel.append(note(title === '人物' && data.design.noCharacters ? '本设计不出现人物。' : `${title}细节尚未保存。`));
          for (const item of rows) {
            if (title === '人物') { const description = el('p', 'creative-board-character-summary', item.summary || item.description);
              description.title = item.description; panel.append(el('strong', '', item.label || item.id), description);
            } else { const details = el('details', 'creative-board-scene-details');
              details.append(el('summary', '', item.label || item.id), el('p', '', item.description)); panel.append(details); }
          }
          grid.append(panel);
        }
        design.append(grid);
        if (data.design.continuity.length) { const continuity = el('details', 'creative-board-continuity');
          continuity.append(el('summary', '', '跨镜一致性'), note(data.design.continuity.join('；'))); design.append(continuity); }
      }
      if (data.material) design.append(button('查看人物与场景', 'design', () => call('openStage', 'material_plan', data.material.version)));
      body.append(design);
      const heading = el('div', 'creative-board-section-heading'); heading.append(el('h3', '', data.photo ? '逐页画面与文案' : '逐镜画面与口播'));
      if (data.writing) heading.append(button('查看完整方案', 'writing', () => call('openStage', 'writing', data.writing.version)));
      body.append(heading); if (data.writingIssue) body.append(el('p', 'creative-board-warning', data.writingIssue));
      if (!data.rows.length) body.append(note(data.writing ? '这份历史稿没有可读的分镜表，可查看完整方案；不会猜测镜头数量。' : '方案尚未保存，团队会在当前对话中继续准备。'));
      else {
        const scroll = el('div', 'creative-board-table-scroll'); scroll.setAttribute('data-board-scroll', ''); scroll.tabIndex = 0;
        scroll.setAttribute('aria-label', data.photo ? '逐页方案表' : '逐镜分镜表');
        const table = el('table', 'creative-board-shot-table'), thead = el('thead'), head = el('tr');
        for (const title of [data.photo ? '页' : '时间', '画面', data.photo ? '文案' : '口播 / 字幕']) { const th = el('th', '', title); th.scope = 'col'; head.append(th); }
        thead.append(head); table.append(thead); const tbody = el('tbody');
        for (const row of data.rows) {
          const tr = el('tr'); if (row.shotId) tr.setAttribute('data-shot-id', row.shotId);
          const time = el('td'), picture = el('td'), line = el('td');
          time.append(el('strong', '', data.photo ? `第 ${row.index} 页` : row.time || `第 ${row.index} 镜`));
          if (row.shotId) time.append(button('查看这一镜', `shot:image:${row.shotId}`, () => call('openShot', 'image', row.shotId)));
          const selectable = (node, value, name, key) => {
            if (value && !data.writing.stale && data.writing.integrity_ok !== false && /^[a-f0-9]{64}$/.test(data.writing.artifact_sha256 || '')) {
              node.setAttribute('data-creator-target', `${data.photo ? '第 ' + row.index + ' 页' : '第 ' + row.index + ' 段'}${name}`);
              node.setAttribute('data-creator-path', JSON.stringify(['segments', row.index - 1, key]));
            }
            return node;
          };
          picture.append(selectable(el('p', '', row.picture || '这版未注明画面。'), row.picture, '画面', 'picture'));
          if (row.characters || row.scene) picture.append(el('small', '', [row.characters, row.scene].filter(Boolean).join(' · ')));
          line.append(selectable(el('p', '', row.line || '这版未注明台词或字幕。'), row.line, data.photo ? '图内文案' : '台词', 'line'));
          if (row.sound) line.append(selectable(el('small', '', row.sound), row.sound, '声音', 'sound'));
          tr.append(time, picture, line); tbody.append(tr);
        }
        table.append(tbody); scroll.append(table); body.append(scroll);
      }
      if (data.caption) { const caption = el('details', 'creative-board-caption'), copy = el('p', '', data.caption);
        copy.setAttribute('data-board-scroll', '');
        if (!data.writing.stale && data.writing.integrity_ok !== false && /^[a-f0-9]{64}$/.test(data.writing.artifact_sha256 || '')) {
          copy.setAttribute('data-creator-target', '发布文案'); copy.setAttribute('data-creator-path', JSON.stringify(['caption']));
        }
        caption.append(el('summary', '', '发布文案'), copy); body.append(caption); }
    }
    function resultNode() {
      const body = node(data.photo ? '图片结果' : '媒体结果', '03', 'results', 1110, 350);
      body.append(note('按本版镜头对应展示。'));
      const gallery = el('div', 'creative-board-result-scroll'); gallery.setAttribute('data-board-scroll', ''); gallery.tabIndex = 0;
      gallery.setAttribute('aria-label', '当前镜头的媒体结果');
      const shots = [...new Set(data.media.map(item => item.shotId))];
      if (!shots.length) gallery.append(note(data.writing ? '当前方案尚无对应的制作结果清单。' : '方案保存后，真实生成的结果会按镜头显示在这里。'));
      for (const shotId of shots) {
        const items = data.media.filter(item => item.shotId === shotId), section = el('section', 'creative-board-result-shot');
        section.setAttribute('data-result-shot', shotId);
        section.append(el('h3', '', `${data.photo ? '第' : '镜头'} ${items[0].ordinal}${data.photo ? ' 页' : ''}`));
        for (const item of items) {
          const card = el('div', `creative-board-media creative-board-media-${item.kind}`); card.setAttribute('data-media-kind', item.kind);
          card.append(el('span', 'creative-board-media-label', `${item.kind === 'image' ? '图片' : '视频'} · ${item.status}`));
          if (item.url) {
            if (item.kind === 'image') {
              const preview = button('', `output:${item.name}`, () => call('openMedia', item.name), 'creative-board-output');
              preview.setAttribute('aria-label', `查看镜头 ${item.ordinal} 图片`);
              const image = el('img'); image.src = item.url; image.alt = `镜头 ${item.ordinal}：${item.label || item.name}`; image.loading = 'lazy'; preview.append(image); card.append(preview);
            } else { const video = el('video'); video.src = item.url; video.controls = true; video.playsInline = true; video.preload = 'metadata';
              video.setAttribute('aria-label', `播放镜头 ${item.ordinal} 视频`); card.append(video); }
          } else { card.append(note(item.issue || item.reason || '这面镜头尚无可核对的当前结果。')); }
          card.append(button('查看这一镜', `shot:${item.kind}:${shotId}`, () => call('openShot', item.kind, shotId))); section.append(card);
        }
        gallery.append(section);
      }
      body.append(gallery);
      if (data.historyCount) body.append(note(`${data.historyCount} 份旧输入保留在镜头详情与历史中。`));
      const history = el('div', 'creative-board-history');
      history.append(button('查看图片记录', 'history:image', () => call('openShot', 'image', null)));
      if (!data.photo) history.append(button('查看视频记录', 'history:video', () => call('openShot', 'video', null)));
      body.append(history);
    }
    function analysisNode() {
      const body = node('分析报告', '02', 'analysis', 420, 600);
      const report = note(data.analysis || '尚未保存分析报告；已保存的原片与任务状态可在参考详情查看。'); report.setAttribute('data-board-scroll', '');
      if(data.reference){
        body.append(el('strong','',data.reference.title||data.reference.name||data.reference.source_url||'当前参考'));
        const status=root.StudioReferenceVideo?.taskPresentation(data.reference);
        if(status)body.append(note(status.label));
        if(data.reference.failure_reason)body.append(el('p','creative-board-warning',data.reference.failure_reason));
      }
      body.append(el('h3', '', '参考的表达与可借鉴之处'), report,
        button('查看参考分析', 'reference', () => call('openReference')));
    }
    function update(input, actions) {
      if (destroyed) return api;
      const nextData = projectBoard(input || {}), nextKey = JSON.stringify(nextData);
      callbacks = actions || {};
      // Routine polling must preserve playback, selection and scroll. Callback
      // closures use the latest handlers while the displayed object stays put.
      if (data && nextKey === renderKey) return api;
      const oldWriting = data?.writing, changedRun = runId !== nextData.runId;
      const sameWriting = !changedRun && oldWriting && nextData.writing && oldWriting.version === nextData.writing.version &&
        oldWriting.artifact_sha256 === nextData.writing.artifact_sha256;
      const scrolls = sameWriting ? ['.creative-board-table-scroll', '.creative-board-result-scroll'].map(selector => {
        const node = world.querySelector(selector); return {selector, top: node?.scrollTop || 0, left: node?.scrollLeft || 0};
      }) : [];
      data = nextData; renderKey = nextKey; runId = data.runId;
      focus.textContent = data.analysisOnly ? '看报告' : data.photo ? '看方案' : '看分镜';
      if (changedRun) { stopDrag(); view = {zoom: 1, x: 32, y: 32}; autoFit = true; }
      const active = doc.activeElement, focusKey = host.contains(active) ? active?.getAttribute?.('data-board-action') : null;
      bounds = {width: data.analysisOnly ? 1020 : 1460, height: 680};
      world.style.width = `${bounds.width}px`; world.style.height = `${bounds.height}px`; world.replaceChildren();
      linkNodes(330, 420); if (!data.analysisOnly) linkNodes(1020, 1110);
      needNode(); if (data.analysisOnly) analysisNode(); else { planNode(); resultNode(); }
      for (const saved of scrolls) { const node = world.querySelector(saved.selector);
        if (node) { node.scrollTop = saved.top; node.scrollLeft = saved.left; } }
      const height = Math.max(...[...world.querySelectorAll('[data-board-node]')].map(card => card.offsetHeight || 0));
      if (height) { bounds.height = height + 64; world.style.height = `${bounds.height}px`; }
      if (changedRun) fit(); else apply();
      if (focusKey && !changedRun) { const button = [...host.querySelectorAll('[data-board-action]')].find(item => item.getAttribute('data-board-action') === focusKey);
        button?.focus({preventScroll: true}); }
      return api;
    }
    function destroy() { if (destroyed) return; destroyed = true; stopDrag(); resize?.disconnect();
      for (const dispose of listeners) dispose(); shell.remove(); controllers.delete(host); }
    const api = {update, destroy, fit, zoomBy, focusPlan, getView: () => ({...view}), getProjection: () => data};
    return api;
  }

  function render(host, input, callbacks) {
    if (!host) return null;
    let controller = controllers.get(host);
    if (!controller) { controller = createController(host); controllers.set(host, controller); }
    return controller.update(input, callbacks);
  }
  function destroy(host) { controllers.get(host)?.destroy(); }
  root.StudioCreativeBoard = {render, destroy, projectBoard, safeMediaUrl, sourceMatches, fitView, zoomView};
  if (typeof module !== 'undefined') module.exports = root.StudioCreativeBoard;
})(typeof window !== 'undefined' ? window : globalThis);
