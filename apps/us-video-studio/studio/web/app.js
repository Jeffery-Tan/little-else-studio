/* US video studio — the stages, and the two things only a person can do.
 *
 * The screen is four stages. Pick one on the rail and you get that stage and
 * nothing else: what is waiting on you, the document, what the reviewer said,
 * and the questions it left open — answerable where they are written.
 *
 * It used to be one conversation, oldest first, with every artifact ever written
 * stacked above whatever was actually waiting. After a full run the thing he had
 * to click on sat more than eight screens down, behind four documents he had
 * already read. The unit of work here is a stage, not a message, so the page is
 * built out of stages and the conversation is demoted to a band he can open.
 *
 * The band is not decoration and not a leftover: reading one document closely
 * and following what happened over time are two different needs, and every tool
 * measured keeps both views side by side. What changed is which one owns the
 * screen.
 *
 * The UI has exactly two write paths that change the run: sending a message and
 * deciding a gate. It cannot dispatch work, cannot submit a stage, cannot write
 * a review. The approval nonce is fetched for the displayed version and retained with its local retry draft.
 * The third write path — dropping a file on the materials shelf — puts a file in
 * a folder without creating stage artifacts; changed bytes invalidate dependent work.
 *
 * No framework and no build step: this runs from a folder on one machine.
 * The wording ("人话") is NOT here — it comes from the server's vocabulary, so
 * the live stream and a reloaded page say the same thing. Icons are, because a
 * shape is not a sentence. The stage panel's markup is not here either; it is
 * `stage.js`, which returns a string and is therefore testable without a browser.
 */
"use strict";

/* Icons as inline SVG, replacing the ✓ ● ○ the server used to send as text.
 *
 * Hand-written rather than imported: this app has no build step and no
 * dependencies, and the first icon is the expensive one. They are shapes, not
 * words, so they may live here — `vocabulary.py` owns what the screen *says*,
 * and none of these say anything. `currentColor` and a 1.5px stroke so they
 * inherit the size and colour of whatever they sit in.
 */
const ICON = {
  done: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3.5 8.5 l3 3 l6 -7"/></svg>',
  active: '<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="3.2" fill="currentColor" stroke="none"/></svg>',
  stale: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 2.8 l5.4 9.4 H2.6 Z"/><path d="M8 6.6 v2.6"/><path d="M8 10.9 v0.1"/></svg>',
  todo: '<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="3.2" opacity="0.55"/></svg>',
};

const $ = (id) => document.getElementById(id);
const box = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
};

const state = {
  view: 'home',
  runs: [],
  artifactBodies: new Map(),
  chatDrafts: new Map(),
  runId: null,
  timeline: null,
  rail: [],
  // Which stage owns the screen. Everything the page shows is drawn from this
  // one field, so it is also the thing the rail's buttons change.
  activeStage: null,
  // The run's shelf, as the server last reported it. Held here rather than
  // looked up per picture because the same list answers two questions — what is
  // on the shelf, and whether a file an artifact named is really there.
  materials: [],
  busy: false,
  decisionBusy: false,
  refreshSerial: 0,
  machineDetail: false,
  materialsOpen: false,
  // The conversation band. Collapsed by default, because the point of this
  // screen is the stage; forced open while a turn runs, because work in flight
  // is not something he should be able to hide from himself.
  conversationOpen: true,
  atBottom: true,
  // Answers he has written on the question rows but not sent yet, keyed by the
  // question they answer. Local to the browser and to one artifact version —
  // see `answerKey` in stage.js. Nothing here has reached the server.
  draft: {},
  // draft key -> the input on screen showing it, so undoing a draft can empty
  // the box without rebuilding the list he may be typing in. Only ever holds
  // inputs that are on screen, which is now one stage's worth.
  draftInputs: new Map(),
};

/* ------------------------------------------------------------------ api -- */

async function api(path, options) {
  const res = await fetch(path, { headers: { "content-type": "application/json" }, ...options });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail || detail;
    } catch (_) { /* non-JSON body */ }
    const error = new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    error.status = res.status;
    throw error;
  }
  return res.json();
}

/* --------------------------------------------------------------- blocks -- */

/** The laid-out document, from `blocks.js` — a string, like `renderMarkdown`.
 *
 * Kept out of this file for the same reason: the block text is model output
 * landing in innerHTML, so the escaping has to live in one tested place rather
 * than in an ad-hoc DOM builder. A stage a newer server sends that this file
 * knows nothing about renders as a missing section, never as an error.
 */
function blocksElement(blocks) {
  const wrap = box("div");
  wrap.innerHTML = blocksHtml(blocks);
  return wrap.firstChild || wrap;
}

function toast(message, isError) {
  const node = $("toast");
  node.textContent = message;
  node.className = "toast show" + (isError ? " error" : "");
  clearTimeout(node._t);
  node._t = setTimeout(() => { node.className = "toast"; }, 4200);
}

/* ----------------------------------------------------------- projects -- */

async function loadRuns() {
  const runs = await api("/api/runs");
  state.runs = runs;
  const select = $("run-select");
  select.innerHTML = "";
  if (!runs.length) {
    const opt = document.createElement("option");
    opt.textContent = "还没有项目";
    opt.value = "";
    select.appendChild(opt);
    state.runId = null;
    renderProjects();
    return;
  }
  for (const run of runs) {
    const opt = document.createElement("option");
    opt.value = run.run_id;
    opt.textContent = run.title;
    select.appendChild(opt);
  }
  const remembered = sessionStorage.getItem("studio-selected-run");
  if (!state.runId && runs.some((r) => r.run_id === remembered)) state.runId = remembered;
  if (!state.runId || !runs.some((r) => r.run_id === state.runId)) state.runId = runs[0].run_id;
  select.value = state.runId;
  renderProjects();
}

async function selectRun(runId) {
  if (state.decisionBusy) { $('run-select').value = state.runId; return false; }
  $('spend-panel').hidden = true;
  $('spend-panel').costRequest = null;
  if (state.runId) state.chatDrafts.set(state.runId, $('chat-input').value);
  state.runId = runId;
  $('run-select').value = runId;
  $('chat-input').value = state.chatDrafts.get(runId) || '';
  state.timeline = null;
  state.rail = [];
  $("rail").innerHTML = "";
  $("project-progress").innerHTML = "";
  state.materials = [];
  state.artifactBodies.clear();
  sessionStorage.setItem("studio-selected-run", runId);
  state.busy = true;
  $("send").disabled = true;
  // A different run is a different set of stages; the stage that owned the
  // screen before has no claim on it.
  state.activeStage = null;
  $("stage").innerHTML = "";
  // Before the first render, so the question rows come back carrying whatever he
  // had written and not sent.
  loadDraft();
  // Restoring a durable execution must not depend on a successful content fetch.
  await Promise.all([
    refreshTimeline().catch(() => toast("项目内容暂时无法读取，任务状态仍会继续同步。", true)),
    restoreJob(runId),
  ]);
  if (state.view === 'project') setStudioView('project');
  return state.runId === runId;
}

async function refreshTimeline() {
  if (!state.runId) return;
  const runId = state.runId, serial = ++state.refreshSerial;
  const tl = await api(`/api/runs/${runId}/timeline`);
  if (state.runId !== runId || serial !== state.refreshSerial) return;
  state.timeline = tl;
  const project = state.runs.find(run => run.run_id === runId);
  if (project) project.progress = tl.progress;
  state.artifactBodies.clear();
  // Set before anything is drawn: `wireMedia` runs while the pane is being
  // built, and it resolves filenames against this list.
  state.materials = tl.materials || [];
  // The rail first: it decides which stage is on screen, and everything below
  // is drawn for that stage.
  renderRail(tl.rail);
  // Pruning runs against every question the run has, never against the stage on
  // screen — see `pruneDrafts`. Before the pane, so a dropped answer is not
  // drawn one last time.
  pruneDrafts(tl);
  renderPane();
  renderConversation(tl);
  renderDraft();
  renderSpend(tl);
  renderMaterials();
  renderProjects();
}

/* --------------------------------------------------------------- header -- */

function renderRail(rail) {
  const host = $("rail");
  host.innerHTML = "";
  state.rail = rail;

  // Which stage owns the screen. Kept if it still exists, so a refresh does not
  // yank him off the stage he was reading; chosen otherwise.
  if (!state.activeStage || !rail.some((s) => s.stage === state.activeStage)) {
    state.activeStage = defaultStage(rail, state.timeline || {});
  }

  for (const step of rail) {
    const here = step.stage === state.activeStage;
    const node = box(
      "button",
      `step ${step.state}` + (step.needs_you ? " needs" : "") + (here ? " here" : "")
    );
    if (here) node.setAttribute("aria-current", "true");
    // The icon comes from the state; the server's `mark` is a character chosen
    // for a text-only rail and is kept only as the fallback for a state this
    // file has not been taught.
    node.innerHTML =
      `<span class="mark">${ICON[step.state] || escapeHtml(step.mark)}</span>` +
      `<span class="name">${escapeHtml(step.label)}</span>`;
    // How many things this stage could not settle and is waiting on him for.
    // The strip that used to hold them has no replacement: they are in the
    // stage they belong to now, and this is the signpost that says which.
    const n = (step.questions || []).length;
    if (n) node.appendChild(box("span", "q-count", String(n)));

    // The four situations, in his words. It used to say 「还没有内容」 for all of
    // them, which was wrong for three: an attempt that failed and a document
    // that came out empty need different things from him than a step nobody has
    // started.
    node.title = [step.blank.text, n ? `${n} 个问题等你回答` : ""].filter(Boolean).join(" · ");
    node.onclick = () => selectStage(step.stage);
    host.appendChild(node);
  }
}

/* Which stage to open when there is no good reason to open a different one.
 *
 * The thing waiting on him outranks everything — that is the whole point of
 * moving off a conversation, where finding it cost eight screens of scrolling.
 * Then the stage that is next to run. Then the furthest stage that has something
 * in it, because that is what he most recently got back.
 *
 * Note this is *narrower* than the measured norm: GitLab, Gitea, Forgejo and
 * Phorge all land on a wide "everything I am involved in" default and make you
 * switch to "what needs me". That is right when one person has thirty open
 * items. Here there is at most one thing waiting at a time.
 */
function defaultStage(rail, tl) {
  if (tl.progress?.stage && rail.some(s => s.stage === tl.progress.stage)) return tl.progress.stage;
  const pending = tl.pending;
  if (pending && rail.some((s) => s.stage === pending.stage)) return pending.stage;

  const active = rail.find((s) => s.state === "active");
  if (active) return active.stage;

  const withDoc = rail.filter((s) => s.has_doc);
  if (withDoc.length) return withDoc[withDoc.length - 1].stage;

  return rail.length ? rail[0].stage : null;
}

function selectStage(stage) {
  setStudioView('project');
  if (!stage || stage === state.activeStage) return;
  state.activeStage = stage;
  renderRail(state.rail);
  renderPane();
}

/* ------------------------------------------------------------ the stage -- */

function renderPane() {
  const host = $("stage");
  const step = (state.rail || []).find((s) => s.stage === state.activeStage);
  if (!step) {
    host.innerHTML = "";
    return;
  }
  const tl = state.timeline || {};
  renderProjectProgress();
  const expected = tl.pending?.stage === step.stage && tl.pending.version === step.version
    ? tl.pending.artifact_sha256 : undefined;
  const displayStep = {...step, artifact_sha256: expected};
  const bodyKey = `${state.runId}:${step.stage}:${step.version}`;
  if (step.has_doc && !(step.blocks?.length) && !state.artifactBodies.has(bodyKey)) loadStageBody(displayStep);

  // Scroll is put back where he left it. Every refresh used to jump the stream
  // to the bottom, which is half of what 「翻来翻去」 meant: a stage that updated
  // under him moved the text out from beneath his eyes.
  const kept = host.scrollTop;
  host.innerHTML = stageHtml(displayStep, {
    artifactBody: state.artifactBodies.get(bodyKey),
    // Every question the run has. `stageHtml` keeps the ones belonging to this
    // stage — the rule lives there so it has one home and one test.
    questions: tl.questions || [],
    // `stageHtml` draws the gate only when it names this stage.
    pending: tl.pending || null,
    drafts: state.draft,
  });
  wireGate(host);
  if (typeof wireRevisionActions === 'function') wireRevisionActions(host, state.runId, step);
  if (typeof wireProductCards === 'function') wireProductCards(host, state.runId, step);
  if (typeof wireInlineFeedback === 'function') wireInlineFeedback(host, state.runId, step);
  for (const button of host.querySelectorAll('[data-act="read"]')) {
    button.onclick = () => openReader(button.dataset.stage, Number(button.dataset.version))
      .catch(err => toast(`原文暂时无法打开：${err.message || err}`));
  }
  for (const button of host.querySelectorAll('[data-act="retry-body"]')) button.onclick = () => {
    state.artifactBodies.delete(bodyKey); renderPane();
  };
  // The answer boxes come and go with the stage, so they are bound here rather
  // than once at load. Only the pane's boxes are in the map — the tray keeps
  // drafts for stages that are not on screen, and reaches their inputs by a key
  // that is deliberately absent from it.
  wireAnswers(host);
  // After the pane exists, not per block: a picture an artifact names is only
  // resolvable once `state.materials` is loaded.
  wireMedia(host);
  host.scrollTop = kept;
}

async function loadStageBody(step) {
  const runId = state.runId, key = `${runId}:${step.stage}:${step.version}`;
  const entry = {stage:step.stage,version:step.version,status:'loading'};
  state.artifactBodies.set(key,entry);
  try {
    const record = await api(`/api/runs/${encodeURIComponent(runId)}/artifacts/${step.stage}/${step.version}`);
    if (record.stage !== step.stage || record.version !== step.version || !record.integrity_ok
        || (step.artifact_sha256 && record.sha256 !== step.artifact_sha256)) throw new Error('方案版本已变化，请刷新项目后重新阅读。');
    Object.assign(entry,{status:'loaded',text:record.content,sha256:record.sha256});
  } catch (error) {
    Object.assign(entry,{status:'error',error:'暂时无法读取这一版内容，请重新加载。'});
  }
  if (state.runId === runId && state.activeStage === step.stage && state.artifactBodies.get(key) === entry) renderPane();
}

/* ---------------------------------------------------------- the questions -- */

/* The one part of the page that is a request rather than a record.
 *
 * Every stage writes what it could not settle; until now those went into the
 * document's `limitations` as a disclaimer he scrolled past. They stay until the
 * stage is rewritten without them — there is no dismiss button, because a
 * question he has not answered is still open whether or not he has read it.
 *
 * They are not collected anywhere. A question lives in the stage that asked it,
 * drawn by `stage.js` under that stage's document, and the rail carries a count
 * so he can see from anywhere that a stage is holding some. The strip that used
 * to hold all of them across the top is gone: answers belong next to the
 * document that raised them, and a strip twenty rows tall had no height limit.
 */

/* Drafts: what he has written on a question row but not sent.
 *
 * Answers accumulate rather than firing one at a time, for two reasons that
 * point the same way. Sending after the first answer would start an agent turn
 * against a brief he is still filling in; and every message is an agent turn, so
 * answering four questions one at a time buys four turns' worth of tokens for
 * one turn's worth of work.
 *
 * Kept in the browser, keyed by the artifact version the question came from —
 * see `image-review.js` in the old Backlot pilot, which solved the same problem
 * with a manifest hash. A reworked stage produces new questions, and an answer
 * written against the old ones must not ride onto the new version: it would name
 * a question that no longer exists, and the orchestrator would take it at face
 * value.
 *
 * The key itself is `answerKey`, in `stage.js` — see there for why it is not
 * defined in this file.
 */

function loadDraft() {
  state.draft = {};
  try {
    const raw = localStorage.getItem(`studio-draft:${state.runId}`);
    const parsed = raw ? JSON.parse(raw) : null;
    // Anything that is not the shape we wrote is discarded rather than merged:
    // a half-parsed draft is worse than none, because it would send under a
    // question it may not belong to.
    if (parsed && typeof parsed === "object") {
      for (const [key, value] of Object.entries(parsed)) {
        if (value && typeof value.text === "string") state.draft[key] = value;
      }
    }
  } catch (err) {
    state.draft = {};
  }
}

function saveDraft() {
  try {
    localStorage.setItem(`studio-draft:${state.runId}`, JSON.stringify(state.draft));
  } catch (err) {
    // A browser that refuses storage costs him the draft, not the screen.
  }
}

/* Keyed, not given the question.
 *
 * The input that calls this carries its draft key in an attribute and nothing
 * else — the question is looked up by it. The alternative was to load the
 * question onto the element as more attributes, which is the same lookup written
 * out by hand and one more place for the two to disagree about which question a
 * box belongs to.
 */
function setDraft(key, text) {
  const q = questionByKey(key);
  if (!q) return;
  if (text.trim()) {
    state.draft[key] = {
      text,
      question: q.question,
      prefix: q.send_prefix || "",
      stage: q.stage,
      version: q.version,
    };
  } else {
    delete state.draft[key];
  }
  saveDraft();
  // Only the tray is redrawn. Redrawing the question list here would rebuild the
  // input he is typing into and take the caret with it.
  renderDraft();
}

function draftItems() {
  return Object.entries(state.draft)
    .map(([key, value]) => ({ key, ...value }))
    .filter((it) => it.text.trim());
}

/* The tray, and the one message the drafts become.
 *
 * The header is the server's (`answers_header`) and each line's lead-in is the
 * server's (`send_prefix`) — this composes, it does not write sentences. Sent
 * through the same `send()` every typed message goes through: there is still
 * exactly one way to talk to the agent, and the endpoints did not change.
 */
function composeDraft() {
  const items = draftItems();
  if (!items.length) return "";
  const lines = items.map((it) => `${it.prefix}${it.text.trim()}`);
  if (lines.length === 1) return lines[0];
  const header = String((state.timeline || {}).answers_header || "").replace(
    "{n}",
    String(lines.length)
  );
  const numbered = lines.map((line, i) => `${i + 1}. ${line}`);
  return header ? [header, ...numbered].join("\n") : numbered.join("\n");
}

function renderDraft() {
  const host = $("draft");
  const items = draftItems();
  host.innerHTML = "";
  if (!items.length) {
    host.hidden = true;
    return;
  }
  host.hidden = false;

  const head = box("div", "draft-head");
  head.append(
    box("span", "draft-count", `${items.length} 条待发`),
    box("span", "draft-note", "一起发出去，主 Agent 一次看到全部")
  );
  host.appendChild(head);

  for (const it of items) {
    const row = box("div", "draft-row");
    row.appendChild(box("span", "draft-q", it.question));
    row.appendChild(box("span", "draft-a", it.text.trim()));
    const undo = box("button", "ghost small", "撤销");
    undo.type = "button";
    undo.onclick = () => {
      delete state.draft[it.key];
      saveDraft();
      // The row's own input has to be emptied too — otherwise the text is gone
      // from the tray and still sitting in the box. Held by reference rather
      // than looked up, so the list is never rebuilt here: rebuilding it would
      // take the caret out of whatever else he is typing in.
      //
      // Missing is normal and not a problem: the box is only on screen when its
      // stage is the one being shown, and the tray is global. Undoing an answer
      // written in another stage drops the draft and has no box to clear.
      const input = state.draftInputs.get(it.key);
      if (input) input.value = "";
      renderDraft();
    };
    row.appendChild(undo);
    host.appendChild(row);
  }

  const sendBtn = box("button", "primary small", "发送");
  sendBtn.type = "button";
  sendBtn.disabled = state.busy || state.decisionBusy || state.recoveryBlocked;
  sendBtn.onclick = () => {
    const text = composeDraft();
    if (!text || state.busy || state.decisionBusy || state.recoveryBlocked || !state.runId) return;
    state.draft = {};
    saveDraft();
    renderDraft();
    // Cleared by rebuilding the pane, which is safe here: he is clicking a
    // button, not typing.
    renderPane();
    send(text);
  };
  host.appendChild(sendBtn);
}

/* Drop drafts whose question no longer exists.
 *
 * A draft is an answer to one question in one version of one stage. When the
 * stage is rewritten the question goes with it, and an answer left behind would
 * name something that is no longer there — and the orchestrator would take it at
 * face value.
 *
 * THE SET IS EVERY QUESTION THE RUN HAS, NOT THE STAGE ON SCREEN. That is the
 * one thing this move could quietly break: the pane now draws a single stage, so
 * "the questions currently drawn" is a strict subset, and pruning against it
 * would delete every answer he had written in the other three stages the first
 * time he clicked a rail step.
 */
function pruneDrafts(tl) {
  const questions = tl.questions || [];
  const live = new Set(questions.map(answerKey));
  let dropped = false;
  for (const key of Object.keys(state.draft)) {
    if (!live.has(key)) {
      delete state.draft[key];
      dropped = true;
    }
  }
  if (dropped) saveDraft();
}

function questionByKey(key) {
  return ((state.timeline || {}).questions || []).find((q) => answerKey(q) === key) || null;
}

function renderSpend(tl) {
  $("spend").textContent = "用量与费用";
  $("spend").title = [tl.spend_sentence,tl.all_runs_sentence].filter(Boolean).join('\n');
}

$("spend").onclick = () => {
  if(!state.runId){toast('先打开一个项目，再查看用量与费用。');return;}
  const panel = $("spend-panel");
  panel.hidden = !panel.hidden;
  if(!panel.hidden)StudioProjectCosts.mount(panel,{runId:state.runId,isCurrent:id=>state.runId===id,
    onRecover:id=>{if(state.runId!==id)return;setStudioView('project');setProjectFocus('chat');$('job-status').scrollIntoView({block:'nearest'});}});
};

/* --------------------------------------------------------------- media -- */

/* A filename becoming a picture, and what happens when it cannot.
 *
 * `blocks.js` emits `data-media="name"` and stops, because from inside a
 * renderer it knows neither the run nor whether the file is really there. Both
 * are known here. A name matching nothing on the shelf becomes 「文件不在」
 * rather than a broken-image glyph: the artifact claimed it had a photo, and the
 * honest report is that it does not. The same rule as every other degradation in
 * this app — fail into something readable, and say what happened.
 */

function materialByName(name) {
  return state.materials.find((m) => m.name === name) || null;
}

function materialUrl(name) {
  return (
    `/api/runs/${encodeURIComponent(state.runId)}/materials/` +
    encodeURIComponent(name)
  );
}

function humanBytes(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1048576) return `${Math.round(n / 1024)} KB`;
  return `${(n / 1048576).toFixed(1)} MB`;
}

function wireMedia(root) {
  for (const slot of root.querySelectorAll("[data-media]")) {
    const name = slot.getAttribute("data-media");
    const found = materialByName(name);
    slot.textContent = "";
    slot.classList.remove("missing", "is-video");

    if (!found || !found.usable) {
      slot.classList.add("missing");
      slot.appendChild(box("span", "media-missing", `文件不在：${name}`));
      continue;
    }

    if (found.kind === "video") {
      const video = document.createElement("video");
      // `#t=0.5` asks the browser to draw that frame as the poster. No
      // transcoding, no ffmpeg, and no second file on disk that could drift out
      // of sync with the one it is a picture of.
      video.src = `${materialUrl(name)}#t=0.5`;
      video.preload = "metadata";
      video.muted = true;
      video.playsInline = true;
      slot.appendChild(video);
      slot.appendChild(box("span", "media-play"));
      slot.classList.add("is-video");
    } else {
      const img = document.createElement("img");
      img.src = materialUrl(name);
      img.alt = name;
      img.loading = "lazy";
      // A file that vanished between the listing and the fetch gets the same
      // answer as one that was never there. The listing is a moment, not a
      // promise.
      img.onerror = () => {
        slot.textContent = "";
        slot.classList.add("missing");
        slot.appendChild(box("span", "media-missing", `打不开：${name}`));
      };
      slot.appendChild(img);
    }
  }
}

// One delegated listener rather than an `onclick` per slot: the same slot can be
// a card's picture, a timeline bar's reference frame or a tile on the shelf, and
// all three should open the same viewer. Attaching the handler to the slot would
// also mean a tile's own click and the slot's click both firing.
document.addEventListener("click", (e) => {
  const slot = e.target.closest("[data-media]");
  if (!slot) return;
  const found = materialByName(slot.getAttribute("data-media"));
  if (found && found.usable) openViewer(found);
});

/* One material, big. Clicking a photo must not take him off the screen he is
 * already reading — the same reason the artifact reader is an overlay. */
function openViewer(m) {
  $("viewer-name").textContent = `${m.name} · ${humanBytes(m.bytes)}`;
  const body = $("viewer-body");
  body.innerHTML = "";
  if (m.kind === "video") {
    const video = document.createElement("video");
    video.src = materialUrl(m.name);
    video.controls = true;
    video.autoplay = true;
    video.playsInline = true;
    body.appendChild(video);
  } else {
    const img = document.createElement("img");
    img.src = materialUrl(m.name);
    img.alt = m.name;
    body.appendChild(img);
  }
  $("viewer").hidden = false;
}

function closeViewer() {
  // Emptied rather than hidden, so a playing video actually stops.
  $("viewer-body").innerHTML = "";
  $("viewer").hidden = true;
}

/* ------------------------------------------------------------- the shelf -- */

/* What this run really has on disk.
 *
 * A plain square grid with no card backgrounds — a contact sheet, not a list of
 * attachments. The picture is the thing; the name and size sit under it in small
 * muted type. Unusable files are shown too, greyed, with the reason: the folder
 * and the shelf must not disagree about what is in the folder.
 *
 * It is also the drop target. Dropping is the third write path in this app and
 * the only one that touches no state — it puts a file in a folder. Every rule
 * about what may land there lives in `studio/materials.py`, and a refusal is
 * shown in the server's own words rather than re-worded here.
 */

function renderMaterials() {
  const list = state.materials;
  const usable = list.filter((m) => m.usable);
  // The count rides on the chip as well as in the panel. A shelf he has to open
  // to find out whether anything is on it is a shelf he will forget about.
  $("materials-toggle").textContent = list.length ? `项目素材 · ${usable.length}` : "项目素材";
  $("materials-count").textContent = list.length
    ? `${usable.length} 个能用 · 共 ${list.length} 个文件`
    : "还没有素材";

  const grid = $("materials-grid");
  grid.innerHTML = "";
  if (!list.length) {
    grid.appendChild(
      box("p", "materials-empty", "添加商品照片、参考图或视频，再记录可确认的内容，供团队策划使用。")
    );
    return;
  }
  for (const m of list) grid.appendChild(materialTile(m));
  wireMedia(grid);
  if (state.materialsOpen) loadMaterialWorkspace();
}

function materialTile(m) {
  const tile = box("div", "tile" + (m.usable ? "" : " unusable"));
  const thumb = box("span", "tile-thumb");
  if (m.usable) {
    // Wired by `wireMedia` like every other slot, so a tile and a card draw the
    // same picture through the same code path.
    thumb.setAttribute("data-media", m.name);
  } else {
    thumb.classList.add("tile-nope");
    thumb.textContent = "?";
  }
  tile.appendChild(thumb);
  tile.appendChild(box("span", "tile-name", m.name));
  tile.appendChild(
    box("span", "tile-meta", m.usable ? humanBytes(m.bytes) : "不是图片也不是视频")
  );
  return tile;
}

function toggleMaterials(open) {
  if (!state.runId && open !== false) {newRunDialog();return;}
  state.materialsOpen = open == null ? !state.materialsOpen : open;
  $("materials").hidden = !state.materialsOpen;
  $("materials-toggle").setAttribute("aria-expanded", String(state.materialsOpen));
  if (state.materialsOpen) loadMaterialWorkspace();
}

/* Dropping files on the shelf.
 *
 * One request per file rather than one for the batch. The server's rules are per
 * file, and a batch would have to invent an answer for "three of five landed";
 * per file, every refusal already names the file it refused.
 */
async function uploadFiles(files) {
  if (!state.runId) {
    toast("先有个项目，再往里放素材。", true);
    return;
  }
  if (!files.length) return;
  const uploadRunId = state.runId;
  const problems = [];
  let landed = 0;
  for (const file of files) {
    const form = new FormData();
    // The third argument is the filename the browser sends. `append(name, blob)`
    // with two arguments sends "blob", and every upload would collide with the
    // last one.
    form.append("file", file, file.name);
    try {
      // Raw `fetch`, not `api()`: that helper forces a JSON content type, and
      // this body is multipart.
      const res = await fetch(
        `/api/runs/${encodeURIComponent(uploadRunId)}/materials`,
        { method: "POST", body: form }
      );
      if (res.ok) {
        landed += 1;
        continue;
      }
      let detail = res.statusText;
      try {
        detail = (await res.json()).detail || detail;
      } catch (_) { /* non-JSON body */ }
      problems.push(`${file.name}：${detail}`);
    } catch (err) {
      problems.push(`${file.name}：${String(err.message || err)}`);
    }
  }

  if (state.runId === uploadRunId) await refreshTimeline();
  if (problems.length) toast(problems.join("；"), true);
  else toast(`放进去了 ${landed} 个。`);
}

const shelf = $("materials");
for (const type of ["dragenter", "dragover"]) {
  shelf.addEventListener(type, (e) => {
    // Without this the browser never fires `drop` at all.
    e.preventDefault();
    shelf.classList.add("dropping");
  });
}
shelf.addEventListener("dragleave", (e) => {
  // `dragleave` fires again every time the pointer crosses into a child element,
  // so a plain remove would flicker the whole way across the panel.
  if (!shelf.contains(e.relatedTarget)) shelf.classList.remove("dropping");
});
shelf.addEventListener("drop", (e) => {
  e.preventDefault();
  shelf.classList.remove("dropping");
  uploadFiles([...e.dataTransfer.files]);
});
// A file dropped anywhere else would navigate the page to it, losing the
// conversation. Both handlers still run for the shelf itself.
for (const type of ["dragover", "drop"]) {
  document.addEventListener(type, (e) => e.preventDefault());
}

$("materials-toggle").onclick = () => toggleMaterials();
$("materials-close").onclick = () => toggleMaterials(false);
$("conversation-toggle").onclick = () => setConversationOpen(!state.conversationOpen);
$("viewer-close").onclick = closeViewer;
$("viewer").addEventListener("click", (e) => {
  if (e.target === $("viewer")) closeViewer();
});

/* ------------------------------------------------------ the conversation -- */

/* What happened, in order, in a band he can open.
 *
 * Demoted, not deleted. Reading one document closely and following the run over
 * time are two different needs, and every tool measured keeps both views side by
 * side — Gerrit's Comments tab, Phorge's grouped summary, GitLab's pending list,
 * Gitea's review blocks in the thread. Only one of them can own the screen, and
 * the stage owns it because that is where the thing waiting on him is.
 *
 * An artifact is one line here, not a card. The document itself is in its stage,
 * laid out, one rail click away; drawing it a second time inside a band 30% of a
 * screen tall would give the same document two places to be read and two places
 * to disagree. The review goes the same way — it is next to the version it is
 * about, which is the only place it means anything.
 */
function renderConversation(tl) {
  const host = $("conversation-body");
  host.innerHTML = "";
  $("conversation-count").textContent = tl.turns.length
    ? `${tl.turns.length} 轮`
    : "还没有对话";
  setConversationOpen(state.conversationOpen);

  if (!tl.turns.length) host.innerHTML = '<div class="chat-welcome"><span class="chat-welcome-mark">le.</span><h3>从你的想法聊起</h3><p>说说商品、想打动的人，或者这次想尝试的表达。我们一起把方案做出来。</p><button class="chat-suggestion" type="button">帮我梳理这个项目的创意方向 ↗</button></div>';
  const suggestion=host.querySelector('.chat-suggestion');
  if (suggestion) suggestion.onclick=()=>{$('chat-input').value='先帮我梳理这个项目的创意方向。';$('chat-input').focus();};
  for (const turn of tl.turns) host.appendChild(renderTurn(turn));
  if (state.atBottom) host.scrollTop = host.scrollHeight;
}

function setConversationOpen(open) {
  state.conversationOpen = open;
  $("conversation-body").hidden = !open;
  $("conversation").classList.toggle("open", open);
  $("conversation-toggle").setAttribute("aria-expanded", String(open));
  $("conversation-caret").textContent = open ? "收起" : "展开";
}

function renderTurn(turn) {
  const wrap = box("div", "turn");

  // A legacy run holds a user record with empty text; rendering it would put a
  // blank grey bubble at the top of the conversation with nothing to explain it.
  if (turn.user && turn.user.text) {
    const bubble = box("div", "msg user");
    bubble.append(box("div", "who", "你"), box("div", "body", turn.user.text));
    wrap.appendChild(bubble);
  }

  for (const item of turn.items) {
    if (item.kind === "note") wrap.appendChild(renderNote(item));
    else if (item.kind === "artifact") wrap.appendChild(renderArtifactLine(item));
    else if (item.kind === "approval") wrap.appendChild(renderApproval(item));
    else if (item.kind === "decision_request") wrap.appendChild(renderPendingLine(item));
  }

  if (turn.reply) {
    const bubble = box("div", "msg assistant");
    const who = box("div", "who", "制片团队");
    const body = box("div", "body");
    body.innerHTML = renderMarkdown(turn.reply.text);
    bubble.append(who, body);
    wrap.appendChild(bubble);
  }

  const detail = state.machineDetail ? renderMachineDetail(turn.detail) : null;
  if (detail) wrap.appendChild(detail);

  return wrap;
}

function renderNote(item) {
  return box("div", "note", item.text);
}

/* One artifact, as one line.
 *
 * The summary is here and the document is not. A line long enough to hold a
 * whole summary is a line the CSS truncates, which is the point: the band is for
 * finding the moment, and 「去看」 is how you go read it.
 */
function renderArtifactLine(item) {
  const line = box("div", "line artifact");
  line.appendChild(box("span", "line-what", `${item.numbered} · ${item.version_label}`));
  const by = box("span", "line-by");
  by.textContent = item.summary ? `${item.author}交回：${item.summary}` : `${item.author}交回`;
  line.appendChild(by);
  line.appendChild(goToStage(item.stage));
  return line;
}

/* Something is waiting on him, and it is the only request in the history.
 *
 * The offer is repeated here rather than left to the rail: this is the line he
 * is looking at when he opens the band to find out where the run got to, and a
 * signpost that only exists in the navigation is a signpost he has to already
 * know to look for.
 */
function renderPendingLine(item) {
  const line = box("div", "line pending");
  line.appendChild(box("span", "line-what", "在等你决定"));
  line.appendChild(box("span", "line-by", `「${item.stage_label}」`));
  line.appendChild(goToStage(item.stage));
  return line;
}

function goToStage(stage) {
  const go = box("button", "ghost small", "去看");
  go.type = "button";
  go.onclick = () => selectStage(stage);
  return go;
}

function renderApproval(item) {
  const card = box("div", "card approval");
  card.append(box("div", "card-title", `你的决定 · ${item.stage_label}`));
  card.appendChild(box("p", "card-summary", item.sentence));
  return card;
}

/* What a click in the gate panel does.
 *
 * The markup is `stage.js`'s; the effects are here, and that split is the same
 * one `blocks.js` uses when it emits `data-media` and stops. Nothing in the
 * panel knows what a `decide` is, and this file never composes a sentence about
 * one — the gate's headline, its option labels and the reason each option would
 * record all arrive written.
 */
function wireGate(host) {
  const gate = host.querySelector(".gate");
  if (!gate) return;
  const reason = gate.querySelector(".reason");
  const key = `studio-decision:${state.runId}:${gate.dataset.stage}:${gate.dataset.sha}`;
  const status = gate.querySelector(".gate-feedback");
  let draft = { note: "", option: null, nonce: null };
  try { draft = { ...draft, ...JSON.parse(localStorage.getItem(key) || "{}") }; } catch {}
  reason.value = draft.note || "";
  for (const radio of gate.querySelectorAll('input[name="direction-choice"]')) {
    radio.checked = radio.value === draft.option;
    radio.onchange = () => { draft.option = radio.value; draft.nonce = null; save(); };
  }
  function save() {
    draft.note = reason.value;
    try { localStorage.setItem(key, JSON.stringify(draft)); status.textContent = "草稿已保存 · 尚未提交"; }
    catch { status.textContent = "草稿仅暂存在此页，浏览器存储不可用。"; }
  }
  reason.oninput = () => { draft.nonce = null; save(); };
  if (draft.note || draft.option) status.textContent = "已恢复此版本的草稿 · 尚未提交";
  for (const btn of gate.querySelectorAll('[data-act]')) btn.onclick = async () => {
    if (state.busy || state.decisionBusy || gate.dataset.saving === "1") return;
    const decision = btn.dataset.act === "reject" ? "returned" : "approved";
    const note = reason.value.trim();
    if ((decision === "returned" || gate.dataset.passed !== "1") && !note) {
      status.textContent = "请写明需要调整的地方，或覆盖审核的具体理由。"; reason.focus(); return;
    }
    if (decision === "approved" && gate.querySelector('input[name="direction-choice"]') && !draft.option) {
      status.textContent = "请先选择一个方向；推荐不代表已选。"; return;
    }
    const runId = state.runId;
    state.decisionBusy = true;
    $("run-select").disabled = true; $("send").disabled = true;
    gate.dataset.saving = "1";
    for (const control of gate.querySelectorAll('button,input,textarea')) control.disabled = true;
    const shown = { stage: gate.dataset.stage, expected_version: Number(gate.dataset.version), expected_artifact_sha256: gate.dataset.sha };
    save(); status.textContent = "正在保存…";
    try {
      if (!draft.nonce || draft.decision !== decision) {
        const query = new URLSearchParams(shown);
        draft.nonce = (await api(`/api/runs/${runId}/nonce?${query}`, { method: "POST" })).nonce;
        draft.decision = decision; save();
      }
      await api(`/api/runs/${runId}/decision`, {method:"POST",body:JSON.stringify({
        ...shown, nonce:draft.nonce, decision, note,
        override_reason: decision === "approved" && gate.dataset.passed !== "1" ? note : "",
        selected_option_id: decision === "approved" ? draft.option : null,
      })});
      try { localStorage.removeItem(key); } catch {}
      toast(decision === "approved" ? "采用决定已保存，后续可在对话中继续。" : "调整意见已保存。");
      await refreshTimeline();
    } catch (err) {
      const message = String(err.message || err);
      const explanation = message.includes("version_conflict") ? "这份方案已更新，当前页面不能批准新版本。" : message.includes("in progress") ? "正在生成内容，请完成后再决定。" : message;
      status.textContent = `${explanation} 草稿仍保留；请载入最新状态后重新阅读。`;
      if (!gate.querySelector('.gate-reload')) {
        const reload = box('button','ghost small gate-reload','载入最新状态');
        reload.onclick = () => refreshTimeline(); gate.appendChild(reload);
      }
    } finally {
      state.decisionBusy = false;
      $("run-select").disabled = false; $("send").disabled = state.busy || state.recoveryBlocked;
      gate.dataset.saving = "0";
      for (const control of gate.querySelectorAll('button,input,textarea')) control.disabled = false;
    }
  };
}

/* The answer boxes in the pane.
 *
 * Held by reference so the tray can empty the right one when a draft is undone
 * without rebuilding the list he may be typing in. Only the boxes on screen are
 * in the map — one stage's worth — and the tray handles a miss by doing nothing
 * to a box that is not there.
 */
function wireAnswers(host) {
  state.draftInputs = new Map();
  for (const input of host.querySelectorAll("[data-draft-key]")) {
    const key = input.getAttribute("data-draft-key");
    state.draftInputs.set(key, input);
    input.oninput = () => setDraft(key, input.value);
  }
}

function renderMachineDetail(detail) {
  if (!detail || !detail.calls) return null;
  const more = document.createElement("details");
  more.className = "machine";
  more.open = state.machineDetail;
  const tokens = detail.input_tokens + detail.output_tokens;
  more.appendChild(
    box("summary", null, `调用记录 · ${detail.calls} 次 · ${tokens.toLocaleString()} token`)
  );
  const list = box("div", "machine-list");
  for (const r of detail.roles) {
    const rounds = r.rounds ? ` · 跑了 ${r.rounds} 轮` : "";
    list.appendChild(
      box(
        "div",
        "machine-row",
        `${r.label}　${r.calls} 次调用${rounds} · ` +
          `${(r.input_tokens + r.output_tokens).toLocaleString()} token`
      )
    );
  }
  more.appendChild(list);
  return more;
}

/* --------------------------------------------------------------- reader -- */

let readerState = { stage: null, version: null };

async function openReader(stage, version) {
  readerState = { stage, version };
  $("reader").hidden = false;
  await loadReader();
}

async function loadReader() {
  const { stage, version } = readerState;
  const body = $("reader-body");
  body.innerHTML = "<p class='muted'>正在打开…</p>";

  let record;
  try {
    record = await api(`/api/runs/${state.runId}/artifacts/${stage}/${version}`);
  } catch (err) {
    body.innerHTML = "";
    body.appendChild(box("p", "muted", String(err.message || err)));
    return;
  }
  const versions = await api(`/api/runs/${state.runId}/artifacts?stage=${stage}`);

  $("reader-title").textContent = `${record.numbered} · ${record.version_label}`;
  const sub = `${record.author}写于 ${record.created_at}`;
  $("reader-sub").textContent = record.integrity_ok
    ? sub
    : `${sub} · 这份文件保存后被改动过，不能作为依据`;

  const tabs = $("reader-versions");
  tabs.innerHTML = "";
  for (const v of versions) {
    const btn = box("button", v.version === version ? "active" : null, v.version_label);
    btn.onclick = () => {
      readerState.version = v.version;
      loadReader();
    };
    tabs.appendChild(btn);
  }

  body.innerHTML = "";
  if (record.summary) {
    body.appendChild(box("h3", null, "摘要"));
    body.appendChild(box("p", null, record.summary));
  }
  // The same layout as the card in the stream, so the two never disagree about
  // what the document says. The raw text stays below it, unchanged: the layout
  // is a reading of the document, and the document is the thing of record.
  if (record.blocks && record.blocks.length) {
    body.appendChild(box("h3", null, "排好版的样子"));
    body.appendChild(blocksElement(record.blocks));
  }
  if (record.questions && record.questions.length) {
    body.appendChild(box("h3", null, "这一份里没解决的问题"));
    const ul = box("ul", "plain");
    for (const q of record.questions) {
      const li = box("li");
      li.appendChild(box("span", "blk-item", q.question));
      if (q.blocks) li.appendChild(box("span", "blk-note", `卡住了：${q.blocks}`));
      ul.appendChild(li);
    }
    body.appendChild(ul);
  }
  body.appendChild(box("h3", null, "原文"));
  const article = box("article", "doc");
  article.innerHTML = renderMarkdown(record.content);
  body.appendChild(article);
  if (record.evidence && record.evidence.length) {
    body.appendChild(box("h3", null, "作者列出的证据"));
    const ul = box("ul", "plain");
    for (const e of record.evidence) ul.appendChild(box("li", null, e));
    body.appendChild(ul);
  }
  if (record.limitations && record.limitations.length) {
    body.appendChild(box("h3", null, "作者自述的局限"));
    const ul = box("ul", "plain");
    for (const l of record.limitations) ul.appendChild(box("li", null, l));
    body.appendChild(ul);
  }
  wireMedia(body);
  body.scrollTop = 0;
}

$("reader-close").onclick = () => { $("reader").hidden = true; };
$("reader").addEventListener("click", (e) => {
  if (e.target === $("reader")) $("reader").hidden = true;
});
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  // Innermost first: Escape out of the picture, then out of the document.
  if (!$("viewer").hidden) closeViewer();
  else $("reader").hidden = true;
});

/* ------------------------------------------------------------ decisions -- */

async function decide(stage, decision, reason) {
  // Decisions are made in the stage panel against its displayed version.
  state.activeStage = stage;
  renderPane();
  toast("请在当前阶段的决策区选择并提交，意见会绑定正在展示的版本。");
}

/* ----------------------------------------------------------------- chat -- */

/* Scrolling belongs to the band now, and only to the band.
 *
 * The pane does not follow anything: it holds one stage, and a stage that
 * rewrites itself under him must not move the text he is reading. A turn
 * starting while he is scrolled up in the transcript does not yank him to the
 * bottom either — `state.atBottom` is set by his own scrolling, and only a send
 * (where he is asking for the reply) forces it back down.
 */
function scrollDown() {
  const host = $("conversation-body");
  if (state.atBottom) host.scrollTop = host.scrollHeight;
}

$("conversation-body").addEventListener("scroll", () => {
  const host = $("conversation-body");
  state.atBottom = host.scrollHeight - host.scrollTop - host.clientHeight < 60;
});

async function send(text) {
  if (state.busy || state.decisionBusy || state.recoveryBlocked || !state.runId) return;
  state.atBottom = true;
  setConversationOpen(true);
  await submitJob(state.runId, { text, request_id: crypto.randomUUID() });
}

/* ---------------------------------------------------------------- input -- */

function submit() {
  const value = $("chat-input").value.trim();
  if (!value || !state.runId || state.busy || state.decisionBusy || state.recoveryBlocked) return;
  $("chat-input").value = "";
  $("chat-input").style.height = "auto";
  send(value);
}

$("send").onclick = submit;
$("chat-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    submit();
  }
});
$("chat-input").addEventListener("input", (e) => {
  e.target.style.height = "auto";
  e.target.style.height = `${Math.min(e.target.scrollHeight, 200)}px`;
});

$("detail-toggle").onclick = () => {
  state.machineDetail = !state.machineDetail;
  $("detail-toggle").textContent = state.machineDetail ? "收起运行详情" : "运行详情";
  $("detail-toggle").setAttribute('aria-pressed',String(state.machineDetail));
  if (state.timeline) renderConversation(state.timeline);
};

/* -------------------------------------------------------- new project -- */

function newRunDialog(format='video') {
  if (state.decisionBusy) { toast('正在保存你的决定，请稍后再新建项目。'); return; }
  if (document.querySelector('.new-project-dialog')) return;
  const dialog = document.createElement("dialog");
  const runId = autoRunId();
  let createdRun = null, creationPayload = null, submitting = false;
  dialog.className='new-project-dialog';
  dialog.innerHTML = `
    <form>
      <div class="dialog-kicker">A NEW STORY</div><h2>这次，你想创作什么？</h2>
      <p class="muted">从商品和一个想法开始，细节可以一起打磨。</p>
      <label>内容形式<select id="nr-format"><option value="video">商品视频</option><option value="photo">带货图文</option></select></label>
      <label>项目名称<input id="nr-title" placeholder="通勤午餐包 · 周末短片" required maxlength="200"></label>
      <div data-product-card-picker></div>
      <label>聊聊你的想法<textarea id="nr-brief" rows="5" required maxlength="19000" placeholder="想介绍什么商品？给谁看？希望对方看完记住什么？"></textarea></label>
      <p class="form-note">先建立项目，发送对话后才开始调用模型。</p><p class="form-error" role="alert"></p>
      <div class="dialog-actions">
        <button type="button" class="ghost" data-cancel>取消</button>
        <button type="submit" class="primary">创建项目 ↗</button>
      </div>
    </form>`;
  document.body.appendChild(dialog);
  dialog.querySelector('#nr-format').value=format==='photo'?'photo':'video';
  if (typeof attachProductCardPicker === 'function') attachProductCardPicker(dialog);
  dialog.querySelector('[data-cancel]').onclick=()=>dialog.close();
  dialog.querySelector('form').onsubmit=async(event)=>{
    event.preventDefault();
    if (submitting || state.decisionBusy) return;
      const title = $("nr-title").value.trim();
      const brief = $("nr-brief").value.trim();
      if (title && brief) {
        const submitButton=dialog.querySelector('[type="submit"]');submitting=true;submitButton.disabled=true;submitButton.textContent=createdRun?'正在打开…':'正在创建…';
        dialog.querySelector('[data-cancel]').disabled=true;
        if (!creationPayload) {
          creationPayload = { run_id: runId, title, brief: `交付形式：${$('nr-format').value==='photo'?'带货图片与文案':'商品视频'}。\n${brief}` };
          const card=dialog.productCardBinding?.();if(card)creationPayload.product_card=card;
        }
        for (const field of dialog.querySelectorAll('input,textarea,select')) field.disabled=true;
        try {
          if (!createdRun) {
            try {
              createdRun = await api("/api/runs", {
                method: "POST",
                body: JSON.stringify(creationPayload),
              });
            } catch (err) {
              // A lost response may have followed a successful write. Reuse this
              // form's identity and recover that exact project on a retry.
              if (![400,409].includes(err.status)) throw err;
              const existingRuns=await api('/api/runs');
              createdRun = existingRuns.find(run=>run.run_id===runId && run.title===creationPayload.title && run.brief===creationPayload.brief);
              if(!createdRun&&existingRuns.some(run=>run.run_id===runId))err.creationConflict=true;
              if (!createdRun) throw err;
            }
          }
          for (const field of dialog.querySelectorAll('input,textarea,select')) field.disabled=true;
          await loadRuns();
          if (!await selectRun(createdRun.run_id)) throw new Error('Project switch is busy');
          setStudioView('project');dialog.close();$('chat-input').focus();
          toast("项目已创建，和制片团队聊聊下一步。");
        } catch (err) {
          const invalid=!createdRun&&!err.creationConflict&&[400,422].includes(err.status);
          dialog.querySelector('.form-error').textContent=createdRun?'项目已创建，但暂时无法打开。重试会打开同一个项目。':invalid?err.message:'暂时无法确认创建结果，请检查连接后重试。';
          if(invalid){creationPayload=null;for(const field of dialog.querySelectorAll('input,textarea,select'))field.disabled=false;}
          submitButton.disabled=false;submitButton.textContent=createdRun?'重新打开':invalid?'创建项目 ↗':'重试创建';
        } finally {
          submitting=false;dialog.querySelector('[data-cancel]').disabled=false;
        }
      }
  };
  dialog.addEventListener('close',()=>dialog.remove());
  dialog.addEventListener('cancel',event=>{if (submitting) event.preventDefault();});
  dialog.showModal();
}

/** A run id he never has to see. Must satisfy ^[a-z0-9][a-z0-9-]*$ on the server. */
function autoRunId() {
  const now = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  const stamp =
    `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}` +
    `-${pad(now.getHours())}${pad(now.getMinutes())}`;
  const tail = Math.random().toString(36).slice(2, 6);
  return `p-${stamp}-${tail}`;
}

$("new-run").onclick = () => newRunDialog();
$("run-select").onchange = async (e) => {await selectRun(e.target.value);setStudioView('project');};

/* ----------------------------------------------------------------- init -- */

(async function init() {
  try {
    initStudioNavigation();
    await loadRuns();
    if (state.runId) await selectRun(state.runId);
    setStudioView('home');
    loadInspiration();
  } catch (err) {
    toast(String(err.message || err), true);
  }
})();
