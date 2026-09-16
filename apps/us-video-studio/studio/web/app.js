/* US video studio — the conversation, and the two things only a person can do.
 *
 * The screen is a conversation. Everything else — who was dispatched, what a
 * review said, what it cost — appears as a card in that conversation, because
 * that is how the work actually happened.
 *
 * The UI has exactly two write paths that change the run: sending a message and
 * deciding a gate. It cannot dispatch work, cannot submit a stage, cannot write
 * a review. The approval nonce is fetched on demand and held in memory only.
 * The third write path — dropping a file on the materials shelf — puts a file in
 * a folder and touches no state at all; see `studio/materials.py`.
 *
 * No framework and no build step: this runs from a folder on one machine.
 * The wording ("人话") is NOT here — it comes from the server's vocabulary, so
 * the live stream and a reloaded page say the same thing. Icons are, because a
 * shape is not a sentence.
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
  runId: null,
  timeline: null,
  rail: [],
  // The run's shelf, as the server last reported it. Held here rather than
  // looked up per picture because the same list answers two questions — what is
  // on the shelf, and whether a file an artifact named is really there.
  materials: [],
  busy: false,
  machineDetail: false,
  questionsOpen: false,
  materialsOpen: false,
  atBottom: true,
};

/* ------------------------------------------------------------------ api -- */

async function api(path, options) {
  const res = await fetch(path, { headers: { "content-type": "application/json" }, ...options });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail || detail;
    } catch (_) { /* non-JSON body */ }
    throw new Error(detail);
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
  const select = $("run-select");
  select.innerHTML = "";
  if (!runs.length) {
    const opt = document.createElement("option");
    opt.textContent = "还没有项目";
    opt.value = "";
    select.appendChild(opt);
    state.runId = null;
    return;
  }
  for (const run of runs) {
    const opt = document.createElement("option");
    opt.value = run.run_id;
    opt.textContent = run.title;
    select.appendChild(opt);
  }
  if (!state.runId || !runs.some((r) => r.run_id === state.runId)) state.runId = runs[0].run_id;
  select.value = state.runId;
}

async function selectRun(runId) {
  state.runId = runId;
  $("stream").innerHTML = "";
  await refreshTimeline();
}

async function refreshTimeline() {
  if (!state.runId) return;
  const tl = await api(`/api/runs/${state.runId}/timeline`);
  state.timeline = tl;
  // Set before anything is drawn: `wireMedia` runs while the stream is being
  // built, and it resolves filenames against this list.
  state.materials = tl.materials || [];
  renderRail(tl.rail);
  renderQuestions(tl);
  renderSpend(tl);
  renderMaterials();
  renderStream(tl);
}

/* --------------------------------------------------------------- header -- */

function renderRail(rail) {
  const host = $("rail");
  host.innerHTML = "";
  state.rail = rail;
  for (const step of rail) {
    const node = box("button", `step ${step.state}` + (step.needs_you ? " needs" : ""));
    // The icon comes from the state; the server's `mark` is a character chosen
    // for a text-only rail and is kept only as the fallback for a state this
    // file has not been taught.
    node.innerHTML =
      `<span class="mark">${ICON[step.state] || escapeHtml(step.mark)}</span>` +
      `<span class="name">${escapeHtml(step.label)}</span>`;
    // The four situations, in his words. It used to say 「还没有内容」 for all of
    // them, which was wrong for three: an attempt that failed and a document
    // that came out empty need different things from him than a step nobody has
    // started.
    node.title = step.blank.text;
    node.onclick = () => openStage(step.stage);
    host.appendChild(node);
  }
}

/* ---------------------------------------------------------- the questions -- */

/* The one part of the page that is a request rather than a record.
 *
 * Every stage writes what it could not settle; until now those went into the
 * document's `limitations` as a disclaimer he scrolled past. They are collected
 * here and stay until the stage is rewritten without them — there is no dismiss
 * button, because a question he has not answered is still open whether or not he
 * has read it.
 *
 * The strip itself is never hidden, only the list under it: a count of zero
 * means nothing is waiting, and a count of nine that he has collapsed still says
 * nine.
 */

function renderQuestions(tl) {
  const strip = $("questions");
  const list = $("questions-list");
  const questions = tl.questions || [];
  list.innerHTML = "";

  if (!questions.length) {
    strip.hidden = true;
    return;
  }
  strip.hidden = false;

  const byStage = new Map();
  for (const q of questions) {
    if (!byStage.has(q.numbered)) byStage.set(q.numbered, []);
    byStage.get(q.numbered).push(q);
  }

  $("questions-count").textContent = `${questions.length} 个问题等你回答`;
  $("questions-where").textContent = [...byStage.keys()].join(" · ");

  for (const q of questions) {
    const item = box("div", "q");
    item.appendChild(box("div", "q-where", q.numbered));
    item.appendChild(box("div", "q-text", q.question));
    if (q.blocks) item.appendChild(box("div", "q-blocks", `卡住了：${q.blocks}`));
    list.appendChild(item);
  }
  list.hidden = !state.questionsOpen;
  $("questions-toggle").setAttribute("aria-expanded", String(state.questionsOpen));
}

$("questions-toggle").onclick = () => {
  state.questionsOpen = !state.questionsOpen;
  $("questions-list").hidden = !state.questionsOpen;
  $("questions-toggle").setAttribute("aria-expanded", String(state.questionsOpen));
  $("questions-caret").textContent = state.questionsOpen ? "收起" : "展开";
};

function renderSpend(tl) {
  $("spend").textContent = tl.spend_sentence;
  const panel = $("spend-panel");
  const p = tl.budget.pricing;
  panel.innerHTML = "";
  panel.append(
    box("div", "spend-line", tl.spend_sentence),
    box("div", "spend-line", tl.all_runs_sentence),
    box("div", "spend-note", `美元是按价目表估算的，不是账单。价目来源：${p.source}（${p.as_of}）`),
    box("div", "spend-note", p.note)
  );
}

$("spend").onclick = () => {
  const panel = $("spend-panel");
  panel.hidden = !panel.hidden;
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
  $("materials-toggle").textContent = list.length ? `素材 · ${usable.length}` : "素材";
  $("materials-count").textContent = list.length
    ? `${usable.length} 个能用 · 共 ${list.length} 个文件`
    : "还没有素材";

  const grid = $("materials-grid");
  grid.innerHTML = "";
  if (!list.length) {
    grid.appendChild(
      box("p", "materials-empty", "这个项目手上没有任何图片或视频。没有素材是个答案——上面那句「把图拖进来」就是唯一的下一步。")
    );
    return;
  }
  for (const m of list) grid.appendChild(materialTile(m));
  wireMedia(grid);
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
  state.materialsOpen = open == null ? !state.materialsOpen : open;
  $("materials").hidden = !state.materialsOpen;
  $("materials-toggle").setAttribute("aria-expanded", String(state.materialsOpen));
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
        `/api/runs/${encodeURIComponent(state.runId)}/materials`,
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

  await refreshTimeline();
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
$("viewer-close").onclick = closeViewer;
$("viewer").addEventListener("click", (e) => {
  if (e.target === $("viewer")) closeViewer();
});

/* ----------------------------------------------------------- the stream -- */

function renderStream(tl) {
  const host = $("stream");
  host.innerHTML = "";

  if (!tl.turns.length) {
    const empty = box("div", "empty-state");
    empty.append(
      box("h2", null, "还没有开始"),
      box("p", null, "在下面直接说你要做什么。主 Agent 会先跟你把需求问清楚，再决定派谁去做。")
    );
    host.appendChild(empty);
    return;
  }

  for (const turn of tl.turns) host.appendChild(renderTurn(turn));
  // After the whole stream exists, not per card: a picture an artifact names is
  // only resolvable once `state.materials` is loaded, and doing it here means
  // one walk instead of one per card.
  wireMedia(host);
  if (state.atBottom) host.scrollTop = host.scrollHeight;
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
    else if (item.kind === "artifact") wrap.appendChild(renderArtifact(item));
    else if (item.kind === "approval") wrap.appendChild(renderApproval(item));
    else if (item.kind === "decision_request") wrap.appendChild(renderDecision(item));
  }

  if (turn.reply) {
    const bubble = box("div", "msg assistant");
    const who = box("div", "who", "主 Agent");
    const body = box("div", "body");
    body.innerHTML = renderMarkdown(turn.reply.text);
    bubble.append(who, body);
    wrap.appendChild(bubble);
  }

  const detail = renderMachineDetail(turn.detail);
  if (detail) wrap.appendChild(detail);

  return wrap;
}

function renderNote(item) {
  return box("div", "note", item.text);
}

function renderArtifact(item) {
  const card = box("div", "card artifact");
  const head = box("div", "card-head");
  head.append(
    box("span", "card-title", `${item.numbered} · ${item.version_label}`),
    box("span", "card-by", `${item.author}交回`)
  );
  card.appendChild(head);
  if (item.summary) card.appendChild(box("p", "card-summary", item.summary));

  // The document itself, laid out, in the stream where he is already looking.
  // It used to be a summary and a button: the three options, the attributes, the
  // dependency matrix and the seven open questions were all in the file and none
  // of them were on the screen without opening a reader and scrolling.
  if (item.blocks && item.blocks.length) {
    card.appendChild(blocksElement(item.blocks));
  } else {
    // The model produced no shape for this stage, so there is nothing to lay
    // out. Said out loud rather than left as a suspiciously empty card: a blank
    // panel and a panel that failed to load look identical otherwise.
    card.appendChild(box("p", "blk-none", "这一版没有可排版的内容，打开原文看正文。"));
  }

  if (item.questions && item.questions.length) {
    card.appendChild(
      box("div", "card-questions",
        `这一份里还有 ${item.questions.length} 个问题等你回答，在顶部清单里。`)
    );
  }

  if (item.review) {
    const r = item.review;
    const line = box("div", `review ${r.verdict === "pass" ? "pass" : "return"}`);
    line.append(box("span", "review-say", r.sentence));
    if (r.findings.length) {
      const more = document.createElement("details");
      more.appendChild(box("summary", null, `看这 ${r.findings.length} 条意见`));
      for (const f of r.findings) {
        const fNode = box("div", "finding");
        if (f.kind) fNode.appendChild(box("span", "finding-kind", f.kind));
        fNode.appendChild(box("span", "finding-text", f.issue));
        more.appendChild(fNode);
      }
      line.appendChild(more);
    }
    card.appendChild(line);
  } else {
    card.appendChild(box("div", "review none", "这一版还没有审核过。"));
  }

  const actions = box("div", "card-actions");
  const read = box("button", "ghost small", "看原文");
  read.onclick = () => openReader(item.stage, item.version);
  actions.appendChild(read);
  card.appendChild(actions);
  return card;
}

function renderApproval(item) {
  const card = box("div", "card approval");
  card.append(box("div", "card-title", `你的决定 · ${item.stage_label}`));
  card.appendChild(box("p", "card-summary", item.sentence));
  return card;
}

function renderDecision(item) {
  const card = box("div", "card decide");

  const head = box("div", "decide-head");
  head.append(box("span", "decide-mark", "●"), box("span", null, "现在等你决定"));
  card.appendChild(head);

  card.appendChild(box("p", "decide-headline", item.headline));
  card.appendChild(box("p", "decide-body", item.body));

  if (item.findings.length) {
    const more = document.createElement("details");
    more.appendChild(box("summary", null, `审核提的 ${item.findings.length} 点`));
    for (const f of item.findings) {
      const fNode = box("div", "finding");
      if (f.kind) fNode.appendChild(box("span", "finding-kind", f.kind));
      fNode.appendChild(box("span", "finding-text", f.issue));
      more.appendChild(fNode);
    }
    card.appendChild(more);
  }

  const actions = box("div", "card-actions");
  const read = box("button", "ghost small", "看原文");
  read.onclick = () => openReader(item.stage, item.version);
  actions.appendChild(read);

  const reason = box("input", "reason");
  reason.placeholder = "写一句为什么（会记下来）";

  const approve = box("button", "good small", item.passed ? "通过" : "明知有问题仍然通过");
  const reject = box("button", "bad small", "退回重做");

  approve.onclick = () => {
    if (item.passed) return decide(item.stage, "approved", "");
    // Approving against the reviewer needs a stated reason, and the field only
    // appears once he has actually asked to do it — otherwise the form reads as
    // if a reason were required for the normal path too.
    if (reason.hidden) {
      reason.hidden = false;
      approve.textContent = "确认通过";
      reason.focus();
      return;
    }
    if (!reason.value.trim()) {
      toast("要写明理由才能这样通过。", true);
      return;
    }
    decide(item.stage, "approved", reason.value.trim());
  };
  reject.onclick = () => decide(item.stage, "returned", reason.hidden ? "" : reason.value.trim());

  reason.hidden = true;
  actions.append(reason, approve, reject);
  card.appendChild(actions);

  // The price of what this click opens, printed on the card that asks for the
  // click — the way Higgsfield prints the cost on its Generate button. Read from
  // the ledger, said as a past measurement: it is the last time this step ran,
  // not a forecast.
  if (item.next_cost_sentence) {
    const next = box("div", "decide-next");
    if (item.next_stage_label) {
      next.appendChild(
        box("div", "decide-next-what", `通过之后，轮到「${item.next_stage_label}」（${item.next_owner}）`)
      );
    }
    next.appendChild(box("div", "decide-next-cost", item.next_cost_sentence));
    card.appendChild(next);
  }
  return card;
}

function renderMachineDetail(detail) {
  if (!detail || !detail.calls) return null;
  const more = document.createElement("details");
  more.className = "machine";
  more.open = state.machineDetail;
  const tokens = detail.input_tokens + detail.output_tokens;
  more.appendChild(
    box("summary", null, `机器细节 · ${detail.calls} 次调用 · ${tokens.toLocaleString()} token`)
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

/** A rail step. A step with nothing in it says why rather than doing nothing. */
async function openStage(stage) {
  const step = (state.rail || []).find((s) => s.stage === stage);
  if (!step) return;

  if (!step.has_doc) {
    readerState = { stage, version: null };
    $("reader").hidden = false;
    $("reader-title").textContent = `${step.number}${step.label}`;
    $("reader-sub").textContent = "";
    $("reader-versions").innerHTML = "";
    const body = $("reader-body");
    body.innerHTML = "";
    body.appendChild(box("p", `blk-none ${step.blank.state}`, step.blank.text));
    return;
  }
  await openReader(stage, step.version);
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
  try {
    const { nonce } = await api(`/api/runs/${state.runId}/nonce?stage=${stage}`, { method: "POST" });
    await api(`/api/runs/${state.runId}/decision`, {
      method: "POST",
      body: JSON.stringify({
        stage,
        nonce,
        decision,
        note: decision === "returned" ? reason : "",
        override_reason: decision === "approved" ? reason : "",
      }),
    });
    toast(decision === "approved" ? "已通过。" : "已退回。");
    await refreshTimeline();
  } catch (err) {
    toast(String(err.message || err), true);
  }
}

/* ----------------------------------------------------------------- chat -- */

function liveTurn(userText) {
  const wrap = box("div", "turn live");
  const bubble = box("div", "msg user");
  bubble.append(box("div", "who", "你"), box("div", "body", userText));
  wrap.appendChild(bubble);

  const progress = box("div", "progress");
  wrap.appendChild(progress);

  const reply = box("div", "msg assistant");
  const body = box("div", "body");
  reply.append(box("div", "who", "主 Agent"), body);
  reply.hidden = true;
  wrap.appendChild(reply);

  $("stream").appendChild(wrap);
  scrollDown();
  return { progress, reply, body };
}

function scrollDown() {
  const host = $("stream");
  if (state.atBottom) host.scrollTop = host.scrollHeight;
}

$("stream").addEventListener("scroll", () => {
  const host = $("stream");
  state.atBottom = host.scrollHeight - host.scrollTop - host.clientHeight < 60;
});

async function send(text) {
  if (state.busy || !state.runId) return;
  state.busy = true;
  state.atBottom = true;
  $("send").disabled = true;

  const live = liveTurn(text);
  live.progress.appendChild(box("div", "progress-line working", "主 Agent 正在处理…"));

  try {
    const res = await fetch(`/api/runs/${state.runId}/message`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ text }),
    });
    if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let first = true;

    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const parts = buffer.split("\n\n");
      buffer = parts.pop();
      for (const part of parts) {
        const line = part.split("\n").find((l) => l.startsWith("data: "));
        if (!line) continue;
        first = handleEvent(JSON.parse(line.slice(6)), live, first);
      }
    }
  } catch (err) {
    live.progress.appendChild(box("div", "progress-line fail", String(err.message || err)));
  } finally {
    state.busy = false;
    $("send").disabled = false;
    // The persisted turn is the source of truth; re-rendering from it means the
    // live view can never drift from what is on disk.
    await refreshTimeline();
  }
}

function handleEvent(ev, live, first) {
  if (ev.type === "done") {
    if (ev.text) {
      live.body.innerHTML = renderMarkdown(ev.text);
      live.reply.hidden = false;
    }
    if (ev.stopped_because === "max_rounds") {
      live.progress.appendChild(
        box("div", "progress-line fail", "它这一轮步骤太多被叫停了，可能没做完。")
      );
    }
    return false;
  }
  if (ev.type === "error") {
    live.progress.appendChild(box("div", "progress-line fail", ev.message));
    return first;
  }
  if (ev.type === "blocked") {
    live.progress.appendChild(box("div", "progress-line fail", ev.message));
    return first;
  }

  // The server already turned this event into Chinese; draw it as-is.
  if (ev.say) {
    if (first) {
      live.progress.innerHTML = "";
      first = false;
    }
    live.progress.appendChild(box("div", "progress-line", ev.say));
    scrollDown();
  }
  if (state.machineDetail && ev.type === "agent_tool") {
    const who = ev.role === "orchestrator" ? "" : `[${ev.role}] `;
    live.progress.appendChild(
      box("div", "progress-line raw", `${ev.ok ? "✓" : "✗"} ${who}${ev.tool}`)
    );
  }
  return first;
}

/* ---------------------------------------------------------------- input -- */

function submit() {
  const value = $("chat-input").value.trim();
  if (!value || !state.runId || state.busy) return;
  $("chat-input").value = "";
  $("chat-input").style.height = "auto";
  send(value);
}

$("send").onclick = submit;
$("chat-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
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
  $("detail-toggle").textContent = state.machineDetail ? "隐藏机器细节" : "显示机器细节";
  if (state.timeline) renderStream(state.timeline);
};

/* -------------------------------------------------------- new project -- */

function newRunDialog() {
  const dialog = document.createElement("dialog");
  dialog.innerHTML = `
    <form method="dialog">
      <h2>新建项目</h2>
      <p class="muted">起个你认得出的名字就行。编号由系统生成，不用你取。</p>
      <label>这个项目叫什么
        <input id="nr-title" placeholder="保温午餐包 15 秒" required></label>
      <label>你要做什么（照你平时说话写，不用整理成需求文档）
        <textarea id="nr-brief" rows="6"
          placeholder="例：给已经采购的保温午餐包做一条 15 秒的短视频，投 TikTok Shop 美国线。"></textarea></label>
      <div class="dialog-actions">
        <button value="cancel" class="ghost">取消</button>
        <button value="ok" class="primary">建立</button>
      </div>
    </form>`;
  document.body.appendChild(dialog);
  dialog.addEventListener("close", async () => {
    if (dialog.returnValue === "ok") {
      const title = $("nr-title").value.trim();
      const brief = $("nr-brief").value.trim();
      if (title && brief) {
        try {
          const run = await api("/api/runs", {
            method: "POST",
            body: JSON.stringify({ run_id: autoRunId(), title, brief }),
          });
          await loadRuns();
          $("run-select").value = run.run_id;
          await selectRun(run.run_id);
          toast("项目已建立。在下面说要做什么就行。");
        } catch (err) {
          toast(String(err.message || err), true);
        }
      }
    }
    dialog.remove();
  });
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

$("new-run").onclick = newRunDialog;
$("run-select").onchange = (e) => selectRun(e.target.value);

/* ----------------------------------------------------------------- init -- */

(async function init() {
  try {
    await loadRuns();
    if (state.runId) await selectRun(state.runId);
    else $("stream").appendChild(box("p", "empty-state", "还没有项目。点右上角「＋ 新项目」开始。"));
  } catch (err) {
    toast(String(err.message || err), true);
  }
})();
