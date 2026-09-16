/* Tests for the Markdown renderer.
 *
 *   node --test apps/us-video-studio/tests/js/
 *
 * The renderer is the one place where model-generated text becomes HTML, so the
 * escaping tests are the point of this file, not an afterthought. The rest check
 * that the documents the role agents actually write come out readable.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { renderMarkdown, escapeHtml } = require("../../studio/web/md.js");

/* ------------------------------------------------------------- security -- */

test("a script tag in a document renders as text, not as an element", () => {
  const html = renderMarkdown("前 3 秒钩子\n\n<script>alert(1)</script>\n\n结束");
  assert.ok(!html.includes("<script"), "no script element may be produced");
  assert.ok(html.includes("&lt;script&gt;"), "it should be visible as literal text");
});

test("an img onerror payload cannot become an element", () => {
  const html = renderMarkdown('<img src=x onerror="alert(1)">');
  assert.ok(!html.includes("<img"), "no img element may be produced");
  assert.ok(html.includes("&lt;img"));
});

test("an attribute-break attempt inside bold cannot escape the tag", () => {
  const html = renderMarkdown('**"><script>alert(1)</script>**');
  assert.ok(!html.includes("<script"));
  assert.ok(html.includes("<strong>"));
});

test("a javascript: link is not made clickable", () => {
  const html = renderMarkdown("[点我](javascript:alert(1))");
  assert.ok(!html.includes("<a "), "javascript: must not produce an anchor");
  assert.ok(!html.includes("href"));
});

test("a data: link is not made clickable", () => {
  const html = renderMarkdown("[x](data:text/html;base64,PHNjcmlwdD4=)");
  assert.ok(!html.includes("<a "));
});

test("http and https links are clickable and open detached", () => {
  const html = renderMarkdown("货源 [1688 链接](https://detail.1688.com/offer/1)");
  assert.ok(html.includes('href="https://detail.1688.com/offer/1"'));
  assert.ok(html.includes('rel="noopener noreferrer"'));
});

test("escaped text is escaped exactly once", () => {
  assert.equal(escapeHtml("a & b < c"), "a &amp; b &lt; c");
  // Already-escaped input must not be double-escaped into &amp;amp;
  assert.equal(renderMarkdown("&lt;script&gt;"), "<p>&amp;lt;script&amp;gt;</p>");
});

/* ---------------------------------------------------------- formatting -- */

test("headings of several levels", () => {
  const html = renderMarkdown("# 一\n\n## 二\n\n### 三");
  assert.ok(html.includes("<h1>一</h1>"));
  assert.ok(html.includes("<h2>二</h2>"));
  assert.ok(html.includes("<h3>三</h3>"));
});

test("bold and italic", () => {
  const html = renderMarkdown("**角度**：主张「值得被看见」");
  assert.ok(html.includes("<strong>角度</strong>"));
  assert.ok(html.includes("主张「值得被看见」"));
});

test("bullet lists and nested bullets", () => {
  const html = renderMarkdown("- 一层\n  - 二层\n- 又一层");
  assert.ok(html.includes("<ul>"));
  assert.ok(html.includes("<li>一层</li>"));
  assert.ok(html.includes("<li>二层</li>"));
  assert.equal((html.match(/<ul>/g) || []).length, 2, "nesting opens a second list");
  assert.equal((html.match(/<\/ul>/g) || []).length, 2, "and closes it");
});

test("numbered lists", () => {
  const html = renderMarkdown("1. 先看状态\n2. 再决定");
  assert.ok(html.includes("<ol>"));
  assert.ok(html.includes("<li>先看状态</li>"));
});

test("a table with a header row", () => {
  const html = renderMarkdown("| 项 | 值 |\n| --- | --- |\n| 容量 | 15L / 25L |");
  assert.ok(html.includes("<th>项</th>"));
  assert.ok(html.includes("<td>15L / 25L</td>"));
});

test("a horizontal rule", () => {
  assert.equal(renderMarkdown("上\n\n---\n\n下"), "<p>上</p>\n<hr>\n<p>下</p>");
});

test("a blockquote", () => {
  const html = renderMarkdown("> 未回答项：主推 SKU");
  assert.ok(html.includes("<blockquote>未回答项：主推 SKU</blockquote>"));
});

test("a fenced code block keeps its content literal", () => {
  const html = renderMarkdown("```\n**not bold**\n```");
  assert.ok(html.includes("<pre><code>**not bold**</code></pre>"));
});

/* --------------------------------------------- the placeholder regression -- */

test("numbers in ordinary prose are left alone", () => {
  // Regression: an earlier placeholder used spaces and digits, so every number
  // in a sentence like this one was replaced by a code span reading undefined.
  const html = renderMarkdown("在 3 个颜色和 5 个容量档里挑，共 90 件");
  assert.ok(!html.includes("undefined"), "must not substitute for a code span");
  assert.ok(html.includes("在 3 个颜色和 5 个容量档里挑，共 90 件"));
});

test("an inline code span is kept and not bolded inside", () => {
  const html = renderMarkdown("字段 `**不要**变粗` 结束");
  assert.ok(html.includes("<code>**不要**变粗</code>"));
  assert.ok(!html.includes("<strong>"));
});

test("a document mixing everything does not throw", () => {
  const doc = [
    "# direction v2 — 午餐包",
    "",
    "先说清楚口径：",
    "",
    "- 只看过 intake-v1。**没打开链接**。",
    "  - 材质不可用",
    "  - 保温不可用",
    "",
    "---",
    "",
    "## 1. 三条选项",
    "",
    "| 选项 | 依赖 |",
    "| --- | --- |",
    "| A | 官方图 |",
    "",
    "> 未决问题：主推 SKU",
    "",
    "1. 第一",
    "2. 第二",
  ].join("\n");
  const html = renderMarkdown(doc);
  for (const want of ["<h1>", "<h2>", "<ul>", "<table>", "<blockquote>", "<ol>", "<hr>"]) {
    assert.ok(html.includes(want), `expected ${want} in output`);
  }
});

test("empty and null input produce nothing rather than throwing", () => {
  assert.equal(renderMarkdown(""), "");
  assert.equal(renderMarkdown(null), "");
  assert.equal(renderMarkdown(undefined), "");
});
