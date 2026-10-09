/* Minimal Markdown renderer. No dependencies, no build step.
 *
 * Why hand-written: this is a local tool with no network dependency, and a CDN
 * would make the workbench fail without internet. The subset below covers what
 * the role agents actually produce — headings, bold, inline code, nested lists,
 * tables, quotes, rules.
 *
 * SECURITY — the reason this file is worth reading carefully:
 * artifact text is model output, and it lands in innerHTML. Everything is
 * escaped BEFORE any tag is introduced, so a document containing
 * `<script>alert(1)</script>` renders as visible text and produces no element.
 * The inline pass therefore may not receive raw text: `inline()` takes escaped
 * input and is only ever handed `escapeHtml(...)` output. If you add a rule
 * here, keep that ordering.
 *
 * Degrading is fine; injecting is not. Anything unrecognised becomes a
 * paragraph of escaped text.
 */
"use strict";

const ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

function escapeHtml(value) {
  return String(value == null ? "" : value).replace(/[&<>"']/g, (c) => ESCAPES[c]);
}

/** Inline formatting. `s` MUST already be escaped. */
function inline(s) {
  // Code spans are pulled out first so that ** inside them is not bolded.
  const codes = [];
  s = s.replace(/`([^`]+)`/g, (_, code) => {
    codes.push(code);
    return `\u0000${codes.length - 1}\u0000`;
  });

  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, "$1<em>$2</em>");

  // Links: only http(s) become anchors. Anything else (javascript:, data:) is
  // left as literal text — an artifact must not be able to produce a live link
  // to a scheme that executes.
  s = s.replace(/\[([^\]]*)\]\(([^)\s]+)\)/g, (whole, label, href) =>
    /^https?:\/\//i.test(href)
      ? `<a href="${href}" target="_blank" rel="noopener noreferrer">${label}</a>`
      : whole
  );

  // The NUL sentinel cannot occur in escaped body text, so this cannot
  // fire on ordinary prose. (A space-and-digit placeholder was tried
  // first; it matched every number in a sentence like "in 3 of 5").
  if (codes.length) {
    s = s.replace(/\u0000(\d+)\u0000/g, (_, i) => `<code>${codes[Number(i)]}</code>`);
  }
  return s;
}

function renderMarkdown(source, { scrollTables = false } = {}) {
  const lines = String(source == null ? "" : source).replace(/\r\n?/g, "\n").split("\n");
  const out = [];
  let para = [];
  let list = null; // { ordered, depth } stack is simulated below
  let table = null;
  let fence = null;

  const flushPara = () => {
    if (para.length) {
      out.push(`<p>${inline(escapeHtml(para.join("\n"))).replace(/\n/g, "<br>")}</p>`);
      para = [];
    }
  };
  const flushList = () => {
    while (list) {
      out.push(list.ordered ? "</ol>" : "</ul>");
      list = list.parent;
    }
  };
  const flushTable = () => {
    if (!table) return;
    const head = `<tr>${table.head.map((c) => `<th${scrollTables ? ' scope="col"' : ""}>${inline(escapeHtml(c))}</th>`).join("")}</tr>`;
    const body = table.rows
      .map((r) => `<tr>${r.map((c) => `<td>${inline(escapeHtml(c))}</td>`).join("")}</tr>`)
      .join("");
    const markup = `<table><thead>${head}</thead><tbody>${body}</tbody></table>`;
    out.push(scrollTables
      ? `<p class="markdown-table-hint">表格较宽时，可左右滚动查看</p><div class="markdown-table-scroll" tabindex="0" role="region" aria-label="表格，可用左右方向键滚动">${markup}</div>`
      : markup);
    table = null;
  };
  const flushAll = () => {
    flushPara();
    flushList();
    flushTable();
  };

  const splitRow = (line) =>
    line.replace(/^\s*\|/, "").replace(/\|\s*$/, "").split("|").map((c) => c.trim());

  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];
    const trimmed = line.trim();

    if (fence) {
      if (/^```/.test(trimmed)) {
        out.push(`<pre><code>${escapeHtml(fence.lines.join("\n"))}</code></pre>`);
        fence = null;
      } else {
        fence.lines.push(line);
      }
      continue;
    }
    if (/^```/.test(trimmed)) {
      flushAll();
      fence = { lines: [] };
      continue;
    }

    if (!trimmed) {
      flushAll();
      continue;
    }

    // Table: a pipe row whose next line is a separator row.
    if (/^\|/.test(trimmed) && !table) {
      const next = (lines[i + 1] || "").trim();
      if (/^\|?[\s:|-]+\|[\s:|-]*$/.test(next) && next.includes("-")) {
        table = { head: splitRow(trimmed), rows: [] };
        i += 1;
        continue;
      }
    }
    if (table && /^\|/.test(trimmed)) {
      table.rows.push(splitRow(trimmed));
      continue;
    }
    if (table) flushTable();

    let m = trimmed.match(/^(#{1,6})\s+(.*)$/);
    if (m) {
      flushAll();
      const level = Math.min(m[1].length, 6);
      out.push(`<h${level}>${inline(escapeHtml(m[2]))}</h${level}>`);
      continue;
    }

    if (/^(---+|\*\*\*+|___+)$/.test(trimmed)) {
      flushAll();
      out.push("<hr>");
      continue;
    }

    if (/^>\s?/.test(trimmed)) {
      flushAll();
      const quote = [];
      while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
        quote.push(lines[i].replace(/^\s*>\s?/, ""));
        i += 1;
      }
      i -= 1;
      out.push(`<blockquote>${inline(escapeHtml(quote.join("\n"))).replace(/\n/g, "<br>")}</blockquote>`);
      continue;
    }

    const bullet = line.match(/^(\s*)[-*+]\s+(.*)$/);
    const ordered = line.match(/^(\s*)\d+[.)]\s+(.*)$/);
    if (bullet || ordered) {
      const depth = Math.floor((bullet || ordered)[1].replace(/\t/g, "  ").length / 2);
      const isOrdered = Boolean(ordered);
      const text = (bullet || ordered)[2];
      flushPara();

      if (!list) {
        out.push(isOrdered ? "<ol>" : "<ul>");
        list = { ordered: isOrdered, depth, parent: null };
      } else {
        // Walk back up, then down, opening/closing one level at a time.
        while (list && depth < list.depth) {
          out.push(list.ordered ? "</ol>" : "</ul>");
          list = list.parent;
        }
        while (list && depth > list.depth) {
          out.push(isOrdered ? "<ol>" : "<ul>");
          list = { ordered: isOrdered, depth: list.depth + 1, parent: list };
        }
        if (!list) {
          out.push(isOrdered ? "<ol>" : "<ul>");
          list = { ordered: isOrdered, depth, parent: null };
        } else if (list.ordered !== isOrdered) {
          // A type change at the same depth is a new list, not a broken one.
          out.push(list.ordered ? "</ol>" : "</ul>");
          out.push(isOrdered ? "<ol>" : "<ul>");
          list.ordered = isOrdered;
        }
      }
      out.push(`<li>${inline(escapeHtml(text))}</li>`);
      continue;
    }

    flushList();
    para.push(line);
  }

  if (fence) out.push(`<pre><code>${escapeHtml(fence.lines.join("\n"))}</code></pre>`);
  flushAll();
  return out.join("\n");
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { renderMarkdown, escapeHtml, inline };
}
