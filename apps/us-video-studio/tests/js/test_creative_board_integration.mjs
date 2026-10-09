/* The board's app/journey wiring, through the shipped controllers and a fake
 * HTTP boundary. The board component has its own node/renderer tests: here its
 * render adapter captures callbacks so the exact navigation authority is tested.
 * No server, model, approval or media-generation request is made. */
import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {webcrypto} from 'node:crypto';
import {loadApp, makeDom, webSource} from './dom-fixture.mjs';

const RUN = 'board-preview';
const WRITING_SHA = 'a'.repeat(64);
const DRAFT_SHA = 'b'.repeat(64);
const MATERIAL_SHA = 'c'.repeat(64);

async function settle() {
  for (let i = 0; i < 6; i++) await new Promise(resolve => setImmediate(resolve));
}

function stage(name, version, sha, text) {
  return {stage:name, label:name === 'writing' ? '脚本与分镜' : '创意方向',
    state:'done', has_doc:true, version, artifact_sha256:sha, integrity_ok:true,
    stale:false, blank:{text:''}, questions:[], review:null, team_issues:[],
    blocks:name === 'writing'
      ? [{type:'cards', title:'逐段', cards:[{head:'0:00–0:03', fields:[
          {label:'画面', value:'Offline shot'}, {label:'台词', value:text},
        ]}]}]
      : [{type:'text', title:'方案', text}]};
}

function fixture({kind = 'creative', format = 'video', analysisOnly = false, readyImages = false, approvedImages = false,
  approvedUpload = false, unbound = false} = {}) {
  const run = {run_id:RUN, kind, content_format:format, title:'Offline board',
    brief:'A US product creative fixture.'};
  const rail = [stage('direction', 3, 'd'.repeat(64), 'Current direction'),
    stage('writing', 4, WRITING_SHA, 'Exact current script')];
  const materials = [{name:'product front.jpg', kind:'image', bytes:2048,
    sha256:MATERIAL_SHA, usable:true}];
  const production = {run_id:RUN, materials, sources:[], drafts:[{
    draft_id:'image-shot-2-v3', kind:'image', shot_id:'shot-2',
    draft_sha256:DRAFT_SHA, source_version:4, source_sha256:WRITING_SHA,
    prompt:'Offline second shot', attempts:[], review:{verdict:'ready'},
  }], targets:{
    image:{source_version:4, source_sha256:WRITING_SHA},
    video:{source_version:4, source_sha256:WRITING_SHA},
  }, shots:{image:{shots:[{shot_id:'shot-2', draft_id:'image-shot-2-v3'}]},
    video:{shots:[{shot_id:'shot-2', draft_id:'video-shot-2-v1'}]}}};
  const timeline = {run, rail, materials, turns:[], questions:[], pending:null,
    brief:{stage:'writing', phase:'ready', execution:{active:false}}, progress:{},
    spend_sentence:'Offline usage', all_runs_sentence:'Offline totals'};
  const references = [];
  if (approvedUpload) {
    // The real photo HTTP projection also has no video_sequence. An approved
    // uploaded image does not by itself create a generated-image draft.
    production.drafts = [];
    production.sources = [{version:4, artifact_sha256:WRITING_SHA, ready:true}];
    production.materials = [{...materials[0], approved:true,
      url:`/api/runs/${RUN}/materials/product%20front.jpg`}];
    production.latest_approved_image = production.materials[0];
    production.video_sequence = [];
    production.visual_design_status = {ready:true, issues:[]};
    production.shots.image = {kind:'image', total:1, complete_list:true,
      shots:[{ordinal:1,shot_id:'shot-2',label:'Current page',stage:'unprepared',
        stage_label:'输入尚未准备',blocker:'unprepared'}]};
  }
  if (readyImages) {
    production.sources = [{version:4, artifact_sha256:WRITING_SHA, ready:true}];
    production.capabilities = {image:{available:true, connection_revision:7}};
    production.drafts = Array.from({length:5}, (_, index) => ({
      draft_id:`image-shot-${index + 1}`, kind:'image', shot_id:`shot-${index + 1}`,
      draft_sha256:String(index + 1).repeat(64), source_version:4, source_sha256:WRITING_SHA,
      prompt:`Offline ready shot ${index + 1}`, references:[], attempts:[],
      source_ready:true, can_generate:true, is_current:true, review:{verdict:'ready'},
    }));
    production.shots.image = {shots:production.drafts.map(draft => ({
      shot_id:draft.shot_id, draft_id:draft.draft_id, stage:'input_ready',
    }))};
    if (approvedImages) {
      production.materials = production.drafts.map((draft, index) => ({
        name:`generated-${index + 1}.png`, kind:'image', sha256:String(index + 1).repeat(64),
        approved:true, usable:true, url:`/api/runs/${RUN}/materials/generated-${index + 1}.png`,
      }));
      production.video_sequence = production.materials.map((material, index) => ({
        index:index + 1, approved:true, reference:{name:material.name,sha256:material.sha256},
      }));
      production.drafts.forEach((draft, index) => {
        draft.attempts = [{status:'succeeded',attempt_id:`offline-${index}`,output:production.materials[index]}];
      });
    }
  }
  if (analysisOnly) {
    timeline.rail = [];
    production.drafts = [];
    references.push({reference_id:'analysis-only-ref', name:'reference clip.mp4',
      intent:'analyze', status:'completed', report:'Saved reference report',
      candidate:{analysis_id:'saved-analysis', candidate_sha256:'9'.repeat(64),
        timeline:[{start_seconds:0, end_seconds:2, visible:'Existing analysis observation',
          audible:'Original voice', relationship:'Same action'}],
        reference_methods:[], limitations:['Offline fixture']}});
  }
  if (unbound) {
    run.content_format = null;
    rail.length = 0;
    production.drafts = [];
    production.targets = {};
    production.shots = {};
    timeline.brief = {phase:'new', execution:{active:false}};
  }
  return {run, timeline, production, references};
}

async function page(options = {}) {
  const data = fixture(options), boards = [], openedProduction = [], openedDesign = [], focus = [], learning = [];
  let materialWorkspaceLoads = 0;
  const dom = makeDom({body:webSource('index.html').match(/<body[^>]*>([\s\S]*?)<\/body>/)[1]});
  // The focused preview cases use the real parsed index subtree, not id placeholders.
  if(options.realPreview)for(const node of dom.document.querySelectorAll('[id]'))
    dom.document.__ids.set(node.getAttribute('id'),node);
  const app = loadApp({dom, runtime:options.realTransport?'real':'shim', routes:{
    '/material-approvals/batch/continue':() => options.videoPreparationResult,
    '/timeline':() => data.timeline,
    '/production':() => data.production,
    '/reference-videos':() => ({tasks:data.references}),
    '/material-observations':() => ({materials:data.timeline.materials}),
    '/image-analysis':{candidates:[]},
    '/learning/session':{session_id:'offline-learning'},
    '/api/learning':{methods:[]},
    '/jobs/latest':null,
    '/recovery':{},
    '/jobs':({method})=>method==='POST'?{run_id:RUN,job_id:'offline-dialogue',job_kind:'conversation',
      status:'succeeded',cursor:0,events:[],role_sessions:[]}:null,
    '/artifacts/material_plan/2':{stage:'material_plan', version:2,
      sha256:'e'.repeat(64), integrity_ok:true, numbered:'人物场景第 2 版',
      author:'material', created_at:'2026-10-02', content:'Exact displayed material design v2'},
    '/artifacts/writing/4':{stage:'writing', version:4,
      sha256:WRITING_SHA, integrity_ok:true, numbered:'脚本第 4 版',
      author:'writer', created_at:'2026-10-02', content:'Exact current unprepared shot plan v4'},
  }, globals:{
    initStudioNavigation:() => {}, renderProjects:() => {}, loadInspiration:() => {},
    setStudioView:() => {}, renderProjectProgress:() => {}, renderJobProgress:() => {},
    setProjectFocus:value => focus.push(value), currentJob:null,
    crypto:webcrypto, setTimeout:() => 1, clearTimeout:() => {},
    loadMaterialWorkspace:() => {materialWorkspaceLoads++;},
    StudioCreativeBoard:{
      render:(host, context, callbacks) => {
        host.innerHTML='<article class="offline-board-marker">Current board</article>';
        boards.push({host, context, callbacks});
      }, destroy:() => {},
    },
    StudioLearningSession:{mount:args => learning.push(args)},
    __submitJob:() => {throw new Error('Navigation cannot request a model job');},
    __submitSteer:() => {throw new Error('Navigation cannot change running work');},
  }});
  for (const file of ['md.js', 'blocks.js', 'stage.js', 'production.js', 'design-upgrade.js', 'reference-video.js', 'creator-view.js',
    ...(options.realWorkspace?['workspace.js']:[]),
    ...(options.realBoard?['creative-board.js','materials.js','image-analysis.js','image-workspace.js','material-workspace.js']:[]),
    ...(options.realTransport?[]:['journey.js'])])
    vm.runInContext(webSource(file), app.ctx, {filename:file});
  // Navigation must run for real; the drawer boundary is observed without
  // mounting its separate editor/authorization flow.
  app.ctx.__openProduction = args => openedProduction.push(JSON.parse(JSON.stringify(args)));
  app.ctx.__openDesignUpgrade = (runId,production) => openedDesign.push({runId,
    context:production.design_upgrade_action?.expected_context_sha256,proposal:production.design_upgrade_proposal?.id});
  vm.runInContext(`
    setStudioView = view => { state.view = view; };
    openProduction = options => globalThis.__openProduction(options);
    openDesignUpgrade = (runId,production) => globalThis.__openDesignUpgrade(runId,production);
    initJourney();
  `, app.ctx);
  await settle(); // let app.js's initial empty project listing settle first
  app.ctx.__run = data.run;
  vm.runInContext('state.runs = [globalThis.__run];', app.ctx);
  await app.ctx.selectRun(RUN);
  vm.runInContext('state.busy = false; state.view = "project";', app.ctx);
  return {...app, data, boards, openedProduction, openedDesign, focus, learning,
    read:expression => vm.runInContext(expression, app.ctx),
    get materialWorkspaceLoads() {return materialWorkspaceLoads;}};
}

function noWrites(app) {
  assert.deepEqual(app.observed.filter(call => call.method !== 'GET'), []);
  assert.equal(app.observed.some(call => /\/jobs|\/actions|authoriz|approval|\/generate/.test(call.path)), false);
}

test('an unbound new project stays neutral across the real page controllers', async()=>{
  const app=await page({unbound:true,realBoard:true});
  assert.equal(app.read('state.creatorView.format'), null);
  assert.equal(app.document.getElementById('journey-tab-script').textContent, '创作方案');
  assert.equal(app.document.getElementById('journey-tab-video').hidden, true);
  const board=app.document.getElementById('creative-board');
  assert.match(board.textContent, /创作方案|画面与文字|作品结果/);
  assert.equal(board.querySelector('[data-board-action="history:video"]'), null);
  assert.doesNotMatch(board.textContent, /分镜|镜头|口播|视频记录/);
  app.data.timeline.run={...app.data.run,content_format:'photo'};
  await app.ctx.refreshTimeline();
  assert.equal(app.document.getElementById('journey-tab-script').textContent, '图文方案');
  assert.match(board.textContent, /图文方案|按本版页码对应展示/);
  assert.equal(app.document.getElementById('journey-tab-video').hidden, true);
  noWrites(app);
});

test('polling a cached video project updates home, top action and board to the current photo format',async()=>{
  const app=await page({format:'video',approvedUpload:true,realBoard:true,realWorkspace:true});
  assert.equal(app.read('state.creatorView.format'),'video');
  assert.equal(app.document.getElementById('journey-tab-video').hidden,false);
  // Replace only the latest HTTP response; the already loaded project card is
  // intentionally still video, exactly like a format change in an open page.
  app.data.timeline.run={...app.data.run,content_format:'photo'};
  await app.ctx.refreshTimeline();
  app.read('renderJourneyStatus()');
  assert.equal(app.read('state.runs[0].content_format'),'photo');
  assert.equal(app.read('state.creatorView.format'),'photo');
  assert.match(app.document.getElementById('project-grid').textContent,/带货图片与文案/);
  assert.equal(app.document.getElementById('journey-tab-script').textContent,'图文方案');
  assert.equal(app.document.getElementById('journey-tab-video').hidden,true);
  const next=app.document.getElementById('project-progress').querySelector('[data-creator-next]');
  assert.equal(next.textContent,'准备画面输入');
  next.click();
  await settle();
  assert.equal(app.read('state.canvasTab'),'storyboard');
  assert.match(app.document.getElementById('journey-storyboard').textContent,/图文图片（1）/);
  assert.match(app.document.getElementById('journey-storyboard').textContent,/目标 1 页/);
  assert.match(app.document.getElementById('journey-storyboard').textContent,/第 01 页/);
  assert.doesNotMatch(app.document.getElementById('journey-storyboard').textContent,/分镜首帧|面镜头|这一镜/);
  app.ctx.showJourneyTab('board');
  assert.match(app.document.getElementById('creative-board').textContent,/图文方案|逐页画面与文案/);
  assert.equal(app.document.getElementById('creative-board').querySelector('[data-board-action="history:video"]'),null);
  noWrites(app);
});

test('a fresh photo with an approved uploaded image offers usable image actions in every project surface',async()=>{
  const app=await page({format:'photo',approvedUpload:true,realBoard:true,realWorkspace:true});
  assert.equal(app.read("StudioProduction.currentProductionWork(state.productionData,'video',state.runId).phase"),'prepare');
  assert.equal(app.read('state.creatorView.action.tab'),'storyboard');
  app.read('renderJourneyStatus()');
  app.document.getElementById('project-progress').querySelector('[data-creator-next]').click();
  await settle();
  assert.equal(app.read('state.canvasTab'),'storyboard');
  app.ctx.showJourneyTab('board');
  const action=app.document.getElementById('production-action');
  assert.match(action.textContent,/画面制作输入待准备/);
  assert.doesNotMatch(action.textContent,/准备.*视频/);
  action.querySelector('button').click();
  assert.deepEqual(app.openedProduction.at(-1),{kind:'image'});
  app.document.getElementById('creative-board').querySelector('[data-board-action="history:image"]').click();
  assert.equal(app.read('state.canvasTab'),'storyboard');
  noWrites(app);
});

test('photo conversation cannot open a new video batch from historical video records',async()=>{
  const app=await page({format:'photo',readyImages:true,approvedImages:true,realTransport:true});
  assert.ok(app.read("StudioProduction.videoSequenceState(state.productionData,state.runId).length")>1);
  app.read("openProductionBatch('choose')");
  const action=app.document.getElementById('production-action');
  assert.doesNotMatch(action.textContent,/准备整组视频|生成.*段视频|拼接成片/);
  app.read('saveBatchIntent(null)');
  const result=await app.ctx.send('准备全部视频输入');
  assert.equal(result.accepted,true);
  assert.equal(app.read('currentBatchIntent()'),null);
  const posts=app.observed.filter(call=>call.method==='POST');
  assert.equal(posts.length,1);
  assert.equal(posts[0].path,`/api/runs/${RUN}/jobs`);
  assert.equal(posts[0].body.text,'准备全部视频输入');
  assert.equal(app.observed.some(call=>/batch\/continue|batch-generate|\/assemble/.test(call.path)),false);
});

test('switching to photo clears only unsent video confirmations and preserves uncertain original submissions',async()=>{
  const app=await page({readyImages:true,approvedImages:true});
  app.read("openProductionBatch('video_prepare')");
  const original=app.read('currentBatchIntent().request_id');
  app.data.timeline.run={...app.data.run,content_format:'photo'};
  await app.ctx.refreshTimeline();
  assert.equal(app.read('currentBatchIntent()'),null);
  app.data.timeline.run={...app.data.run,content_format:'video'};
  await app.ctx.refreshTimeline();
  app.read("openProductionBatch('video_prepare');currentBatchIntent().uncertain=true;saveBatchIntent(currentBatchIntent())");
  const uncertain=app.read('currentBatchIntent().request_id');
  assert.notEqual(uncertain,original);
  app.data.timeline.run={...app.data.run,content_format:'photo'};
  await app.ctx.refreshTimeline();
  assert.equal(app.read('currentBatchIntent().request_id'),uncertain);
  assert.equal(app.document.getElementById('production-action').querySelector('[data-batch-confirm]').textContent,'核实同一请求');
  noWrites(app);
});

test('the default board exposes the ready image set and prepares its exact confirmation without submitting', async () => {
  const app = await page({readyImages:true});
  const host = app.document.getElementById('production-action');
  assert.equal(app.read('state.canvasTab'), 'board');
  assert.match(host.textContent, /核对并一次生成 5 张/);
  const button = [...host.querySelectorAll('button')].find(item => /核对并一次生成/.test(item.textContent));
  button.click();
  assert.equal(app.read('state.canvasTab'), 'board');
  const intent = JSON.parse(app.read('JSON.stringify(currentBatchIntent())'));
  assert.equal(intent.connection_revision, 7);
  assert.deepEqual(intent.drafts, app.data.production.drafts.map(draft => ({
    draft_id:draft.draft_id, expected_sha256:draft.draft_sha256,
  })));
  assert.ok(host.querySelector('[data-batch-confirm]'));
  noWrites(app);
  await app.ctx.refreshTimeline();
  assert.equal(app.read('currentBatchIntent().request_id'), intent.request_id);
  assert.ok(app.document.getElementById('production-action').querySelector('[data-batch-confirm]'));
  noWrites(app);
});

test('the reported generation phrase opens a board confirmation without sending a model job', async () => {
  const app = await page({readyImages:true});
  const result = await app.ctx.send('开始做分镜生成吧');
  assert.equal(result.accepted, true);
  assert.equal(app.read('state.canvasTab'), 'board');
  assert.equal(app.read('currentBatchIntent().drafts.length'), 5);
  assert.ok(app.document.getElementById('production-action').querySelector('[data-batch-confirm]'));
  noWrites(app);
});

test('a board confirmation rejects changed inputs instead of replacing the reviewed set on refresh', async () => {
  const app = await page({readyImages:true});
  app.read("openProductionBatch('generate')");
  const oldHash = app.read('currentBatchIntent().drafts[1].expected_sha256');
  app.data.production.drafts[1].draft_sha256 = 'f'.repeat(64);
  await app.ctx.refreshTimeline();
  assert.equal(app.read('state.canvasTab'), 'board');
  assert.equal(app.read('currentBatchIntent().drafts[1].expected_sha256'), oldHash);
  const host = app.document.getElementById('production-action');
  assert.match(host.textContent, /版本已变化/);
  assert.equal(host.querySelector('[data-batch-confirm]'), null);
  noWrites(app);
});

test('the board keeps original-task recovery visible and gives pending decisions priority', async () => {
  const app = await page({readyImages:true});
  app.data.production.drafts[0].attempts = [{attempt_id:'original-task',status:'unknown'}];
  await app.ctx.refreshTimeline();
  const host = app.document.getElementById('production-action');
  assert.match(host.textContent, /核实这次图片任务/);
  assert.doesNotMatch(host.textContent, /核对并一次生成/);
  app.data.timeline.interaction = {question:'Clarify first'};
  app.ctx.__pendingTimeline = app.data.timeline;
  app.read('state.timeline = globalThis.__pendingTimeline; renderProductionAction();');
  assert.equal(host.textContent, '');
  noWrites(app);
});

test('missing historical design cannot hide an original unknown task behind fresh batch inputs',async()=>{
  const app=await page({readyImages:true});
  const original=app.data.production.drafts[0];
  original.source_ready=false;original.design_issue='Historical input needs design';
  original.attempts=[{attempt_id:'original-image-task',status:'unknown'}];
  app.data.production.unresolved_attempts=[{kind:'image',shot_id:original.shot_id,ordinal:8,
    draft_id:original.draft_id,attempt_id:'original-image-task',status:'unknown'}];
  await app.ctx.refreshTimeline();
  const host=app.document.getElementById('production-action');
  assert.match(host.textContent,/镜头 08.*图片结果待确认/);
  assert.doesNotMatch(host.textContent,/核对并一次生成|查看出图输入与费用/);
  host.querySelector('button').onclick();
  assert.deepEqual(app.openedProduction.at(-1),{kind:'image',focusDraft:original.draft_id,
    focusAttempt:'original-image-task'});
  assert.equal(app.read("openProductionBatch('generate')"),false);
  noWrites(app);
});

test('coverage task identity survives replacement and generation returns only after its resolution',async()=>{
  const app=await page({readyImages:true});
  const replacement=app.data.production.drafts[0];
  const original={...replacement,draft_id:'superseded-image-input',source_ready:false,is_current:false,
    design_issue:'Missing old design',superseded_by:replacement.draft_id,
    attempts:[{attempt_id:'original-replaced-task',status:'submission_unknown',task_id:'provider-original'}]};
  app.data.production.drafts.unshift(original);
  Object.assign(app.data.production.shots.image.shots[0],{ordinal:8,stage:'unverified',
    attempt_id:'original-replaced-task',attempt_status:'submission_unknown',task_id:'provider-original'});
  await app.ctx.refreshTimeline();
  const host=app.document.getElementById('production-action');
  assert.match(host.textContent,/镜头 08.*结果待确认/);
  assert.doesNotMatch(host.textContent,/核对并一次生成/);
  host.querySelector('button').onclick();
  assert.deepEqual(app.openedProduction.at(-1),{kind:'image',focusDraft:original.draft_id,
    focusAttempt:'original-replaced-task'});
  original.attempts[0].status='retry_authorized';
  app.data.production.unresolved_attempts=[];
  await app.ctx.refreshTimeline();
  assert.match(host.textContent,/核对并一次生成 5 张/);
  assert.equal(app.read("openProductionBatch('generate')"),true);
  assert.match(host.textContent,/一次生成所选 5 张首帧/);
  noWrites(app);
});

test('an unknown accepted batch preserves its request identity despite newly invalid original inputs',async()=>{
  const app=await page({readyImages:true});
  assert.equal(app.read("openProductionBatch('generate')"),true);
  app.read('currentBatchIntent().uncertain=true;saveBatchIntent(currentBatchIntent())');
  const request=JSON.parse(app.read('JSON.stringify(currentBatchIntent())'));
  const original=app.data.production.drafts[0];
  original.source_ready=false;original.design_issue='Historical design now invalid';
  original.attempts=[{attempt_id:'accepted-batch-task',status:'unknown'}];
  app.data.production.unresolved_attempts=[{kind:'image',draft_id:original.draft_id,
    shot_id:original.shot_id,ordinal:1,attempt_id:'accepted-batch-task',status:'unknown'}];
  await app.ctx.refreshTimeline();
  const host=app.document.getElementById('production-action');
  assert.match(host.textContent,/核实同一请求/);
  assert.doesNotMatch(host.textContent,/版本已变化|确认并生成/);
  assert.equal(app.read('currentBatchIntent().request_id'),request.request_id);
  assert.deepEqual(JSON.parse(app.read('JSON.stringify(currentBatchIntent().drafts)')),request.drafts);
  noWrites(app);
});

test('original media tasks precede the current design view and video batch actions',async()=>{
  const app=await page({readyImages:true,approvedImages:true});
  const original=app.data.production.drafts[0];
  original.is_current=false;original.source_ready=false;
  original.attempts=[{attempt_id:'old-unconfirmed-file',status:'unknown'}];
  app.data.production.unresolved_attempts=[{kind:'image',draft_id:original.draft_id,
    shot_id:original.shot_id,ordinal:1,attempt_id:'old-unconfirmed-file',status:'unknown'}];
  await app.ctx.refreshTimeline();
  app.read("state.canvasTab='video';renderProductionAction()");
  const host=app.document.getElementById('production-action');
  assert.match(host.textContent,/核实这次图片任务/);
  assert.doesNotMatch(host.textContent,/准备整组视频输入|核对并顺序生成/);
  app.read("state.canvasTab='plan';renderProductionAction()");
  assert.match(host.textContent,/核实这次图片任务/);
  noWrites(app);
});

test('legacy adopted projects expose the real design supplement dialog without sending a model request',async()=>{
  const app=await page({readyImages:true});
  app.data.production.drafts.forEach(draft=>{draft.source_ready=false;draft.design_issue='Missing historical design';});
  app.data.production.design_upgrade_action={label:'补齐制作设计，保留原脚本',
    expected_context_sha256:WRITING_SHA,notice:'一次素材角色模型调用；先预览，保存后仍需采用。'};
  await app.ctx.refreshTimeline();
  const host=app.document.getElementById('production-action');
  assert.match(host.textContent,/原脚本已采用，制作设计待补齐|补齐制作设计，保留原脚本/);
  const button=host.querySelector('[data-design-upgrade]');assert.ok(button);
  button.onclick();
  assert.deepEqual(app.openedDesign,[{runId:RUN,context:WRITING_SHA,proposal:undefined}]);
  noWrites(app);
});

test('original unknown media stays the main action while an existing supplement remains separately readable',async()=>{
  const app=await page({readyImages:true});
  const original=app.data.production.drafts[0];
  original.source_ready=false;original.attempts=[{attempt_id:'original-task',status:'unknown'}];
  app.data.production.unresolved_attempts=[{kind:'image',draft_id:original.draft_id,
    shot_id:original.shot_id,ordinal:8,attempt_id:'original-task',status:'unknown'}];
  app.data.production.design_upgrade_proposal={id:'existing-design-proposal',sha256:WRITING_SHA};
  await app.ctx.refreshTimeline();
  const host=app.document.getElementById('production-action');
  assert.match(host.querySelector('strong').textContent,/镜头 08.*结果待确认/);
  assert.match(host.querySelector('button').textContent,/核实这次图片任务/);
  host.querySelector('[data-design-upgrade]').onclick();
  assert.deepEqual(app.openedDesign,[{runId:RUN,context:undefined,proposal:'existing-design-proposal'}]);
  assert.doesNotMatch(host.textContent,/核对并一次生成/);
  noWrites(app);
});

test('a preparation refusal cannot be displayed as a started video task', async () => {
  const app = await page({readyImages:true, approvedImages:true,
    videoPreparationResult:{continuation:{status:'not_started',reason:'Offline connection unavailable'}}});
  assert.equal(app.read("openProductionBatch('video_prepare')"), true);
  await app.ctx.submitProductionBatch();
  assert.match(app.document.getElementById('toast').textContent, /视频输入尚未开始.*Offline connection unavailable/);
  assert.doesNotMatch(app.document.getElementById('toast').textContent, /团队正在准备/);
  const writes = app.observed.filter(call => call.method !== 'GET');
  assert.equal(writes.length, 1);
  assert.match(writes[0].path, /material-approvals\/batch\/continue$/);
});

test('creative projects open on the board and polling never follows creatorView away from it', async () => {
  const app = await page();
  assert.equal(app.read('state.canvasTab'), 'board');
  assert.equal(app.read('state.creatorView.tab'), 'storyboard');
  assert.equal(app.document.getElementById('creative-board').hidden, false);
  assert.equal(app.document.getElementById('stage').hidden, true);
  const before = app.boards.length;
  await app.ctx.refreshTimeline();
  assert.equal(app.read('state.canvasTab'), 'board');
  app.data.production.drafts = [];
  await app.ctx.refreshTimeline();
  assert.equal(app.read('state.creatorView.tab'), 'plan');
  assert.equal(app.read('state.canvasTab'), 'board');
  assert.equal(app.read('state.canvasTouched'), false);
  assert.equal(app.boards.length, before + 2);
  assert.equal(app.boards.at(-1).context.production, app.data.production);
  assert.equal(app.boards.at(-1).context.timeline, app.data.timeline);
  noWrites(app);
});

test('the stage node reads its displayed exact version while material nodes use the recorded shelf', async () => {
  const app = await page();
  const callbacks = app.boards.at(-1).callbacks;
  callbacks.openStage('material_plan', 2);
  await settle();
  assert.equal(app.document.getElementById('reader').hidden, false);
  assert.match(app.document.getElementById('reader-body').textContent, /Exact displayed material design v2/);
  assert.equal(app.observed.filter(call => call.path.endsWith('/artifacts/material_plan/2')).length, 1);
  assert.equal(app.observed.some(call => call.path.includes('/artifacts/material_plan/latest')), false);
  assert.equal(app.read('state.canvasTab'), 'board');
  callbacks.openMaterials();
  assert.equal(app.read('state.materialsOpen'), true);
  assert.equal(app.materialWorkspaceLoads, 1);
  callbacks.openMedia('product front.jpg');
  assert.equal(app.document.getElementById('viewer').hidden, false);
  assert.equal(app.document.getElementById('viewer-body').querySelector('img').src,
    `/api/runs/${RUN}/materials/product%20front.jpg`);
  callbacks.openMedia('not-on-the-shelf.jpg');
  assert.match(app.document.getElementById('toast').textContent, /无法读取/);
  noWrites(app);
});

test('shot results follow the backend shot target only for the current script version and hash', async () => {
  const app = await page();
  const callbacks = app.boards.at(-1).callbacks;
  callbacks.openShot('image', 'shot-2');
  callbacks.openShot('video', 'shot-2');
  assert.deepEqual(app.openedProduction, [
    {kind:'image', focusDraft:'image-shot-2-v3'},
    {kind:'video', focusDraft:'video-shot-2-v1'},
  ]);
  app.data.production.targets.image.source_sha256 = 'f'.repeat(64);
  callbacks.openShot('image', 'shot-2');
  assert.equal(app.openedProduction.length, 2);
  assert.match(app.document.getElementById('toast').textContent, /方案版本已变化/);
  app.data.production.targets.image.source_sha256 = WRITING_SHA;
  app.data.production.targets.image.source_version = 3;
  callbacks.openShot('image', 'shot-2');
  assert.equal(app.openedProduction.length, 2);
  assert.equal(app.read('state.canvasTab'), 'board');
  noWrites(app);
});

test('an unprepared shot opens its exact current plan rather than unrelated historical production inputs', async () => {
  const app = await page();
  app.data.production.shots.image.shots.push({shot_id:'shot-3', draft_id:null});
  app.data.production.drafts.push({draft_id:'old-unrelated-shot-3', kind:'image', shot_id:'shot-3',
    source_version:2, source_sha256:'2'.repeat(64), draft_sha256:'3'.repeat(64),
    prompt:'Unrelated historical input', attempts:[]});
  await app.ctx.refreshTimeline();
  const callbacks = app.boards.at(-1).callbacks;
  callbacks.openShot('image', 'shot-3');
  await settle();
  assert.equal(app.document.getElementById('reader').hidden, false);
  assert.match(app.document.getElementById('reader-body').textContent, /Exact current unprepared shot plan v4/);
  assert.doesNotMatch(app.document.getElementById('reader-body').textContent, /Unrelated historical input/);
  assert.equal(app.observed.filter(call => call.path.endsWith('/artifacts/writing/4')).length, 1);
  assert.deepEqual(app.openedProduction, []);
  assert.equal(app.read('state.canvasTab'), 'board');
  assert.equal(app.document.getElementById('journey-storyboard').hidden, true);
  const requests = app.observed.length;
  callbacks.openShot('image', 'missing-shot');
  await settle();
  assert.match(app.document.getElementById('toast').textContent, /镜头暂时无法核对/);
  assert.equal(app.observed.length, requests);
  assert.deepEqual(app.openedProduction, []);
  noWrites(app);
});

test('callbacks from another project cannot read, open media, focus a decision or submit work', async () => {
  const app = await page();
  const callbacks = app.boards.at(-1).callbacks, requests = app.observed.length;
  vm.runInContext('state.runId = "another-project";', app.ctx);
  callbacks.openStage('material_plan', 2);
  callbacks.openMaterials(); callbacks.openMedia('product front.jpg');
  callbacks.openShot('image', 'shot-2'); callbacks.focusDecision(); callbacks.openReference();
  await settle();
  assert.equal(app.observed.length, requests);
  assert.deepEqual(app.openedProduction, []);
  assert.deepEqual(app.focus, []);
  assert.equal(app.read('state.canvasTab'), 'board');
  noWrites(app);
});

test('a shot callback retained from the old board cannot open a new script result in the same project', async () => {
  const app = await page();
  const old = app.boards.at(-1).callbacks;
  const writing = app.data.timeline.rail.find(row => row.stage === 'writing');
  writing.version = 5;
  writing.artifact_sha256 = 'f'.repeat(64);
  app.data.production.targets.image = {source_version:5, source_sha256:'f'.repeat(64)};
  app.data.production.shots.image.shots[0].draft_id = 'new-script-shot-2';
  await app.ctx.refreshTimeline();
  old.openShot('image', 'shot-2');
  assert.deepEqual(app.openedProduction, []);
  assert.match(app.document.getElementById('toast').textContent, /方案版本已变化/);
  app.boards.at(-1).callbacks.openShot('image', 'shot-2');
  assert.deepEqual(app.openedProduction, [{kind:'image', focusDraft:'new-script-shot-2'}]);
  noWrites(app);
});

test('opening the already active stage from the board renders its details and back is view-only', async () => {
  const app = await page();
  assert.equal(app.read('state.activeStage'), 'writing');
  assert.equal(app.document.getElementById('stage').textContent, '');
  app.ctx.showJourneyTab('plan', {stage:'writing'});
  assert.equal(app.read('state.activeStage'), 'writing');
  assert.equal(app.read('state.canvasTab'), 'plan');
  assert.equal(app.document.getElementById('stage').hidden, false);
  assert.match(app.document.getElementById('stage').textContent, /Exact current script/);
  assert.equal(app.document.getElementById('creative-board').hidden, true);
  assert.equal(app.document.getElementById('canvas-detail-nav').hidden, false);
  const requests = app.observed.length;
  await app.document.getElementById('canvas-back').click();
  assert.equal(app.read('state.canvasTab'), 'board');
  assert.equal(app.document.getElementById('creative-board').hidden, false);
  assert.equal(app.document.getElementById('stage').hidden, true);
  assert.equal(app.document.getElementById('canvas-detail-nav').hidden, true);
  assert.equal(app.observed.length, requests);
  await app.ctx.refreshTimeline();
  assert.equal(app.read('state.canvasTab'), 'board');
  noWrites(app);
});

test('learning projects keep their own session view and never render the creative board', async () => {
  const app = await page({kind:'learning'});
  assert.equal(app.read('state.canvasTab'), 'plan');
  assert.equal(app.document.getElementById('creative-board').hidden, true);
  assert.equal(app.document.getElementById('stage').hidden, false);
  assert.equal(app.boards.length, 0);
  assert.equal(app.learning.length, 1);
  assert.equal(app.learning[0].run.kind, 'learning');
  await app.ctx.refreshTimeline();
  assert.equal(app.learning.length, 2);
  assert.equal(app.boards.length, 0);
  assert.equal(app.observed.some(call => call.path.endsWith('/production')), false);
  noWrites(app);
});

test('the analysis-only reference node opens the saved analysis despite its hidden reference tab', async () => {
  const app = await page({analysisOnly:true});
  assert.equal(app.read('state.creatorView.analysisOnly'), true);
  assert.equal(app.document.getElementById('journey-tab-reference').hidden, true);
  app.boards.at(-1).callbacks.openReference();
  assert.equal(app.read('state.canvasTab'), 'plan');
  assert.equal(app.document.getElementById('stage').hidden, false);
  assert.equal(app.document.getElementById('creative-board').hidden, true);
  assert.match(app.document.getElementById('stage').textContent, /Existing analysis observation/);
  assert.equal(app.document.getElementById('stage').querySelector('video').getAttribute('src'),
    `/api/runs/${RUN}/materials/reference%20clip.mp4`);
  noWrites(app);
});

test('real board and material drawer read the selected saved reference from the actual timeline API shape',async()=>{
  const app=await page({analysisOnly:true,realBoard:true});
  assert.equal('reference' in app.data.timeline,false);
  assert.equal('analysis' in app.data.timeline,false);
  const board=app.document.getElementById('creative-board');
  assert.match(board.textContent,/Saved reference report/);
  assert.match(board.querySelector('[data-board-node="brief"]').textContent,/本轮分析范围/);
  assert.doesNotMatch(board.querySelector('[data-board-node="brief"]').textContent,/方向尚未准备|创意方向/);
  assert.match(board.textContent,/参考处理已完成/);
  assert.equal(board.querySelector('[data-board-node="results"]'),null);
  const older=app.data.references[0];
  app.data.references.push({...older,reference_id:'second-ref',name:'other.mp4',report:'Other saved report',
    status:'stale',execution_status:'failed',failure_reason:'Reference changed'});
  vm.runInContext(`state.referenceSelectedId='analysis-only-ref';`,app.ctx);
  await app.ctx.refreshTimeline();
  assert.match(board.textContent,/Saved reference report/);
  assert.doesNotMatch(board.textContent,/Other saved report/);
  vm.runInContext(`state.materialsOpen=true;`,app.ctx);
  await app.ctx.loadMaterialWorkspace();
  const drawer=app.document.getElementById('reference-video-results');
  drawer.querySelector('[data-reference-view="second-ref"]').click();
  assert.equal(app.read('state.canvasTab'),'plan');
  assert.equal(app.document.getElementById('stage').hidden,false);
  assert.equal(board.hidden,true);
  assert.equal(app.document.getElementById('stage').querySelector('[data-reference-breakdown]').dataset.referenceBreakdown,'second-ref');
  assert.match(app.document.getElementById('stage').textContent,/来源已变化/);
  app.ctx.showJourneyTab('board');
  assert.match(board.textContent,/Other saved report/);
  assert.match(board.textContent,/Reference changed/);
  noWrites(app);
});

test('saved stage receipt updates the running board and preserves view layout and reading scroll',async()=>{
  const app=await page({realBoard:true,realTransport:true});
  app.data.timeline.materials[0].url=`/api/runs/${RUN}/materials/product%20front.jpg`;
  await app.ctx.refreshTimeline();
  app.ctx.showJourneyTab('board');
  const board=app.document.getElementById('creative-board');
  assert.match(board.textContent,/共同人物与场景设计尚未保存/);
  board.querySelector('[data-board-action="zoom-in"]').click();
  const viewport=board.querySelector('.creative-board-viewport');
  const handle=board.querySelector('[data-image-drag]');
  const event=values=>({target:handle,preventDefault(){},...values});
  viewport.listeners.pointerdown(event({button:0,pointerId:12,clientX:10,clientY:10}));
  viewport.listeners.pointermove(event({pointerId:12,clientX:120,clientY:90}));
  viewport.listeners.pointerup(event({pointerId:12}));
  const worldTransform=board.querySelector('.creative-board-world').style.transform;
  const card=handle.closest('[data-board-image]');
  const cardPosition=[card.style.left,card.style.top];
  board.querySelector('.creative-board-table-scroll').scrollTop=77;
  board.querySelector('.creative-board-result-scroll').scrollTop=90;
  viewport.scrollTop=140;
  const conversation=app.document.getElementById('conversation-body');
  conversation.scrollTop=120;
  app.read('state.atBottom=false;');
  app.data.timeline.rail.push({...stage('material_plan',1,'e'.repeat(64),'Saved design'),
    structured:{visual_design:{everyday_context:'Offline weekday lunch',characters:[
      {id:'person-a',label:'Saved worker',appearance:'Blue shirt'}],scenes:[
      {id:'kitchen',label:'Saved kitchen',location:'Offline US kitchen'}],continuity:[]}}});
  const job={job_id:'active-stage-save',status:'running',cursor:12,events:[
    {seq:12,type:'agent_tool',role:'material',stage:'material_plan',tool:'submit_stage',ok:true,
      args:{stage:'material_plan'}},
  ]};
  await app.ctx.watchJob(RUN,job,app.read('jobWatch'));
  assert.equal(app.read('state.busy'),true);
  assert.match(board.textContent,/Saved worker|Saved kitchen/);
  assert.doesNotMatch(board.textContent,/共同人物与场景设计尚未保存/);
  assert.equal(board.querySelector('.creative-board-world').style.transform,worldTransform);
  const refreshedCard=board.querySelector('[data-image-drag]').closest('[data-board-image]');
  assert.deepEqual([refreshedCard.style.left,refreshedCard.style.top],cardPosition);
  assert.equal(board.querySelector('.creative-board-table-scroll').scrollTop,77);
  assert.equal(board.querySelector('.creative-board-result-scroll').scrollTop,90);
  assert.equal(viewport.scrollTop,140);
  assert.equal(conversation.scrollTop,120);
  assert.deepEqual(app.observed.filter(call=>call.method!=='GET'),[]);
});


async function generatedImagePreview() {
  const app=await page({realBoard:true,realPreview:true});
  // Exact /materials HTTP row: no sha256. The hash belongs to production.output.
  const material={name:'exact generated shot.png',kind:'image',
    bytes:2048,usable:true,url:`/api/runs/${RUN}/materials/exact%20generated%20shot.png`};
  const output={name:material.name,sha256:'f'.repeat(64),url:material.url};
  const original={name:'product front.jpg',kind:'image',bytes:2048,usable:true,
    url:`/api/runs/${RUN}/materials/product%20front.jpg`};
  assert.deepEqual(Object.keys(material).sort(),['bytes','kind','name','url','usable']);
  const draft=app.data.production.drafts[0];
  Object.assign(draft,{source_ready:true,is_current:true,attempts:[{
    attempt_id:'exact-generation-attempt',draft_id:draft.draft_id,
    draft_sha256:draft.draft_sha256,status:'succeeded',output_available:true,output,
  }]});
  app.data.production.materials=[{...material,sha256:output.sha256}];
  app.data.production.targets.image={source_version:4,source_sha256:WRITING_SHA,complete_list:true,
    shots:[{ordinal:2,shot_id:draft.shot_id,label:'Exact second shot'}]};
  app.data.production.shots.image={shots:[{ordinal:2,shot_id:draft.shot_id,
    draft_id:draft.draft_id,draft_sha256:draft.draft_sha256,stage:'completed',source_ready:true,output}]};
  app.ctx.__previewProduction=app.data.production;
  app.ctx.__previewMaterials=[original,material];
  app.read('state.productionData=__previewProduction;state.materials=__previewMaterials;renderCreativeBoard();');
  const image=[...app.document.getElementById('creative-board').querySelectorAll('[data-board-action]')]
    .find(node=>node.getAttribute('data-board-action')==='output:exact generated shot.png');
  assert.ok(image,'the real board must render the exact generated image');
  app.observed.length=0;
  image.click();
  const viewer=app.document.getElementById('viewer');
  const feedback=viewer.querySelector('[data-viewer-feedback]');
  assert.ok(feedback,'the real shared preview exposes generated-image feedback');
  const input=app.document.getElementById('chat-input');
  const focus=input.focus.bind(input);
  input.focus=()=>{focus();app.document.activeElement=input;};
  return {...app,material,output,draft,viewer,feedback};
}

test('the exact board image preview hands off its original attempt to the composer and preserves the unsent draft',async()=>{
  const app=await generatedImagePreview();
  const input=app.document.getElementById('chat-input');
  input.value='  My existing feedback\nkeep this draft  ';
  app.ctx.setConversationOpen(false);
  const world=app.document.getElementById('creative-board').querySelector('.creative-board-world');
  const transform=world.style.transform;
  const card=app.document.getElementById('creative-board').querySelector('[data-board-image]');
  const position=[card.style.left,card.style.top];
  assert.match(app.feedback.getAttribute('aria-label'),/批注这张生成图：exact generated shot.png/);
  app.feedback.click();
  assert.equal(app.viewer.hidden,true);
  assert.equal(app.viewer.querySelector('[data-viewer-feedback]'),null);
  assert.equal(app.document.getElementById('viewer-body').querySelector('img'),null);
  assert.equal(app.document.getElementById('conversation-body').hidden,false);
  assert.ok(app.document.activeElement===input);
  assert.equal(input.value,'  My existing feedback\nkeep this draft  ');
  assert.deepEqual(JSON.parse(app.read('JSON.stringify(state.feedbackTarget)')),
    {kind:'media',run_id:RUN,attempt_id:'exact-generation-attempt',output_sha256:app.output.sha256});
  assert.match(app.document.getElementById('feedback-target').textContent,/首帧 02 的生成图/);
  assert.equal(app.read('state.canvasTab'),'board');
  assert.equal(world.style.transform,transform);
  assert.deepEqual([card.style.left,card.style.top],position);
  assert.deepEqual(app.observed,[]);
});

for(const change of ['project','replacement','source','output_hash','unavailable','file_metadata']) {
  test(`an open generated preview refuses ${change} changes without rebinding or sending feedback`,async()=>{
    const app=await generatedImagePreview();
    const input=app.document.getElementById('chat-input');input.value='Keep my draft';
    if(change==='project')app.read('state.runId="another-project"');
    else if(change==='replacement')app.draft.is_current=false;
    else if(change==='source')app.read('state.rail.find(row=>row.stage==="writing").version=5');
    else if(change==='output_hash')app.output.sha256='9'.repeat(64);
    else if(change==='unavailable')app.draft.attempts[0].output_available=false;
    else app.material.bytes=4096;
    app.feedback.click();
    assert.equal(app.read('state.feedbackTarget'),null);
    assert.equal(input.value,'Keep my draft');
    assert.match(app.document.getElementById('toast').textContent,/已变化/);
    assert.deepEqual(app.observed,[]);
  });
}

test('opening a source image removes the generated feedback entry and invalidates the old click handler',async()=>{
  const app=await generatedImagePreview();
  app.read('openViewer(state.materials[0])');
  assert.equal(app.viewer.querySelector('[data-viewer-feedback]'),null);
  app.feedback.click();
  assert.equal(app.read('state.feedbackTarget'),null);
  assert.equal(app.viewer.hidden,false);
  assert.deepEqual(app.observed,[]);
});

test('source images and unconfirmed or ambiguous generated bytes do not receive a generated-image feedback target',async()=>{
  const app=await generatedImagePreview();
  app.read('openViewer(state.materials[0])');
  assert.equal(app.viewer.querySelector('[data-viewer-feedback]'),null);
  app.draft.attempts[0].status='unknown';
  app.read('openViewer(state.materials[1])');
  assert.equal(app.viewer.querySelector('[data-viewer-feedback]'),null);
  app.draft.attempts[0].status='succeeded';
  app.draft.attempts.push({...app.draft.attempts[0],attempt_id:'ambiguous-other-attempt'});
  app.read('openViewer(state.materials[1])');
  assert.equal(app.viewer.querySelector('[data-viewer-feedback]'),null);
  assert.equal(app.read('state.feedbackTarget'),null);
  assert.deepEqual(app.observed,[]);
});

test('a readable previous image stays pinned to the shown successful attempt when the latest attempt failed',async()=>{
  const app=await generatedImagePreview();
  app.draft.attempts.push({attempt_id:'later-failed-attempt',status:'failed'});
  app.feedback.click();
  assert.equal(app.read('state.feedbackTarget.attempt_id'),'exact-generation-attempt');
  assert.equal(app.read('state.feedbackTarget.output_sha256'),'f'.repeat(64));
  assert.deepEqual(app.observed,[]);
});


test('current photo writing detail navigation matches its heading without any write or approval',async()=>{
  const app=await page({format:'photo',realBoard:true});
  const original=JSON.stringify(app.data.timeline.rail);
  app.ctx.showJourneyTab('plan',{stage:'writing'});
  assert.equal(app.document.getElementById('canvas-detail-title').textContent,'图文方案');
  assert.match(app.document.getElementById('stage').innerHTML,/<h1>图文方案<\/h1>/);
  assert.equal(JSON.stringify(app.data.timeline.rail),original);
  assert.equal(app.read('state.activeStage'),'writing');
  app.data.timeline.run={...app.data.run,content_format:'video'};
  await app.ctx.refreshTimeline();
  app.ctx.showJourneyTab('plan',{stage:'writing'});
  assert.equal(app.document.getElementById('canvas-detail-title').textContent,'脚本与分镜');
  assert.match(app.document.getElementById('stage').innerHTML,/<h1>脚本与分镜<\/h1>/);
  noWrites(app);
});


test('the current photo plan from the canvas shares the page reader and exact version',async()=>{
  const app=await page({format:'photo'});
  const callbacks=app.boards.at(-1).callbacks;
  callbacks.openStage('writing',4);
  await settle();
  assert.equal(app.read('state.canvasTab'),'plan');
  assert.equal(app.read('state.activeStage'),'writing');
  const html=app.document.getElementById('stage').innerHTML;
  assert.match(html,/<h1>图文方案<\/h1>/);
  assert.match(html,/第 4 版/);
  assert.doesNotMatch(html,/data-act="approve"/);
  assert.equal(app.read('state.rail.find(step=>step.stage==="writing").artifact_sha256'),WRITING_SHA);
  assert.equal(app.document.getElementById('reader').hidden,true);
  noWrites(app);
});

test('an obsolete canvas writing callback reads its original version instead of selecting the new plan',async()=>{
  const app=await page({format:'photo'});
  const callbacks=app.boards.at(-1).callbacks;
  app.read('state.rail=state.rail.map(step=>step.stage==="writing"?{...step,version:5,artifact_sha256:"f".repeat(64)}:step)');
  callbacks.openStage('writing',4);
  await settle();
  assert.equal(app.read('state.canvasTab'),'board');
  assert.equal(app.document.getElementById('reader').hidden,false);
  assert.match(app.document.getElementById('reader-body').textContent,/Exact current unprepared shot plan v4/);
  assert.equal(app.observed.filter(call=>call.path.endsWith('/artifacts/writing/4')).length,1);
  assert.equal(app.observed.some(call=>call.path.endsWith('/artifacts/writing/5')),false);
  noWrites(app);
});
