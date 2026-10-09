import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import fs from 'node:fs';
import vm from 'node:vm';
const require = createRequire(import.meta.url);
globalThis.escapeHtml = require('../../studio/web/md.js').escapeHtml;
const { projectCardHtml, filterProjects } = require('../../studio/web/workspace.js');

test('damaged projects have visible escaped diagnosis and no mutation/open controls', () => {
  const run = {run_id:'damaged',title:'<img src=x>',project_brief:{phase:'unavailable'},integrity:{available:false,
    issues:[{file:'events.jsonl',category:'invalid_json',line:2,sha256:'a'.repeat(64)}]}};
  const html = projectCardHtml(run, '/private-preview.png');
  assert.match(html,/项目资料损坏，已暂停/);
  assert.match(html,/查看资料错误/);
  assert.match(html,/events.jsonl/);
  assert.match(html,/第 2 行/);
  assert.match(html,/&lt;img/);
  assert.doesNotMatch(html,/data-open-project|data-delete-project|private-preview|<img/);
  assert.equal(filterProjects([{run_id:'healthy',title:'Good'},run],'')[0],run);
});

test('startup does not automatically select damaged or remembered damaged projects', async () => {
  const source = fs.readFileSync(new URL('../../studio/web/app.js', import.meta.url),'utf8');
  const code = source.slice(source.indexOf('async function loadRuns()'),source.indexOf('function composerDraft()'));
  const runs = [{run_id:'damaged',title:'Bad',integrity:{available:false}},{run_id:'healthy',title:'Good'}];
  const state = {runs:[],runId:null}, select = {innerHTML:'',children:[],appendChild(value){this.children.push(value);}};
  const context = vm.createContext({state,api:async()=>runs,$:()=>select,document:{createElement:()=>({})},
    projectOptionLabel:run=>run.title,sessionStorage:{getItem:()=> 'damaged'},renderProjects:()=>{}});
  vm.runInContext(code + ';globalThis.load=loadRuns;',context);
  await context.load();
  assert.equal(state.runId,'healthy');
  assert.equal(state.runs.length,2);
  assert.deepEqual(select.children.map(item=>item.value),['healthy']);
  runs.pop();
  await context.load();
  assert.equal(state.runId,null);
  assert.equal(select.children.at(-1).textContent,'暂无可打开项目');
});
