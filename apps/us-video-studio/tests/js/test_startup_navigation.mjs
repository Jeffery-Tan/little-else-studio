/* Shipped startup, selection and rendered navigation with a deferred local
 * task-state read. Fake HTTP/data only; no server, model or business writes. */
import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {loadApp, makeDom, webSource} from './dom-fixture.mjs';

async function pendingStartup() {
  const html=webSource('index.html');
  const runs = [{run_id:'current',title:'Current project',created_at:'2026-10-02'},
    {run_id:'other',title:'Other project',created_at:'2026-10-02'}];
  let release, entered = false;
  const pending = new Promise(resolve => { release = resolve; });
  const app = loadApp({runtime:'page',
    dom:makeDom({body:html.match(/<body[^>]*>([\s\S]*?)<\/body>/)[1]}),
    routes:{'/api/runs':runs,'/api/learning':{cases:[]}},
    globals:{openDelivery() {},openOutcomes() {},initJourney() {},initProjectManagement() {},
      clearFeedbackTarget() {},closeEmbeddedProduction() {},
      restoreJob:async()=>{if(!entered){entered=true;await pending;}},
    }});
  app.document.body.dataset.view=html.match(/<body[^>]*data-view="([^"]+)"/)[1];
  vm.runInContext(webSource('workspace.js'),app.ctx,{filename:'workspace.js'});
  const source = webSource('app.js').replace('(async function init() {',
    'globalThis.__startup = (async function init() {');
  vm.runInContext(source,app.ctx,{filename:'app.js'});
  // The deferred boundary is task-state restoration. Content and rendering
  // are unrelated to this navigation failure and need no provider responses.
  app.ctx.refreshTimeline = async()=>{};
  app.ctx.loadDraft = ()=>{};
  for(let i=0;i<10&&!entered;i++)await new Promise(resolve=>setImmediate(resolve));
  assert.ok(entered,'startup must actually reach project restoration');
  const read=expression=>vm.runInContext(expression,app.ctx);
  return {...app,read,release,finish:()=>app.ctx.__startup};
}

function assertProject(app,id,title) {
  assert.equal(app.read('state.runId'),id);
  assert.equal(app.read('state.view'),'project');
  assert.equal(app.document.body.dataset.view,'project');
  assert.equal(app.document.getElementById('workspace').hidden,true);
  assert.equal(app.document.getElementById('project-workspace').hidden,false);
  assert.equal(app.document.getElementById('page-heading').textContent,title);
  assert.deepEqual(app.observed.filter(call=>call.method!=='GET'),[]);
}

test('opening the current project while refresh restores it survives late startup completion',async()=>{
  const app=await pendingStartup();
  app.document.getElementById('nav-project').onclick();
  assertProject(app,'current','Current project');
  app.release();await app.finish();
  assertProject(app,'current','Current project');
});

test('selecting another project while startup restores the old one stays in the chosen workspace',async()=>{
  const app=await pendingStartup();
  await app.ctx.selectRun('other');
  app.ctx.setStudioView('project');
  assertProject(app,'other','Other project');
  app.release();await app.finish();
  assertProject(app,'other','Other project');
});

test('the default homepage appears before project restoration finishes and a deliberate home choice stays',async()=>{
  const app=await pendingStartup();
  assert.equal(app.document.body.dataset.view,'home');
  app.document.getElementById('nav-project').onclick();
  app.document.getElementById('nav-home').onclick();
  app.release();await app.finish();
  assert.equal(app.read('state.view'),'home');
  assert.equal(app.document.body.dataset.view,'home');
  assert.equal(app.document.getElementById('workspace').hidden,false);
  assert.equal(app.document.getElementById('project-workspace').hidden,true);
});
