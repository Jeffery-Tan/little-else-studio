/* Conversation as the single stream:
 *
 *   real agent progress appears in the originating turn → the conclusion is
 *   a bubble of its own → clarification options arrive as an additional card
 *   after the answer → a finished round with no reply still says so.
 *
 * And the project chrome carries no persistent status band: the one
 * next-action is a small button in the workspace tab bar (Open WebUI's top
 * bar is just title + actions), not a coloured “最终交付/现在” banner.
 *
 * Loaded like the shipped page: real team-progress/runtime/journey/app.js,
 * only fetch and the clock replaced.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {loadApp, makeDom, webSource} from './dom-fixture.mjs';

const PAGE_BODY = '<nav class="journey-tabs" role="tablist" aria-label="作品内容">'
  + '<button id="journey-tab-plan" role="tab" data-journey-tab="plan" aria-selected="true">创意与脚本</button>'
  + '</nav>'
  + '<div id="project-progress"></div>';

const IDS = ['chat-input', 'send', 'toast', 'conversation-body', 'conversation-count',
  'conversation', 'conversation-toggle', 'conversation-caret', 'draft', 'job-status',
  'rail', 'stage', 'journey-decision', 'run-select'];

function page() {
  const timers = [];
  const p = loadApp({
    runtime: 'real',
    dom: makeDom({ids: IDS, body: PAGE_BODY}),
    globals: {
      setTimeout: (fn, ms) => { timers.push({fn, ms}); return timers.length; },
      clearTimeout: (id) => { if (id && timers[id - 1]) timers[id - 1] = null; },
    },
  });
  vm.runInContext('state.runId = "run-1"; state.rail = []; null;', p.ctx);
  p.read = (expr) => vm.runInContext(expr, p.ctx);
  p.el = (id) => p.document.getElementById(id);
  return p;
}

function turn(overrides = {}) {
  return {
    user: {text: '现在的结论是什么', at: '2026-09-25T15:54:12Z'},
    reply: {text: '结论：先补一版只动达人层的方向稿。', at: '2026-09-25T15:56:38Z'},
    items: [], detail: null,
    ...overrides,
  };
}

test('failure bubbles remove known internal class prefixes while technical notes keep original evidence',()=>{
  const p=page(),raw='这一轮没能做完：NonRetryableStageError: ModelError: 工具参数不是有效 JSON。已保存内容保留，不会自动重试。';
  const tl={turns:[turn({reply:null,items:[{kind:'note',failure:true,text:raw}]})],rail:[],questions:[],progress:{},pending:null};
  const before=JSON.stringify(tl);p.ctx.__tl=tl;
  p.read('state.busy=false;state.machineDetail=true;state.timeline=globalThis.__tl;renderConversation(globalThis.__tl)');
  const host=p.el('conversation-body'),alert=host.querySelector('.turn-failure');
  assert.match(alert.textContent,/这一轮没能做完：工具参数不是有效 JSON/);
  assert.match(alert.textContent,/已保存内容保留，不会自动重试/);
  assert.doesNotMatch(alert.textContent,/NonRetryableStageError|ModelError/);
  assert.match(host.querySelector('.turn-work').textContent,/NonRetryableStageError: ModelError:/);
  assert.equal(JSON.stringify(tl),before);
});

test('marked failure replies use the same display boundary without changing successful or business text',()=>{
  const p=page();
  for(const [raw,expected] of [
    ['NonRetryableStageError: ModelError: 原模型连接中断','原模型连接中断'],
    ['后续任务未完成：SafePreflightError: 当前配置已变化','后续任务未完成：当前配置已变化'],
    ['VIPAccessError: 该账户无访问权限','VIPAccessError: 该账户无访问权限'],
    ['服务返回的说明中包含 ModelError: 原文仍可追溯','服务返回的说明中包含 ModelError: 原文仍可追溯'],
  ]) {
    p.ctx.__tl={turns:[turn({reply:{text:raw,failure:true}})],rail:[],questions:[],progress:{},pending:null};
    p.read('state.timeline=globalThis.__tl;renderConversation(globalThis.__tl)');
    assert.equal(p.el('conversation-body').querySelector('.turn-failure').textContent,expected);
    assert.equal(p.ctx.__tl.turns[0].reply.text,raw);
  }
  p.ctx.__tl={turns:[turn({reply:{text:'ModelError: 这是你要求查看的名称说明'}})],rail:[],questions:[],progress:{},pending:null};
  p.read('state.timeline=globalThis.__tl;renderConversation(globalThis.__tl)');
  assert.match(p.el('conversation-body').querySelector('.msg.assistant').textContent,/ModelError:/);
});

const CLARIFICATION = {
  id: 'q-1',
  question: '要补充哪一版？',
  options: [
    {label: '在 A 基础上补', description: '其余内容不动'},
    {label: '整版重做', description: '三条一起重开'},
  ],
  recommended: 0,
};

test('direction choice remains in the conversation when its draft also asks a question', () => {
  const p = page();
  vm.runInContext(webSource('stage.js'), p.ctx, {filename:'stage.js'});
  const sha = 'a'.repeat(64);
  p.ctx.__tl = {turns:[turn()],interaction:null,progress:{},
    pending:{stage:'direction',version:1,artifact_sha256:sha,passed:true,
      stage_label:'创意方向',headline:'选一个方向',options:[
        {key:'A',recommended:false,admissible:true},
        {key:'B',recommended:true,admissible:true},
        {key:'C',recommended:false,admissible:true}],findings:[]},
    questions:[{stage:'direction',version:1,question:'有实物吗？',reason:'影响镜头安排'}],
    rail:[{stage:'direction',version:1,blocks:[]}]};
  p.read('state.busy=false; state.timeline=globalThis.__tl; renderJourneyDecision()');
  const host = p.el('journey-decision');
  assert.equal(host.querySelectorAll('input[name="direction-choice"]').length,3);
  assert.ok(host.querySelector('[data-act="approve"]'));
  assert.ok(host.querySelector('[data-send-artifact-answer]'));
});

test('the same exact draft updates its decision copy after its source job ends blocked',()=>{
  const p=page();
  vm.runInContext(webSource('stage.js'),p.ctx,{filename:'stage.js'});
  p.ctx.__tl={turns:[],interaction:null,questions:[],rail:[{stage:'writing',version:1,blocks:[]}],
    pending:{stage:'writing',version:1,artifact_sha256:'a'.repeat(64),stage_label:'脚本',
      passed:true,headline:'脚本已保存，等你决定',findings:[]}};
  p.read('state.busy=false;state.timeline=globalThis.__tl;renderJourneyDecision()');
  const host=p.el('journey-decision');assert.match(host.textContent,/确认当前脚本并继续/);
  p.ctx.__tl.pending.source_execution={job_id:'source',status:'blocked',notice:'草稿已保存，检查或交付未完成；请先查看或修改。'};
  p.ctx.__tl.pending.headline='「脚本」草稿已保存，检查或交付未完成。';
  p.ctx.__tl.pending.body='请先查看或修改；采用只记录用户决定，不代表检查完成。';
  p.read('renderJourneyDecision()');
  assert.match(host.textContent,/当前草稿.*请先查看/);
  assert.match(host.textContent,/草稿已保存，检查或交付未完成/);
  assert.match(host.textContent,/不代表检查完成/);
  assert.doesNotMatch(host.textContent,/确认当前脚本并继续|看完后，在这里选择继续/);
  assert.ok(host.querySelector('[data-view-plan]'));
  assert.equal(host.querySelector('[data-act="approve"]').textContent,'采用当前草稿');
  assert.ok(host.querySelector('[data-act="reject"]'));
});

test('stale direction exposes one typed resume command instead of a copied prompt', async () => {
  const p = page();
  const hash = 'c'.repeat(64);
  p.ctx.__tl = {turns:[turn()],interaction:null,progress:{},pending:null,
    questions:[],rail:[],next_action:{kind:'resume_direction',stage:'direction',
      label:'重做创意方向',expected_context_sha256:hash}};
  p.read('state.busy=false; state.timeline=globalThis.__tl; globalThis.__calls=[]; '+
    'submitJob=async (run,payload)=>{globalThis.__calls.push({run,payload});return {accepted:true}};'+
    'renderJourneyDecision()');
  const host = p.el('journey-decision');
  const button = host.querySelector('[data-resume-direction]');
  assert.ok(button);
  assert.match(host.textContent, /会发起一次文字模型任务/);
  await button.onclick();
  const [call] = p.ctx.__calls;
  assert.equal(call.run, 'run-1');
  assert.equal(call.payload.request_kind, 'direction_resume');
  assert.equal(call.payload.expected_context_sha256, hash);
  assert.equal(p.ctx.__calls.length, 1);
});

test('a pending clarification is an extra card after the conclusion, not a replacement', () => {
  const p = page();
  p.ctx.__tl = {
    turns: [turn({pending_clarification_id: 'q-1'})],
    interaction: CLARIFICATION,
    rail: [], questions: [], progress: {}, pending: null,
  };
  p.read('state.machineDetail = true; state.timeline = globalThis.__tl; renderConversation(globalThis.__tl)');

  const host = p.el('conversation-body');
  const assistant = host.querySelectorAll('.msg.assistant');
  assert.equal(assistant.length, 2,
    'the conclusion bubble AND the question card bubble both render');
  const replyBubble = assistant.find(b => !b.querySelector('.question-host'));
  assert.ok(replyBubble, 'the conclusion has its own bubble');
  assert.match(replyBubble.textContent, /结论：先补一版只动达人层的方向稿/,
    'the answer is visible even while a clarification is pending');
  const questionBubble = assistant.find(b => b.querySelector('.question-host'));
  assert.ok(questionBubble, 'the clarification renders as its own card');
  assert.match(questionBubble.textContent, /要补充哪一版/,
    'and it carries the actual question');
  assert.match(questionBubble.textContent, /在 A 基础上补/,
    'with its options (rendered through the real question card)');
});

test('creator conversation hides internal artifact handles without rewriting history', () => {
  const p = page();
  const raw = '需求已存档（intake-v1），创意方向稿出来了（direction-v2）。详情见 writing-v3。';
  const saved = turn({reply:{text:raw},items:[
    {kind:'progress',type:'agent_narration',text:'我在检查 direction-v2，先核对 intake。'},
    {kind:'note',text:'技术记录：direction-v2'},
  ]});
  p.ctx.__tl = {turns:[saved],rail:[],questions:[],progress:{},pending:null};
  p.read('state.machineDetail = true; state.timeline = globalThis.__tl; renderConversation(globalThis.__tl)');
  const host = p.el('conversation-body');
  assert.match(host.querySelector('.msg.assistant').textContent, /需求已存档，创意方向稿出来了。详情见 脚本/);
  assert.doesNotMatch(host.querySelector('.msg.assistant').textContent, /(?:intake|direction|writing)-v\d+/);
  assert.match(host.querySelector('.turn-progress').textContent, /我在检查 创意方向，先核对 创作需求/);
  assert.match(host.querySelector('.turn-work').textContent, /技术记录：direction-v2/);
  assert.equal(saved.reply.text, raw, 'the persisted reply is unchanged');
});

test('same-source continuation receipt and conclusion render without changing the historical reply', () => {
  const p = page();
  const raw = '已采用方向 A。下一步已记录，待本轮结束后启动；实际进展会显示在任务状态。';
  const saved = turn({reply:{text:raw},replies:[
    {text:raw,display_text:'已采用方向 A。正在按你的要求继续处理后续任务。'},
    {text:'素材方案已保存，脚本草稿请在方案区核对。'},
  ]});
  p.ctx.__tl = {turns:[saved],rail:[],questions:[],progress:{},pending:null};
  p.read('state.timeline = globalThis.__tl; renderConversation(globalThis.__tl)');
  const bubbles = p.el('conversation-body').querySelectorAll('.msg.assistant');
  assert.equal(bubbles.length,2);
  assert.match(bubbles[0].textContent,/正在按你的要求继续/);
  assert.match(bubbles[1].textContent,/脚本草稿请在方案区核对/);
  assert.doesNotMatch(p.el('conversation-body').textContent,/待本轮结束后启动/);
  assert.equal(saved.reply.text,raw);
  assert.equal(saved.replies[0].text,raw);
});

test('exact continuation failure is visible even when the parent saved an adoption acknowledgment', () => {
  const p = page();
  const reason = 'DeepSeek 连接中断，本次结果未完成。已保存内容保留，没有自动重试。';
  const raw = '已采用方向 A。下一步已记录，待本轮结束后启动；实际进展会显示在任务状态。';
  p.ctx.__tl = {turns:[turn({reply:{text:raw},replies:[
    {text:raw,display_text:`已采用方向 A。后续任务未完成。${reason}`,failure:true},
  ]})],rail:[],questions:[],progress:{},pending:null};
  p.read('state.busy = false; state.timeline = globalThis.__tl; renderConversation(globalThis.__tl)');
  const alert = p.el('conversation-body').querySelector('[role="alert"]');
  assert.ok(alert);
  assert.match(alert.textContent,/DeepSeek 连接中断.*没有自动重试/);
  assert.doesNotMatch(p.el('conversation-body').textContent,/待本轮结束后启动|本轮没有留下结论/);
});

test('an identity-bound receipt remains readable when the parent left no final reply', () => {
  const p = page();
  p.ctx.__tl = {turns:[turn({reply:null,decision_continuation:{
    separate:true,status:'cancelled',text:'已采用方向 A。后续任务已停止，已保存内容保留。',
  }})],rail:[],questions:[],progress:{},pending:null};
  p.read('state.timeline = globalThis.__tl; renderConversation(globalThis.__tl)');
  assert.match(p.el('conversation-body').textContent,/后续任务已停止/);
  assert.doesNotMatch(p.el('conversation-body').textContent,/本轮没有留下结论/);
});

test('a finished round with no reply says so instead of leaving the assistant side blank', () => {
  const p = page();
  p.ctx.__tl = {
    turns: [turn({reply: null, items: [{kind: 'note', text: '导演这一次没能交出「创意方向」：派工失败'}]})],
    rail: [], questions: [], progress: {}, pending: null,
  };
  p.read('state.busy = false; state.machineDetail = true; state.timeline = globalThis.__tl; renderConversation(globalThis.__tl)');

  const host = p.el('conversation-body');
  const assistant = host.querySelectorAll('.msg.assistant');
  assert.equal(assistant.length, 1, 'the assistant side answers, even with no reply text');
  assert.match(assistant[0].textContent, /本轮没有留下结论回复/,
    'an honest fallback, not silence');
  assert.match(assistant[0].textContent, /本轮工作记录/,
    'pointing at the work journal right above it');
  assert.ok(host.querySelector('.turn-work'),
    'the work journal still holds the details');
});

test('a failed round shows its saved cause in the answer bubble', () => {
  const p = page();
  p.ctx.__tl = {turns:[turn({reply:null,items:[
    {kind:'note',failure:true,text:'这轮受托角色的模型回复格式不完整，新版未保存。系统已停止自动重试。'},
  ]})],rail:[],questions:[],progress:{},pending:null};
  p.read('state.busy = false; state.machineDetail = false; state.timeline = globalThis.__tl; renderConversation(globalThis.__tl)');
  const bubble = p.el('conversation-body').querySelector('.msg.assistant');
  assert.match(bubble.textContent,/模型回复格式不完整.*停止自动重试/);
  assert.doesNotMatch(bubble.textContent,/本轮没有留下结论回复/);
  assert.equal(bubble.querySelector('[role="alert"]').textContent.includes('新版未保存'),true);
});

test('latest failed round offers a draft without sending another model request', () => {
  const p = page();
  p.ctx.prepareProjectDraft = text => { p.el('chat-input').value = text; };
  p.ctx.__tl = {turns:[turn({reply:null,items:[
    {kind:'note',failure:true,text:'模型回复格式不完整，新版未保存。'},
  ]})],rail:[],questions:[],progress:{},pending:null};
  p.read(`state.busy = false; state.timeline = globalThis.__tl;
    state.projectBrief = {action:{kind:'draft',stage:'direction',label:'准备更新',prompt:'请更新创意方向'}};
    renderConversation(globalThis.__tl)`);
  const button=p.el('conversation-body').querySelector('.msg.assistant button');
  assert.equal(button.textContent,'准备更新');
  button.onclick();
  assert.match(p.el('chat-input').value,/请更新创意方向/);
});

test('while a round is still running the fallback stays out of the way', () => {
  const p = page();
  p.ctx.__tl = {
    turns: [turn({reply: null, items: []})],
    rail: [], questions: [], progress: {}, pending: null,
  };
  p.read('state.busy = true; state.timeline = globalThis.__tl; renderConversation(globalThis.__tl)');

  const host = p.el('conversation-body');
  assert.equal(host.querySelectorAll('.msg.assistant').length, 0,
    'the live progress area owns the in-progress state; no premature “no conclusion”');
});

test('saved and live agent progress stays in the chat turn without duplicate lines', () => {
  const p = page();
  p.ctx.__tl = {
    turns: [turn({reply: null, items: [
      {kind:'progress',type:'agent_narration',seq:10,at:'2026-09-25T15:55:00Z',
       role:'orchestrator',text:'我先核对旧方案。'},
    ]})],
    rail: [], questions: [], progress: {}, pending: null,
  };
  vm.runInContext(`
    state.busy = true;
    currentJob = {job_id:'job-1',job_kind:'conversation',status:'running',events:[
      {type:'agent_narration',seq:10,at:'2026-09-25T15:55:00Z',role:'orchestrator',text:'我先核对旧方案。'},
      {type:'agent_tool',seq:11,at:'2026-09-25T15:55:20Z',role:'orchestrator',
       tool:'dispatch',ok:true,args:{stage:'direction',role:'director'}},
      {type:'agent_activity',seq:12,at:'2026-09-25T15:55:21Z',role:'director',phase:'thinking'},
    ]};
    state.timeline = globalThis.__tl;
    renderConversation(globalThis.__tl);
    renderJobProgress(currentJob);
    null;
  `, p.ctx);
  const host = p.el('conversation-body');
  const progress = host.querySelector('.turn-progress');
  assert.equal(progress.querySelector('.turn-progress-history').open,false,
    'tool narration is preserved in a collapsed journal by default');
  assert.equal(progress.querySelector('.turn-working')?.parentNode,progress,
    'the current working receipt remains visible outside the collapsed journal');
  assert.equal(progress.querySelectorAll('.progress-line').length, 2,
    'the persisted narration and new dispatch appear once each');
  assert.match(progress.textContent, /我先核对旧方案/);
  assert.match(progress.textContent, /创意导演已交回 · 创意方向/);
  assert.match(progress.querySelector('.turn-working').textContent, /创意导演/);
  assert.equal(host.querySelectorAll('.msg.assistant').length, 0,
    'an unfinished turn does not claim a conclusion');

  host.scrollHeight = 888;
  p.read('state.atBottom = true');
  p.read(`currentJob.events.push({type:'agent_narration',seq:13,
    at:'2026-09-25T15:55:40Z',role:'director',text:'方向的开头已经明确。'});
    renderJobProgress(currentJob)`);
  assert.match(progress.children.at(-2).textContent, /方向的开头已经明确/,
    'a new update goes before the current working indicator');
  assert.match(progress.children.at(-1).className, /turn-working/);
  assert.equal(host.scrollTop,888, 'new progress stays visible when the user is following the latest turn');

  p.read('currentJob.status = "succeeded"; renderJobProgress(currentJob)');
  assert.equal(progress.querySelector('.turn-working'), null,
    'the working indicator clears as soon as execution ends');
});

test('only the live turn stays quiet — an older failed round still gets its fallback', () => {
  const p = page();
  p.ctx.__tl = {
    turns: [
      turn({user: {text: '再重新递交审核一次'}, reply: null,
            items: [{kind: 'note', text: '导演这一次没能交出「创意方向」：派工失败'}]}),
      turn({user: {text: '再试一次'}, reply: null, items: []}),   // the running one
    ],
    rail: [], questions: [], progress: {}, pending: null,
  };
  p.read('state.busy = true; state.timeline = globalThis.__tl; renderConversation(globalThis.__tl)');

  const turns = p.el('conversation-body').querySelectorAll('.turn');
  assert.equal(turns.length, 2);
  assert.match(turns[0].querySelectorAll('.msg.assistant')[0]?.textContent || '',
    /本轮没有留下结论回复/, 'the historical failed round is not silenced by a new run');
  assert.equal(turns[1].querySelectorAll('.msg.assistant').length, 0,
    'the live turn defers to the status row');
});

test('the title bar keeps only the next action without repeating current progress', () => {
  const p = page();
  vm.runInContext(`
    state.busy = false;
    state.runs = [];
    state.timeline = null;
    state.creatorView = {
      goal: '交付一部短片', current: '等待用户决定', detail: '',
      action: {kind: 'chat', label: '继续讨论', stage: 'direction'},
      tab: 'plan',
    };
    renderJourneyStatus();
    null;
  `, p.ctx);

  const slot = p.el('project-progress');
  assert.equal(slot.querySelector('.creator-status'), null);
  assert.equal(slot.hidden, false);
  const button = slot.querySelector('[data-creator-next]');
  assert.ok(button, 'the next action stays in the chrome as a plain slot button');
  assert.equal(button.textContent, '继续讨论');
  assert.ok(button.onclick, 'and its handler is bound');
  assert.match(button.className, /ghost/,
    'a neutral toolbar control, not a banner call-to-action');
});

test('the title bar action disappears while the team is working', () => {
  const p = page();
  vm.runInContext(`
    state.busy = true;
    state.runs = [];
    state.timeline = null;
    state.creatorView = {
      goal: '交付一部短片', current: '团队正在处理',
      action: {kind: 'review_stage', label: '重试本版审核', stage: 'direction',
               version: 1, sha256: 'a'.repeat(64)},
      tab: 'plan',
    };
    renderJourneyStatus();
    null;
  `, p.ctx);
  assert.equal(p.el('project-progress').querySelector('[data-creator-next]'), null,
    'in-progress state belongs to the status row, not to a second action');
  assert.equal(p.el('project-progress').hidden, true);
});

test('a UI approval owns its exact failed continuation and fresh preparation request', async () => {
  const p = page();
  p.ctx.payloads=[];
  p.ctx.item={kind:'approval',approval_id:'approval-exact',stage:'writing',stage_label:'脚本',version:2,
    sentence:'已采用。',continuation:{status:'blocked',text:'后续准备未完成。',reason:'Exact scope failure',
      retry:{stage:'writing',expected_version:2,expected_artifact_sha256:'a'.repeat(64),failed_job_id:'failed-exact'}}};
  p.read(`state.busy=false; state.recoveryBlocked=false; state.decisionBusy=false;
    api=async(path,options)=>{if(!options)return null;payloads.push({path,body:JSON.parse(options.body)});throw new Error('Offline connection lost');};
    refreshTimeline=async()=>true;
    document.getElementById('conversation-body').append(renderApproval(item));`);
  const card=p.el('conversation-body').querySelector('.approval');
  assert.match(card.querySelector('[role="alert"]').textContent,/Exact scope failure/);
  assert.equal(card.querySelectorAll('.msg.user').length,0,'no synthetic user message is introduced');
  const retry=card.querySelector('button');
  assert.equal(retry.textContent,'重新准备分镜输入');
  await retry.onclick();
  await p.read('restoreJob("run-1")');
  await retry.onclick();
  assert.equal(p.ctx.payloads.length,2);
  assert.equal(p.ctx.payloads[0].path,'/api/runs/run-1/decision-preparation');
  assert.deepEqual(p.ctx.payloads[0].body,p.ctx.payloads[1].body,'lost response keeps the same new identity');
  assert.equal(p.ctx.payloads[0].body.failed_job_id,'failed-exact');
  assert.equal(p.ctx.payloads[0].body.expected_version,2);
  assert.ok(p.ctx.payloads[0].body.request_id);
});

test('continuation terminal and live state is displayed without inventing a retry', () => {
  const p=page();
  for(const status of ['running','succeeded','cancelled','interrupted']) {
    p.ctx.receipt={stage:'writing',stage_label:'脚本',version:2,sentence:'已采用。',
      continuation:{status,text:`Exact ${status}`,reason:''}};
    const card=p.read('renderApproval(receipt)');
    assert.match(card.textContent,new RegExp(`Exact ${status}`));
    assert.equal(card.querySelector('button'),null);
    assert.equal(card.querySelector('[role="alert"]')!==null,['cancelled','interrupted'].includes(status));
  }
});

test('preparation refuses to post without durable request identity and releases the composer', async () => {
  const p=page();p.ctx.posted=0;
  p.ctx.receipt={stage:'writing',stage_label:'脚本',version:2,sentence:'已采用。',approval_id:'exact',
    continuation:{status:'blocked',text:'准备失败。',reason:'Exact',
      retry:{stage:'writing',expected_version:2,expected_artifact_sha256:'a'.repeat(64),failed_job_id:'old'}}};
  p.read(`state.busy=false;state.decisionBusy=false;state.recoveryBlocked=false;
    localStorage.setItem=()=>{throw new Error('Request identity cannot be saved');};
    api=async()=>{posted++;};`);
  const card=p.read('renderApproval(receipt)');
  await card.querySelector('button').onclick();
  assert.equal(p.ctx.posted,0);
  assert.equal(p.read('state.decisionBusy'),false);
  assert.equal(p.el('send').disabled,false);
  assert.match(card.querySelector('[role="alert"]').textContent,/identity cannot be saved/);
});
