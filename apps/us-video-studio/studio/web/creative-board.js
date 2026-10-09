/* Canonical creative work with project-owned layouts and exact-object actions. */
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
        draftId: draft?.draft_id, draftSha256: draft?.draft_sha256, attemptId: attempt?.attempt_id, sourceVersion: draft?.source_version,
        outputSha256: url ? material.sha256 : '',
        state: row?.stage || 'unprepared', status: text(row?.stage_label) || '尚未准备输入',
        reason: text(row?.reason), name: url ? material.name : '', url,
        approved: url && typeof material.approved === 'boolean' ? material.approved : null,
        issue: valid && !url ? '结果文件暂时无法核对。' : ''};
    });
  }

  function photoResultStatus(item) {
    if (!item.url) return item.issue || item.status;
    return item.approved === true ? '已生成 · 已确认' : item.approved === false ? '已生成 · 待确认' : '已生成 · 确认状态待核对';
  }

  function imageCards(production, writing, materials, media, runId, format) {
    const cards = [], seen = new Set();
    const photo = format === 'photo', video = format === 'video';
    const sourceName = photo ? '图文方案' : video ? '脚本' : '方案';
    const itemName = photo ? '页' : video ? '镜头' : '画面';
    const itemLabel = ordinal => photo ? `第 ${ordinal} 页` : `${itemName} ${ordinal}`;
    const add = card => { const identity = `${card.name}:${card.sha256}`;
      if (seen.has(identity)) return; seen.add(identity); cards.push({...card, key: identity}); };
    const files = list(production.materials);
    for (const item of media.filter(row => row.kind === 'image' && row.url)) {
      const material = files.find(file => file.name === item.name && file.url === item.url);
      if (material) add({...item, sha256: material.sha256, historical: false,
        title: `${itemLabel(item.ordinal)} · ${sourceName}第 ${item.sourceVersion} 版`, issue: ''});
    }
    // Read previous successful bytes separately from current coverage. In particular,
    // missing design bindings must never turn a historical input into executable work.
    for (const draft of list(production.drafts).filter(row => row.kind === 'image')) {
      const attempts = list(draft.attempts), latest = attempts.at(-1);
      const target = sourceMatches(draft, writing) ? production.targets?.image : null;
      const shot = list(target?.shots).find(row => row.shot_id === draft.shot_id);
      const row = list(production.shots?.image?.shots).find(item => item.draft_id === draft.draft_id);
      for (const attempt of attempts) {
        const output = attempt.output;
        if (!['succeeded', 'unknown', 'unverified'].includes(attempt.status) || attempt.output_available !== true ||
            !output || !/^[a-f0-9]{64}$/.test(output.sha256 || '')) continue;
        const material = files.find(file => file.kind === 'image' && file.name === output.name && file.sha256 === output.sha256);
        const url = material && safeMediaUrl(material.url, runId, material.name);
        if (!url) continue;
        const version = Number.isInteger(draft.source_version) ? `${sourceName}第 ${draft.source_version} 版` : `${sourceName}版本未记录`;
        const identity = shot ? itemLabel(shot.ordinal) : draft.shot_id ? `原${itemName} ${draft.shot_id}` : `原${itemName}身份未记录`;
        const unconfirmed = attempt.status !== 'succeeded';
        const originalInput = Boolean(draft.design_issue) && draft.is_current !== false && sourceMatches(draft, writing);
        const issues = [unconfirmed ? '提交结果未确认；保留文件仅供核实，不能作为已完成结果。' : '',
          draft.design_issue ? `原输入结果可查看；补设计并核对来源后才能继续制作。${draft.design_issue}`
            : draft.source_ready === false ? '原输入需重新核对设计或来源，不能继续执行。' : ''];
        if (latest !== attempt) {
          const label = ({unknown: '结果待核实', unverified: '结果待核实', failed: '失败', submitted: '已提交',
            generating: '生成中', retrieving: '取回中', cancelled: '已停止'})[latest?.status] ||
            (row?.attempt_id === latest?.attempt_id ? text(row?.stage_label) : '') || '状态待核实';
          issues.push(`这是上一份已生成结果；当前尝试${label}。`);
        }
        let previous = false;
        if (draft.is_current === false) {
          const replacement = list(production.drafts).find(item => item.kind === 'image' && item.shot_id === draft.shot_id &&
            item.is_current === true && item.source_sha256 === draft.source_sha256 && item.source_version === draft.source_version);
          previous = Boolean(replacement && !list(replacement.attempts).some(item => item.output_available === true && item.output));
          issues.push(previous ? '上一版（修改后尚未重新生成）。' : '这份输入已被替换。');
        }
        add({name: material.name, sha256: material.sha256, url, kind: 'image',
          shotId: draft.shot_id || null, ordinal: shot?.ordinal, draftId: draft.draft_id,
          attemptId: attempt.attempt_id, sourceVersion: draft.source_version, historical: true,
          unconfirmed, title: `${unconfirmed ? '保留文件 · 提交结果未确认' : previous ? '上一版图片' : originalInput ? '原输入结果' : '历史图片'} · ${version} · ${identity}`,
          issue: issues.filter(Boolean).join(' ') || '只读历史结果，不代表本版完成或已采用。'});
      }
    }
    const generated = new Set(list(production.drafts).flatMap(draft => list(draft.attempts))
      .map(attempt => attempt.output?.name).filter(Boolean));
    for (const material of list(materials)) {
      if (material.kind !== 'image' || material.origin === 'generated' || generated.has(material.name)) continue;
      const url = safeMediaUrl(material.url, runId, material.name);
      if (url) add({name: material.name, sha256: material.sha256 || '', url, kind: 'image',
        title: `项目素材 · ${material.name}`, historical: false, source: true, issue: ''});
    }
    return cards;
  }

  function projectBoard({runId, run = {}, timeline = {}, production = {}, materials = [], format,
    analysisOnly = false, referenceTasks = [], referenceSelectedId = null} = {}) {
    run = run || {}; timeline = timeline || {}; production = production || {}; materials = materials || [];
    const id = runId || run.run_id || '', writing = stage(timeline, 'writing'), direction = stage(timeline, 'direction');
    const material = stage(timeline, 'material_plan'), intake = stage(timeline, 'intake');
    const imageAnalysis = timeline.brief?.analysis?.kind === 'image' &&
      timeline.brief.analysis.analysis_only === true && !direction && !material && !writing
      ? {...timeline.brief.analysis} : null;
    analysisOnly = Boolean(imageAnalysis || analysisOnly);
    const deliveryFormat = format || run.content_format;
    const photo = deliveryFormat === 'photo', video = deliveryFormat === 'video';
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
    const media = analysisOnly ? [] : ['image', ...(video ? ['video'] : [])].flatMap(kind =>
      currentMedia(production, writing, production.targets?.[kind], kind, id));
    let writingIssue = '';
    if (writing?.stale || writing?.integrity_ok === false) writingIssue = writing.stale_sentence || writing.integrity_sentence || '当前脚本依据需要重新核对。';
    else if (writing && !adopted(writing)) writingIssue = `这版方案尚未采用；${photo ? '逐页' : video ? '镜头' : '画面'}清单只供阅读，不代表可以生成。`;
    const historyCount = list(production.drafts).filter(draft => draft.is_current === false ||
      !sourceMatches(draft, writing) || draft.source_ready === false).length;
    const selectedReference = referenceSelectedId
      ? list(referenceTasks).find(task => task.reference_id === referenceSelectedId) : list(referenceTasks).at(-1);
    const reference = !imageAnalysis && selectedReference ? {reference_id:selectedReference.reference_id, name:selectedReference.name,source_url:selectedReference.source_url,
      title:selectedReference.title, report:selectedReference.report, status:selectedReference.status,
      execution_status:selectedReference.execution_status, job_id:selectedReference.job_id,
      failure_reason:selectedReference.failure_reason} : null;
    return {runId: id, photo, video, analysisOnly: Boolean(analysisOnly), title: text(run.title) || '当前作品',
      brief: text(run.brief) || text(timeline.brief?.goal) || '需求尚未保存，可以在右侧对话里说明想做什么。',
      intake, direction, directionId, directionTitle: selected ? field(selected, '标题', '方向名') || text(selected.head) : '',
      directionSummary: selected ? field(selected, '主张', '核心主张', '推进概念', '故事', '故事梗概', '开场识别点', '开场') : '',
      directionIssue: direction?.stale ? direction.stale_sentence || '此前方向已过期，请查看当前决定。' :
        adopted(direction) && !directionId ? '已采用方向，旧记录未注明具体选项。' : '',
      thumbs, material, design: designProjection(material, production), writing, writingIssue, rows, media, historyCount,
      photoDelivery: production.photo_delivery || null,
      images: imageCards(production, writing, allMaterials, media, id, deliveryFormat),
      caption: text(list(writing?.blocks).find(block => block.type === 'text' && block.title === '发布文案')?.text),
      reference, imageAnalysis, analysis: text(imageAnalysis?.report ?? reference?.report)};
  }

  const clamp = value => Math.max(.35, Math.min(1.6, value));
  function fitView(width, height, bounds) {
    const zoom = clamp(Math.min(1, (width - 64) / bounds.width, (height - 72) / bounds.height));
    return {zoom, x: (width - bounds.width * zoom) / 2 - (bounds.left || 0) * zoom,
      y: Math.max(24, (height - bounds.height * zoom) / 2) - (bounds.top || 0) * zoom};
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
    let bounds = {width: 1460, height: 680}, autoFit = false, initialViewPending = false,
      restoringFocus = false, dragging = null, destroyed = false, renderKey = '';
    let positions = {}, imageDrag = null, photoCollection = null;
    let layoutRevision = null, layoutSequence = 0, layoutTimer = null, layoutSaving = false, layoutExtras = {groups:[],notes:[]};
    const layoutNote = el('span', 'creative-board-layout-note', '布局仅保存在本机');
    layoutNote.setAttribute('role', 'status');
    const layoutKey = () => `studio-creative-board-layout-v1:${encodeURIComponent(runId)}`;
    const coordinate = value => Number.isFinite(value) && Math.abs(value) <= 100000;
    function restoreLayout() {
      positions = {};
      try {
        const saved = JSON.parse(root.localStorage?.getItem(layoutKey()) || 'null');
        if (!saved || saved.version !== 1) return false;
        for (const [key, point] of Object.entries(saved.positions || {})) {
          if (point && coordinate(point.x) && coordinate(point.y)) positions[key] = {x: point.x, y: point.y};
        }
        if (saved.view && coordinate(saved.view.x) && coordinate(saved.view.y) &&
            Number.isFinite(saved.view.zoom) && saved.view.zoom >= .35 && saved.view.zoom <= 1.6) {
          view = {...saved.view}; autoFit = false; return true;
        }
      } catch (_) { layoutNote.textContent = '本机布局无法读取；使用默认视图'; }
      return false;
    }
    function persistLayout() {
      if (!runId || destroyed) return;
      initialViewPending = false;
      try {
        if (!root.localStorage) throw Error('storage unavailable');
        root.localStorage.setItem(layoutKey(), JSON.stringify({version: 1, view, positions}));
        layoutNote.textContent = typeof callbacks.saveLayout === 'function' ? '布局待保存到项目' : '布局仅保存在本机';
      } catch (_) { layoutNote.textContent = '布局未保存；仅本次页面保留'; }
      layoutSequence += 1;
      if (typeof callbacks.saveLayout === 'function' && layoutRevision !== null) {
        root.clearTimeout(layoutTimer); layoutTimer = root.setTimeout(saveProjectLayout, 400);
      }
    }
    async function saveProjectLayout() {
      if (destroyed || layoutSaving || layoutRevision === null || typeof callbacks.saveLayout !== 'function') return;
      const savedRun = runId, sequence = layoutSequence, snapshot = {view:{...view},positions:JSON.parse(JSON.stringify(positions))};
      layoutSaving = true; layoutNote.textContent = '正在保存项目布局…';
      try {
        const result = await callbacks.saveLayout({expected_revision:layoutRevision,...snapshot,...layoutExtras});
        if (destroyed || savedRun !== runId) return;
        if(!Number.isInteger(result?.revision)||result.revision!==layoutRevision+1)throw new Error('项目布局回执版本无法核对。');
        layoutRevision = result.revision;
        layoutNote.textContent = '布局已保存到项目';
      } catch (error) {
        if (destroyed || savedRun !== runId) return;
        layoutNote.textContent = error.status === 409 ? '布局已在其他窗口更新；本机摆放保留，重新打开核对后再保存' : '项目布局未保存；本机摆放保留';
        layoutRevision = null;
      } finally {
        layoutSaving = false;
        if (!destroyed && savedRun === runId && sequence !== layoutSequence && layoutRevision !== null) void saveProjectLayout();
      }
    }
    async function restoreProjectLayout() {
      if (typeof callbacks.loadLayout !== 'function') return;
      const savedRun = runId, sequence = layoutSequence;
      try {
        const saved = await callbacks.loadLayout();
        if (destroyed || savedRun !== runId || !Number.isInteger(saved?.revision)) return;
        layoutRevision = saved.revision;
        layoutExtras = {groups:list(saved.groups),notes:list(saved.notes)};
        if (sequence !== layoutSequence) { void saveProjectLayout(); return; }
        if (saved.revision > 0) {
          positions = Object.fromEntries(Object.entries(saved.positions || {}).filter(([,point]) => point && coordinate(point.x) && coordinate(point.y)));
          if (saved.view && coordinate(saved.view.x) && coordinate(saved.view.y) && Number.isFinite(saved.view.zoom)) {
            view = {...saved.view,zoom:clamp(saved.view.zoom)}; initialViewPending = false;
          }
          positionImages(); updateBounds(); apply(); layoutNote.textContent = '布局已保存到项目';
        } else { layoutNote.textContent = '当前本机布局；调整后保存到项目'; }
      } catch (_) { if (savedRun === runId && !destroyed) layoutNote.textContent = '项目布局暂未读取；本机摆放保留'; }
    }
    const listeners = [];
    const listen = (node, name, handler, options) => { node.addEventListener(name, handler, options);
      listeners.push(() => node.removeEventListener(name, handler, options)); };
    const button = (value, key, handler, cls = 'creative-board-button') => {
      const node = el('button', cls, value); node.type = 'button'; node.setAttribute('data-board-action', key);
      node.onclick = () => { if (!destroyed && host.contains(node)) handler(); }; return node;
    };
    function closePhotoCollection(restoreFocus = true) {
      if (!photoCollection) return;
      const {dialog, opener, controller} = photoCollection;
      controller?.abort();
      photoCollection = null;
      dialog.onclose = null; dialog.oncancel = null;
      if (dialog.open) dialog.close();
      dialog.remove();
      if (restoreFocus && !destroyed && host.contains(opener)) opener.focus({preventScroll: true});
    }
    function showPhotoCollection() {
      if (!data?.photo || data.analysisOnly || destroyed) return;
      closePhotoCollection(false);
      const opener = host.contains(doc.activeElement) ? doc.activeElement : overview;
      const dialog = el('dialog', 'creative-photo-collection');
      dialog.setAttribute('aria-label', '整组图文');
      const header = el('header', 'creative-photo-collection-header');
      const heading = el('div'); heading.append(el('h2', '', '整组图文'), el('p', '', data.title));
      header.append(heading, button('关闭', 'close-photo-collection', () => closePhotoCollection()));
      const body = el('div', 'creative-photo-collection-body');
      const pages = data.media.filter(item => item.kind === 'image');
      const verifiedList = pages.length > 0 && pages.every(item =>
        Number.isInteger(item.ordinal) && item.ordinal > 0 && item.shotId &&
        pages.filter(other => other.ordinal === item.ordinal).length === 1 &&
        pages.filter(other => other.shotId === item.shotId).length === 1);
      const version = Number.isInteger(data.writing?.version) && data.writing.version > 0
        ? `图文方案第 ${data.writing.version} 版` : '图文方案版本待核对';
      body.append(el('p', 'creative-photo-collection-summary', verifiedList
        ? `${version} · 当前 ${pages.filter(item => item.url).length} / ${pages.length} 页有生成结果 · ${pages.filter(item => item.url && item.approved === true).length} 页已确认`
        : `${version} · 当前逐页结果清单尚未核对`));
      if (data.writingIssue) body.append(el('p', 'creative-board-warning', data.writingIssue));
      body.append(el('p', 'creative-board-note', '逐页核对图片、文字和商品外观。下载只保存原文件；图审确认仍在制作详情中完成。'));
      const archive = data.photoDelivery;
      const archiveMatches = verifiedList && !data.writingIssue && archive?.available === true &&
        archive.run_id === runId && sourceMatches(archive, data.writing) && /^[a-f0-9]{64}$/.test(text(archive.snapshot_sha256)) &&
        list(archive.pages).length === pages.length && pages.every(item => item.url &&
          /^[a-f0-9]{64}$/.test(text(item.draftSha256)) && /^[a-f0-9]{64}$/.test(text(item.outputSha256)) && list(archive.pages).filter(page =>
          page.ordinal === item.ordinal && page.shot_id === item.shotId && page.draft_id === item.draftId &&
          page.draft_sha256 === item.draftSha256 && page.attempt_id === item.attemptId && page.name === item.name &&
          page.sha256 === item.outputSha256 && page.approved === item.approved).length === 1);
      const archiveNotice = el('p', 'creative-board-note', archiveMatches ? '含当前逐页原图、发布文案和真实确认状态；不会调用模型。' :
        sourceMatches(archive, data.writing) && archive?.available === false ? text(archive.reason) : '整组下载还需核对当前图文和结果，可以先下载已有单页。');
      archiveNotice.setAttribute('role', 'status');
      const archiveButton = button(archiveMatches ? `下载当前 ${pages.length} 页图文 ZIP` : '整组 ZIP 待齐全',
        'download-photo-collection', () => downloadPhotoCollection(dialog, archive, archiveNotice, archiveButton));
      archiveButton.disabled = !archiveMatches;
      body.append(archiveButton, archiveNotice);
      const grid = el('div', 'creative-photo-collection-grid');
      if (!verifiedList) grid.append(el('p', 'creative-board-warning', '尚无可核对的当前逐页结果清单，暂不展示整组图片。原图和历史结果仍可在画布中查看。'));
      else for (const item of [...pages].sort((a, b) => a.ordinal - b.ordinal)) {
        const page = el('section', 'creative-photo-collection-page');
        page.setAttribute('data-photo-page', item.shotId);
        page.append(el('h3', '', `第 ${item.ordinal} 页`));
        const status = el('p', 'creative-photo-collection-status', photoResultStatus(item));
        page.append(status);
        if (item.url) {
          const image = el('img'); image.src = item.url; image.alt = `第 ${item.ordinal} 页当前生成图片`;
          const download = el('a', 'creative-board-button', `下载第 ${item.ordinal} 页原图`);
          download.setAttribute('href', item.url); download.setAttribute('download', item.name);
          image.onerror = () => {
            image.remove(); download.remove(); status.textContent = '当前图片未能读取';
            page.append(el('p', 'creative-board-warning', '请关闭后重新打开核对当前结果；不会用旧图替代。'));
          };
          page.append(image, download);
          page.append(button('保存这一页的批注',`annotate:${item.attemptId}`,()=>call('annotateMedia',item)));
        } else page.append(el('p', 'creative-photo-collection-missing', item.issue || item.reason || '这一页尚无可核对的当前图片。'));
        const rows = data.rows.filter(row => row.shotId === item.shotId);
        if (rows.length === 1 && rows[0].line) {
          const copy = el('details', 'creative-photo-collection-copy');
          copy.append(el('summary', '', '核对方案中的图内文案'), el('p', '', rows[0].line)); page.append(copy);
        }
        grid.append(page);
      }
      const caption = el('section', 'creative-photo-collection-caption');
      caption.append(el('h3', '', '发布文案'), el('p', '', data.caption || '本版尚无已保存的发布文案。'));
      body.append(grid, caption); dialog.append(header, body); shell.append(dialog);
      photoCollection = {dialog, opener};
      dialog.oncancel = event => { event.preventDefault(); closePhotoCollection(); };
      dialog.onclose = () => { if (photoCollection?.dialog === dialog) closePhotoCollection(); };
      dialog.showModal();
    }
    async function downloadPhotoCollection(dialog, archive, notice, control) {
      if (photoCollection?.dialog !== dialog || control.disabled) return;
      const controller = new root.AbortController();
      photoCollection.controller = controller;
      control.disabled = true; notice.textContent = '正在打包你查看的当前图文…';
      const current = () => !destroyed && photoCollection?.dialog === dialog && host.contains(dialog);
      try {
        const response = await root.fetch(`/api/runs/${encodeURIComponent(runId)}/delivery/photos/download?expected_snapshot_sha256=${encodeURIComponent(archive.snapshot_sha256)}`,
          {signal: controller.signal});
        if (!current()) return;
        if (!response.ok) {
          const problem = await response.json().catch(() => null);
          notice.textContent = typeof problem?.detail === 'string' ? problem.detail : '图文下载未能完成，请关闭后重新核对当前结果。';
          return;
        }
        if ((response.headers.get('content-type') || '').split(';')[0] !== 'application/zip') {
          notice.textContent = '未收到有效的图文 ZIP，请关闭后重新核对当前结果。'; return;
        }
        const bytes = await response.blob();
        if (!current()) return;
        const url = root.URL.createObjectURL(bytes), link = el('a');
        link.setAttribute('href', url); link.setAttribute('download', `${runId}-v${archive.source_version}-photos.zip`);
        dialog.append(link); link.click(); link.remove();
        root.setTimeout(() => root.URL.revokeObjectURL(url), 0);
        notice.textContent = '当前图文 ZIP 已交给浏览器下载；图审和内容验收状态保持原样。';
      } catch (error) {
        if (current() && error.name !== 'AbortError') notice.textContent = '图文下载连接未能完成，请关闭后重新核对当前结果。';
      } finally {
        if (current()) { control.disabled = false; photoCollection.controller = null; }
      }
    }
    function size() { return {width: viewport.clientWidth || host.clientWidth || 1000, height: viewport.clientHeight || host.clientHeight || 680}; }
    function apply() {
      world.style.transform = `translate(${view.x}px, ${view.y}px) scale(${view.zoom})`;
      label.textContent = `${Math.round(view.zoom * 100)}%`;
    }
    const isStacked = () => typeof root.matchMedia === 'function' && root.matchMedia('(max-width:650px)').matches;
    function initializeReadingView() {
      // A hidden pane has no real viewport yet. Retain the one-time default
      // until its first measured display; never use size()'s synthetic fallback.
      if (!initialViewPending || !data || destroyed || isStacked() ||
          !(viewport.clientWidth > 0 && viewport.clientHeight > 0)) return false;
      const key = data.analysisOnly ? (data.analysis.trim() ? 'analysis' : 'brief')
        : data.material || data.writing ? 'plan' : 'brief';
      const target = world.querySelector(`[data-board-node="${key}"]`);
      if (!target) return false;
      const left = parseFloat(target.style.left) || 0;
      const width = target.offsetWidth || parseFloat(target.style.width) || 330;
      view = {zoom: 1, x: Math.max(24, (viewport.clientWidth - width) / 2) - left,
        y: 24 - (parseFloat(target.style.top) || 32)};
      initialViewPending = false;
      autoFit = false;
      apply();
      return true;
    }
    function fit(save = true) { initialViewPending = false; autoFit = true;
      if (isStacked()) { viewport.scrollTop = 0; return; }
      updateBounds();
      const dimensions = size(); view = fitView(dimensions.width, dimensions.height, bounds); apply(); if (save) persistLayout(); }
    function zoomBy(factor, anchor) { const dimensions = size(); autoFit = false;
      view = zoomView(view, factor, anchor || {x: dimensions.width / 2, y: dimensions.height / 2}); apply(); persistLayout(); }
    function focusPlan() { autoFit = false;
      if (isStacked()) { world.querySelector(data.analysisOnly ? '.creative-board-analysis' : '.creative-board-plan')?.scrollIntoView({block: 'start'}); return; }
      const dimensions = size();
      const plan = world.querySelector(data.analysisOnly ? '.creative-board-analysis' : '.creative-board-plan');
      const height = plan?.offsetHeight || bounds.height - 64;
      const zoom = Math.max(.7, Math.min(1, (dimensions.width - 48) / 600, (dimensions.height - 48) / (height + 64)));
      view = {zoom, x: (dimensions.width - 600 * zoom) / 2 - 420 * zoom, y: 24 - 32 * zoom}; apply(); persistLayout(); }
    const minus = button('−', 'zoom-out', () => zoomBy(1 / 1.2)); minus.setAttribute('aria-label', '缩小画布');
    const plus = button('+', 'zoom-in', () => zoomBy(1.2)); plus.setAttribute('aria-label', '放大画布');
    const focus = button('看分镜', 'focus-plan', focusPlan);
    const overview = button('看图文', 'photo-collection', showPhotoCollection);
    toolbar.append(hint, focus, overview, button('对象与关系', 'objects', () => call('openObjects')),
      button('批注汇总', 'annotations', () => call('openAnnotations')),
      button('看图片', 'focus-images', () => focusImage(data.images[0]?.key)),
      minus, label, plus, button('适配视图', 'fit', fit), button('重置布局', 'reset-layout', resetLayout), layoutNote);
    viewport.append(world); shell.append(toolbar, viewport); host.replaceChildren(shell);
    const interactive = target => target?.closest?.('button,a,input,textarea,select,video,summary,[role="button"],[data-board-scroll]');
    // Keep keyboard focus visible through the same canvas transform as panning.
    // Native hidden-overflow scrolling creates a second, unsaved coordinate
    // system and can visually cancel a card's ArrowRight movement.
    listen(viewport, 'focusin', event => {
      if (isStacked() || restoringFocus || imageDrag || dragging || !world.contains(event.target)) return;
      const target = event.target.getBoundingClientRect(), frame = viewport.getBoundingClientRect();
      const margin = 16;
      const dx = target.left < frame.left + margin ? frame.left + margin - target.left :
        target.right > frame.right - margin ? frame.right - margin - target.right : 0;
      const dy = target.top < frame.top + margin ? frame.top + margin - target.top :
        target.bottom > frame.bottom - margin ? frame.bottom - margin - target.bottom : 0;
      if (dx || dy) { autoFit = false; view.x += dx; view.y += dy; apply(); persistLayout(); }
    });
    listen(viewport, 'wheel', event => {
      if (isStacked()) return;
      if (event.ctrlKey || event.metaKey) {
        event.preventDefault(); const rect = viewport.getBoundingClientRect();
        zoomBy(Math.exp(-event.deltaY * .002), {x: event.clientX - rect.left, y: event.clientY - rect.top});
      } else if (!interactive(event.target)) {
        event.preventDefault(); autoFit = false; view.x -= event.deltaX; view.y -= event.deltaY; apply(); persistLayout();
      }
    }, {passive: false});
    listen(viewport, 'pointerdown', event => {
      const handle = event.target?.closest?.('[data-image-drag]');
      if (!isStacked() && handle && (event.button === undefined || event.button === 0)) {
        const card = handle.closest('[data-board-image]'), point = imagePosition(card);
        initialViewPending = false; autoFit = false; imageDrag = {id: event.pointerId, x: event.clientX, y: event.clientY,
          left: point.x, top: point.y, card};
        viewport.setPointerCapture?.(event.pointerId); handle.focus({preventScroll: true}); event.preventDefault(); return;
      }
      if (isStacked() || (event.button !== undefined && event.button !== 0) || interactive(event.target) ||
          event.target?.closest?.('.creative-board-node-body,.creative-board-image-card')) return;
      initialViewPending = false; autoFit = false;
      dragging = {id: event.pointerId, x: event.clientX, y: event.clientY, left: view.x, top: view.y};
      viewport.setPointerCapture?.(event.pointerId); viewport.classList.add('is-dragging');
      viewport.focus({preventScroll: true}); event.preventDefault();
    });
    listen(viewport, 'pointermove', event => {
      if (imageDrag && event.pointerId === imageDrag.id) {
        const point = {x: imageDrag.left + (event.clientX - imageDrag.x) / view.zoom,
          y: imageDrag.top + (event.clientY - imageDrag.y) / view.zoom};
        placeImage(imageDrag.card, point); return;
      }
      if (!dragging || event.pointerId !== dragging.id) return;
      view.x = dragging.left + event.clientX - dragging.x; view.y = dragging.top + event.clientY - dragging.y; apply();
    });
    function stopDrag() {
      const previous = imageDrag || dragging; imageDrag = null; dragging = null;
      if (previous && viewport.hasPointerCapture?.(previous.id)) viewport.releasePointerCapture(previous.id);
      viewport.classList.remove('is-dragging');
      if (previous) { updateBounds(); persistLayout(); }
    }
    for (const type of ['pointerup', 'pointercancel', 'lostpointercapture']) listen(viewport, type, stopDrag);
    listen(viewport, 'keydown', event => {
      const handle = event.target?.closest?.('[data-image-drag]');
      if (handle && !isStacked() && !event.ctrlKey && !event.metaKey && !event.altKey &&
          ['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(event.key)) {
        event.preventDefault(); const card = handle.closest('[data-board-image]'), amount = event.shiftKey ? 100 : 10;
        const current = imagePosition(card);
        placeImage(card, {x: current.x + (event.key === 'ArrowLeft' ? -amount : event.key === 'ArrowRight' ? amount : 0),
          y: current.y + (event.key === 'ArrowUp' ? -amount : event.key === 'ArrowDown' ? amount : 0)});
        updateBounds(); persistLayout(); return;
      }
      if (event.ctrlKey || event.metaKey || event.altKey || interactive(event.target)) return;
      if (isStacked()) { if (['0', 'f', 'F'].includes(event.key)) { event.preventDefault(); fit(); } return; }
      if (['+', '=', '-', '_', '0', 'f', 'F', 'ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(event.key)) {
        event.preventDefault();
        if (['+', '='].includes(event.key)) zoomBy(1.2);
        else if (['-', '_'].includes(event.key)) zoomBy(1 / 1.2);
        else if (['0', 'f', 'F'].includes(event.key)) fit();
        else { autoFit = false; const amount = event.shiftKey ? 100 : 40;
          view.x += event.key === 'ArrowLeft' ? amount : event.key === 'ArrowRight' ? -amount : 0;
          view.y += event.key === 'ArrowUp' ? amount : event.key === 'ArrowDown' ? -amount : 0; apply(); persistLayout(); }
      }
    });
    const resize = typeof root.ResizeObserver === 'function' ? new root.ResizeObserver(() => {
      if (destroyed || initializeReadingView()) return;
      if (autoFit) fit();
    }) : null;
    resize?.observe(viewport);

    const call = (name, ...args) => { if (typeof callbacks[name] === 'function') callbacks[name](...args); };
    const note = value => el('p', 'creative-board-note', value);
    const imagePosition = card => ({x: parseFloat(card.style.left) || 0, y: parseFloat(card.style.top) || 0});
    function placeImage(card, point) {
      if (!card || !coordinate(point.x) || !coordinate(point.y)) return;
      card.style.left = `${point.x}px`; card.style.top = `${point.y}px`;
      positions[card.getAttribute('data-board-image')] = {...point};
    }
    function updateBounds() {
      const nodes = [...world.querySelectorAll('[data-board-node]')];
      let right = data?.analysisOnly ? 1020 : 1460, bottom = Math.max(680, ...nodes.map(card => (card.offsetHeight || 0) + 64));
      let left = 0, top = 0;
      for (const card of world.querySelectorAll('[data-board-image]')) {
        const point = imagePosition(card);
        left = Math.min(left, point.x - 24); top = Math.min(top, point.y - 24);
        right = Math.max(right, point.x + 320 + 24); bottom = Math.max(bottom, point.y + (card.offsetHeight || 360) + 24);
      }
      bounds = {width: right - left, height: bottom - top, left, top};
      world.style.width = `${right}px`; world.style.height = `${bottom}px`;
    }
    function focusImage(key) {
      const card = [...world.querySelectorAll('[data-board-image]')].find(item => item.getAttribute('data-board-image') === key);
      if (!card) return;
      if (isStacked()) { card.scrollIntoView({block: 'start'}); return; }
      const point = imagePosition(card), dimensions = size(); autoFit = false;
      const zoom = Math.max(.7, Math.min(1, (dimensions.height - 64) / (card.offsetHeight || 360)));
      view = {zoom, x: (dimensions.width - 320 * zoom) / 2 - point.x * zoom, y: 24 - point.y * zoom};
      apply(); persistLayout();
    }
    function resetLayout() {
      stopDrag(); positions = {};
      try { root.localStorage?.removeItem(layoutKey()); layoutNote.textContent = '已重置本项目的本机布局'; }
      catch (_) { layoutNote.textContent = '本次已重置；本机保存的布局无法清除'; }
      positionImages(); fit(false);
      if(typeof callbacks.saveLayout==='function')persistLayout();
    }
    function positionImages() {
      const cards = [...world.querySelectorAll('[data-board-image]')];
      const rowHeight = Math.max(396, ...cards.map(card => card.offsetHeight || 0)) + 24;
      for (const [index, card] of cards.entries()) {
        const key = card.getAttribute('data-board-image');
        const point = positions[key] || {x: (data.analysisOnly ? 1070 : 1510) + (index % 2) * 344, y: 32 + Math.floor(index / 2) * rowHeight};
        card.style.left = `${point.x}px`; card.style.top = `${point.y}px`;
      }
    }
    function renderImages() {
      for (const item of data.images) {
        const card = el('article', 'creative-board-image-card' + (item.historical ? ' is-historical' : ''));
        card.setAttribute('data-board-image', item.key);
        const handle = button('⠿ 移动图片', `move:${item.key}`, () => {}, 'creative-board-image-handle');
        handle.setAttribute('data-image-drag', '');
        handle.setAttribute('aria-label', `移动 ${item.title}；方向键移动，Shift 加速`);
        handle.title = '拖动此手柄摆放图片；方向键移动，Shift 加速。';
        card.append(handle, el('h3', '', item.title));
        const preview = button('', item.source ? `source:${item.name}` : `output:${item.name}`,
          () => call('openMedia', item.name), 'creative-board-output');
        preview.setAttribute('aria-label', `查看 ${item.title}`);
        const image = el('img'); image.src = item.url; image.alt = item.title; image.loading = 'lazy'; image.draggable = false;
        preview.append(image); card.append(preview);
        if (item.issue) card.append(note(item.issue));
        card.append(el('span', 'creative-board-image-filename', item.name)); world.append(card);
      }
      positionImages();
    }
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
      const body = node(data.imageAnalysis ? '图片分析' : '需求', '01', 'brief', 0, 330);
      const brief = el('p', 'creative-board-brief-text', data.brief); brief.setAttribute('data-board-scroll', '');
      body.append(el('h3', 'creative-board-work-title', data.title), brief);
      if (data.intake) body.append(button('查看完整需求', 'intake', () => call('openStage', 'intake', data.intake.version)));
      const direction = el('section', 'creative-board-direction');
      if (data.analysisOnly) {
        direction.append(el('h3', '', '本轮分析范围'),
          note(data.imageAnalysis ? '本轮图片观察及不能确认的信息见图片分析报告。'
            : '本轮只拆解参考视频，分析报告与任务状态见参考节点。若要继续创作，请在对话中说明新的作品要求。'));
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
      const body = node(data.photo ? '图文方案' : data.video ? '人物、场景与分镜' : '创作方案', data.writing ? `第 ${data.writing.version} 版` : '02', 'plan', 420, 600);
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
          continuity.append(el('summary', '', data.photo ? '跨页一致性' : data.video ? '跨镜一致性' : '画面一致性'), note(data.design.continuity.join('；'))); design.append(continuity); }
      }
      if (data.material) design.append(button('查看人物与场景', 'design', () => call('openStage', 'material_plan', data.material.version)));
      body.append(design);
      const heading = el('div', 'creative-board-section-heading'); heading.append(el('h3', '', data.photo ? '逐页画面与文案' : data.video ? '逐镜画面与口播' : '画面与文字'));
      if (data.writing) heading.append(button('查看完整方案', 'writing', () => call('openStage', 'writing', data.writing.version)));
      body.append(heading); if (data.writingIssue) body.append(el('p', 'creative-board-warning', data.writingIssue));
      if (!data.rows.length) body.append(note(data.writing ? `这份历史稿没有可读的方案表，可查看完整方案；不会猜测${data.photo ? '页数' : data.video ? '镜头数量' : '画面数量'}。` : '方案尚未保存，团队会在当前对话中继续准备。'));
      else {
        const scroll = el('div', 'creative-board-table-scroll'); scroll.setAttribute('data-board-scroll', ''); scroll.tabIndex = 0;
        scroll.setAttribute('aria-label', data.photo ? '逐页方案表' : data.video ? '逐镜分镜表' : '方案表');
        const table = el('table', 'creative-board-shot-table'), thead = el('thead'), head = el('tr');
        for (const title of [data.photo ? '页' : data.video ? '时间' : '序号', '画面', data.video ? '口播 / 字幕' : '文案']) { const th = el('th', '', title); th.scope = 'col'; head.append(th); }
        thead.append(head); table.append(thead); const tbody = el('tbody');
        for (const row of data.rows) {
          const tr = el('tr'); if (row.shotId) tr.setAttribute('data-shot-id', row.shotId);
          const time = el('td'), picture = el('td'), line = el('td');
          time.append(el('strong', '', data.photo ? `第 ${row.index} 页` : data.video ? row.time || `第 ${row.index} 镜` : `第 ${row.index} 项`));
          if (row.shotId) time.append(button(data.photo ? '查看这一页' : data.video ? '查看这一镜' : '查看画面', `shot:image:${row.shotId}`, () => call('openShot', 'image', row.shotId)));
          const selectable = (node, value, name, key) => {
            if (value && !data.writing.stale && data.writing.integrity_ok !== false && /^[a-f0-9]{64}$/.test(data.writing.artifact_sha256 || '')) {
              node.setAttribute('data-creator-target', `${data.photo ? '第 ' + row.index + ' 页' : '第 ' + row.index + ' 段'}${name}`);
              node.setAttribute('data-creator-path', JSON.stringify(['segments', row.index - 1, key]));
            }
            return node;
          };
          picture.append(selectable(el('p', '', row.picture || '这版未注明画面。'), row.picture, '画面', 'picture'));
          if (row.characters || row.scene) picture.append(el('small', '', [row.characters, row.scene].filter(Boolean).join(' · ')));
          line.append(selectable(el('p', '', row.line || (data.video ? '这版未注明台词或字幕。' : '这版未注明文案。')), row.line, data.photo ? '图内文案' : data.video ? '台词' : '文案', 'line'));
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
      const body = node(data.photo ? '图片结果' : data.video ? '媒体结果' : '作品结果', '03', 'results', 1110, 350);
      body.append(note(data.photo ? '按本版页码对应展示。' : data.video ? '按本版镜头对应展示。' : '按当前方案对应展示。'));
      if (data.photo) body.append(button('查看整组图文', 'photo-collection:results', showPhotoCollection));
      const gallery = el('div', 'creative-board-result-scroll'); gallery.setAttribute('data-board-scroll', ''); gallery.tabIndex = 0;
      gallery.setAttribute('aria-label', data.photo ? '当前图文的图片结果' : data.video ? '当前镜头的媒体结果' : '当前方案的作品结果');
      const shots = [...new Set(data.media.map(item => item.shotId))];
      if (!shots.length) gallery.append(note(data.writing ? '当前方案尚无对应的制作结果清单。' : data.photo ? '方案保存后，真实生成的图片会按页显示在这里。' : data.video ? '方案保存后，真实生成的结果会按镜头显示在这里。' : '方案保存后，真实生成的结果会显示在这里。'));
      for (const shotId of shots) {
        const items = data.media.filter(item => item.shotId === shotId), section = el('section', 'creative-board-result-shot');
        section.setAttribute('data-result-shot', shotId);
        section.append(el('h3', '', `${data.photo ? '第' : data.video ? '镜头' : '画面'} ${items[0].ordinal}${data.photo ? ' 页' : ''}`));
        for (const item of items) {
          const card = el('div', `creative-board-media creative-board-media-${item.kind}`); card.setAttribute('data-media-kind', item.kind);
          card.append(el('span', 'creative-board-media-label', `${item.kind === 'image' ? '图片' : '视频'} · ${data.photo && item.kind === 'image' ? photoResultStatus(item) : item.status}`));
          if (item.url) {
            if (item.kind === 'image') {
              const image = data.images.find(row => row.name === item.name);
              card.append(button('定位图片 ↗', `locate:${image?.key}`, () => focusImage(image?.key)));
            } else { const video = el('video'); video.src = item.url; video.controls = true; video.playsInline = true; video.preload = 'metadata';
              video.setAttribute('aria-label', `播放镜头 ${item.ordinal} 视频`); card.append(video,
                button('批注当前时间',`annotate:${item.attemptId}`,()=>call('annotateMedia',item,video.currentTime))); }
          } else { card.append(note(item.issue || item.reason || (data.photo ? '这一页尚无可核对的当前图片。' : data.video ? '这面镜头尚无可核对的当前结果。' : '这一画面尚无可核对的当前结果。'))); }
          card.append(button(data.photo ? '查看这一页' : data.video ? '查看这一镜' : '查看画面', `shot:${item.kind}:${shotId}`, () => call('openShot', item.kind, shotId))); section.append(card);
        }
        gallery.append(section);
      }
      body.append(gallery);
      if (data.historyCount) body.append(note(`${data.historyCount} 份旧输入保留在制作详情与历史中。`));
      const history = el('div', 'creative-board-history');
      history.append(button('查看图片记录', 'history:image', () => call('openShot', 'image', null)));
      if (data.video) history.append(button('查看视频记录', 'history:video', () => call('openShot', 'video', null)));
      body.append(history);
    }
    function analysisNode() {
      const body = node(data.imageAnalysis ? '图片分析报告' : '分析报告', '02', 'analysis', 420, 600);
      if (data.imageAnalysis) {
        const report = note(data.analysis || text(data.imageAnalysis.detail) || '本轮尚无已保存的图片分析回复。');
        report.setAttribute('data-board-scroll', '');
        body.append(el('strong', '', text(data.imageAnalysis.title) || '图片分析'));
        if (data.analysis && data.imageAnalysis.detail) body.append(note(text(data.imageAnalysis.detail)));
        body.append(el('h3', '', '图片观察结果与限制'), report,
          button('查看完整图片分析', 'image-analysis', () => call('openReference')));
        return;
      }
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
      if (data && nextKey === renderKey) { initializeReadingView(); return api; }
      const oldWriting = data?.writing, changedRun = runId !== nextData.runId;
      closePhotoCollection(!changedRun);
      const sameWriting = !changedRun && oldWriting && nextData.writing && oldWriting.version === nextData.writing.version &&
        oldWriting.artifact_sha256 === nextData.writing.artifact_sha256;
      const scrolls = sameWriting ? ['.creative-board-table-scroll', '.creative-board-result-scroll'].map(selector => {
        const node = world.querySelector(selector); return {selector, top: node?.scrollTop || 0, left: node?.scrollLeft || 0};
      }) : [];
      stopDrag();
      data = nextData; renderKey = nextKey; runId = data.runId;
      focus.textContent = data.analysisOnly ? '看报告' : data.video ? '看分镜' : '看方案';
      overview.hidden = !data.photo || data.analysisOnly;
      if (changedRun) { root.clearTimeout(layoutTimer); layoutRevision = null; layoutSequence = 0; layoutExtras = {groups:[],notes:[]};
        view = {zoom: 1, x: 32, y: 32}; autoFit = false;
        layoutNote.textContent = '布局仅保存在本机'; initialViewPending = !restoreLayout(); }
      const active = doc.activeElement, focusKey = host.contains(active) ? active?.getAttribute?.('data-board-action') : null;
      bounds = {width: data.analysisOnly ? 1020 : 1460, height: 680};
      world.style.width = `${bounds.width}px`; world.style.height = `${bounds.height}px`; world.replaceChildren();
      linkNodes(330, 420); if (!data.analysisOnly) linkNodes(1020, 1110);
      needNode(); if (data.analysisOnly) analysisNode(); else { planNode(); resultNode(); }
      renderImages();
      for (const saved of scrolls) { const node = world.querySelector(saved.selector);
        if (node) { node.scrollTop = saved.top; node.scrollLeft = saved.left; } }
      updateBounds();
      if (!initializeReadingView()) apply();
      if (changedRun) void restoreProjectLayout();
      if (focusKey && !changedRun) { const button = [...host.querySelectorAll('[data-board-action]')].find(item => item.getAttribute('data-board-action') === focusKey);
        // Restore the focused control after new saved content without treating
        // that refresh as a new keyboard navigation request to move the view.
        restoringFocus = true;
        try { button?.focus({preventScroll: true}); } finally { restoringFocus = false; } }
      return api;
    }
    function destroy() { if (destroyed) return; destroyed = true; root.clearTimeout(layoutTimer); closePhotoCollection(false); stopDrag(); resize?.disconnect();
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
