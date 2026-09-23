import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
globalThis.escapeHtml = require('../../studio/web/md.js').escapeHtml;
const { filterProjects, inspirationCases, projectCardHtml, referenceCardHtml } = require('../../studio/web/workspace.js');

test('project search preserves identity and supports trimmed mixed case queries', () => {
  const projects = [{run_id:'one', title:'US 午餐包'}, {run_id:'two', title:'通勤内容'}];
  assert.deepEqual(filterProjects(projects, ' us '), [projects[0]]);
  assert.deepEqual(filterProjects(projects, '通勤'), [projects[1]]);
  assert.deepEqual(filterProjects(projects, 'missing'), []);
});
test('actionable projects surface first and cards use the server project status', () => {
  const runs=[
    {run_id:'recent',title:'最近项目',updated_at:'2026-09-23T10:00:00Z',project_brief:{phase:'ready',title:'策划已就绪'}},
    {run_id:'decision',title:'待决定项目',updated_at:'2026-09-22T10:00:00Z',content_format:'photo',
      progress:{title:'笼统状态'},project_brief:{phase:'reviewed',title:'图文方案等你决定',action:{label:'阅读并决定'}}},
  ];
  assert.equal(filterProjects(runs,'')[0].run_id,'decision');
  const card=projectCardHtml(runs[1],null);
  assert.match(card,/图文方案等你决定/);
  assert.match(card,/阅读并决定/);
  assert.match(card,/图文项目/);
  assert.match(card,/图文 · 更新于/);
  assert.doesNotMatch(card,/笼统状态/);
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
  assert.ok(html.includes('日期未记录'));
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
