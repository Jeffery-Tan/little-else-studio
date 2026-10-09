import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import fs from 'node:fs';
import vm from 'node:vm';
import { makeDom } from './dom-fixture.mjs';
const require = createRequire(import.meta.url);
globalThis.escapeHtml = require('../../studio/web/md.js').escapeHtml;
const { filterProjects, inspirationCases, projectCardHtml, referenceCardHtml, shortTitle, studioDateTime, projectOptionLabel } = require('../../studio/web/workspace.js');

test('initial home card uses the exact pure-analysis summary without a cached creator view',()=>{
  const run={run_id:'analysis-run',title:'用户命名保持原样',brief:'不生成视频',content_format:null,
    project_brief:{title:'图片分析已完成',analysis:{kind:'image',analysis_only:true,goal:'图片分析',status:'complete'}}};
  const card=projectCardHtml(run,null);
  assert.match(card,/图片分析已完成/);
  assert.match(card,/目标：图片分析/);
  assert.match(card,/分析项目/);
  assert.match(card,/用户命名保持原样/);
  assert.doesNotMatch(card,/交付形式待确认|等待继续创作|参考视频拆解报告/);
  const cached=projectCardHtml({...run,creator_view:{goal:'商品视频',current:'等待继续创作'}},null);
  assert.match(cached,/图片分析已完成/);
  assert.doesNotMatch(cached,/等待继续创作|目标：商品视频/);
  const partial=projectCardHtml({...run,project_brief:{title:'图片分析未完成',
    analysis:{kind:'image',analysis_only:true,goal:'图片分析',status:'incomplete'}}},null);
  assert.match(partial,/图片分析未完成/);
  assert.doesNotMatch(partial,/图片分析已完成/);
  const creative=projectCardHtml({...run,content_format:'video',project_brief:{
    title:'图片分析已完成',analysis:{kind:'image',analysis_only:false,goal:'图片分析'}}},null);
  assert.match(creative,/目标：商品视频/);
  assert.doesNotMatch(creative,/图片分析已完成|分析项目/);
});

test('project search preserves identity and supports trimmed mixed case queries', () => {
  const projects = [{run_id:'one', title:'US 午餐包'}, {run_id:'two', title:'通勤内容'}];
  assert.deepEqual(filterProjects(projects, ' us '), [projects[0]]);
  assert.deepEqual(filterProjects(projects, '通勤'), [projects[1]]);
  assert.deepEqual(filterProjects(projects, 'missing'), []);
});
test('actionable projects surface first while cards lead to the current work', () => {
  const runs=[
    {run_id:'recent',title:'最近项目',updated_at:'2026-09-23T10:00:00Z',project_brief:{phase:'ready',title:'策划已就绪'}},
    {run_id:'decision',title:'待决定项目',updated_at:'2026-09-22T10:00:00Z',content_format:'photo',
      progress:{title:'笼统状态'},project_brief:{phase:'reviewed',title:'图文方案等你决定',action:{label:'阅读并决定'}}},
  ];
  assert.equal(filterProjects(runs,'')[0].run_id,'decision');
  const card=projectCardHtml(runs[1],null);
  assert.match(card,/打开查看当前作品与下一步/);
  assert.match(card,/打开作品/);
  assert.match(card,/图文项目/);
  assert.match(card,/目标：带货图片与文案 · 创建于/);
  assert.doesNotMatch(card,/图文方案等你决定/);
  assert.doesNotMatch(card,/笼统状态/);
  assert.match(projectCardHtml({run_id:'video',title:'午餐包',content_format:'video',
    brief:'目标是一条短视频与英文发布文案。'},null),/目标：商品视频与发布文案/);
});

test('same-named projects show stable creation times in cards and picker labels', () => {
  const runs=[
    {run_id:'first',title:'资料带货视频',created_at:'2026-09-27T04:51:16Z'},
    {run_id:'second',title:'资料带货视频',created_at:'2026-09-27T12:42:34Z'},
  ];
  assert.notEqual(projectOptionLabel(runs[0],runs),projectOptionLabel(runs[1],runs));
  for (const run of runs) {
    assert.ok(projectCardHtml(run,null).includes(`创建于 ${studioDateTime(run.created_at)}`));
    assert.ok(projectCardHtml(run,null).includes(`data-open-project="${run.run_id}"`));
  }
});
test('discovery includes eligible actual references, never presents own results or holdouts as inspiration', () => {
  const base = {media:{seconds:15}, partition:'calibration', origin:'user_reference', eligible_for:['message']};
  const cases = [base, {...base, origin:'discovered_reference'}, {...base, origin:'own_result'}, {...base, partition:'holdout'}, {...base, eligible_for:[]}, {...base, media:null}];
  assert.deepEqual(inspirationCases(cases), cases.slice(0,2));
});
test('project title and identity cannot inject markup; missing artwork is not a fabricated result', () => {
  const html = projectCardHtml({run_id:'" onclick="alert(1)',title:'<img src=x onerror=alert(1)>',created_at:'invalid'},null);
  assert.ok(!html.includes('<img'));
  assert.ok(html.includes('&lt;img'));
  assert.ok(html.includes('创作项目'));
  assert.ok(html.includes('时间未记录'));
  assert.ok(!html.includes('data-open-project="" onclick='));
});
test('reference cards use registered media route and never autoplay or expose local paths', () => {
  const html = referenceCardHtml({id:'case/#1',title:'<script>bad</script>',media:{path:'J:/private/source.mp4'},strategy:{situation:'<b>reason</b>'}});
  assert.ok(html.includes('/api/learning/cases/case%2F%231/media'));
  assert.ok(html.includes('参考片'));
  assert.ok(!html.includes('autoplay'));
  assert.ok(!html.includes('J:/private'));
  assert.ok(!html.includes('<script>'));
  assert.ok(html.includes('&lt;b&gt;reason'));
});

test('shortTitle truncates long prompt-like titles for display only', () => {
  assert.equal(shortTitle('午餐包短视频'), '午餐包短视频');
  const long = '给这个午餐包做一条自然一点的短视频突出防水面料风格参考';
  const shown = shortTitle(long, 18);
  assert.equal(shown.length, 18);
  assert.ok(shown.endsWith('…'));
  const html = projectCardHtml({run_id:'x', title: long, created_at:'invalid'}, null);
  assert.ok(html.includes('title='));
  assert.ok(!html.includes(long.slice(0, 30) + '</strong>'));
});

test('home has one creation composer; empty project list stays out of the way and trash remains reachable', () => {
  const html = fs.readFileSync(new URL('../../studio/web/index.html', import.meta.url), 'utf8');
  assert.equal((html.match(/id="start-conversation"/g) || []).length, 1);
  assert.equal((html.match(/id="trash-open"/g) || []).length, 1);
  assert.doesNotMatch(html, /id="top-create"/);

  const source = fs.readFileSync(new URL('../../studio/web/workspace.js', import.meta.url), 'utf8');
  const start = source.indexOf('function renderProjects()');
  const end = source.indexOf('async function loadInspiration()', start);
  assert.ok(start >= 0 && end > start);
  const section = {hidden:false}, grid = {innerHTML:''}, search = {value:''};
  const state = {runs:[],runId:null,materials:[]};
  const document = {body:{dataset:{}},querySelector:() => section};
  const context = vm.createContext({state,document,$:id => id === 'project-grid' ? grid : search,
    filterProjects,projectCardHtml,studioIcon:() => '<svg></svg>',materialUrl:() => ''});
  vm.runInContext(source.slice(start,end), context);
  vm.runInContext('renderProjects()', context);
  assert.equal(section.hidden, true);
  assert.equal(grid.innerHTML, '');

  state.runs = [{run_id:'one',title:'真实项目',content_format:'video'}];
  vm.runInContext('renderProjects()', context);
  assert.equal(section.hidden, false);
  assert.match(grid.innerHTML, /data-open-project="one"/);
  assert.doesNotMatch(grid.innerHTML, /新建创作项目|新的想法|data-create-format/);

  search.value = 'missing';
  vm.runInContext('renderProjects()', context);
  assert.match(grid.innerHTML, /还没有找到这个项目/);
  assert.doesNotMatch(grid.innerHTML, /新建创作项目|data-create-format/);
});


test('home mounts the real creator-view summary using photo, video or an undecided deliverable name',()=>{
  const {creatorProjectView}=require('../../studio/web/creator-view.js');
  for(const [format,name,goal] of [
    ['photo','图文方案','带货图片与文案'],
    ['video','脚本','商品视频'],
    [null,'创作方案','交付形式待确认'],
  ]) {
    const input={run:{run_id:'home-format-case',title:'Synthetic current project',
      brief:'在意见里提到视频与图文，不因此改变交付形式。',content_format:format==='video'?'photo':'video'},
      timeline:{run:{content_format:format},rail:[],pending:{stage:'direction',passed:true},
        brief:{execution:{active:false}}}};
    const before=JSON.stringify(input),view=creatorProjectView(input);
    const card={...input.run,content_format:format,creator_view:view};
    const cardBefore=JSON.stringify(card),html=projectCardHtml(card,null);
    const {document}=makeDom({body:html});
    const current=document.querySelector('.project-next');
    assert.equal(current.textContent,`正在选择创意方向；采用后会继续准备${name}`);
    assert.ok(document.body.textContent.includes(`目标：${goal}`));
    assert.equal(document.querySelector('[data-open-project]').dataset.openProject,'home-format-case');
    if(format==='photo') {
      assert.ok(document.body.textContent.includes('图文项目'));
      assert.doesNotMatch(current.textContent,/脚本|视频/);
    } else if(format===null) assert.doesNotMatch(current.textContent,/脚本|视频|图文方案/);
    assert.equal(JSON.stringify(input),before);
    assert.equal(JSON.stringify(card),cardBefore);
  }
});
