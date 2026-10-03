/* The board's app/journey wiring, through the shipped controllers and a fake
 * HTTP boundary. The board component has its own node/renderer tests: here its
 * render adapter captures callbacks so the exact navigation authority is tested.
 * No server, model, approval or media-generation request is made. */
import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
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

function fixture({kind = 'creative', format = 'video', analysisOnly = false, readyImages = false, approvedImages = false} = {}) {
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
  return {run, timeline, production, references};
}

async function page(options = {}) {
  const data = fixture(options), boards = [], openedProduction = [], focus = [], learning = [];
  let materialWorkspaceLoads = 0;
  const dom = makeDom({body:webSource('index.html').match(/<body[^>]*>([\s\S]*?)<\/body>/)[1]});
  const app = loadApp({dom, routes:{
    '/material-approvals/batch/continue':() => options.videoPreparationResult,
    '/timeline':() => data.timeline,
    '/production':() => data.production,
    '/reference-videos':() => ({tasks:data.references}),
    '/learning/session':{session_id:'offline-learning'},
    '/api/learning':{methods:[]},
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
    setTimeout:() => 1, clearTimeout:() => {},
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
  for (const file of ['md.js', 'blocks.js', 'stage.js', 'production.js', 'reference-video.js', 'creator-view.js', 'journey.js'])
    vm.runInContext(webSource(file), app.ctx, {filename:file});
  // Navigation must run for real; the drawer boundary is observed without
  // mounting its separate editor/authorization flow.
  app.ctx.__openProduction = args => openedProduction.push(JSON.parse(JSON.stringify(args)));
  vm.runInContext(`
    setStudioView = view => { state.view = view; };
    openProduction = options => globalThis.__openProduction(options);
    initJourney();
  `, app.ctx);
  await settle(); // let app.js's initial empty project listing settle first
  app.ctx.__run = data.run;
  vm.runInContext('state.runs = [globalThis.__run];', app.ctx);
  await app.ctx.selectRun(RUN);
  vm.runInContext('state.busy = false; state.view = "project";', app.ctx);
  return {...app, data, boards, openedProduction, focus, learning,
    read:expression => vm.runInContext(expression, app.ctx),
    get materialWorkspaceLoads() {return materialWorkspaceLoads;}};
}

function noWrites(app) {
  assert.deepEqual(app.observed.filter(call => call.method !== 'GET'), []);
  assert.equal(app.observed.some(call => /\/jobs|\/actions|authoriz|approval|\/generate/.test(call.path)), false);
}

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
