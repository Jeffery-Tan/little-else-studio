/* The stage panel renders current creative results in the central canvas.
 * Decisions and answers are projected into the conversation; readOnly mode
 * suppresses the former in-canvas gate, question and review controls.
 *
 * WHAT IT DOES NOT KNOW. It does not decide whether a gate is open — the server
 * does (`timeline._pending_action`), and the gate is drawn only when the pending
 * item names this stage. It does not decide whether a stage has a document, or
 * what to say when it does not: `blank.text` arrives already worded, as one of
 * four situations. It does not decide which questions belong to a stage; they
 * arrive tagged and are filtered here so the rule has exactly one home. It never
 * sends anything — the buttons carry `data-act`, and `app.js` is what turns a
 * click into a request, exactly as `blocks.js` leaves `data-media` for `app.js`
 * to resolve.
 *
 * SECURITY — same rule as md.js and blocks.js, for the same reason: model output
 * lands in innerHTML. Every piece of text is escaped before a tag is introduced.
 * That includes attribute values: `data-note` carries a sentence the server
 * composed from the director's own option key, so it is escaped like any other
 * text. Nothing here emits an attribute the payload controls except through
 * `escapeHtml`.
 *
 * Returns a string rather than building nodes so it can be tested without a DOM
 * or a browser: see tests/js/test_stage.mjs.
 */
"use strict";

/* The identity of one answer.
 *
 * Lives here rather than in `app.js` because this file is what writes it into
 * the DOM (`data-draft-key`) and `app.js` is what reads it back out and what
 * keys `localStorage` by it. A second copy of this formula is a bug that only
 * shows up as answers silently attaching to the wrong question — or, after a
 * stage is redone, to a question that no longer exists.
 *
 * The version is in the key on purpose. A reworked stage produces new questions,
 * and an answer written against the old ones must not ride onto the new version.
 */
function answerKey(q) {
  return `${q.stage}:v${q.version}:${q.question}`;
}

function stageHtml(step, ctx) {
  if (!step) return "";
  ctx = ctx || {};
  const questions = (ctx.questions || []).filter((q) => q && q.stage === step.stage);
  const drafts = ctx.drafts || {};
  // The gate is the server's to open. A pending item naming a different stage is
  // not this stage's business, and drawing an empty gate strip here would put a
  // box with no question in it above a document nobody is being asked about.
  const pending = !ctx.readOnly && ctx.pending && ctx.pending.stage === step.stage &&
    ctx.pending.version === step.version &&
    (!step.artifact_sha256 || !ctx.pending.artifact_sha256 ||
      ctx.pending.artifact_sha256 === step.artifact_sha256) ? ctx.pending : null;
  const direction = step.stage === 'direction' && step.has_doc ? directionWorkHtml(step, pending, ctx) : null;
  const writing = step.stage === 'writing' && step.has_doc ? writingWorkHtml(step, {...ctx, pending}) : null;
  const inlineDirection = Boolean(direction?.inlineGate);

  return (
    `<div class="pane${pending ? " has-gate" : ""}" data-stage="${escapeHtml(step.stage)}">` +
    (pending && !inlineDirection ? `<a class="decision-jump" href="#stage-decision">查看审核意见与决定 ↓</a>` : "") +
    titleHtml(step) +
    warnHtml(step) +
    (ctx.readOnly ? '' : headHtml(step)) +
    (direction?.html || writing || docHtml(step, ctx)) +
    (step.decision_sentence ? `<div class="pane-decision">${escapeHtml(step.decision_sentence)}</div>` : "") +
    (!ctx.readOnly && step.review ? reviewHtml(step.review) : "") +
    (pending && !inlineDirection ? gateHtml(pending) : "") +
    (ctx.readOnly ? '' : askHtml(step, questions, drafts)) +
    (ctx.readOnly ? '' : teamIssuesHtml(step.team_issues || [])) +
    `</div>`
  );
}

function fieldValue(card, labels) {
  return (card.fields || []).find(field => labels.includes(field.label))?.value || '';
}

function shortRecommendation(text) {
  const value = String(text || '').trim();
  if (value.length <= 170) return value;
  const sentence = value.match(/^.{24,170}?[。！？.!?](?=\s|$|[^。！？.!?])/u)?.[0];
  return sentence || `${value.slice(0, 170).trimEnd()}…`;
}

function directionWorkHtml(step, pending, ctx) {
  const blocks = step.blocks || [];
  const cards = blocks.find(block => block.type === 'cards' && block.cards?.length)?.cards || [];
  if (!cards.length) return {html: legacyWorkHtml(step, '创意方向', '这份历史方向按原文保存；没有可核对的选项卡。', ctx), inlineGate:false};
  const keyOf = card => String(card.head || '').trim().replace(/^方向\s+/, '');
  const options = pending?.options || [];
  const matching = options.map(option => cards.find(card => keyOf(card) === option.key));
  const inlineGate = Boolean(pending && options.length && options.length === cards.length &&
    matching.every(Boolean) && new Set(matching).size === cards.length);
  const selected = step.approval_decision?.startsWith('approved') ? step.selected_option_id : null;
  const selectedLabel = step.stale || step.integrity_ok === false ? '历史采用' : '已采用';
  const recommended = new Set([
    ...options.filter(option => option.recommended).map(option => option.key),
    ...cards.filter(card => card.emphasis === 'chosen').map(card => keyOf(card)),
  ]);
  const recommendation = blocks.find(block => block.type === 'callout' && block.title === '推荐');
  const other = blocks.filter(block => block.type !== 'cards' && block !== recommendation);
  const ordered = inlineGate ? matching : cards;
  const optionCard = card => {
    const key = keyOf(card), titleField = fieldValue(card, ['标题', '名称', '创意名称']);
    const claim = fieldValue(card, ['主张', '创意主张']);
    const title = titleField || claim || `方向 ${key}`;
    const story = fieldValue(card, ['推进概念', '画面与表达', '故事概述']);
    const opening = fieldValue(card, ['开场识别点', '开场', '前 3 秒', '开场句']);
    // Older direction artifacts sometimes contain timed shots and full lines.
    // Keep their source available, but let the choice screen compare concepts.
    const hasDraftDetail = story.length > 280 || opening.length > 140 ||
      /(?:\d+\s*[-–—]\s*\d+\s*(?:s|秒)|\d{1,2}:\d{2}|台词[：:]|字幕[：:]|说话人[：:])/iu.test(`${story} ${opening}`);
    const selectedHere = Boolean(selected && selected === key);
    // The same per-option verdict the gate enforces, read off the option the
    // server sent. This branch used to offer "采用这个方向" for every card, so a
    // refused option still showed a button the server would reject, and the
    // amendment it proposed had no path from the card that proposed it (T3).
    const admission = inlineGate
      ? options.find(option => option.key === key) || {} : {};
    const blocked = admission.admissible === false;
    const used = new Set(['标题', '名称', '创意名称', '主张', '创意主张', '推进概念', '画面与表达', '故事概述', '开场识别点', '开场', '前 3 秒', '开场句']);
    const background = new Set(['依据', '证据强度', '素材依赖', '素材需求', '执行难度']);
    const choiceRisk = new Set(['方案取舍', '取舍', '风险', '可能失败', '限制']);
    const visible = (card.fields || []).filter(field => !used.has(field.label) &&
      (choiceRisk.has(field.label) || (!field.secondary && !background.has(field.label))));
    const details = (card.fields || []).filter(field => !used.has(field.label) && !visible.includes(field));
    return `<article class="creator-direction-card${selectedHere ? ' is-selected' : ''}${blocked ? ' is-blocked' : ''}" data-direction-card="${escapeHtml(key)}">` +
      (card.media ? `<div class="creator-direction-media" data-media="${escapeHtml(card.media)}"></div>` : '') +
      `<div class="creator-direction-body"><span class="creator-kicker">${selectedHere ? `${selectedLabel} · ` : ''}${recommended.has(key) ? '团队推荐 · ' : ''}方向 ${escapeHtml(key)}</span>` +
      `<h3>${escapeHtml(title)}</h3>` +
      (titleField && claim ? `<p class="creator-direction-claim">${escapeHtml(claim)}</p>` : '') +
      (!hasDraftDetail && story ? `<p class="creator-direction-story">${escapeHtml(story)}</p>` : '') +
      (!hasDraftDetail && opening ? `<div class="creator-opening"><span>开场识别点</span><p>${escapeHtml(opening)}</p></div>` : '') +
      (visible.length ? `<dl class="creator-direction-fields">${visible.map(field => `<dt>${escapeHtml(field.label)}</dt><dd>${escapeHtml(field.value)}</dd>`).join('')}</dl>` : '') +
      (hasDraftDetail ? `<details class="creator-direction-draft"><summary>查看原稿中的制作细节</summary>` +
        (opening ? `<div><strong>开场</strong><p>${escapeHtml(opening)}</p></div>` : '') +
        (story ? `<div><strong>画面与表达</strong><p>${escapeHtml(story)}</p></div>` : '') +
        `</details>` : '') +
      (details.length ? `<details class="creator-direction-details"><summary>研究依据与制作条件</summary><dl>${details.map(field => `<dt>${escapeHtml(field.label)}</dt><dd>${escapeHtml(field.value)}</dd>`).join('')}</dl></details>` : '') +
      (inlineGate && blocked
        ? `<div class="creator-direction-blocked"><strong>当前版不能采用</strong>` +
          (admission.blocked_reason ? `<p>${escapeHtml(admission.blocked_reason)}</p>` : '') + `</div>`
        : '') +
      (inlineGate ? `<button type="button" class="ghost creator-direction-choose" data-act="pick-option" data-option-key="${escapeHtml(key)}" data-admissible="${blocked ? '0' : '1'}">${blocked ? '以此构想为基础修改' : `选择方向 ${escapeHtml(key)}`}</button>` : '') +
      `</div></article>`;
  };
  const selectedCard = selected && !inlineGate ? ordered.find(card => keyOf(card) === selected) : null;
  const alternatives = selectedCard ? ordered.filter(card => card !== selectedCard) : [];
  const cardList = selectedCard ? `<div class="creator-direction-current">${optionCard(selectedCard)}</div>` +
    (alternatives.length ? `<details class="creator-direction-alternatives"><summary>比较其他 ${alternatives.length} 个方向</summary><div class="creator-direction-grid">${alternatives.map(optionCard).join('')}</div></details>` : '') :
    `<div class="creator-direction-grid">${ordered.map(optionCard).join('')}</div>`;
  const decision = selected ? `<p class="creator-direction-selected">${selectedLabel}方向 ${escapeHtml(selected)}。${alternatives.length ? '其他提案可展开比较。' : ''}</p>` :
    step.approval_decision === 'returned' ? '<p class="creator-direction-selected">这一版已退回，提案保留供回看。</p>' : '';
  const findings = pending?.findings?.length ? `<div class="gate-findings creator-review-warning"><strong>${pending.passed ? '采用前请留意' : '审核尚未通过，采用前请核对'}</strong>${pending.findings.map(findingHtml).join('')}</div>` : '';
  const recommendationText = recommendation?.text || '';
  const recommendationShort = shortRecommendation(recommendationText);
  const recommendationHtml = recommendationText ? `<div class="creator-direction-recommendation"><strong>${selectedCard ? '当时的团队建议' : '团队建议'}</strong><p>${escapeHtml(recommendationShort)}</p>` +
    (recommendation.items?.length || recommendationShort !== recommendationText ? `<details><summary>推荐依据与待核实条件</summary>` +
      (recommendationShort !== recommendationText ? `<p>${escapeHtml(recommendationText)}</p>` : '') +
      (recommendation.items?.length ? `<ul>${recommendation.items.map(item => `<li>${escapeHtml(item.text)}</li>`).join('')}</ul>` : '') + '</details>' : '') + '</div>' : '';
  const gateAttrs = inlineGate ? ` id="stage-decision" class="gate creator-direction-gate" role="group" aria-label="选择这一版创意方向" data-stage="direction" data-version="${escapeHtml(pending.version)}" data-sha="${escapeHtml(pending.artifact_sha256 || '')}" data-passed="${pending.passed ? '1' : '0'}" data-needs-choice="1"` + gateAmendAttrs(pending) : ' class="creator-direction-work"';
  // The inline card is the screen people actually use, so it has to carry the
  // same three things the fallback form does: the brief the decision is bound
  // to, the amendment an option proposed, and the brief's own conflicts (T3).
  const footer = inlineGate ? `<div class="creator-direction-footer">` + conflictsHtml(pending) +
    `<p class="gate-choice-summary" role="status">尚未选择方向；团队推荐不代表你已选择。</p>` +
    revisionNoteHtml('想保留哪个想法、具体哪里要改？不选方向也可以要求全部重做。') +
    amendBlockHtml(pending) + decisionActionsHtml(pending, true) +
    `<p class="gate-limit">仅确认这一版策划，不开始图片或视频生成。</p>` +
    `<details class="gate-next"><summary>后续阶段与历史用量</summary>${pending.next_stage_label ? `<div>下一步：${escapeHtml(pending.next_stage_label)} · ${escapeHtml(pending.next_owner)}</div>` : ''}<div class="gate-next-cost">${escapeHtml(pending.next_cost_sentence || '')}</div></details></div>` : '';
  const html = `<div class="pane-doc"><section${gateAttrs}><div class="creator-decision-intro"><span class="creator-kicker">${inlineGate ? '现在需要你决定' : '当前创意'}</span><h2>${inlineGate ? '选一个方向，继续写成方案' : selected ? `${selectedLabel}的方向` : '看看这些创意方向'}</h2></div>${decision}${selectedCard ? cardList + recommendationHtml : recommendationHtml + findings + cardList}${footer}${other.length ? `<details class="creator-direction-support"><summary>完整依据与其他内容</summary>${blocksHtml(other)}</details>` : ''}${sourceSummaryHtml(step)}</section></div>`;
  return {html, inlineGate};
}

function legacyWorkHtml(step, name, message, ctx) {
  return `<div class="pane-doc creator-legacy-work"><div class="creator-legacy-intro"><span class="creator-kicker">${escapeHtml(name)}</span><p>${escapeHtml(message)}</p></div>${artifactBodyHtml(step, ctx?.artifactBody)}${sourceSummaryHtml(step)}</div>`;
}

function sourceSummaryHtml(step) {
  return step.summary ? `<details class="creator-original-summary"><summary>查看原始摘要</summary>${summaryHtml(step.summary)}</details>` : '';
}

function writingWorkHtml(step, ctx) {
  const blocks = step.blocks || [];
  const cards = blocks.find(block => block.type === 'cards' && block.cards?.length)?.cards || [];
  if (!cards.length) return legacyWorkHtml(step, '脚本正文', '这份历史脚本按原文保存；可阅读原稿并对当前作品提出修改。', ctx);
  const isPhoto = ctx?.contentFormat === 'photo' ||
    (!ctx?.contentFormat && cards.every(card => /^(?:第\s*)?\d+\s*页|^页\s*\d+|^P\s*\d+/iu.test(String(card.head || '').trim())));
  const canSelect = !step.stale && step.integrity_ok !== false && /^[a-f0-9]{64}$/.test(step.artifact_sha256 || '');
  const caption = blocks.find(block => block.type === 'text' && block.title === '发布文案');
  const assumptions = blocks.find(block => block.type === 'list' && block.title === '这份脚本做了哪些假设');
  const other = blocks.filter(block => block !== caption && block !== assumptions && block.type !== 'cards');
  const scenePreview = value => {
    const plain = String(value || '').replace(/\s+/gu, ' ').trim();
    const chars = Array.from(plain);
    return chars.length > 48 ? `${chars.slice(0, 48).join('')}…` : plain;
  };
  const outline = cards.map((card, index) => {
    const title = String(card.head || '').trim() || (isPhoto ? `第 ${index + 1} 页` : `第 ${index + 1} 段`);
    const line = fieldValue(card, isPhoto ? ['图内文案', '图内文字', '文案', '台词', '台词或字幕'] : ['台词', '台词或字幕']);
    const picture = fieldValue(card, ['画面', '画面与动作']);
    const spoken = /^(?:[（(]?无台词[）)]?|不使用口播|无口播|无|—|-)$/u.test(line.trim()) ? '' : line;
    const primary = isPhoto ? picture || spoken : spoken || picture;
    const secondary = isPhoto ? (picture && spoken ? spoken : '') : (spoken && picture ? picture : '');
    return `<li><a href="#script-segment-${index + 1}"><span>${escapeHtml(title)}</span>` +
      `<span class="creator-script-overview-text"><strong>${escapeHtml(scenePreview(primary) || '内容待补充')}</strong>` +
      (secondary ? `<small>${isPhoto ? '文案' : '画面'}：${escapeHtml(scenePreview(secondary))}</small>` : '') + '</span></a></li>';
  }).join('');
  const pending = ctx?.pending;
  const reviewState = pending && pending.stage === step.stage && pending.version === step.version
    ? `<p class="creator-script-review-state">${pending.passed ? '审核已通过，仍待你决定' : '审核未通过，建议先看修改意见'}${pending.findings?.length ? ` · ${pending.findings.length} 点` : ''}</p>` : '';
  const overview = `<nav class="creator-script-overview" aria-label="${isPhoto ? '图文稿页码' : '脚本段落'}"><div class="creator-script-overview-head"><strong>${cards.length} ${isPhoto ? '页' : '段'}内容</strong><span>点一段直接阅读</span></div>${reviewState}<ol>${outline}</ol></nav>`;
  const segments = cards.map((card, index) => {
    const number = index + 1;
    const line = fieldValue(card, isPhoto ? ['图内文案', '图内文字', '文案', '台词', '台词或字幕'] : ['台词', '台词或字幕']);
    const picture = fieldValue(card, ['画面', '画面与动作']);
    const sound = fieldValue(card, ['声音']), duration = fieldValue(card, ['时长']);
    const time = String(card.head || '').trim();
    const explicitNoLine = !isPhoto && /^(?:[（(]?无台词[）)]?|不使用口播|无口播)$/u.test(line.trim());
    const hasLine = Boolean(line.trim()) && !explicitNoLine && !['无', '—', '-'].includes(line.trim());
    const target = isPhoto ? `第 ${number} 页` : `第 ${number} 段`;
    const fieldTarget = (name, field) => canSelect
      ? ` data-creator-target="${name}" data-creator-path="${escapeHtml(JSON.stringify(['segments',index,field]))}"` : '';
    const words = hasLine ? `<div class="creator-script-words"><strong>${isPhoto ? '图内文案' : '台词或字幕'}</strong><p class="creator-script-line vf-value"${fieldTarget(`${target}${isPhoto ? '图内文案' : '台词'}`,'line')}>${escapeHtml(line)}</p></div>` :
      explicitNoLine ? `<p class="creator-script-no-line">原稿注明：${escapeHtml(line)}</p>` : '';
    const visual = picture || card.media ? `<div class="creator-script-visual">` +
      (card.media ? `<div class="creator-script-media" data-media="${escapeHtml(card.media)}"></div>` : '') +
      (picture ? `<div class="creator-script-picture-wrap"><strong>${isPhoto ? '画面' : '画面与动作'}</strong><p class="creator-script-picture vf-value"${fieldTarget(`${target}画面`,'picture')}>${escapeHtml(picture)}</p></div>` : '') + '</div>' : '';
    const audio = sound ? `<div class="creator-script-sound"><strong>声音</strong><p${fieldTarget(`${target}声音`,'sound')}>${escapeHtml(sound)}</p></div>` : '';
    const content = isPhoto || !hasLine ? visual + words : words + visual;
    const heading = isPhoto ? (time || `第 ${number} 页`) : `第 ${number} 段${time ? ` · ${time}` : ''}`;
    return `<article id="script-segment-${number}" class="creator-script-segment${isPhoto ? ' is-photo' : hasLine ? ' has-words' : ' is-visual'}"><header><span>${escapeHtml(heading)}</span>${duration && !isPhoto ? `<small>${escapeHtml(duration)}</small>` : ''}</header>` +
      `<div class="creator-script-content">${content}${audio}${!content && !audio ? `<p class="creator-script-unwritten">这一${isPhoto ? '页' : '段'}尚未写出可读内容。</p>` : ''}</div></article>`;
  }).join('');
  return `<div class="pane-doc creator-script-work"><div class="creator-script-intro"><span class="creator-kicker">${isPhoto ? '当前图文稿' : '当前脚本'}</span><h2>${isPhoto ? '逐页看画面与图内文案' : '逐段读画面与表达'}</h2><p>${canSelect ? '点选画面或文字，可在对话中讨论，也可按意见直接修改该字段。' : '先阅读这份作品；来源或版本需要核对后才能针对具体内容修改。'}</p></div>${overview}<div class="creator-script-lines">${segments}</div>` +
    (caption?.text ? `<section class="creator-script-caption"><h3>发布文案</h3><p class="blk-text"${canSelect ? ` data-creator-target="发布文案" data-creator-path="${escapeHtml(JSON.stringify(['caption']))}"` : ''}>${escapeHtml(caption.text)}</p></section>` : '') +
    (assumptions?.items?.length ? `<section class="creator-script-assumptions"><h3>这份脚本仍需核对</h3><ul>${assumptions.items.map(item => `<li>${escapeHtml(item.text)}</li>`).join('')}</ul></section>` : '') +
    (other.length ? `<details class="creator-script-support"><summary>查看分镜顺序与其他依据</summary>${blocksHtml(other)}</details>` : '') + sourceSummaryHtml(step) + '</div>';
}

function teamIssuesHtml(issues) {
  if(!issues.length) return '';
  return `<details class="team-issues"><summary>团队待整理与后续事项 · ${issues.length} 项</summary>` +
    '<p class="muted">这些事项尚未解决，由制片团队梳理；不要求你逐项作答。</p>' +
    issues.map(q=>`<p>${escapeHtml(q.question)}${q.blocks ? `<small>${escapeHtml(q.blocks)}</small>` : ''}</p>`).join('') + '</details>';
}

/* --------------------------------------------------------------- the gate -- */

/* Decision copy lives with the sole version-bound stage gate. Legacy journey
 * labels remain for old renderer tests; the conversation now only navigates
 * to this card. Note and override requirements live in wireGate and the server.
 */
function gateApproveLabel(pending, surface) {
  const journey = surface === "journey";
  if (!pending.passed) {
    return journey ? "填写理由，仍按此方案继续" : "明确覆盖审核并通过";
  }
  if (journey) {
    return pending.stage === "direction" ? "按此方向完善方案" : "确认方案，准备分镜";
  }
  return pending.stage === "direction" ? "采用并继续完善方案" : "确认并准备分镜";
}

function gateRejectLabel(surface) {
  return surface === "journey" ? "按补充意见调整" : "退回并继续修改";
}

/* Read the work before deciding. A top link reaches this exact-version card;
 * findings and historical usage remain available without overtaking the work.
 *
 * ONE SHAPE FOR EVERY GATE. Both gate kinds carry the same two nodes —
 * `.reason` (what he wrote) and `.gate-feedback` (what just happened) — plus
 * `.gate-actions`. That is not cosmetic: `wireGate` binds those two by selector
 * before it binds any button, and the brief-amendment card used to ship without
 * them, so the card threw on open and its buttons were never wired. The note box
 * is genuinely needed on both: approving past a failing review needs a reason,
 * and returning always does.
 *
 * The data contract is declared, not inferred — `data-gate-act` on the action
 * row, `data-passed` for whether a note is mandatory. A test can assert the
 * markup and the binder agree without a browser.
 */
function gateHtml(pending, step) {
  const amend = pending.kind === "brief_amendment";
  return amend ? briefAmendmentGateHtml(pending) : stageDecisionGateHtml(pending, step);
}

/* The note box and the action row. Every gate type carries both — see the
 * contract note above `gateHtml`. `acts` is written into `data-gate-act` so the
 * markup itself says which actions this card offers, and the binder's job is to
 * match it rather than to guess.
 */
function gateReasonHtml(placeholder) {
  return `<label class="gate-note-label">补充意见` +
    `<textarea class="reason" rows="3" maxlength="8000" aria-label="决定意见" ` +
    `placeholder="${escapeHtml(placeholder)}"></textarea></label>`;
}

function revisionNoteHtml(placeholder) {
  return `<label class="gate-note-label">希望怎么修改` +
    `<textarea class="reason" rows="3" maxlength="8000" aria-label="修改意见" ` +
    `placeholder="${escapeHtml(placeholder)}"></textarea></label>`;
}

function decisionActionsHtml(pending, direction = false) {
  const revise = `<button type="button" class="good small" data-act="reject">按意见修改，先出新版</button>`;
  const adopt = pending.passed
    ? `<button type="button" class="ghost small" data-act="approve">${direction ? '按所选方向继续' : pending.stage === 'direction' ? '确认当前方向并继续' : '确认当前脚本并继续'}</button>`
    : `<details class="gate-override"><summary>仍要采用未通过审核的当前版？</summary>` +
      `<p>这会跳过当前审核意见并继续。请单独说明原因。</p>` +
      `<label class="gate-note-label">覆盖审核的理由<textarea class="override-reason" rows="3" maxlength="8000" aria-label="覆盖审核的理由"></textarea></label>` +
      `<button type="button" class="bad small" data-act="approve">明确覆盖审核并采用当前版</button></details>`;
  return gateActionsHtml(['approve', 'reject'], '选择和填写意见都不会立即提交。',
    `<div class="gate-decision-buttons">${revise}${adopt}</div>`);
}

function gateActionsHtml(acts, hint, buttons) {
  return `<div class="gate-actions" data-gate-act="${acts.map(a => escapeHtml(a)).join(" ")}">` +
    buttons +
    `<p class="gate-feedback" role="status">${escapeHtml(hint)}</p></div>`;
}

/* The blocked-alignment card.
 *
 * It only offers a change it can actually make. A brief amendment is a new
 * clause body the author wrote down (`amend_proposal`); when there is none, the
 * blocker text is a *diagnosis* ("未声明满足 goal.primary"), and pasting that in
 * as the replacement body would have rewritten the product goal with an error
 * message. So with no real proposal the card shows the diagnosis, offers the
 * return-with-reason, and says the team has to propose the change first — the
 * decision stays possible, it just isn't a fabricated one.
 */
function briefAmendmentGateHtml(pending) {
  const amend = pending.amend || {};
  const diff = amend.valid !== false && amend.text
    ? `<div class="gate-amend-diff">` +
        `<div class="gate-amend-row"><span class="gate-amend-what">现在是</span>` +
        `<span class="gate-amend-was">${escapeHtml(amend.current_text || "（这一版简报没有这条内容）")}</span></div>` +
        `<div class="gate-amend-row"><span class="gate-amend-what">改为</span>` +
        `<span class="gate-amend-now">${escapeHtml(amend.text)}</span></div>` +
      `</div>`
    : "";
  const missing = amend.valid === false
    ? `<p class="gate-amend-missing">${escapeHtml(amend.reason || "这一版没有提出可批准的简报修订正文。")}` +
      `需要改目标时，请退回让团队先写出要改成什么。</p>`
    : "";
  return `<section id="stage-decision" class="gate gate-brief-amendment" role="group" aria-label="简报修订" ` +
    `data-stage="${escapeHtml(pending.stage)}" data-version="${escapeHtml(pending.version)}" ` +
    `data-sha="${escapeHtml(pending.artifact_sha256 || '')}" data-passed="${pending.passed ? '1' : '0'}" ` +
    `data-pending-kind="brief_amendment"` + gateAmendAttrs(pending) + `>` +
    `<div class="gate-head">下一步 · ${escapeHtml(pending.stage_label)}</div>` +
    `<p class="gate-headline">${escapeHtml(pending.headline)}</p>` +
    `<p>${escapeHtml(pending.body || "")}</p>` + conflictsHtml(pending) + findingsHtml(pending) + blockersHtml(pending) +
    diff + missing +
    revisionNoteHtml(amend.valid === false
      ? "请写明要在原简报下改什么，再退回团队"
      : "这条简报改法还需调整？写明意见并退回团队出新版；批准只采用上方原文。") +
    gateActionsHtml(amend.valid === false ? ["reject"] : ["amend_brief", "reject"],
      amend.valid === false ? "这一版不能批准修订；可以退回重做。" : "有修改意见请退回；批准只采用上方修订正文，并立即重做。",
      (amend.valid === false ? "" :
        `<button type="button" class="good small" data-act="amend_brief">` +
        `批准修订简报（本版作废，团队重做）</button>`) +
      `<button type="button" class="bad small" data-act="reject" data-stage="${escapeHtml(pending.stage)}">` +
      `${escapeHtml(gateRejectLabel("stage"))}</button>`) +
    `<p class="gate-limit">批准修订会改产品目标/简报版本，不会直接通过当前方向。</p>` +
    (pending.next_cost_sentence ? `<div class="gate-next-cost">${escapeHtml(pending.next_cost_sentence)}</div>` : "") +
    `</section>`;
}

function stageDecisionGateHtml(pending, step) {
  const options = pending.options || [];
  const cards = (step?.blocks || []).find(block => block.type === 'cards')?.cards || [];
  const cardFor = key => cards.find(card => String(card.head || '').trim().replace(/^方向\s+/, '') === key);
  // An option the gate refuses is drawn but not selectable, with the reason next
  // to it. Leaving it selectable would advertise a click the server answers with
  // a refusal; hiding it would hide that the team proposed it at all (R3).
  const choices = options.length ? `<fieldset class="gate-options"><legend>想以哪个方向继续</legend>` +
    options.map(o => {
      const blocked = o.admissible === false;
      const card = cardFor(o.key);
      const title = fieldValue(card || {}, ['标题', '名称', '创意名称']) ||
        fieldValue(card || {}, ['主张', '创意主张']);
      const claim = fieldValue(card || {}, ['主张', '创意主张']);
      return `<label class="gate-choice${blocked ? " is-blocked" : ""}">` +
        `<input type="radio" name="direction-choice" value="${escapeHtml(o.key)}"` +
        (blocked ? ` disabled data-blocked="1"` : "") + `>` +
        `<span><strong>方向 ${escapeHtml(o.key)}${title ? ` · ${escapeHtml(title)}` : ''}</strong>` +
        (o.recommended ? '<small>团队推荐</small>' : '') +
        (claim && claim !== title ? `<small>${escapeHtml(shortRecommendation(claim))}</small>` : '') +
        (blocked ? '<small class="gate-choice-blocked">当前稿需先修改</small>' : '') + `</span>` +
        (blocked && o.blocked_reason
          ? `<small class="gate-choice-why">${escapeHtml(o.blocked_reason)}</small>` : '') +
        `</label>`;
    }).join("") + `</fieldset>` : "";
  return `<section id="stage-decision" class="gate" role="group" aria-label="等你决定的这一版" ` +
    `data-stage="${escapeHtml(pending.stage)}" data-version="${escapeHtml(pending.version)}" ` +
    `data-sha="${escapeHtml(pending.artifact_sha256 || '')}" data-passed="${pending.passed ? '1' : '0'}"` +
    gateAmendAttrs(pending) + `>` +
    `<div class="gate-head">下一步 · ${escapeHtml(pending.stage_label)}</div>` +
    `<p class="gate-headline">${escapeHtml(pending.headline)}</p>` +
    conflictsHtml(pending) + findingsHtml(pending) + choices +
    (options.length ? `<p class="gate-choice-summary" role="status">尚未选择方向；团队推荐不代表你已选择。</p>` : '') +
    amendBlockHtml(pending) +
    revisionNoteHtml(options.length ? "想保留哪个想法、具体哪里要改？不选方向也可以要求全部重做。" : "当前脚本需要改哪里？") +
    decisionActionsHtml(pending, Boolean(options.length)) +
    `<details class="gate-next"><summary>后续阶段与历史用量</summary>` +
    (pending.next_stage_label ? `<div>下一步：${escapeHtml(pending.next_stage_label)} · ${escapeHtml(pending.next_owner)}</div>` : '') +
    `<div class="gate-next-cost">${escapeHtml(pending.next_cost_sentence || '')}</div></details>` +
    `<p class="gate-limit">仅确认这一版策划，不开始图片或视频生成。</p></section>`;
}

/* The amendment an option proposed, offered from the same card.
 *
 * Same contract as the blocked-alignment card: only a real proposal produces a
 * button, and a clause id is never sent as the new body. What differs is the
 * company it keeps — here the compliant options are still selectable, so the
 * user chooses between "adopt this one" and "change the brief first" instead of
 * being told the whole version is unusable (R3).
 */
function amendBlockHtml(pending) {
  const amend = pending.amend || {};
  if (!amend.key && !amend.clause_id) return "";
  const valid = amend.valid !== false && Boolean(amend.text);
  const diff = valid
    ? `<div class="gate-amend-diff">` +
        `<div class="gate-amend-row"><span class="gate-amend-what">现在是</span>` +
        `<span class="gate-amend-was">${escapeHtml(amend.current_text || "（这一版简报没有这条内容）")}</span></div>` +
        `<div class="gate-amend-row"><span class="gate-amend-what">改为</span>` +
        `<span class="gate-amend-now">${escapeHtml(amend.text)}</span></div>` +
      `</div>`
    : `<p class="gate-amend-missing">${escapeHtml(amend.reason || `方案 ${amend.key || ""} 要求先修订简报，但没有写出修订正文。`)}` +
      `要改目标的话，请退回让团队写出要改成什么。</p>`;
  return `<div class="gate-amend-inline"><div class="gate-amend-head">` +
    `方向 ${escapeHtml(amend.key || "?")} 提出：先修订有效简报</div>` + diff +
    (valid ? `<button type="button" class="good small" data-act="amend_brief">` +
      `批准修订简报（本版作废，团队重做）</button>` : "") +
    `</div>`;
}

/* The brief's own unresolved amendments. Not about this document, which is why
 * it is worded as a block on the brief rather than on a direction. */
function conflictsHtml(pending) {
  const rows = pending.conflicts || [];
  if (!rows.length) return "";
  return `<div class="gate-conflicts" role="alert">` +
    `<strong>目标里有一条你以前批准过的改动，和当前稿对不上了</strong>` +
    `<ul>${rows.map(row => `<li>${escapeHtml(String(row.detail || ""))}</li>`).join("")}</ul>` +
    `<p>在处理它之前，方向都先不能采用。</p></div>`;
}

/* The amendment attributes the binder reads. Declaration only: when there is no
 * clause or no body the card says so with `data-amend-valid="0"`, and `wireGate`
 * refuses to send a body it does not have. */
function gateAmendAttrs(pending) {
  const amend = pending.amend || {};
  const briefFp = pending.brief_fingerprint
    ? ` data-brief-fp="${escapeHtml(pending.brief_fingerprint)}"` : "";
  if (!amend.key && !amend.clause_id) return briefFp;
  return briefFp +
    ` data-amend-valid="${amend.valid === false || !amend.text ? "0" : "1"}"` +
    ` data-amend-clause="${escapeHtml(amend.clause_id || '')}"` +
    ` data-amend-text="${escapeHtml(amend.text || '')}"`;
}

function findingsHtml(pending) {
  return pending.findings?.length ? `<details class="gate-findings"${pending.passed ? '' : ' open'}><summary>审核提的 ${pending.findings.length} 点</summary>` +
    pending.findings.map(findingHtml).join("") + `</details>` : "";
}

function blockersHtml(pending) {
  // Human labels for machine kinds: `unmapped` is a state the system knows,
  // not a word for the person deciding (write like you speak — no jargon).
  const labels = {
    unmapped: '还没说明和目标的关系',
    goal_uncovered: '没有对上作品目标',
    unknown_clause: '引用的要求已不在目标里',
    needs_amendment: '要先修改目标',
    violates: '和目标冲突',
  };
  return pending.blockers?.length
    ? `<ul class="gate-brief-blockers">${pending.blockers.map(b =>
        `<li data-kind="${escapeHtml(String(b.kind || ""))}">方向 ${escapeHtml(String(b.key || ""))} · ${escapeHtml(labels[b.kind] || "需要处理")}：${escapeHtml(String(b.detail || b.clause_id || ""))}</li>`
      ).join("")}</ul>`
    : "";
}

function findingHtml(f) {
  const labels = {content:"内容", feasibility:"可执行性", evidence:"证据", unverified:"尚未验证",
    unsupported:"依据不足", gap:"表达缺口"};
  const kind = labels[f.kind] || (/\p{Script=Han}/u.test(String(f.kind || '')) ? f.kind : '审核意见');
  return (
    `<div class="finding">` +
    (kind ? `<span class="finding-kind">${escapeHtml(kind)}</span>` : "") +
    `<span class="finding-text">${escapeHtml(f.issue)}</span>` +
    `</div>`
  );
}

/* ------------------------------------------------------------- the warning -- */

/* The two ways a document can be on screen and not be usable.
 *
 * Stale: an upstream stage was invalidated, so this version was written against
 * something that no longer holds. Changed-on-disk: the file fails its own
 * fingerprint. Both used to be either unshown or shown only in the overlay
 * reader — and the overlay is no longer where he reads, so if this file does not
 * say it, nothing on the screen does.
 */
function warnHtml(step) {
  let out = "";
  if (step.stale_sentence) {
    out += `<div class="pane-warn stale">${escapeHtml(step.stale_sentence)}</div>`;
  }
  if (step.integrity_sentence) {
    out += `<div class="pane-warn integrity">${escapeHtml(step.integrity_sentence)}</div>`;
  }
  return out;
}

function titleHtml(step) {
  const explanation = {
    intake: "明确要打动谁，以及这次创作要解决什么。",
    direction: "比较创意主张，找到值得继续的方向。",
    material_plan: "看看已有的画面证据，还缺哪些素材。",
    writing: "阅读完整表达，检查画面、文案与节奏。",
  }[step.stage] || "阅读这一步的创作内容。";
  return `<header class="stage-heading"><h1>${escapeHtml(step.label || "创作项目")}</h1>` +
    `<p class="stage-description">${escapeHtml(explanation)}</p></header>`;
}

function headHtml(step) {
  if (!step.has_doc) return "";
  const date = String(step.created_at || "").match(/^(\d{4})-(\d{2})-(\d{2})(?:T|\s|$)/);
  const shortDate = date ? `${Number(date[2])}月${Number(date[3])}日` : "";
  const details = (step.author || step.created_at) ? `<details class="pane-version-details"><summary>版本详情</summary>` +
    (step.author ? `<div>创作者：${escapeHtml(step.author)}</div>` : "") +
    (step.created_at ? `<div>保存时间：${escapeHtml(step.created_at)}</div>` : "") + `</details>` : "";
  return (
    `<div class="pane-head">` +
    `<span class="pane-title">版本 ${escapeHtml(String(step.version))}</span>` +
    (shortDate ? `<span class="pane-by">${escapeHtml(shortDate)}</span>` : "") +
    details +
    `<button type="button" class="ghost small" data-act="read" ` +
    `data-stage="${escapeHtml(step.stage)}" data-version="${escapeHtml(String(step.version))}">` +
    `看原文</button>` +
    `</div>`
  );
}

/* ------------------------------------------------------------ the document -- */

function docHtml(step, ctx) {
  // Nothing written yet. The server already worked out which of four situations
  // this is — waiting, in progress, failed, came out empty — and worded it. This
  // file repeats none of that judgement.
  if (!step.has_doc) {
    const state = (step.blank && step.blank.state) || "todo";
    const text = (step.blank && step.blank.text) || "";
    return `<div class="pane-blank ${escapeHtml(state)}">${escapeHtml(text)}</div>`;
  }

  const body = step.blocks && step.blocks.length ? blocksHtml(step.blocks) : artifactBodyHtml(step, ctx.artifactBody);

  return (
    `<div class="pane-doc">` +
    summaryHtml(step.summary) +
    body +
    `</div>`
  );
}

function summaryHtml(summary) {
  if (!summary) return "";
  const characters = Array.from(String(summary));
  if (characters.length <= 180) return `<p class="pane-summary">${escapeHtml(summary)}</p>`;
  // This is a literal excerpt, never a new model-written summary.
  return `<details class="pane-summary summary-expand"><summary>${escapeHtml(characters.slice(0, 120).join(""))}… ` +
    `<span>展开完整摘要</span></summary><p class="pane-summary-full">${escapeHtml(summary)}</p></details>`;
}

function artifactBodyHtml(step, artifactBody) {
  // Fetch completion must identify the exact stage/version. A late response
  // from a previously selected document must never be dressed as this one.
  const matches = artifactBody && artifactBody.stage === step.stage && artifactBody.version === step.version;
  const fingerprintMatches = !step.artifact_sha256 || artifactBody?.sha256 === step.artifact_sha256;
  const state = !matches ? "loading" : artifactBody.status === "loaded" && !fingerprintMatches ? "error" : artifactBody.status;
  if (state === "loaded" && typeof artifactBody.text === "string" && artifactBody.text.trim()) {
    return `<article class="artifact-prose">${renderMarkdown(artifactBody.text)}</article>`;
  }
  const attrs = `data-stage="${escapeHtml(step.stage)}" data-version="${escapeHtml(String(step.version))}"`;
  if (state === "error" || state === "loaded") {
    const message = state === "error" ? "正文暂时没有加载成功。" : "这一版正文为空，请查看原文确认。";
    return `<div class="artifact-body-state" role="status"><p>${message}</p>` +
      `<button type="button" class="ghost small" data-act="retry-body" ${attrs}>重新加载正文</button> ` +
      `<button type="button" class="ghost small" data-act="read" ${attrs}>查看原文</button></div>`;
  }
  return `<div class="artifact-body-state" role="status" aria-busy="true"><p>正在加载这一版正文…</p>` +
    `<button type="button" class="ghost small" data-act="read" ${attrs}>先查看原文</button></div>`;
}

function reviewHtml(review) {
  const pass = review.verdict === "pass";
  const findings = review.findings || [];
  return (
    `<div class="pane-review ${pass ? "pass" : "return"}">` +
    `<span class="review-say">${escapeHtml(review.sentence)}</span>` +
    (findings.length
      ? `<details><summary>看这 ${findings.length} 条意见</summary>` +
        findings.map(findingHtml).join("") +
        `</details>`
      : "") +
    `</div>`
  );
}

/* -------------------------------------------------------------- the asking -- */

/* What this stage could not settle, answerable where it is written.
 *
 * Answers accumulate in the tray above the composer rather than firing one at a
 * time: several questions become one message, which is one agent turn instead of
 * four. The input carries `data-draft-key` so the tray can empty the right box
 * when a draft is undone.
 */
function askHtml(step, questions, drafts) {
  if (!questions.length) return "";
  let rows = "";
  for (const q of questions) {
    const key = answerKey(q);
    const value = (drafts[key] || {}).text || "";
    rows +=
      `<div class="q">` +
      `<div class="q-text">${escapeHtml(q.question)}</div>` +
      (q.reason ? `<div class="q-blocks">需要你确认的原因：${escapeHtml(q.reason)}</div>` : "") +
      (q.blocks ? `<div class="q-blocks">影响：${escapeHtml(q.blocks)}</div>` : "") +
      `<input class="q-answer" type="text" placeholder="在这里回答" ` +
      `data-draft-key="${escapeHtml(key)}" aria-label="回答：${escapeHtml(q.question)}" ` +
      `value="${escapeHtml(value)}">` +
      `</div>`;
  }
  return (
    `<div class="pane-ask">` +
    `<div class="ask-head">「${escapeHtml(step.label)}」还有 ${questions.length} 个问题等你回答` +
    `<span class="ask-note">答完一起发，主 Agent 一次看到全部</span></div>` +
    rows +
    `</div>`
  );
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { stageHtml, gateHtml, answerKey, gateApproveLabel, gateRejectLabel };
}
