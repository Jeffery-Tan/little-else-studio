/* Tests for the stage panel.
 *
 *   node --test apps/us-video-studio/tests/js/
 *
 * Two kinds of test here, and both matter for the same reason: the author of
 * this file cannot open a browser. The escaping tests are the security ones —
 * this is the third place model output becomes HTML. The rest check the
 * boundaries this file is responsible for: which stage's gate is drawn, which
 * questions belong to which stage, and that a gate this stage has no business
 * showing is left out rather than drawn empty.
 *
 * What is NOT tested here, and cannot be: whether it looks right. That is the
 * whole reason the file returns a string.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);

/* In the browser md.js and blocks.js are classic scripts loaded before this one,
 * so `escapeHtml` and `blocksHtml` are globals and stage.js calls them as such.
 * In Node they are module-scoped, so the two dependencies are restored here —
 * deliberately, rather than by copying them, so these tests exercise the same
 * functions the page uses. */
const { escapeHtml, renderMarkdown } = require("../../studio/web/md.js");
globalThis.escapeHtml = escapeHtml;
globalThis.renderMarkdown = renderMarkdown;
const { blocksHtml } = require("../../studio/web/blocks.js");
globalThis.blocksHtml = blocksHtml;

const { stageHtml, answerKey } = require("../../studio/web/stage.js");

/* ---------------------------------------------------------------- fixtures -- */

function step(over) {
  return Object.assign(
    {
      stage: "direction",
      number: "②",
      label: "创意方向",
      mark: "●",
      state: "active",
      has_doc: true,
      version: 2,
      version_label: "第 2 版",
      summary: "三个方向，推荐 B。",
      author: "导演",
      created_at: "2026-09-17T04:05:06",
      integrity_ok: true,
      integrity_sentence: "",
      stale: false,
      stale_sentence: "",
      needs_you: true,
      blank: { state: "todo", text: "还没开始。" },
      blocks: [],
      questions: [],
      review: null,
    },
    over || {}
  );
}

function pending(over) {
  return Object.assign(
    {
      kind: "decision_request",
      stage: "direction",
      stage_label: "创意方向",
      version: 2,
      passed: true,
      headline: "「创意方向」写好了，审核也通过了。",
      body: "你看一遍，觉得可以就通过；要改就退回重做。",
      findings: [],
      next_stage_label: "素材计划",
      next_owner: "素材统筹",
      next_cost_sentence: "上一次这一步花了 $0.02。",
      options: [],
      options_hint: "",
    },
    over || {}
  );
}

const threeOptions = [
  { key: "A", recommended: false, label: "改用 A", return_note: "改用 A（导演推荐的是 B）" },
  { key: "B", recommended: true, label: "就按 B 走（导演推荐）", return_note: "" },
  { key: "C", recommended: false, label: "改用 C", return_note: "改用 C（导演推荐的是 B）" },
];

/* --------------------------------------------------------------- security -- */

test("a script tag in the summary renders as text, not as an element", () => {
  const html = stageHtml(step({ summary: "<script>alert(1)</script>" }), {});
  assert.ok(!html.includes("<script"), "no script element may be produced");
  assert.ok(html.includes("&lt;script&gt;"), "it should be visible as literal text");
});

test("a question cannot become an element", () => {
  const html = stageHtml(step(), {
    questions: [
      { stage: "direction", version: 2, question: '<img src=x onerror="alert(1)">' },
    ],
  });
  assert.ok(!html.includes("<img"), "no img element may be produced");
});

test("an attribute the payload controls cannot break out of its quotes", () => {
  /* The note is a sentence the server composed from the director's own option
   * key, so the key is upstream of an attribute value. A key holding a quote
   * must not be able to close the attribute and open a tag.
   *
   * The value is read back out rather than searched for, because that is the
   * property that matters: `[^"]*` stops at the first raw quote, so a leak shows
   * up as a truncated value, not as a phrase somebody has to think to grep for. */
  const note = '"><script>alert(1)</script>';
  const html = stageHtml(step(), {
    pending: pending({
      options: [{ key: note, recommended: false, label: "方向", return_note: "" }],
    }),
  });
  assert.ok(!html.includes("<script"), "no script element may be produced");

  const m = html.match(/name="direction-choice" value="([^"]*)"/);
  assert.ok(m, "the note is still a quoted attribute value");
  assert.equal(unescapeHtml(m[1]), note, "it must survive the round trip byte for byte");
});

function unescapeHtml(s) {
  return s
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&amp;/g, "&");
}

/* ------------------------------------------------------------------ the gate -- */

test("the gate is drawn only for the stage it is about", () => {
  const mine = stageHtml(step(), { pending: pending() });
  assert.ok(mine.includes('class="gate"'));

  const other = stageHtml(step({ stage: "writing", label: "分镜脚本" }), {
    pending: pending({ stage: "direction" }),
  });
  assert.ok(!other.includes('class="gate"'), "another stage's gate is not this stage's");
});

test("a stage with nothing waiting on him gets no gate at all", () => {
  const html = stageHtml(step(), { pending: null });
  assert.ok(!html.includes('class="gate"'));
  assert.ok(!html.includes("现在等你决定"));
});

test("all options are explicit unselected choices; adoption and revision stay distinct", () => {
  const html = stageHtml(step(), { pending: pending({options: threeOptions, artifact_sha256:'a'.repeat(64)}) });
  assert.equal((html.match(/type="radio"/g)||[]).length,3);
  assert.ok(!/\bchecked\b/.test(html));
  assert.ok(html.includes('data-act="approve"'));
  assert.ok(html.includes('data-act="reject"'));
  assert.ok(html.includes('data-sha="'+'a'.repeat(64)+'"'));
  assert.ok(html.includes('data-version="2"'));
  assert.ok(!html.includes('data-decision="returned"'));
});

test("both structured and prose-only gates require an explicit submit action", () => {
  for (const options of [threeOptions, []]) {
    const html = stageHtml(step(), { pending: pending({options}) });
    assert.ok(html.includes('data-act="approve"'));
    assert.ok(html.includes('采用并继续完善方案'));
    assert.ok(html.includes('退回并继续修改'));
  }
});

test("a document the reviewer did not pass asks the override question differently", () => {
  const html = stageHtml(step(), {
    pending: pending({ passed: false, options: [], headline: "「创意方向」写好了，但审核没有通过它。" }),
  });
  assert.ok(html.includes("明确覆盖审核并通过"));
  assert.ok(html.includes('data-passed="0"'));
});

test("the findings and the price of the click are reachable but folded", () => {
  const html = stageHtml(step(), {
    pending: pending({
      findings: [{ kind: "证据", issue: "容量只有一条实测支撑" }],
    }),
  });
  assert.ok(html.includes("<details"));
  assert.ok(html.includes("审核提的 1 点"));
  assert.ok(html.includes("容量只有一条实测支撑"));
  assert.ok(html.includes("上一次这一步花了 $0.02。"));
});

/* ------------------------------------------------------------- empty stages -- */

test("a stage with no document says which of the four situations it is in", () => {
  const html = stageHtml(
    step({ has_doc: false, version: null, blank: { state: "stale", text: "上一版过期了，要重做。" } }),
    {}
  );
  assert.ok(html.includes("上一版过期了，要重做。"));
  assert.ok(html.includes("pane-blank stale"));
  // Nothing to head, nothing to review, and no 看原文 to click.
  assert.ok(!html.includes("pane-head"));
  assert.ok(!html.includes('data-act="read"'));
  assert.ok(!html.includes("pane-review"));
});

/* ----------------------------------------------------------- the two warnings -- */

test("a file changed after it was written says so where the document is read", () => {
  const html = stageHtml(
    step({ integrity_ok: false, integrity_sentence: "「创意方向」这份文件在保存之后被改动过。", blocks: [] }),
    {}
  );
  assert.ok(html.includes("在保存之后被改动过"));
  assert.ok(html.includes("pane-warn integrity"));
});

test("a stale version says so rather than reading as current", () => {
  const html = stageHtml(
    step({ stale: true, stale_sentence: "上游阶段被改过，这一版要重做。" }),
    {}
  );
  assert.ok(html.includes("pane-warn stale"));
  assert.ok(html.includes("上游阶段被改过"));
});

test("a sound stage carries no warning band", () => {
  const html = stageHtml(step({ summary: "" }), {});
  assert.ok(!html.includes("pane-warn"));
});

/* ---------------------------------------------------------------- the review -- */

test("a pass with findings keeps the sentence that qualifies it", () => {
  const html = stageHtml(
    step({
      review: {
        verdict: "pass",
        sentence: "审核通过了，但提了 2 点要注意——通过只代表它在自己声明的范围内成立。",
        findings: [
          { kind: "证据", stage: "direction", issue: "容量只有一条实测支撑" },
          { kind: "范围", stage: "", issue: "没写清放弃什么" },
        ],
      },
    }),
    {}
  );
  assert.ok(html.includes("但提了 2 点要注意"));
  assert.ok(html.includes("容量只有一条实测支撑"));
  assert.ok(html.includes("看这 2 条意见"));
  assert.ok(html.includes("pane-review pass"));
});

test("a returned version is not dressed as a pass", () => {
  const html = stageHtml(
    step({ review: { verdict: "return", sentence: "审核把它退回了，有 1 条问题。", findings: [] } }),
    {}
  );
  assert.ok(html.includes("pane-review return"));
  assert.ok(!html.includes("pane-review pass"));
});

/* ---------------------------------------------------------------- the asking -- */

test("questions are drawn only in the stage they came from", () => {
  const ctx = {
    questions: [
      { stage: "intake", version: 1, question: "主推哪个 SKU", blocks: "" },
      { stage: "direction", version: 2, question: "B 的前提成立吗", blocks: "素材计划定不了" },
    ],
  };
  const mine = stageHtml(step(), ctx);
  assert.ok(mine.includes("B 的前提成立吗"));
  assert.ok(!mine.includes("主推哪个 SKU"), "another stage's question is not this stage's");
  assert.ok(mine.includes("还有 1 个问题等你回答"));

  const other = stageHtml(step({ stage: "intake", number: "①", label: "需求整理" }), ctx);
  assert.ok(other.includes("主推哪个 SKU"));
  assert.ok(!other.includes("B 的前提成立吗"));
});

test("a stage with no open questions draws no asking block", () => {
  const html = stageHtml(step(), { questions: [{ stage: "intake", version: 1, question: "x" }] });
  assert.ok(!html.includes("pane-ask"));
});

test("each answer box carries the key the tray reads back", () => {
  const q = { stage: "direction", version: 2, question: "B 的前提成立吗" };
  const html = stageHtml(step(), { questions: [q] });
  assert.ok(html.includes(`data-draft-key="${answerKey(q)}"`));
  assert.equal(answerKey(q), "direction:v2:B 的前提成立吗");
});

test("an answer already drafted comes back filled in", () => {
  const q = { stage: "direction", version: 2, question: "B 的前提成立吗" };
  const html = stageHtml(step(), {
    questions: [q],
    drafts: { [answerKey(q)]: { text: "成立，官方图有" } },
  });
  assert.ok(html.includes('value="成立，官方图有"'));
});

test("an answer drafted against an older version does not fill the new one", () => {
  /* The version is in the key precisely so a reworked stage cannot inherit it. */
  const q = { stage: "direction", version: 2, question: "B 的前提成立吗" };
  const html = stageHtml(step(), {
    questions: [q],
    drafts: { "direction:v1:B 的前提成立吗": { text: "旧版本的答案" } },
  });
  assert.ok(!html.includes("旧版本的答案"));
  assert.ok(html.includes('value=""'));
});

/* ------------------------------------------------------------ the document -- */

test("a structured direction stays readable without a pending decision", () => {
  const html = stageHtml(
    step({
      blocks: [
        { type: "cards", title: "3 个方向", cards: [{ head: "A", fields: [{ label: "主张", value: "外观" }] }] },
      ],
    }),
    {}
  );
  assert.ok(html.includes('class="creator-direction-grid"'));
  assert.ok(html.includes('<h3>外观</h3>'));
  assert.ok(!html.includes('data-act="approve"'));
  assert.ok(html.includes("pane-summary"));
});

test("a version with no shape shows an honest loading state and keeps the original reader", () => {
  const html = stageHtml(step({ blocks: [] }), {});
  assert.ok(html.includes("正在加载这一版正文"));
  assert.ok(!html.includes("没有可排版的内容"));
  assert.ok(html.includes('data-act="read"'), "and offers the text");
});

test("the exact prose artifact is safely readable without structured blocks", () => {
  const body = "# 一份完整的创意\n\n**原文的具体主张**\n\n<script>alert(1)</script>";
  const html = stageHtml(step({blocks:[], artifact_sha256:'a'.repeat(64)}), {
    artifactBody:{stage:'direction', version:2, status:'loaded', text:body, sha256:'a'.repeat(64)},
  });
  assert.ok(html.includes(`<article class="artifact-prose">${renderMarkdown(body)}</article>`));
  assert.ok(html.includes('<strong>原文的具体主张</strong>'));
  assert.ok(!html.includes('<script'));
  assert.ok(!html.includes('正在加载这一版正文'));
});

test("a late body response never appears under another stage or version", () => {
  for (const identity of [{stage:'writing',version:2}, {stage:'direction',version:1}]) {
    const html = stageHtml(step(), {artifactBody:{...identity,status:'loaded',text:'OLD_BODY_DO_NOT_SHOW'}});
    assert.ok(!html.includes('OLD_BODY_DO_NOT_SHOW'));
    assert.ok(html.includes('正在加载这一版正文'));
  }
});

test("a mismatched fingerprint keeps body hidden and offers a version-bound retry", () => {
  const html = stageHtml(step({artifact_sha256:'b'.repeat(64)}), {
    artifactBody:{stage:'direction',version:2,status:'loaded',text:'CHANGED_BODY',sha256:'a'.repeat(64)},
  });
  assert.ok(!html.includes('CHANGED_BODY'));
  assert.ok(html.includes('正文暂时没有加载成功'));
  assert.match(html, /data-act="retry-body" data-stage="direction" data-version="2"/);
});

test("a loading response does not need to already know the artifact fingerprint", () => {
  const html = stageHtml(step({artifact_sha256:'b'.repeat(64)}), {
    artifactBody:{stage:'direction',version:2,status:'loading'},
  });
  assert.ok(html.includes('正在加载这一版正文'));
  assert.ok(!html.includes('正文暂时没有加载成功'));
});

test("load errors and truly empty body both have an explicit recovery path", () => {
  for (const status of ['error','loaded']) {
    const html = stageHtml(step(), {artifactBody:{stage:'direction',version:2,status,text:''}});
    assert.ok(html.includes(status === 'error' ? '正文暂时没有加载成功' : '这一版正文为空'));
    assert.match(html, /data-act="retry-body" data-stage="direction" data-version="2"/);
    assert.match(html, /data-act="read" data-stage="direction" data-version="2"/);
  }
});

test("long summaries default to a literal excerpt with the full original inside closed details", () => {
  const summary = '这是原作者的摘要，不能重新编造。'.repeat(20) + '<img src=x onerror="alert(1)">';
  const html = stageHtml(step({summary}), {});
  const details = html.match(/<details class="pane-summary summary-expand">([\s\S]*?)<\/details>/);
  assert.ok(details, 'collapsed by default, no open attribute');
  assert.ok(details[1].includes(escapeHtml(Array.from(summary).slice(0,120).join(''))+'…'));
  assert.ok(details[1].includes(`<p class="pane-summary-full">${escapeHtml(summary)}</p>`));
  assert.ok(!html.includes('<img'));
  const short = stageHtml(step({summary:'保留短摘要原文'}), {});
  assert.ok(short.includes('<p class="pane-summary">保留短摘要原文</p>'));
});

test("human stage heading and version date keep runtime details behind a disclosure", () => {
  const html = stageHtml(step(), {});
  assert.ok(html.includes('<h1>创意方向</h1>'));
  assert.ok(html.includes('比较创意主张'));
  assert.ok(!html.includes('DIRECTION'));
  assert.ok(!html.includes('②'));
  assert.ok(html.includes('版本 2'));
  assert.ok(html.includes('9月17日'));
  const details = html.match(/<details class="pane-version-details">([\s\S]*?)<\/details>/);
  assert.ok(details);
  assert.ok(details[1].includes('创作者：导演'));
  assert.ok(details[1].includes('2026-09-17T04:05:06'));
  assert.match(html, /data-act="read" data-stage="direction" data-version="2"/);
});

test("review findings use human labels and retain every concrete risk", () => {
  const html = stageHtml(step({review:{verdict:'return',sentence:'尚未通过审核',findings:[
    {kind:'content',issue:'缺少明确主张'}, {kind:'feasibility',issue:'动作无法完成'},
    {kind:'evidence',issue:'商品事实没有依据'}, {kind:'unverified',issue:'音效仍未验证'},
  ]}}), {});
  for (const text of ['内容','可执行性','证据','尚未验证','缺少明确主张','动作无法完成','商品事实没有依据','音效仍未验证']) {
    assert.ok(html.includes(text));
  }
  assert.ok(!html.includes('>feasibility<'));
  assert.ok(!html.includes('>unverified<'));
});

test("reading comes before approval without changing the exact gate binding", () => {
  const hash = 'e'.repeat(64);
  const html = stageHtml(step({artifact_sha256:hash}), {
    artifactBody:{stage:'direction',version:2,status:'loaded',text:'原文要先读',sha256:hash},
    pending:pending({artifact_sha256:hash,passed:false,options:threeOptions}),
  });
  assert.ok(html.indexOf('class="artifact-prose"') < html.indexOf('id="stage-decision"'));
  assert.match(html, new RegExp(`data-stage="direction" data-version="2" data-sha="${hash}" data-passed="0"`));
  assert.ok(html.includes('明确覆盖审核并通过'));
  assert.equal((html.match(/type="radio"/g)||[]).length,3);
  assert.ok(!/\bchecked\b/.test(html));
});

test("no stage at all renders as nothing", () => {
  assert.equal(stageHtml(null, {}), "");
});


test("the selected direction stays visible after the gate closes", () => {
  const html = stageHtml(step({decision_sentence:"已通过。已选方向 A。"}), {});
  assert.match(html, /pane-decision/);
  assert.match(html, /已选方向 A/);
});

test('structured direction cards carry the same explicit version-bound choice', () => {
  const hash='b'.repeat(64);
  const cards=threeOptions.map(o=>({head:`方向 ${o.key}`,fields:[
    {label:'主张',value:`主张 ${o.key}`},
    {label:'前 3 秒',value:`开场 ${o.key}`},
  ]}));
  const html=stageHtml(step({blocks:[{type:'cards',cards}]}),
    {pending:pending({options:threeOptions,artifact_sha256:hash})});
  assert.equal((html.match(/class="gate creator-direction-gate"/g)||[]).length,1);
  assert.equal((html.match(/data-option-key=/g)||[]).length,3);
  assert.match(html,/data-needs-choice="1"/);
  assert.match(html,new RegExp(`data-sha="${hash}"`));
  assert.match(html,/开场 B/);
  assert.doesNotMatch(html,/name="direction-choice"/);
});

test('normal direction fields lead with the real claim and show recommendation and risks', () => {
  const html=stageHtml(step({blocks:[
    {type:'cards',cards:[{head:'A',emphasis:'chosen',fields:[
      {label:'主张',value:'先帮观众决定容量'},
      {label:'画面与表达',value:'开场并排，结尾回到选择'},
      {label:'开场',value:'Which size fits your day?'},
      {label:'依据',value:'容量差异已核实',secondary:true},
      {label:'素材依赖',value:'仍需两档产品图',secondary:true},
    ]}]},
    {type:'callout',title:'推荐',text:'推荐 A —— 因为容量差异是当前唯一确认的区别',items:[{text:'两档官方图需先核对'}]},
  ]}),{});
  assert.match(html,/<h3>先帮观众决定容量<\/h3>/);
  assert.match(html,/开场并排，结尾回到选择/);
  assert.match(html,/Which size fits your day\?/);
  assert.match(html,/团队推荐/);
  assert.match(html,/仍需两档产品图/);
  assert.match(html,/两档官方图需先核对/);
  assert.ok(html.indexOf('研究依据与制作条件') < html.indexOf('仍需两档产品图'));
  assert.ok(html.indexOf('完整说明与成立前提') < html.indexOf('两档官方图需先核对'));
  assert.doesNotMatch(html,/data-act="approve"|<h3>方向 A<\/h3>/);
});

test('choice-changing direction risks remain visible while generic evidence stays in details', () => {
  const html=stageHtml(step({blocks:[
    {type:'cards',cards:[{head:'A',fields:[
      {label:'主张',value:'先比较容量'},
      {label:'方案取舍',value:'舍弃细节镜头',secondary:true},
      {label:'证据强度',value:'只有一张样品图',secondary:true},
    ]}]},
    {type:'matrix',columns:['A'],rows:[{label:'两款实际容量',cells:[true]}]},
  ]}),{});
  assert.ok(html.indexOf('舍弃细节镜头') < html.indexOf('研究依据与制作条件'));
  assert.match(html,/「两款实际容量」未解决时，这个方向不能成立/);
  assert.ok(html.indexOf('研究依据与制作条件') < html.indexOf('只有一张样品图'));
});

test('accepted option is highlighted from recorded approval, never from recommendation', () => {
  const cards=[{head:'A',emphasis:'chosen',fields:[{label:'主张',value:'视觉方向'}]},
    {head:'B',fields:[{label:'主张',value:'容量选择'}]}];
  const html=stageHtml(step({blocks:[{type:'cards',cards}],approval_decision:'approved',selected_option_id:'B'}),{});
  assert.match(html,/已采用方向 B/);
  assert.match(html,/class="creator-direction-card is-selected" data-direction-card="B"/);
  assert.doesNotMatch(html,/class="creator-direction-card is-selected" data-direction-card="A"/);
  assert.match(html,/<details class="creator-direction-alternatives"><summary>比较其他 1 个方向<\/summary>/);
  assert.ok(html.indexOf('data-direction-card="B"') < html.indexOf('creator-direction-alternatives'));
  assert.ok(html.indexOf('creator-direction-alternatives') < html.indexOf('data-direction-card="A"'));
  assert.doesNotMatch(html,/data-act="approve"/);
});

test('structured script reads exact lines before optional pictures and the timeline', () => {
  const html=stageHtml(step({stage:'writing',label:'脚本与分镜',blocks:[
    {type:'text',title:'总时长',text:'15 秒'},
    {type:'timeline',title:'分镜',segments:[{tc:'0:00–0:03',label:'A',seconds:3}]},
    {type:'cards',title:'逐段',cards:[
      {head:'0:00–0:03',fields:[{label:'画面',value:'两档产品并排'},{label:'台词',value:'Small or roomy?'},{label:'声音',value:'轻快音乐'}]},
      {head:'0:03–0:06',fields:[{label:'画面',value:'放入午餐盒'},{label:'台词',value:'Here is how I choose.'}]},
    ]},
    {type:'text',title:'发布文案',text:'Choose your lunch size.'},
  ],artifact_sha256:'a'.repeat(64)}),{});
  assert.ok(html.indexOf('Small or roomy?') < html.indexOf('两档产品并排'));
  assert.ok(html.indexOf('Here is how I choose.') < html.indexOf('class="timeline"'));
  assert.match(html,/data-creator-target="第 1 段台词"/);
  assert.match(html,/data-creator-target="第 1 段画面"/);
  assert.match(html,/class="creator-script-caption"/);
  assert.match(html,/查看分镜顺序与其他依据/);
});

test('visual script shows every picture and sound without opening details or inventing silence', () => {
  const html=stageHtml(step({stage:'writing',blocks:[{type:'cards',cards:[
    {head:'0:00–0:03',fields:[{label:'画面',value:'拉开空包，镜头贴近拉链'},{label:'声音',value:'拉链声'}]},
    {head:'0:03–0:07',fields:[{label:'画面',value:'装入午餐盒与水瓶'},{label:'台词',value:'（无台词）'},{label:'声音',value:'脚步与环境声'}]},
    {head:'0:07–0:12',fields:[{label:'画面',value:'合上包并背起离开'},{label:'声音',value:'音乐落点'}]},
  ]}],artifact_sha256:'a'.repeat(64)}),{contentFormat:'video'});
  for (const phrase of ['拉开空包，镜头贴近拉链','装入午餐盒与水瓶','合上包并背起离开','拉链声','脚步与环境声','音乐落点']) assert.match(html,new RegExp(phrase));
  assert.doesNotMatch(html,/creator-script-shot|这一段未记录台词或字幕/);
  assert.equal((html.match(/原稿注明：/g)||[]).length,1);
  assert.equal((html.match(/data-creator-target="第 \d 段画面"/g)||[]).length,3);
  assert.doesNotMatch(html,/data-creator-target="第 1 段台词"/);
});

test('photo script reads page pictures and on-image copy without video labels', () => {
  const html=stageHtml(step({stage:'writing',blocks:[{type:'cards',cards:[
    {head:'第 1 页',fields:[{label:'画面',value:'桌上并排两款包'},{label:'台词',value:'Pick your lunch size'}]},
    {head:'第 2 页',fields:[{label:'画面',value:'大号包放入午餐盒'},{label:'台词',value:'Room for more'}]},
  ]}],artifact_sha256:'a'.repeat(64)}),{contentFormat:'photo'});
  assert.match(html,/逐页看画面与图内文案/);
  assert.match(html,/data-creator-target="第 1 页画面"/);
  assert.match(html,/data-creator-target="第 2 页图内文案"/);
  assert.ok(html.indexOf('桌上并排两款包') < html.indexOf('Pick your lunch size'));
  assert.doesNotMatch(html,/第 1 段|台词或字幕|声音/);
});

test('stale structured script stays readable without offering a current-version text target', () => {
  const html=stageHtml(step({stage:'writing',stale:true,blocks:[{type:'cards',cards:[
    {head:'0:00–0:03',fields:[{label:'画面',value:'旧稿画面'},{label:'台词',value:'旧稿台词'}]},
  ]}],artifact_sha256:'a'.repeat(64)}),{contentFormat:'video'});
  assert.match(html,/旧稿画面/);
  assert.doesNotMatch(html,/data-creator-target=/);
});

test('prose-only historical script displays its original bytes without invented segments', () => {
  const body='# 原脚本\n\nOpen on the bag.\n\nSay the actual line.';
  const html=stageHtml(step({stage:'writing',label:'脚本与分镜',blocks:[],artifact_sha256:'a'.repeat(64)}),{
    artifactBody:{stage:'writing',version:2,status:'loaded',text:body,sha256:'a'.repeat(64)},
  });
  assert.match(html,/Open on the bag/);
  assert.match(html,/Say the actual line/);
  assert.match(html,/这份历史脚本按原文保存/);
  assert.doesNotMatch(html,/creator-script-segment|data-creator-target=/);
});
