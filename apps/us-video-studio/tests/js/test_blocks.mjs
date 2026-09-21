/* Tests for the block renderer.
 *
 *   node --test apps/us-video-studio/tests/js/
 *
 * Same reason as test_md.mjs: this is the second place where model-generated
 * text becomes HTML, so the escaping tests are the point of the file. The rest
 * check that each of the six shapes comes out as the element its CSS expects,
 * and that a shape this file has never heard of renders as nothing rather than
 * as a broken panel.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);

/* In the browser md.js is a classic script, so its top-level `escapeHtml` is a
 * global and `blocks.js` calls it as one. In Node it is module-scoped, so the
 * dependency has to be restored here — deliberately, rather than by copying the
 * function, so these tests exercise the same `escapeHtml` the page uses. */
const { escapeHtml } = require("../../studio/web/md.js");
globalThis.escapeHtml = escapeHtml;

const { blocksHtml } = require("../../studio/web/blocks.js");

const one = (block) => blocksHtml([block]);

test("collapsed evidence preserves and escapes every field", () => {
  const html = one({type: 'cards', cards: [{head: 'A', fields: [
    {label: '主张', value: '主要内容'},
    {label: '依据', value: '<img src=x onerror=alert(1)>', secondary: true},
  ]}]});
  assert.ok(html.includes('主要内容'));
  assert.ok(html.includes('<details class="vcard-evidence">'));
  assert.ok(html.includes('&lt;img'));
  assert.ok(!html.includes('<img'));
});

test("collapsed blocks preserve content without an open attribute", () => {
  const html = one({type: 'text', title: '<script>title</script>', text: '完整依据', collapsed: true});
  assert.ok(html.includes('<details class="blk blk-text">'));
  assert.ok(html.includes('&lt;script&gt;title&lt;/script&gt;'));
  assert.ok(html.includes('完整依据'));
});

/* ------------------------------------------------------------- security -- */

test("a script tag in a table cell renders as text, not as an element", () => {
  const html = one({
    type: "table",
    columns: ["项目"],
    rows: [["<script>alert(1)</script>"]],
  });
  assert.ok(!html.includes("<script"), "no script element may be produced");
  assert.ok(html.includes("&lt;script&gt;"), "it should be visible as literal text");
});

test("a payload in a card field cannot become an element", () => {
  const html = one({
    type: "cards",
    cards: [{ head: "A", fields: [{ label: "画面", value: '<img src=x onerror="alert(1)">' }] }],
  });
  assert.ok(!html.includes("<img"), "no img element may be produced");
  assert.ok(html.includes("&lt;img"));
});

test("a block type carrying markup produces no tag at all", () => {
  // `type` is the one value that lands in an attribute (`class`). It reaches
  // there only for the six shapes the switch names; anything else is rejected
  // before the wrapper is written, so a hostile type cannot get near a tag.
  // The escaping in `blockHtml` is belt-and-braces behind that.
  const html = one({ type: '" onmouseover="alert(1)', text: "还在" });
  assert.equal(html, "", "an unrecognised type must produce no element");
  assert.ok(!html.includes("onmouseover"));
});

test("an option letter cannot inject a width into the matrix style", () => {
  // Matrix columns are model-written; the grid tracks come from the payload's
  // *length*, never from its text. The check is that every style attribute the
  // renderer writes is the one it computed, with no payload text in it.
  const html = one({
    type: "matrix",
    columns: ['A"); background:url(x'],
    rows: [{ label: "实拍官方图", cells: [true] }],
  });
  const styles = [...html.matchAll(/style="([^"]*)"/g)].map((m) => m[1]);
  assert.equal(styles.length, 2, "one head row and one body row carry the tracks");
  for (const style of styles) {
    assert.equal(style, "grid-template-columns: minmax(8rem, 2fr) repeat(1, minmax(3rem, 1fr))");
  }
  // The payload is still shown — as text inside a cell, where it is inert.
  assert.ok(html.includes("A&quot;); background:url(x"));
});

test("a list item's note is escaped separately from its text", () => {
  const html = one({
    type: "list",
    items: [{ text: "<b>缺口</b>", note: "<i>会导致什么</i>" }],
  });
  assert.ok(!html.includes("<b>"));
  assert.ok(!html.includes("<i>"));
  assert.ok(html.includes("&lt;b&gt;缺口&lt;/b&gt;"));
  assert.ok(html.includes("&lt;i&gt;"));
});

/* ---------------------------------------------------------------- shapes -- */

test("no blocks renders as an empty string", () => {
  assert.equal(blocksHtml([]), "");
  assert.equal(blocksHtml(null), "");
  assert.equal(blocksHtml(undefined), "");
});

test("an unknown block type renders as nothing", () => {
  // A newer server sending a shape this file predates must degrade to a missing
  // section. Not to `[object Object]`, and not to a thrown error that would
  // leave the rest of the document undrawn.
  const html = blocksHtml([
    { type: "gantt", title: "排期", rows: [1, 2] },
    { type: "text", text: "后面的还在" },
  ]);
  assert.ok(!html.includes("gantt"));
  assert.ok(!html.includes("[object"));
  assert.ok(html.includes("后面的还在"), "one unknown block must not drop the others");
});

test("a text block keeps its title and its paragraph", () => {
  const html = one({ type: "text", title: "总时长", text: "30 秒" });
  assert.ok(html.includes('<h4 class="blk-title">总时长</h4>'));
  assert.ok(html.includes('<p class="blk-text">30 秒</p>'));
});

test("a table emits a head row and one row per record, in order", () => {
  const html = one({
    type: "table",
    columns: ["项目", "内容"],
    rows: [
      ["货源", "义乌"],
      ["采购量", "500"],
    ],
  });
  const rows = html.split('<div class="tbl-row').length - 1;
  assert.equal(rows, 3, "one head row plus two body rows");
  assert.ok(html.includes('<div class="tbl-row head">'));
  assert.ok(html.indexOf("义乌") < html.indexOf("500"), "row order follows the payload");
});

test("a matrix marks a dead cell and dashes a live one", () => {
  const html = one({
    type: "matrix",
    columns: ["A", "B"],
    rows: [{ label: "实拍官方图", cells: [true, false] }],
  });
  assert.ok(html.includes('<div class="tbl-cell center hit">✕</div>'));
  assert.ok(html.includes('<div class="tbl-cell center miss">—</div>'));
  assert.ok(html.includes("这个空白不解决，这个方案就站不住"), "the legend explains the glyphs");
});

test("a matrix sizes its columns from the payload's length", () => {
  const html = one({ type: "matrix", columns: ["A", "B", "C"], rows: [] });
  assert.ok(
    html.includes("repeat(3, minmax(3rem, 1fr))"),
    "three options should produce three tracks"
  );
});

test("a card shows its head, its chip and its fields in order", () => {
  const html = one({
    type: "cards",
    cards: [
      {
        head: "③",
        chip: { text: "无法获得", tone: "bad" },
        fields: [{ label: "画面", value: "仓库实拍" }],
      },
    ],
  });
  assert.ok(html.includes('<span class="vcard-head">③</span>'));
  assert.ok(html.includes('<span class="status bad">无法获得</span>'));
  assert.ok(html.indexOf("vcard-head") < html.indexOf("status bad"), "head before chip");
  assert.ok(html.indexOf("vf-label") < html.indexOf("vf-value"), "label above its value");
});

test("a card with no chip emits no status span", () => {
  const html = one({ type: "cards", cards: [{ head: "A", fields: [] }] });
  assert.ok(!html.includes("status"), "an unknown status must not be invented");
});

test("emphasis becomes a class, and nothing else does", () => {
  const html = one({
    type: "cards",
    cards: [{ head: "B", emphasis: "chosen", fields: [] }],
  });
  assert.ok(html.includes('<div class="vcard chosen">'));
});

test("a callout quotes the text and lists what has to be confirmed", () => {
  const html = one({
    type: "callout",
    title: "推荐",
    text: "B —— 因为 A 的前提还没确认",
    items_title: "这个推荐要成立，先得确认：",
    items: [{ text: "有没有官方图" }],
  });
  assert.ok(html.includes('<h4 class="blk-title">推荐</h4>'));
  assert.ok(html.includes("B —— 因为 A 的前提还没确认"));
  assert.ok(html.includes("这个推荐要成立，先得确认："));
  assert.ok(html.includes("<li>有没有官方图</li>"));
});

test("a callout with no prerequisites renders no sub-heading", () => {
  const html = one({ type: "callout", title: "推荐", text: "B", items: [] });
  assert.ok(!html.includes("blk-sub"), "an empty list must not leave a dangling heading");
});

test("a list item without a note renders as one line", () => {
  const html = one({ type: "list", items: [{ text: "未确认的观众" }] });
  assert.ok(html.includes('<span class="blk-item">未确认的观众</span>'));
  assert.ok(!html.includes("blk-note"));
});

/* -------------------------------------------------- the whole document -- */

test("the blocks of a direction render in the order the server sent them", () => {
  // The stage's shape is decided in Python. This file must not reorder it.
  const html = blocksHtml([
    { type: "cards", title: "3 个方向，摊开比", cards: [] },
    { type: "callout", title: "推荐", text: "B" },
    { type: "matrix", title: "这几个空白不解决，哪些方向站不住", columns: ["A"], rows: [] },
  ]);
  const cards = html.indexOf("个方向，摊开比");
  const callout = html.indexOf("推荐");
  const matrix = html.indexOf("哪些方向站不住");
  assert.ok(cards < callout && callout < matrix, "the server's order is the drawn order");
});

/* -------------------------------------------------------------- media -- */

test("a card that names a file gets a placeholder for it, not an image", () => {
  // The renderer cannot know whether the file exists, and it does not know the
  // run id, so it emits the name and lets app.js resolve it. Drawing an <img>
  // here would put a broken picture in a card that never admits the file is
  // missing.
  const html = one({
    type: "cards",
    cards: [{ head: "01", media: "O1CN01XrQX.jpg", fields: [] }],
  });
  assert.ok(html.includes('data-media="O1CN01XrQX.jpg"'));
  assert.ok(!html.includes("<img"));
  assert.ok(!html.includes("<video"));
});

test("a card with no file gets no media placeholder", () => {
  const html = one({ type: "cards", cards: [{ head: "01", fields: [] }] });
  assert.ok(!html.includes("vcard-media"));
});

test("a quote in a filename cannot break out of the attribute", () => {
  const html = one({
    type: "cards",
    cards: [{ head: "01", media: 'x" onerror="alert(1)', fields: [] }],
  });
  assert.ok(html.includes('data-media="x&quot; onerror=&quot;alert(1)"'));
  assert.ok(!html.includes('onerror="alert'));
});

/* ----------------------------------------------------------- timeline -- */

test("a storyboard renders one bar per shot, in order", () => {
  const html = one({
    type: "timeline",
    segments: [
      { tc: "0:00–0:03", seconds: 3, label: "两只包并排" },
      { tc: "0:03–0:07", seconds: 4, label: "25L 装得下" },
    ],
  });
  assert.equal(html.split("tl-seg").length - 1, 2);
  assert.ok(html.indexOf("0:00–0:03") < html.indexOf("0:03–0:07"));
});

test("a bar is as wide as its shot is long", () => {
  const html = one({
    type: "timeline",
    segments: [
      { tc: "0:00–0:03", seconds: 3, label: "短" },
      { tc: "0:03–0:07", seconds: 4, label: "长" },
      { tc: "0:07–0:15", seconds: 8, label: "更长" },
    ],
  });
  assert.ok(html.includes('style="flex-grow:3"'));
  assert.ok(html.includes('style="flex-grow:4"'));
  assert.ok(html.includes('style="flex-grow:8"'));
});

test("a shot with no stated length still gets a bar, and admits it is a guess", () => {
  // A zero-width bar would drop the shot out of the strip, which would read as
  // "there is no such shot" rather than "nobody said how long it is".
  const html = one({
    type: "timeline",
    segments: [
      { tc: "0:00–0:03", seconds: 3, label: "有长度" },
      { tc: "然后", seconds: null, label: "没长度" },
    ],
  });
  assert.equal(html.split("tl-seg").length - 1, 2, "both shots are drawn");
  assert.ok(html.includes("tl-seg unknown"));
  assert.ok(html.includes("没长度"));
});

test("a storyboard with no shots renders as nothing at all", () => {
  assert.equal(one({ type: "timeline", segments: [] }), "");
  assert.equal(one({ type: "timeline" }), "");
});

test("a script tag in a timecode or a line renders as text", () => {
  const html = one({
    type: "timeline",
    total: '<img src=x onerror="alert(1)">',
    segments: [{ tc: "<script>alert(1)</script>", seconds: 3, label: "<b>粗</b>" }],
  });
  assert.ok(!html.includes("<script"));
  assert.ok(!html.includes("<b>"));
  assert.ok(!html.includes("<img src=x"));
  assert.ok(html.includes("&lt;script&gt;"));
});

test("a quote in a storyboard filename cannot break out of the attribute", () => {
  const html = one({
    type: "timeline",
    segments: [{ tc: "0:00", seconds: 1, label: "x", media: 'a" onload="alert(1)' }],
  });
  assert.ok(html.includes('data-media="a&quot; onload=&quot;alert(1)"'));
  assert.ok(!html.includes('onload="alert'));
});

test("the reference frame is its own slot, so filling it cannot erase the shot", () => {
  // `wireMedia` in app.js fills a `[data-media]` slot by clearing it first. If
  // that attribute sat on the bar, resolving the picture would delete the time
  // code and the label with it — a bar with a photo in it and no shot to name.
  const html = one({
    type: "timeline",
    segments: [{ tc: "0:00–0:03", seconds: 3, label: "两只包并排", media: "包.jpg" }],
  });
  assert.ok(html.includes('<span class="tl-thumb" data-media="包.jpg"></span>'));
  assert.ok(html.includes('<span class="tl-tc">0:00–0:03</span>'));
  assert.ok(html.includes('<span class="tl-label">两只包并排</span>'));
  // The assertion above also pins the slot as empty — `></span>` immediately
  // after the attribute. Whatever goes inside is put there later, by the one
  // place that knows whether the file exists.
});
