/* The block renderer: seven shapes, no knowledge of any stage.
 *
 * The server decides what a stage's blocks *are* — which columns the fact table
 * has, what a material board shows, which option the director recommended, how
 * long each shot is. This file only knows how to draw a list, a table, a grid
 * of cards, a tick matrix, a callout and a timeline. Adding a stage needs no
 * code here; changing a stage's shape is one edit in one language, in
 * `studio/structured.py`.
 *
 * SECURITY — same rule as md.js, and for the same reason: block text is model
 * output and it lands in innerHTML. Every cell is escaped before any tag is
 * introduced, and nothing here ever emits an attribute taken from the payload
 * except numbers it computed itself. `escapeHtml` is md.js's, not a second copy
 * — md.js must be loaded first (index.html does).
 *
 * A file the payload names is emitted as `data-media="name"` and left empty on
 * purpose. This file knows neither the data root nor the run id, so it cannot
 * build a URL; more to the point it cannot know whether the file is really
 * there. `app.js` resolves the name against the run's material list and draws
 * either the picture or 「文件不在」. Guessing here would put a broken image in
 * a document that otherwise tells the truth about what it does not have.
 *
 * Returns a string rather than building nodes so it can be tested without a
 * DOM: see tests/js/test_blocks.mjs. The escaping tests there are the point.
 */
"use strict";

function blocksHtml(blocks) {
  if (!blocks || !blocks.length) return "";
  const drawn = blocks.map(blockHtml).filter(Boolean);
  // Nothing drew — a payload from a server newer than this file. An empty
  // `.blocks` still costs its margin, so say nothing rather than leave a gap.
  if (!drawn.length) return "";
  return `<div class="blocks">${drawn.join("")}</div>`;
}

function blockHtml(b) {
  // Emptiness is checked before the wrapper is written, not after. A section
  // whose body is unknown still carries the `.blocks` gap, so emitting one and
  // filling it with nothing leaves visible dead space in the middle of the
  // document — the failed degradation this file promises not to do.
  const body = bodyHtml(b);
  if (body === null) return "";
  const title = b.title ? `<h4 class="blk-title">${escapeHtml(b.title)}</h4>` : "";
  return `<section class="blk blk-${escapeHtml(b.type)}">${title}${body}</section>`;
}

function bodyHtml(b) {
  switch (b.type) {
    case "text":
      return `<p class="blk-text">${escapeHtml(b.text)}</p>`;

    case "list":
      return `<ul class="blk-list">${(b.items || [])
        .map(
          (item) =>
            `<li><span class="blk-item">${escapeHtml(item.text)}</span>` +
            (item.note ? `<span class="blk-note">${escapeHtml(item.note)}</span>` : "") +
            `</li>`
        )
        .join("")}</ul>`;

    case "table":
      return `<div class="tbl">${tableRows(b)}</div>`;

    case "matrix":
      return `<div class="tbl matrix">${matrixRows(b)}</div>` +
        `<p class="blk-legend">✕ = 这个空白不解决，这个方案就站不住；— = 不受影响</p>`;

    case "cards":
      return `<div class="cardgrid">${(b.cards || []).map(cardHtml).join("")}</div>`;

    case "timeline":
      return timelineHtml(b);

    case "callout":
      return (
        (b.text ? `<p class="blk-text">${escapeHtml(b.text)}</p>` : "") +
        (b.items && b.items.length
          ? `<div class="blk-sub">${escapeHtml(b.items_title || "")}</div>` +
            `<ul class="blk-list">${b.items
              .map((item) => `<li>${escapeHtml(item.text)}</li>`)
              .join("")}</ul>`
          : "")
      );

    default:
      // `null`, not "": the caller has to be able to tell "this block draws to
      // nothing" from "this block draws an empty container". A newer server
      // sending a block this file predates degrades to a missing section, not
      // to `[object Object]` in the middle of a document.
      return null;
  }
}

function tableRows(b) {
  const head = `<div class="tbl-row head">${(b.columns || [])
    .map((c) => `<div class="tbl-cell">${escapeHtml(c)}</div>`)
    .join("")}</div>`;
  const rows = (b.rows || [])
    .map(
      (row) =>
        `<div class="tbl-row">${row
          .map((cell) => `<div class="tbl-cell">${escapeHtml(cell)}</div>`)
          .join("")}</div>`
    )
    .join("");
  return head + rows;
}

function matrixRows(b) {
  const columns = b.columns || [];
  // Explicit tracks, because the first column holds a sentence and the option
  // columns hold one glyph. Widths come from the payload's *length*, never from
  // its text, so nothing model-written reaches the style attribute.
  const style = ` style="grid-template-columns: minmax(8rem, 2fr) repeat(${columns.length}, minmax(3rem, 1fr))"`;

  const head = `<div class="tbl-row head"${style}><div class="tbl-cell"></div>${columns
    .map((c) => `<div class="tbl-cell center">${escapeHtml(c)}</div>`)
    .join("")}</div>`;

  const rows = (b.rows || [])
    .map((row) => {
      const cells = (row.cells || [])
        .map(
          (hit) =>
            `<div class="tbl-cell center ${hit ? "hit" : "miss"}">${hit ? "✕" : "—"}</div>`
        )
        .join("");
      return `<div class="tbl-row"${style}><div class="tbl-cell">${escapeHtml(
        row.label
      )}</div>${cells}</div>`;
    })
    .join("");
  return head + rows;
}

function cardHtml(card) {
  const emphasis = card.emphasis ? ` ${escapeHtml(card.emphasis)}` : "";
  const head = card.head ? `<span class="vcard-head">${escapeHtml(card.head)}</span>` : "";
  const status = card.chip
    ? `<span class="status ${escapeHtml(card.chip.tone)}">${escapeHtml(card.chip.text)}</span>`
    : "";
  // Drawn above the words, bleeding to the card's rounded edges — the shape
  // ComfyUI's template cards use, and the reason a picture reads as the subject
  // of the card rather than an illustration beside some text.
  const media = card.media ? `<div class="vcard-media" data-media="${escapeHtml(card.media)}"></div>` : "";
  const fields = (card.fields || [])
    .map(
      (f) =>
        `<div class="vf-label">${escapeHtml(f.label)}</div>` +
        `<div class="vf-value">${escapeHtml(f.value)}</div>`
    )
    .join("");
  return `<div class="vcard${emphasis}">${media}<div class="vcard-body"><div class="vcard-top">${head}${status}</div>${fields}</div></div>`;
}

/* The storyboard strip: one bar per shot, as wide as the shot is long.
 *
 * This is the whole reason the writing stage looks like a video tool instead of
 * a document. "15 seconds, five shots" is a sentence you have to read; five bars
 * whose widths total 15 seconds is a fact you can see, and the one thing a shot
 * list exists to answer — does this fill the time, and where is the slow part.
 *
 * Widths come from `flex-grow`, and every number in that attribute is one the
 * server computed. A shot with no stated length is given the average of the
 * shots that have one rather than a zero-width bar it would vanish into — and
 * is marked `unknown`, so the strip never claims a precision the payload lacks.
 */
function timelineHtml(b) {
  const segments = b.segments || [];
  if (!segments.length) return null;

  const seconds = segments.map((s) =>
    typeof s.seconds === "number" && s.seconds > 0 ? s.seconds : 0
  );
  const total = seconds.reduce((sum, n) => sum + n, 0);

  const bars = segments.map((s, i) => {
    const weight = total > 0 ? seconds[i] || total / segments.length : 1;
    const unknown = seconds[i] > 0 ? "" : " unknown";
    // The reference frame goes in its own span rather than on the bar, because
    // the bar already has text in it and `wireMedia` fills a slot by replacing
    // whatever it holds.
    const thumb = s.media
      ? `<span class="tl-thumb" data-media="${escapeHtml(s.media)}"></span>`
      : "";
    return (
      `<div class="tl-seg${unknown}" style="flex-grow:${weight}">` +
      thumb +
      `<span class="tl-tc">${escapeHtml(s.tc)}</span>` +
      `<span class="tl-label">${escapeHtml(s.label)}</span>` +
      `</div>`
    );
  });

  const totalLabel = b.total ? `<span class="tl-total">${escapeHtml(b.total)}</span>` : "";
  return (
    `<div class="timeline">` +
    `<div class="tl-strip">${bars.join("")}</div>` +
    `<div class="tl-foot">${totalLabel}` +
    `<span class="tl-hint">每段的宽度 = 它的时长</span></div>` +
    `</div>`
  );
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { blocksHtml };
}
