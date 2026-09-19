/* The stage panel: one stage, and everything about it, on one screen.
 *
 * The page used to be one conversation, oldest at the top. Every artifact ever
 * written sat above the thing that was actually waiting for him, so after a full
 * run the decision card landed more than eight screens down — behind four
 * documents he had already read and approved. The unit of work in this app is a
 * *stage*, not a message, so the page is now built out of stages: pick one on
 * the rail, see that stage.
 *
 * This file draws one stage and nothing else:
 *
 *   title    a human stage name and its purpose
 *   head     which version, who wrote it, when
 *   warn     the two reasons the document below may not be trustworthy
 *   doc      the document, laid out; or, if there is none, why there is none
 *   review   what the reviewer said about this exact version
 *   gate     the exact version decision, after the work and its review
 *   ask      what this stage could not settle, answerable in place
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
  const pending = ctx.pending && ctx.pending.stage === step.stage ? ctx.pending : null;

  return (
    `<div class="pane${pending ? " has-gate" : ""}" data-stage="${escapeHtml(step.stage)}">` +
    (pending ? `<a class="decision-jump" href="#stage-decision">阅读后提交决定 ↓</a>` : "") +
    titleHtml(step) +
    warnHtml(step) +
    headHtml(step) +
    (step.decision_sentence ? `<div class="pane-decision">${escapeHtml(step.decision_sentence)}</div>` : "") +
    docHtml(step, ctx) +
    (step.review ? reviewHtml(step.review) : "") +
    (pending ? gateHtml(pending) : "") +
    askHtml(step, questions, drafts) +
    `</div>`
  );
}

/* --------------------------------------------------------------- the gate -- */

/* Read the work before deciding. A top link reaches this exact-version card;
 * findings and historical usage remain available without overtaking the work.
 */
function gateHtml(pending) {
  const options = pending.options || [];
  const choices = options.length ? `<fieldset class="gate-options"><legend>采用哪个方向</legend>` +
    options.map(o => `<label class="gate-choice"><input type="radio" name="direction-choice" value="${escapeHtml(o.key)}">` +
      `<span>方向 ${escapeHtml(o.key)}${o.recommended ? '<small>推荐</small>' : ''}</span></label>`).join("") + `</fieldset>` : "";
  const findings = pending.findings?.length ? `<details class="gate-findings"><summary>审核提的 ${pending.findings.length} 点</summary>` +
    pending.findings.map(findingHtml).join("") + `</details>` : "";
  return `<section id="stage-decision" class="gate" role="group" aria-label="等你决定的这一版" ` +
    `data-stage="${escapeHtml(pending.stage)}" data-version="${escapeHtml(pending.version)}" ` +
    `data-sha="${escapeHtml(pending.artifact_sha256 || '')}" data-passed="${pending.passed ? '1' : '0'}">` +
    `<div class="gate-head">下一步 · ${escapeHtml(pending.stage_label)}</div>` +
    `<p class="gate-headline">${escapeHtml(pending.headline)}</p>` + findings + choices +
    `<label class="gate-note-label">补充意见<textarea class="reason" rows="3" maxlength="8000" aria-label="决定意见" placeholder="想保留什么，哪里需要调整？"></textarea></label>` +
    `<p class="gate-feedback" role="status">选择不会立即提交</p>` +
    `<div class="gate-actions"><button type="button" class="good small" data-act="approve" data-stage="${escapeHtml(pending.stage)}">` +
    `${pending.passed ? '提交采用决定' : '明确覆盖审核并通过'}</button>` +
    `<button type="button" class="bad small" data-act="reject" data-stage="${escapeHtml(pending.stage)}">退回修改</button></div>` +
    `<details class="gate-next"><summary>后续阶段与历史用量</summary>` +
    (pending.next_stage_label ? `<div>下一步：${escapeHtml(pending.next_stage_label)} · ${escapeHtml(pending.next_owner)}</div>` : '') +
    `<div class="gate-next-cost">${escapeHtml(pending.next_cost_sentence || '')}</div></details>` +
    `<p class="gate-limit">仅确认这一版策划，不开始图片或视频生成。</p></section>`;
}

function findingHtml(f) {
  const kind = {content:"内容", feasibility:"可执行性", evidence:"证据", unverified:"尚未验证"}[f.kind] || f.kind;
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
      (q.blocks ? `<div class="q-blocks">卡住了：${escapeHtml(q.blocks)}</div>` : "") +
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
  module.exports = { stageHtml, answerKey };
}
